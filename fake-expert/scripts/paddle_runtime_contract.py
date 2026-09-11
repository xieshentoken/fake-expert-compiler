#!/usr/bin/env python3
"""Fail-closed contracts for the externally installed local Paddle runtime.

This module is deliberately independent from Paddle itself.  The compiler
runtime does not import Paddle and does not pretend that its own virtualenv is
the OCR environment.  It freezes the external interpreter/model/worker
boundary, creates deterministic PNG crops, maps crop pixels back to PDF page
coordinates, and normalizes only the result shapes that are explicitly part of
the Phase 7D.1 contract.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import re
import subprocess
import stat
import tempfile
import time
import zlib
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from compiler_version import (
    LEGACY_TECHNICAL_CHART_PROFILE_ID,
    OCR_PROTOCOL_SCHEMA,
    PADDLE_RESULT_ADAPTER_PROTOCOL,
    PADDLE_RESULT_ADAPTER_SCHEMA,
    PADDLE_RUNTIME_PRIVATE_DIAGNOSTIC_SCHEMA,
    PADDLE_RUNTIME_CONTRACT_SCHEMA,
    PADDLE_RUNTIME_PROFILE_SCHEMA,
    PADDLE_RUNTIME_PROTOCOL,
    SELECTIVE_VISUAL_PROTOCOL,
    TECHNICAL_CHART_PROFILE_ID,
    VISUAL_BATCH_RECEIPT_SCHEMA,
)


PADDLE_OCR_BACKEND = "paddleocr-ppocrv6"
PADDLE_STRUCTURE_BACKEND = "paddleocr-ppstructure"
PADDLE_STRUCTURE_V3_BACKEND = "paddleocr-ppstructure-v3"
PADDLE_CHART_BACKEND = "paddleocr-chart-parsing"

TECHNICAL_TEXT_PROFILE_ID = "technical-text-v1"
TECHNICAL_TABLE_PROFILE_ID = "technical-table-v1"
PROFILE_SCHEMA = PADDLE_RUNTIME_PROFILE_SCHEMA

DEFAULT_PADDLE_DISTRIBUTIONS = {
    "paddleocr": "3.7.0",
    "paddlepaddle": "3.3.1",
    "paddlex": "3.7.2",
    "opencv-contrib-python": "4.10.0.84",
}

REQUIRED_MODEL_DIRS = {
    PADDLE_OCR_BACKEND: (
        "text_detection_model_dir",
        "text_recognition_model_dir",
    ),
    PADDLE_STRUCTURE_BACKEND: (
        "layout_detection_model_dir",
        "text_detection_model_dir",
        "text_recognition_model_dir",
        "table_classification_model_dir",
        "wired_table_structure_recognition_model_dir",
        "wireless_table_structure_recognition_model_dir",
        "wired_table_cells_detection_model_dir",
        "wireless_table_cells_detection_model_dir",
    ),
    PADDLE_STRUCTURE_V3_BACKEND: (
        "layout_detection_model_dir",
        "text_detection_model_dir",
        "text_recognition_model_dir",
        "table_classification_model_dir",
        "wired_table_structure_recognition_model_dir",
        "wireless_table_structure_recognition_model_dir",
        "wired_table_cells_detection_model_dir",
        "wireless_table_cells_detection_model_dir",
    ),
    PADDLE_CHART_BACKEND: ("chart_model_dir",),
}

# These names are deliberately bound to the locally selected model directories.
# The profile is not a suggestion: constructor and predict switches are frozen
# together, and a structure run cannot proceed with a library default.
PROFILE_MODEL_DIRS = {
    TECHNICAL_TEXT_PROFILE_ID: {
        "text_detection_model_dir": "PP-OCRv6_medium_det",
        "text_recognition_model_dir": "PP-OCRv6_medium_rec",
    },
    TECHNICAL_TABLE_PROFILE_ID: {
        "layout_detection_model_dir": "PP-DocLayout_plus-L",
        "text_detection_model_dir": "PP-OCRv6_medium_det",
        "text_recognition_model_dir": "PP-OCRv6_medium_rec",
        "table_classification_model_dir": "PP-LCNet_x1_0_table_cls",
        "wired_table_structure_recognition_model_dir": "SLANeXt_wired",
        "wireless_table_structure_recognition_model_dir": "SLANet_plus",
        "wired_table_cells_detection_model_dir": "RT-DETR-L_wired_table_cell_det",
        "wireless_table_cells_detection_model_dir": "RT-DETR-L_wireless_table_cell_det",
    },
    TECHNICAL_CHART_PROFILE_ID: {
        "chart_model_dir": "PP-Chart2Table",
    },
    LEGACY_TECHNICAL_CHART_PROFILE_ID: {
        "chart_model_dir": "PP-Chart2Table",
    },
}

# PaddleX 3.7 resolves a model directory against the default model name.  A
# PP-OCRv6 directory therefore needs its matching model name explicitly bound
# when it is nested inside PP-StructureV3; otherwise PaddleX falls back to
# PP-OCRv5_server_det and rejects the local PP-OCRv6 directory as a mismatch.
PROFILE_MODEL_NAMES = {
    TECHNICAL_TEXT_PROFILE_ID: {
        "text_detection_model_name": "PP-OCRv6_medium_det",
        "text_recognition_model_name": "PP-OCRv6_medium_rec",
    },
    TECHNICAL_TABLE_PROFILE_ID: {
        "layout_detection_model_name": "PP-DocLayout_plus-L",
        "text_detection_model_name": "PP-OCRv6_medium_det",
        "text_recognition_model_name": "PP-OCRv6_medium_rec",
        "table_classification_model_name": "PP-LCNet_x1_0_table_cls",
        "wired_table_structure_recognition_model_name": "SLANeXt_wired",
        "wireless_table_structure_recognition_model_name": "SLANet_plus",
        "wired_table_cells_detection_model_name": "RT-DETR-L_wired_table_cell_det",
        "wireless_table_cells_detection_model_name": "RT-DETR-L_wireless_table_cell_det",
    },
    TECHNICAL_CHART_PROFILE_ID: {
        "model_name": "PP-Chart2Table",
    },
    LEGACY_TECHNICAL_CHART_PROFILE_ID: {
        "model_name": "PP-Chart2Table",
    },
}

PROFILE_SWITCHES = {
    TECHNICAL_TEXT_PROFILE_ID: {
        "constructor": {
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
        },
        "predict": {
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
        },
    },
    TECHNICAL_TABLE_PROFILE_ID: {
        "constructor": {
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "use_seal_recognition": False,
            "use_table_recognition": True,
            "use_formula_recognition": False,
            "use_chart_recognition": False,
            "use_region_detection": False,
        },
        "predict": {
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "use_seal_recognition": False,
            "use_table_recognition": True,
            "use_formula_recognition": False,
            "use_chart_recognition": False,
            "use_region_detection": False,
            "use_table_orientation_classify": False,
            "use_wired_table_cells_trans_to_html": True,
            "use_wireless_table_cells_trans_to_html": True,
            "use_ocr_results_with_table_cells": True,
            "use_e2e_wired_table_rec_model": False,
            "use_e2e_wireless_table_rec_model": False,
        },
    },
    TECHNICAL_CHART_PROFILE_ID: {
        "constructor": {
            "device": "cpu",
            "enable_hpi": False,
            "engine": "paddle_dynamic",
        },
        "predict": {
            "batch_size": 1,
        },
    },
    LEGACY_TECHNICAL_CHART_PROFILE_ID: {
        "constructor": {
            "device": "cpu",
            "enable_hpi": False,
        },
        "predict": {},
    },
}

_SHA256_RE = __import__("re").compile(r"^[0-9a-f]{64}$")


class PaddleRuntimeContractError(RuntimeError):
    """Stable fail-closed Phase 7D.1 contract error."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_response_sha256(response: Mapping[str, Any]) -> str:
    """Hash semantic worker content without timing/inventory host details."""

    volatile = {"elapsed_ms", "runtime_inventory", "worker_receipt"}
    return sha256_json({key: value for key, value in response.items() if key not in volatile})


def classify_paddle_stderr(stderr: bytes) -> dict[str, Any]:
    """Classify stderr one line/event at a time without exposing its content.

    Paddle and PaddleX frequently print static help, ccache, or re-download
    guidance alongside real runtime failures.  A whole-buffer substring search
    turns those notices into false network blockers.  This classifier emits only
    stable event/warning codes and the raw-byte digest; the decoded lines are
    written, when explicitly requested, to a separate 0600 private diagnostic.
    """

    decoded = stderr.decode("utf-8", errors="replace")
    lines = decoded.splitlines()
    warning_codes: set[str] = set()
    blocking = {
        "model_weights_newly_initialized",
        "model_training_required_warning",
        "model_weight_shape_mismatch",
        "model_weight_key_mismatch",
        "runtime_download_or_network_attempt",
    }
    events: list[dict[str, Any]] = []
    event_counts: dict[str, int] = {}

    def add_event(codes: set[str], line_number: int) -> None:
        if not codes:
            return
        for code in sorted(codes):
            event_counts[code] = event_counts.get(code, 0) + 1
        warning_codes.update(codes & blocking)
        events.append(
            {
                "line": line_number,
                "codes": sorted(codes),
                "blocking_codes": sorted(codes & blocking),
            }
        )

    for line_number, raw_line in enumerate(lines, start=1):
        line = re.sub(r"\s+", " ", raw_line.casefold()).strip()
        if not line:
            continue
        codes: set[str] = set()

        static_help = bool(
            re.search(
                r"(?:\busage\s*:|\bhelp\b|documentation|\bdocs?\b|for more information|please refer|see (?:the )?https?://)",
                line,
            )
        )
        static_ccache = "ccache" in line
        static_redownload = bool(
            re.search(r"\bre[ -]?download(?:ing)?\b|\bdownload again\b|\bdownload manually\b", line)
        )
        if static_help:
            codes.add("static_help_or_documentation_notice")
        if static_ccache:
            codes.add("static_ccache_notice")
        if static_redownload:
            codes.add("static_redownload_notice")

        if "newly initialized" in line:
            codes.add("model_weights_newly_initialized")
        if re.search(r"\btrain(?:ing)?\b.*\b(?:this model|required)\b|\btraining required\b", line):
            codes.add("model_training_required_warning")
        if re.search(r"size mismatch|shape mismatch|mismatch.*shape|shape.*mismatch", line):
            codes.add("model_weight_shape_mismatch")
        if re.search(r"unexpected key|missing key|key(?:s)? mismatch|mismatch.*key|key.*mismatch", line):
            codes.add("model_weight_key_mismatch")

        # Require an action/transport signal for network classification.  A URL
        # in documentation or a re-download hint is not an observed attempt.
        actual_transport = bool(
            re.search(
                r"(?<!re-)\bdownloading\b|(?<!re-)\bdownloaded\b|\bfetching\b|\brequest(?:ing|ed)?\s+(?:https?://|model|weights|file|url)\b|\brequest\s+(?:to\s+)?https?://|\b(?:get|post|put|head)\s+https?://|\bhttps?\s+request\b|\b(?:urllib|urlopen|requests\.(?:get|post|request))\b|\b(?:connection refused|connection reset|name resolution|proxy(?: connect)?)\b",
                line,
            )
        )
        contextual_download = bool(
            re.search(r"\bdownload\s+(?:from|url)\b", line)
            and not re.search(r"\b(?:if|when|please|can|could|should|need|want|to)\b", line)
            and not static_help
        )
        if actual_transport or contextual_download:
            codes.add("runtime_download_or_network_attempt")

        if not codes and re.search(r"\b(?:warning|warn|error|exception)\b", line):
            codes.add("runtime_warning_unclassified")
        add_event(codes, line_number)

    return {
        "stderr_bytes": len(stderr),
        "stderr_sha256": sha256_bytes(stderr),
        "warning_codes": sorted(warning_codes | {code for code in event_counts if code not in blocking and not code.startswith("static_")}),
        "blocking_warning_codes": sorted(warning_codes),
        "event_counts": dict(sorted(event_counts.items())),
        "events": events,
        "line_count": len(lines),
        "raw_stderr_included": False,
    }


def _write_private_runtime_diagnostic(
    path: Path | None,
    *,
    backend_id: str,
    stderr: bytes,
    stderr_audit: Mapping[str, Any],
    errors: Sequence[Mapping[str, Any]],
    runtime_events: Sequence[Mapping[str, Any]],
    process: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Persist complete local diagnostics only to an explicitly private file."""

    if path is None:
        return None
    lexical_target = path.expanduser()
    if lexical_target.is_symlink():
        raise PaddleRuntimeContractError("private_runtime_diagnostic_symlink")
    target = lexical_target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    decoded = stderr.decode("utf-8", errors="replace")
    payload = {
        "schema_version": PADDLE_RUNTIME_PRIVATE_DIAGNOSTIC_SCHEMA,
        "backend_id": backend_id,
        "private_host_diagnostic": True,
        "release_included": False,
        "file_mode": "0600",
        "process": dict(process),
        "errors": [dict(row) for row in errors],
        "runtime_events": [dict(row) for row in runtime_events],
        "stderr": {
            "bytes": len(stderr),
            "sha256": sha256_bytes(stderr),
            "text": decoded,
            "lines": decoded.splitlines(),
            "classification": dict(stderr_audit),
        },
    }
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(target), flags, 0o600)
    with os.fdopen(descriptor, "wb", closefd=True) as handle:
        handle.write(encoded)
    os.chmod(target, 0o600)
    return {
        "filename": target.name,
        "sha256": sha256_bytes(encoded),
        "bytes": len(encoded),
        "file_mode": "0600",
        "release_included": False,
    }


def validate_private_runtime_diagnostic(path: Path, *, expected_sha256: str | None = None) -> dict[str, Any]:
    """Validate private diagnostic integrity and restrictive file permissions."""

    lexical_target = path.expanduser()
    if lexical_target.is_symlink():
        raise PaddleRuntimeContractError("private_runtime_diagnostic_symlink")
    target = lexical_target.resolve()
    if target.is_symlink() or not target.is_file():
        raise PaddleRuntimeContractError("private_runtime_diagnostic_missing")
    if stat.S_IMODE(target.stat().st_mode) != 0o600:
        raise PaddleRuntimeContractError("private_runtime_diagnostic_permissions_invalid")
    digest = sha256_file(target)
    if expected_sha256 is not None and digest != expected_sha256:
        raise PaddleRuntimeContractError("private_runtime_diagnostic_hash_mismatch")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PaddleRuntimeContractError("private_runtime_diagnostic_invalid") from error
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != PADDLE_RUNTIME_PRIVATE_DIAGNOSTIC_SCHEMA
        or payload.get("private_host_diagnostic") is not True
        or payload.get("release_included") is not False
        or payload.get("file_mode") != "0600"
    ):
        raise PaddleRuntimeContractError("private_runtime_diagnostic_policy_invalid")
    return dict(payload)


def stable_id(prefix: str, *parts: Any, length: int = 20) -> str:
    return f"{prefix}-{sha256_json(list(parts))[:length]}"


def profile_definition(profile_id: str) -> dict[str, Any]:
    """Return the closed, explicit constructor/predict profile definition."""

    if profile_id not in PROFILE_SWITCHES:
        raise PaddleRuntimeContractError(f"paddle_profile_unknown:{profile_id}")
    return json.loads(
        json.dumps(
            {
                "schema_version": PROFILE_SCHEMA,
                "profile_id": profile_id,
                "model_dirs": PROFILE_MODEL_DIRS[profile_id],
                **PROFILE_SWITCHES[profile_id],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def build_technical_table_profile() -> dict[str, Any]:
    """Return the v0.12.0 PP-StructureV3 technical table profile."""

    return profile_definition(TECHNICAL_TABLE_PROFILE_ID)


def build_technical_chart_profile() -> dict[str, Any]:
    """Return the qualified-route ChartParsing PP-Chart2Table profile."""

    return profile_definition(TECHNICAL_CHART_PROFILE_ID)


def build_legacy_technical_chart_profile() -> dict[str, Any]:
    """Return the immutable v0.13 compatibility profile."""

    return profile_definition(LEGACY_TECHNICAL_CHART_PROFILE_ID)


def _is_cache_metadata(relative_path: str) -> bool:
    parts = set(Path(relative_path).parts)
    name = Path(relative_path).name.casefold()
    return (
        name == ".ds_store"
        or name.endswith(".lock")
        or name in {"lock", "lock.json", "cache.json", "metadata.lock"}
        or any(part.casefold() in {".cache", "cache", "locks"} for part in parts)
    )


def _is_contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _existing_symlink(path: Path) -> Path | None:
    current = path
    for candidate in (current, *current.parents):
        if candidate.exists() and candidate.is_symlink():
            return candidate
    return None


def _resolve_path(
    value: Any,
    *,
    kind: str,
    allowlist: Sequence[str | Path],
    allow_explicit_interpreter_symlink: bool = False,
    must_be_file: bool = False,
    must_be_dir: bool = False,
) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise PaddleRuntimeContractError(f"{kind}_path_required")
    raw = Path(value).expanduser()
    if not raw.exists():
        raise PaddleRuntimeContractError(f"{kind}_path_missing")
    symlink = _existing_symlink(raw)
    if symlink is not None and not (
        allow_explicit_interpreter_symlink and kind == "interpreter"
    ):
        raise PaddleRuntimeContractError(f"{kind}_symlink_forbidden:{symlink}")
    resolved = raw.resolve()
    allowed = [Path(item).expanduser().resolve() for item in allowlist]
    if not allowed or not any(
        resolved == item or _is_contained(resolved, item) for item in allowed
    ):
        raise PaddleRuntimeContractError(f"{kind}_path_not_allowlisted:{resolved}")
    if must_be_file and not resolved.is_file():
        raise PaddleRuntimeContractError(f"{kind}_file_required")
    if must_be_dir and not resolved.is_dir():
        raise PaddleRuntimeContractError(f"{kind}_directory_required")
    return resolved


def _model_manifest(root: Path, *, allow_empty: bool = False) -> dict[str, Any]:
    if not root.is_dir():
        raise PaddleRuntimeContractError("model_root_missing")
    files: list[dict[str, Any]] = []
    cache_metadata: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise PaddleRuntimeContractError(
                f"model_symlink_forbidden:{path.relative_to(root).as_posix()}"
            )
        if not path.is_file():
            continue
        row = {
            "relative_path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        (cache_metadata if _is_cache_metadata(row["relative_path"]) else files).append(row)
    if not files and not cache_metadata and not allow_empty:
        raise PaddleRuntimeContractError("model_root_empty")
    files_sha256 = sha256_json(files)
    cache_metadata_sha256 = sha256_json(cache_metadata)
    identity = {
        "model_root": str(root),
        "file_count": len(files),
        "files": files,
        "files_sha256": files_sha256,
    }
    return {
        **identity,
        "manifest_sha256": files_sha256,
        "identity_sha256": sha256_json(identity),
        "cache_metadata": cache_metadata,
        "cache_metadata_sha256": cache_metadata_sha256,
    }


def _minimal_env(workdir: Path, interpreter: Path) -> dict[str, str]:
    path_entries = [str(interpreter.parent)]
    inherited_path = os.environ.get("PATH")
    if inherited_path:
        path_entries.append(inherited_path)
    return {
        "PATH": os.pathsep.join(path_entries),
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
        "PADDLE_PDX_MODEL_SOURCE": "huggingface",
        "PADDLE_PDX_CACHE_HOME": str(workdir / "paddlex-cache"),
        "EFIREBLE_OCR_NETWORK": "disabled",
        "EFIREBLE_PRIVATE_DIAGNOSTIC": "1",
        "NO_PROXY": "*",
        "no_proxy": "*",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "all_proxy": "",
    }


def probe_external_runtime(
    interpreter: Path,
    expected_distributions: Mapping[str, str],
    *,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Read distribution metadata without importing Paddle or writing caches."""

    names = sorted(str(name) for name in expected_distributions)
    code = (
        "import importlib.metadata as m, json, sys\n"
        "names = json.loads(sys.argv[1])\n"
        "versions = {}\n"
        "for name in names:\n"
        "    try:\n"
        "        versions[name] = m.version(name)\n"
        "    except m.PackageNotFoundError:\n"
        "        versions[name] = None\n"
        "print(json.dumps({'python': sys.version, 'executable': sys.executable, 'distributions': versions}, sort_keys=True))"
    )
    with tempfile.TemporaryDirectory(prefix="tkc-paddle-probe-") as temporary:
        workdir = Path(temporary)
        try:
            completed = subprocess.run(
                [str(interpreter), "-c", code, json.dumps(names)],
                cwd=str(workdir),
                env=_minimal_env(workdir, interpreter),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise PaddleRuntimeContractError("runtime_inventory_timeout") from error
        except OSError as error:
            raise PaddleRuntimeContractError("runtime_inventory_process_failed") from error
    if completed.returncode != 0:
        raise PaddleRuntimeContractError("runtime_inventory_process_failed")
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise PaddleRuntimeContractError("runtime_inventory_shape_invalid")
    try:
        result = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise PaddleRuntimeContractError("runtime_inventory_json_invalid") from error
    if not isinstance(result, dict) or not isinstance(result.get("distributions"), dict):
        raise PaddleRuntimeContractError("runtime_inventory_shape_invalid")
    observed = result["distributions"]
    for name, expected in expected_distributions.items():
        if observed.get(name) != expected:
            raise PaddleRuntimeContractError(
                f"runtime_distribution_drift:{name}:{observed.get(name)}!={expected}"
            )
    result["distributions"] = {
        str(key): observed[key] for key in sorted(observed)
    }
    result["inventory_sha256"] = sha256_json(
        {key: value for key, value in result.items() if key != "inventory_sha256"}
    )
    return result


def _profile_model_manifest(
    model_root: Path,
    profile_id: str,
    resolved_model_dirs: Mapping[str, str],
) -> dict[str, Any]:
    components: dict[str, Any] = {}
    for key in sorted(resolved_model_dirs):
        directory = Path(resolved_model_dirs[key]).resolve()
        files: list[dict[str, Any]] = []
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise PaddleRuntimeContractError(f"model_symlink_forbidden:{key}")
            if path.is_file():
                relative = path.relative_to(model_root).as_posix()
                row = {
                    "relative_path": relative,
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
                if not _is_cache_metadata(relative):
                    files.append(row)
        if not files:
            raise PaddleRuntimeContractError(f"model_component_empty:{key}")
        files_sha256 = sha256_json(files)
        components[key] = {
            "directory": str(directory.relative_to(model_root).as_posix()),
            "file_count": len(files),
            "files": files,
            "files_sha256": files_sha256,
            "identity_sha256": sha256_json(
                {
                    "profile_id": profile_id,
                    "component": key,
                    "directory": str(directory.relative_to(model_root).as_posix()),
                    "files": files,
                    "files_sha256": files_sha256,
                }
            ),
        }
    stable_identity = {
        "profile_id": profile_id,
        "components": components,
    }
    return {
        **stable_identity,
        "stable_identity_sha256": sha256_json(stable_identity),
    }


def _required_model_status(
    backend_id: str, model_root: Path, configuration: Mapping[str, Any]
) -> dict[str, Any]:
    options = configuration.get("backend_options")
    if not isinstance(options, Mapping):
        options = {}
    model_dirs = options.get("model_dirs")
    if not isinstance(model_dirs, Mapping):
        model_dirs = {}
    profile_id = options.get("profile_id")
    if profile_id is None and backend_id == PADDLE_OCR_BACKEND:
        profile_id = TECHNICAL_TEXT_PROFILE_ID
    if profile_id is None and backend_id == PADDLE_CHART_BACKEND:
        profile_id = TECHNICAL_CHART_PROFILE_ID
    if profile_id is not None and not isinstance(profile_id, str):
        raise PaddleRuntimeContractError("paddle_profile_id_invalid")
    if backend_id in {PADDLE_STRUCTURE_BACKEND, PADDLE_STRUCTURE_V3_BACKEND}:
        if profile_id is None:
            profile_id = TECHNICAL_TABLE_PROFILE_ID if set(PROFILE_MODEL_DIRS[TECHNICAL_TABLE_PROFILE_ID]).issubset(model_dirs) else None
        if profile_id != TECHNICAL_TABLE_PROFILE_ID:
            required_profile = PROFILE_MODEL_DIRS[TECHNICAL_TABLE_PROFILE_ID]
            return {
                "profile_id": profile_id,
                "profile_schema": PROFILE_SCHEMA,
                "required_model_dirs": sorted(required_profile),
                "resolved_model_dirs": {},
                "required_model_files": {},
                "missing_model_dirs": sorted(required_profile),
                "available": False,
                "status": "not-run-missing-models",
                "code": "paddle_profile_required",
            }
    elif profile_id not in PROFILE_MODEL_DIRS:
        raise PaddleRuntimeContractError(f"paddle_profile_unknown:{profile_id}")
    expected_dirs = PROFILE_MODEL_DIRS[profile_id]
    if backend_id == PADDLE_OCR_BACKEND:
        expected_dirs = {
            key: expected_dirs[key]
            for key in ("text_detection_model_dir", "text_recognition_model_dir")
        }
    elif backend_id == PADDLE_CHART_BACKEND:
        expected_dirs = {"chart_model_dir": expected_dirs["chart_model_dir"]}
    profile = profile_definition(str(profile_id))
    supplied_constructor = options.get("constructor_options")
    supplied_predict = options.get("predict_options")
    supplied_model_names = options.get("model_names")
    if supplied_model_names is not None and supplied_model_names != PROFILE_MODEL_NAMES[str(profile_id)]:
        raise PaddleRuntimeContractError("paddle_profile_model_names_mismatch")
    if supplied_constructor is not None and supplied_constructor != profile["constructor"]:
        raise PaddleRuntimeContractError("paddle_profile_constructor_switches_mismatch")
    if supplied_predict is not None and supplied_predict != profile["predict"]:
        raise PaddleRuntimeContractError("paddle_profile_predict_switches_mismatch")
    required = tuple(expected_dirs)
    missing: list[str] = []
    resolved: dict[str, str] = {}
    for key in required:
        # A profile is an explicit binding.  Missing keys must remain missing;
        # silently substituting the directory name would make a constructor
        # appear bound when the worker actually received an incomplete config.
        value = model_dirs.get(key)
        if key not in model_dirs or value != expected_dirs[key]:
            missing.append(key)
            continue
        if not isinstance(value, str) or not value:
            missing.append(key)
            continue
        raw_candidate = model_root / value
        if raw_candidate.is_symlink() or any(
            parent.is_symlink() for parent in raw_candidate.parents if parent != model_root
        ):
            raise PaddleRuntimeContractError(f"model_dir_symlink_forbidden:{key}")
        candidate = raw_candidate.resolve()
        if not _is_contained(candidate, model_root) or not candidate.is_dir():
            missing.append(key)
            continue
        if _existing_symlink(candidate) is not None:
            raise PaddleRuntimeContractError(f"model_dir_symlink_forbidden:{key}")
        resolved[key] = str(candidate)
    required_files = {}
    stable_identity = None
    if not missing:
        stable_identity = _profile_model_manifest(model_root, str(profile_id), resolved)
        required_files = stable_identity["components"]
    return {
        "profile_id": profile_id,
        "profile_schema": PROFILE_SCHEMA,
        "required_model_dirs": list(required),
        "resolved_model_dirs": resolved,
        "required_model_files": required_files,
        "missing_model_dirs": missing,
        "available": not missing,
        "status": "available" if not missing else "not-run-missing-models",
        "stable_identity_sha256": stable_identity.get("stable_identity_sha256") if stable_identity else None,
    }


def freeze_external_runtime_contract(
    spec: Mapping[str, Any],
    *,
    backend_id: str,
    configuration: Mapping[str, Any],
    worker_path: Path,
    probe: bool = True,
) -> dict[str, Any]:
    """Freeze an external interpreter/model/worker contract.

    ``spec`` contains host-local paths and therefore belongs in a private job
    workpack, never in a source-free release.  The returned receipt is content
    addressed and can be used by the Phase 7B DAG.
    """

    if backend_id not in REQUIRED_MODEL_DIRS:
        raise PaddleRuntimeContractError(f"unsupported_paddle_backend:{backend_id}")
    if not isinstance(spec, Mapping):
        raise PaddleRuntimeContractError("external_runtime_contract_required")
    if spec.get("network_enabled") is True or spec.get("model_download") is True:
        raise PaddleRuntimeContractError("network_or_model_download_forbidden")
    if spec.get("mcp_http_dependency") is True or spec.get("mcp_http") is True:
        raise PaddleRuntimeContractError("mcp_http_dependency_forbidden")
    interpreter_value = spec.get("interpreter")
    runtime_root_value = spec.get("runtime_root")
    model_root_value = spec.get("model_root")
    worker_value = spec.get("worker_path") or str(worker_path)
    if not isinstance(runtime_root_value, str) or not runtime_root_value:
        raise PaddleRuntimeContractError("runtime_root_required")
    runtime_allowlist = spec.get("runtime_allowlist") or [runtime_root_value]
    if not isinstance(runtime_allowlist, (list, tuple)):
        raise PaddleRuntimeContractError("runtime_allowlist_invalid")
    runtime_root = _resolve_path(
        runtime_root_value,
        kind="runtime_root",
        allowlist=[str(value) for value in runtime_allowlist],
        must_be_dir=True,
    )
    interpreter_allowlist = spec.get("interpreter_allowlist")
    if interpreter_allowlist is None:
        interpreter_allowlist = [str(interpreter_value)]
    if not isinstance(interpreter_allowlist, (list, tuple)):
        raise PaddleRuntimeContractError("interpreter_allowlist_invalid")
    interpreter = _resolve_path(
        interpreter_value,
        kind="interpreter",
        allowlist=[str(value) for value in interpreter_allowlist],
        allow_explicit_interpreter_symlink=True,
        must_be_file=True,
    )
    if not isinstance(worker_value, str) or not worker_value:
        raise PaddleRuntimeContractError("worker_path_required")
    worker_allowlist = spec.get("worker_allowlist") or [worker_value]
    if not isinstance(worker_allowlist, (list, tuple)):
        raise PaddleRuntimeContractError("worker_allowlist_invalid")
    worker = _resolve_path(
        worker_value,
        kind="worker",
        allowlist=[str(value) for value in worker_allowlist],
        must_be_file=True,
    )
    if worker != worker_path.expanduser().resolve():
        raise PaddleRuntimeContractError("worker_path_mismatch")
    if not isinstance(model_root_value, str) or not model_root_value:
        raise PaddleRuntimeContractError("model_root_required")
    model_allowlist = spec.get("model_allowlist") or [model_root_value]
    if not isinstance(model_allowlist, (list, tuple)):
        raise PaddleRuntimeContractError("model_allowlist_invalid")
    model_root = _resolve_path(
        model_root_value,
        kind="model_root",
        allowlist=[str(value) for value in model_allowlist],
        must_be_dir=True,
    )
    expected = spec.get("expected_distributions")
    if expected is None:
        expected = dict(DEFAULT_PADDLE_DISTRIBUTIONS)
    if not isinstance(expected, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in expected.items()
    ):
        raise PaddleRuntimeContractError("expected_distributions_invalid")
    expected_distributions = {
        str(key): str(value) for key, value in sorted(expected.items())
    }
    inventory = spec.get("runtime_inventory")
    if probe:
        # Keep the user-specified venv launcher for execution. Resolving a
        # macOS ``venv/bin/python`` symlink to the system framework Python can
        # silently discard the venv site-packages and produce a false
        # distribution-missing receipt.
        inventory = probe_external_runtime(Path(interpreter_value).expanduser(), expected_distributions)
    if not isinstance(inventory, Mapping) or not isinstance(
        inventory.get("distributions"), Mapping
    ):
        raise PaddleRuntimeContractError("runtime_inventory_required")
    observed_distributions = {
        str(key): inventory["distributions"][key]
        for key in sorted(inventory["distributions"])
    }
    if observed_distributions != expected_distributions:
        raise PaddleRuntimeContractError("runtime_distribution_inventory_mismatch")
    inventory_row = dict(inventory)
    inventory_row["distributions"] = observed_distributions
    inventory_row["inventory_sha256"] = sha256_json(
        {key: value for key, value in inventory_row.items() if key != "inventory_sha256"}
    )
    model_manifest = _model_manifest(model_root, allow_empty=backend_id == PADDLE_CHART_BACKEND)
    config = json.loads(json.dumps(dict(configuration), ensure_ascii=False, sort_keys=True))
    if config.get("network_enabled") is True or config.get("model_download") is True:
        raise PaddleRuntimeContractError("configuration_network_or_download_forbidden")
    options = config.get("backend_options")
    if not isinstance(options, Mapping) or options.get("allow_model_download") is True:
        raise PaddleRuntimeContractError("configuration_model_download_forbidden")
    options = dict(options)
    compatibility_profile = (
        TECHNICAL_TEXT_PROFILE_ID
        if backend_id == PADDLE_OCR_BACKEND and options.get("profile_id") is None
        else TECHNICAL_CHART_PROFILE_ID
        if backend_id == PADDLE_CHART_BACKEND and options.get("profile_id") is None
        else options.get("profile_id")
    )
    if compatibility_profile in PROFILE_SWITCHES:
        profile = profile_definition(str(compatibility_profile))
        options.setdefault("profile_id", compatibility_profile)
        options.setdefault("model_names", PROFILE_MODEL_NAMES[str(compatibility_profile)])
        options.setdefault("constructor_options", profile["constructor"])
        options.setdefault("predict_options", profile["predict"])
        config["backend_options"] = options
    model_status = _required_model_status(backend_id, model_root, config)
    worker_sha256 = sha256_file(worker)
    isolation = {
        "requested": "offline",
        "environment_network_disabled": True,
        "os_network_isolation": "not-verified",
        "kernel_network_isolation": False,
        "gate": "macos_os_network_isolation_not_verified",
        "mcp_http_dependency": False,
    }
    contract = {
        "schema_version": PADDLE_RUNTIME_CONTRACT_SCHEMA,
        "protocol": PADDLE_RUNTIME_PROTOCOL,
        "backend_id": backend_id,
        "interpreter": str(Path(interpreter_value).expanduser()),
        "interpreter_resolved": str(interpreter),
        "interpreter_allowlist": sorted(
            str(Path(value).expanduser().resolve()) for value in interpreter_allowlist
        ),
        "runtime_root": str(runtime_root),
        "runtime_allowlist": sorted(
            str(Path(value).expanduser().resolve()) for value in runtime_allowlist
        ),
        "worker_path": str(worker),
        "worker_sha256": worker_sha256,
        "model_root": str(model_root),
        "model_allowlist": sorted(
            str(Path(value).expanduser().resolve()) for value in model_allowlist
        ),
        "model": model_manifest,
        "model_status": model_status,
        "runtime_profile": profile_definition(str(model_status.get("profile_id"))) if model_status.get("profile_id") in PROFILE_SWITCHES else None,
        "runtime_inventory": inventory_row,
        "configuration": config,
        "configuration_sha256": sha256_json(config),
        "network": isolation,
        "network_enabled": False,
        "model_download": False,
        "mcp_http_dependency": False,
        "available": bool(model_status["available"]),
        "status": "ready" if model_status["available"] else "paused",
        "candidate_only": True,
        "promotion": False,
    }
    contract["contract_sha256"] = sha256_json(contract)
    return contract


def validate_runtime_contract(contract: Mapping[str, Any]) -> None:
    if not isinstance(contract, Mapping):
        raise PaddleRuntimeContractError("runtime_contract_invalid")
    if contract.get("schema_version") != PADDLE_RUNTIME_CONTRACT_SCHEMA:
        raise PaddleRuntimeContractError("runtime_contract_schema_invalid")
    if contract.get("protocol") != PADDLE_RUNTIME_PROTOCOL:
        raise PaddleRuntimeContractError("runtime_contract_protocol_invalid")
    if contract.get("network_enabled") is not False or contract.get("model_download") is not False:
        raise PaddleRuntimeContractError("runtime_contract_network_or_download_invalid")
    if contract.get("mcp_http_dependency") is not False:
        raise PaddleRuntimeContractError("runtime_contract_mcp_http_invalid")
    if contract.get("candidate_only") is not True or contract.get("promotion") is not False:
        raise PaddleRuntimeContractError("runtime_contract_capability_invalid")
    network = contract.get("network")
    if not isinstance(network, Mapping):
        raise PaddleRuntimeContractError("runtime_contract_network_receipt_invalid")
    if network.get("environment_network_disabled") is not True:
        raise PaddleRuntimeContractError("runtime_contract_network_receipt_invalid")
    if network.get("kernel_network_isolation") is not False or network.get("mcp_http_dependency") is not False:
        raise PaddleRuntimeContractError("runtime_contract_network_receipt_invalid")
    if contract.get("contract_sha256") != sha256_json(
        {key: value for key, value in contract.items() if key != "contract_sha256"}
    ):
        raise PaddleRuntimeContractError("runtime_contract_hash_mismatch")
    backend_id = contract.get("backend_id")
    if backend_id not in REQUIRED_MODEL_DIRS:
        raise PaddleRuntimeContractError("runtime_contract_backend_invalid")
    try:
        runtime_root = _resolve_path(
            contract.get("runtime_root"),
            kind="runtime_root",
            allowlist=contract.get("runtime_allowlist") or (),
            must_be_dir=True,
        )
        interpreter = _resolve_path(
            contract.get("interpreter"),
            kind="interpreter",
            allowlist=contract.get("interpreter_allowlist") or (),
            allow_explicit_interpreter_symlink=True,
            must_be_file=True,
        )
        worker = _resolve_path(
            contract.get("worker_path"),
            kind="worker",
            allowlist=contract.get("worker_allowlist") or [contract.get("worker_path")],
            must_be_file=True,
        )
        model_root = _resolve_path(
            contract.get("model_root"),
            kind="model_root",
            allowlist=contract.get("model_allowlist") or (),
            must_be_dir=True,
        )
    except PaddleRuntimeContractError:
        raise
    if str(runtime_root) != str(contract.get("runtime_root")):
        raise PaddleRuntimeContractError("runtime_root_drift")
    if str(interpreter) != str(contract.get("interpreter_resolved")):
        raise PaddleRuntimeContractError("runtime_interpreter_drift")
    if str(worker) != str(contract.get("worker_path")):
        raise PaddleRuntimeContractError("runtime_worker_path_drift")
    if str(model_root) != str(contract.get("model_root")):
        raise PaddleRuntimeContractError("runtime_model_root_drift")
    if sha256_file(worker) != contract.get("worker_sha256"):
        raise PaddleRuntimeContractError("runtime_worker_drift")
    model = contract.get("model")
    if not isinstance(model, Mapping) or not isinstance(model.get("files"), list):
        raise PaddleRuntimeContractError("runtime_model_manifest_invalid")
    current_model = _model_manifest(model_root, allow_empty=backend_id == PADDLE_CHART_BACKEND)
    stable_model_keys = (
        "model_root",
        "file_count",
        "files",
        "files_sha256",
        "manifest_sha256",
        "identity_sha256",
    )
    if any(current_model.get(key) != model.get(key) for key in stable_model_keys):
        raise PaddleRuntimeContractError("runtime_model_manifest_drift")
    if model.get("cache_metadata_sha256") != sha256_json(model.get("cache_metadata", [])):
        raise PaddleRuntimeContractError("runtime_model_cache_metadata_invalid")
    inventory = contract.get("runtime_inventory")
    if not isinstance(inventory, Mapping) or inventory.get("inventory_sha256") != sha256_json(
        {key: value for key, value in inventory.items() if key != "inventory_sha256"}
    ):
        raise PaddleRuntimeContractError("runtime_inventory_hash_invalid")
    configuration = contract.get("configuration")
    if not isinstance(configuration, Mapping) or contract.get("configuration_sha256") != sha256_json(configuration):
        raise PaddleRuntimeContractError("runtime_configuration_hash_invalid")
    model_status = _required_model_status(backend_id, model_root, configuration)
    if model_status != contract.get("model_status"):
        raise PaddleRuntimeContractError("runtime_model_status_drift")
    expected_profile = contract.get("runtime_profile")
    profile_id = model_status.get("profile_id")
    if profile_id in PROFILE_SWITCHES:
        if expected_profile != profile_definition(str(profile_id)):
            raise PaddleRuntimeContractError("runtime_profile_drift")
    if contract.get("status") != "ready" or contract.get("available") is not True:
        raise PaddleRuntimeContractError("runtime_model_not_available")


def _png_decode_rgb(data: bytes) -> tuple[int, int, bytes]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PaddleRuntimeContractError("render_png_signature_invalid")
    position = 8
    width = height = color_type = bit_depth = None
    idat: list[bytes] = []
    while position + 12 <= len(data):
        length = int.from_bytes(data[position : position + 4], "big")
        kind = data[position + 4 : position + 8]
        payload = data[position + 8 : position + 8 + length]
        crc = data[position + 8 + length : position + 12 + length]
        if (
            len(payload) != length
            or len(crc) != 4
            or zlib.crc32(kind + payload) & 0xFFFFFFFF != int.from_bytes(crc, "big")
        ):
            raise PaddleRuntimeContractError("render_png_crc_invalid")
        if kind == b"IHDR":
            width = int.from_bytes(payload[0:4], "big")
            height = int.from_bytes(payload[4:8], "big")
            bit_depth, color_type, compression, filtering, interlace = payload[8:13]
            if (
                bit_depth != 8
                or color_type not in {2, 6}
                or compression != 0
                or filtering != 0
                or interlace != 0
            ):
                raise PaddleRuntimeContractError("render_png_variant_unsupported")
        elif kind == b"IDAT":
            idat.append(payload)
        elif kind == b"IEND":
            break
        position += 12 + length
    if not width or not height or color_type not in {2, 6} or not idat:
        raise PaddleRuntimeContractError("render_png_header_missing")
    channels = 4 if color_type == 6 else 3
    row_size = width * channels
    try:
        decoded = zlib.decompress(b"".join(idat))
    except zlib.error as error:
        raise PaddleRuntimeContractError("render_png_data_invalid") from error
    if len(decoded) != height * (row_size + 1):
        raise PaddleRuntimeContractError("render_png_data_size_invalid")
    rows: list[bytes] = []
    cursor = 0
    previous = bytearray(row_size)
    for _ in range(height):
        filter_type = decoded[cursor]
        cursor += 1
        row = bytearray(decoded[cursor : cursor + row_size])
        cursor += row_size
        for index in range(row_size):
            left = row[index - channels] if index >= channels else 0
            up = previous[index]
            up_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                row[index] = (row[index] + left) & 255
            elif filter_type == 2:
                row[index] = (row[index] + up) & 255
            elif filter_type == 3:
                row[index] = (row[index] + ((left + up) // 2)) & 255
            elif filter_type == 4:
                estimate = left + up - up_left
                pa, pb, pc = abs(estimate - left), abs(estimate - up), abs(estimate - up_left)
                predictor = left if pa <= pb and pa <= pc else (up if pb <= pc else up_left)
                row[index] = (row[index] + predictor) & 255
            elif filter_type != 0:
                raise PaddleRuntimeContractError("render_png_filter_invalid")
        if channels == 4:
            # Paddle receives RGB.  Alpha is deliberately discarded; the
            # source render is still bound by the full render SHA-256.
            rows.append(b"".join(row[index : index + 3] for index in range(0, len(row), 4)))
        else:
            rows.append(bytes(row))
        previous = row
    return int(width), int(height), b"".join(rows)


def _png_encode_rgb(width: int, height: int, pixels: bytes) -> bytes:
    if width <= 0 or height <= 0 or len(pixels) != width * height * 3:
        raise PaddleRuntimeContractError("crop_png_pixels_invalid")

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            len(payload).to_bytes(4, "big")
            + kind
            + payload
            + (zlib.crc32(kind + payload) & 0xFFFFFFFF).to_bytes(4, "big")
        )

    rows = b"".join(
        b"\x00" + pixels[row * width * 3 : (row + 1) * width * 3]
        for row in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00")
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


def _rotation_forward(point: Sequence[float], width: float, height: float, rotation: int) -> tuple[float, float]:
    x, y = float(point[0]), float(point[1])
    if rotation == 0:
        return x, y
    if rotation == 90:
        return height - y, x
    if rotation == 180:
        return width - x, height - y
    if rotation == 270:
        return y, width - x
    raise PaddleRuntimeContractError("page_rotation_invalid")


def _rotation_inverse(point: Sequence[float], width: float, height: float, rotation: int) -> tuple[float, float]:
    x, y = float(point[0]), float(point[1])
    if rotation == 0:
        return x, y
    if rotation == 90:
        return y, height - x
    if rotation == 180:
        return width - x, height - y
    if rotation == 270:
        return width - y, x
    raise PaddleRuntimeContractError("page_rotation_invalid")


def _affine_from_function(function: Any) -> list[float]:
    x00, y00 = function(0.0, 0.0)
    x10, y10 = function(1.0, 0.0)
    x01, y01 = function(0.0, 1.0)
    return [x10 - x00, x01 - x00, x00, y10 - y00, y01 - y00, y00, 0.0, 0.0, 1.0]


def _inverse_affine(matrix: Sequence[float]) -> list[float]:
    a, b, c, d, e, f, _, _, _ = [float(value) for value in matrix]
    determinant = a * e - b * d
    if abs(determinant) < 1e-12:
        raise PaddleRuntimeContractError("crop_transform_not_invertible")
    return [
        e / determinant,
        -b / determinant,
        (b * f - e * c) / determinant,
        -d / determinant,
        a / determinant,
        (d * c - a * f) / determinant,
        0.0,
        0.0,
        1.0,
    ]


def _apply_affine(matrix: Sequence[float], point: Sequence[float]) -> tuple[float, float]:
    x, y = float(point[0]), float(point[1])
    return (
        float(matrix[0]) * x + float(matrix[1]) * y + float(matrix[2]),
        float(matrix[3]) * x + float(matrix[4]) * y + float(matrix[5]),
    )


def build_crop_mapping(
    *,
    bbox_pdf_points: Sequence[Any],
    page_geometry: Mapping[str, Any],
    render_dimensions_px: Mapping[str, Any],
    render_dpi: int,
    allow_mediabox_bbox: bool = False,
) -> dict[str, Any]:
    """Build the reversible PDF-page/render/crop affine contract."""

    if not isinstance(bbox_pdf_points, (list, tuple)) or len(bbox_pdf_points) != 4:
        raise PaddleRuntimeContractError("raster_region_bbox_invalid")
    bbox = [float(value) for value in bbox_pdf_points]
    if not all(math.isfinite(value) for value in bbox) or not (bbox[0] < bbox[2] and bbox[1] < bbox[3]):
        raise PaddleRuntimeContractError("raster_region_bbox_invalid")
    media = page_geometry.get("media_box")
    crop = page_geometry.get("crop_box")
    if not isinstance(media, (list, tuple)) or len(media) != 4 or not isinstance(crop, (list, tuple)) or len(crop) != 4:
        raise PaddleRuntimeContractError("page_geometry_boxes_required")
    media_values = [float(value) for value in media]
    crop_values = [float(value) for value in crop]
    media_width = media_values[2] - media_values[0]
    media_height = media_values[3] - media_values[1]
    if media_width <= 0 or media_height <= 0:
        raise PaddleRuntimeContractError("page_geometry_media_box_invalid")
    if not (
        media_values[0] - 1e-6 <= crop_values[0] <= crop_values[2] <= media_values[2] + 1e-6
        and media_values[1] - 1e-6 <= crop_values[1] <= crop_values[3] <= media_values[3] + 1e-6
    ):
        raise PaddleRuntimeContractError("page_geometry_crop_box_outside_media_box")
    crop_top_left = [crop_values[0] - media_values[0], media_values[3] - crop_values[3], crop_values[2] - media_values[0], media_values[3] - crop_values[1]]
    allowed_box = [0.0, 0.0, media_width, media_height] if allow_mediabox_bbox else crop_top_left
    if not (allowed_box[0] - 1e-6 <= bbox[0] < bbox[2] <= allowed_box[2] + 1e-6 and allowed_box[1] - 1e-6 <= bbox[1] < bbox[3] <= allowed_box[3] + 1e-6):
        raise PaddleRuntimeContractError("raster_region_bbox_out_of_page")
    rotation = int(page_geometry.get("rotation", 0)) % 360
    if rotation not in {0, 90, 180, 270}:
        raise PaddleRuntimeContractError("page_rotation_invalid")
    try:
        render_width = int(render_dimensions_px["width"])
        render_height = int(render_dimensions_px["height"])
    except (KeyError, TypeError, ValueError) as error:
        raise PaddleRuntimeContractError("render_dimensions_invalid") from error
    if render_width <= 0 or render_height <= 0 or int(render_dpi) < 36:
        raise PaddleRuntimeContractError("render_dimensions_invalid")
    rotated_width = media_height if rotation in {90, 270} else media_width
    rotated_height = media_width if rotation in {90, 270} else media_height
    scale_x = render_width / rotated_width
    scale_y = render_height / rotated_height

    def page_to_render(point: Sequence[float]) -> tuple[float, float]:
        rotated = _rotation_forward(point, media_width, media_height, rotation)
        return rotated[0] * scale_x, rotated[1] * scale_y

    render_corners = [page_to_render((bbox[0], bbox[1])), page_to_render((bbox[2], bbox[1])), page_to_render((bbox[2], bbox[3])), page_to_render((bbox[0], bbox[3]))]
    left = math.floor(min(point[0] for point in render_corners))
    top = math.floor(min(point[1] for point in render_corners))
    right = math.ceil(max(point[0] for point in render_corners))
    bottom = math.ceil(max(point[1] for point in render_corners))
    if left < 0 or top < 0 or right > render_width or bottom > render_height or right <= left or bottom <= top:
        raise PaddleRuntimeContractError("raster_region_render_bbox_out_of_bounds")
    crop_width, crop_height = right - left, bottom - top

    def crop_to_page(x: float, y: float) -> tuple[float, float]:
        rotated = ((x + left) / scale_x, (y + top) / scale_y)
        return _rotation_inverse(rotated, media_width, media_height, rotation)

    def page_to_crop(x: float, y: float) -> tuple[float, float]:
        rx, ry = page_to_render((x, y))
        return rx - left, ry - top

    forward = _affine_from_function(page_to_crop)
    inverse = _affine_from_function(crop_to_page)
    inverse_check = _inverse_affine(forward)
    if max(abs(a - b) for a, b in zip(inverse, inverse_check)) > 1e-7:
        raise PaddleRuntimeContractError("crop_transform_inverse_mismatch")
    tolerance = max(1e-6, 72.0 / float(render_dpi) + 1e-6)
    for point in ((bbox[0], bbox[1]), (bbox[2], bbox[1]), (bbox[2], bbox[3]), (bbox[0], bbox[3])):
        round_trip = crop_to_page(*page_to_crop(*point))
        if max(abs(round_trip[0] - point[0]), abs(round_trip[1] - point[1])) > tolerance:
            raise PaddleRuntimeContractError("crop_transform_round_trip_failed")
    return {
        "pdf_bbox_points": [round(value, 8) for value in bbox],
        "render_pixel_bbox": [left, top, right, bottom],
        "crop_dimensions_px": {"width": crop_width, "height": crop_height},
        "render_dimensions_px": {"width": render_width, "height": render_height},
        "page_dimensions_points": {"width": media_width, "height": media_height},
        "rotation": rotation,
        "render_dpi": int(render_dpi),
        "matrix_crop_to_pdf": [round(value, 12) for value in inverse],
        "matrix_pdf_to_crop": [round(value, 12) for value in forward],
        "round_trip_tolerance_points": tolerance,
        "page_to_crop": page_to_crop,
        "crop_to_page": crop_to_page,
    }


def crop_render_png(
    render_bytes: bytes,
    mapping: Mapping[str, Any],
    *,
    expected_crop_sha256: str | None,
) -> dict[str, Any]:
    width, height, pixels = _png_decode_rgb(render_bytes)
    expected_dimensions = mapping.get("render_dimensions_px")
    if expected_dimensions != {"width": width, "height": height}:
        raise PaddleRuntimeContractError("render_dimensions_hash_binding_mismatch")
    bbox = mapping.get("render_pixel_bbox")
    if not isinstance(bbox, list) or len(bbox) != 4 or any(not isinstance(value, int) for value in bbox):
        raise PaddleRuntimeContractError("render_pixel_bbox_invalid")
    left, top, right, bottom = bbox
    if left < 0 or top < 0 or right > width or bottom > height or right <= left or bottom <= top:
        raise PaddleRuntimeContractError("render_pixel_bbox_out_of_bounds")
    rows = [
        pixels[row * width * 3 + left * 3 : row * width * 3 + right * 3]
        for row in range(top, bottom)
    ]
    crop_bytes = _png_encode_rgb(right - left, bottom - top, b"".join(rows))
    crop_sha256 = sha256_bytes(crop_bytes)
    if expected_crop_sha256 is not None and (not isinstance(expected_crop_sha256, str) or not _SHA256_RE.fullmatch(expected_crop_sha256)):
        raise PaddleRuntimeContractError("raster_region_crop_sha256_invalid")
    if expected_crop_sha256 is not None and crop_sha256 != expected_crop_sha256:
        raise PaddleRuntimeContractError("raster_region_crop_sha256_mismatch")
    return {
        "bytes": crop_bytes,
        "sha256": crop_sha256,
        "dimensions_px": {"width": right - left, "height": bottom - top},
        "render_pixel_bbox": [left, top, right, bottom],
    }


def rotate_png_for_ocr(data: bytes, degrees_clockwise: int) -> dict[str, Any]:
    """Rotate a canonical PNG without delegating geometry to the OCR engine."""

    rotation = int(degrees_clockwise) % 360
    if rotation not in {0, 90, 180, 270}:
        raise PaddleRuntimeContractError("crop_content_rotation_invalid")
    width, height, pixels = _png_decode_rgb(data)
    if rotation == 0:
        rotated_width, rotated_height, rotated_pixels = width, height, pixels
    else:
        rotated_width, rotated_height = (height, width) if rotation in {90, 270} else (width, height)
        rotated = bytearray(rotated_width * rotated_height * 3)
        for y in range(height):
            for x in range(width):
                source = (y * width + x) * 3
                if rotation == 90:
                    target_x, target_y = height - 1 - y, x
                elif rotation == 180:
                    target_x, target_y = width - 1 - x, height - 1 - y
                else:
                    target_x, target_y = y, width - 1 - x
                target = (target_y * rotated_width + target_x) * 3
                rotated[target : target + 3] = pixels[source : source + 3]
        rotated_pixels = bytes(rotated)
    result = _png_encode_rgb(rotated_width, rotated_height, rotated_pixels)
    return {
        "bytes": result,
        "sha256": sha256_bytes(result),
        "dimensions_px": {"width": rotated_width, "height": rotated_height},
        "content_rotation_clockwise": rotation,
    }


def build_crop_coordinate_transform(
    *,
    source_sha256: str,
    physical_page: int,
    render_sha256: str,
    render_dpi: int,
    mapping: Mapping[str, Any],
) -> dict[str, Any]:
    preprocessing = {
        "kind": "pdf-raster-region-crop-v1",
        "pdf_bbox_points": mapping["pdf_bbox_points"],
        "render_pixel_bbox": mapping["render_pixel_bbox"],
        "crop_dimensions_px": mapping["crop_dimensions_px"],
        "rotation": mapping["rotation"],
        "matrix_crop_to_pdf": mapping["matrix_crop_to_pdf"],
        "matrix_pdf_to_crop": mapping["matrix_pdf_to_crop"],
    }
    payload = {
        "source_sha256": source_sha256,
        "physical_page": int(physical_page),
        "render_sha256": render_sha256,
        "render_dpi": int(render_dpi),
        "mapping": preprocessing,
    }
    return {
        "schema_version": "tkc.pdf-ocr-coordinate-transform/v0.1",
        "id": stable_id("pct", payload),
        "source_sha256": source_sha256,
        "physical_page": int(physical_page),
        "render_sha256": render_sha256,
        "render_dpi": int(render_dpi),
        "source_space": "preprocessed-image-pixels-v1",
        "target_space": "pdf-page-top-left-points-v1",
        "image_dimensions_px": dict(mapping["crop_dimensions_px"]),
        "pdf_dimensions_points": dict(mapping["page_dimensions_points"]),
        "preprocessing": preprocessing,
        "matrix_3x3": list(mapping["matrix_crop_to_pdf"]),
        "inverse_matrix_3x3": list(mapping["matrix_pdf_to_crop"]),
        "matrix_source": "pdf-top-left-points-render-pixels-crop-local-affine-v1",
        "round_trip_tolerance_points": float(mapping["round_trip_tolerance_points"]),
    }


def _jsonable(value: Any, *, path: str = "result") -> Any:
    """Convert official Paddle/PaddleX result containers without guessing.

    PaddleX exposes NumPy arrays and result objects whose ``json`` property
    returns a ``{"res": ...}`` mapping.  The compiler accepts only the
    explicitly serializable subset and rejects unknown objects instead of
    stringifying them into apparently valid evidence.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PaddleRuntimeContractError(f"paddle_result_nonfinite:{path}")
        return value
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise PaddleRuntimeContractError(f"paddle_result_mapping_key_invalid:{path}")
            result[key] = _jsonable(item, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_jsonable(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            converted = tolist()
        except Exception as error:  # pragma: no cover - backend-specific object
            raise PaddleRuntimeContractError(f"paddle_result_array_conversion_failed:{path}") from error
        if converted is value:
            raise PaddleRuntimeContractError(f"paddle_result_array_conversion_failed:{path}")
        return _jsonable(converted, path=path)
    raise PaddleRuntimeContractError(f"paddle_result_unknown_value:{path}")


def _as_mapping(value: Any) -> dict[str, Any]:
    json_value = getattr(value, "json", None)
    if callable(json_value):
        try:
            json_value = json_value()
        except Exception as error:  # pragma: no cover - backend-specific object
            raise PaddleRuntimeContractError("paddle_result_json_failed") from error
    if isinstance(json_value, Mapping):
        normalized = _jsonable(json_value)
        if isinstance(normalized.get("res"), Mapping):
            return dict(normalized["res"])
        return dict(normalized)
    # PaddleX result objects can also implement Mapping.  Prefer their
    # official JSON view above; the Mapping view may contain renderer-only
    # values such as vis_fonts that are not part of the public result schema.
    if isinstance(value, Mapping):
        return dict(_jsonable(value))
    raise PaddleRuntimeContractError("paddle_result_unknown_shape")


def _to_list(value: Any) -> Any:
    return _jsonable(value) if value is not None else None


def _bbox_from_polygon(polygon: Sequence[Sequence[Any]]) -> list[float]:
    points = [[float(point[0]), float(point[1])] for point in polygon]
    if len(points) < 3:
        raise PaddleRuntimeContractError("paddle_polygon_too_few_points")
    return [min(point[0] for point in points), min(point[1] for point in points), max(point[0] for point in points), max(point[1] for point in points)]


def _validate_mixed_language_health(rows: Sequence[Mapping[str, Any]]) -> None:
    """Reject the observed CJK-to-zero failure when an English run is healthy.

    The guard is opt-in because a genuinely English-only crop is valid.  A
    caller that declares a mixed-language fixture/page receives a stable
    fail-closed error when at least two recognition rows are zero-like, an
    ASCII word is present, and no CJK character survived recognition.
    """

    texts = [str(row.get("text", "")).strip() for row in rows]
    has_latin = any(re.search(r"[A-Za-z]", text) for text in texts)
    has_cjk = any(re.search(r"[\u3400-\u9fff]", text) for text in texts)
    zero_like = [bool(text) and bool(re.fullmatch(r"[0\s.,|:/\\\\()\[\]{}+-]+", text)) and "0" in text for text in texts]
    if has_latin and not has_cjk and sum(zero_like) >= 2:
        raise PaddleRuntimeContractError("paddle_ocr_mixed_language_zero_anomaly")


def normalize_ppocrv6_result(result: Any, *, require_mixed_language: bool = False) -> list[dict[str, Any]]:
    """Normalize the real PaddleOCR 3.7 OCRResult field contract."""

    values = result if isinstance(result, (list, tuple)) else [result]
    rows: list[dict[str, Any]] = []
    for page_index, value in enumerate(values):
        page = _as_mapping(value)
        required = {"rec_texts", "rec_scores"}
        if not required.issubset(page):
            raise PaddleRuntimeContractError("paddle_ocr_result_shape_unknown")
        texts = _to_list(page.get("rec_texts"))
        scores = _to_list(page.get("rec_scores"))
        polygons = _to_list(page.get("rec_polys"))
        boxes = _to_list(page.get("rec_boxes"))
        if not isinstance(texts, (list, tuple)) or not isinstance(scores, (list, tuple)):
            raise PaddleRuntimeContractError("paddle_ocr_result_field_shape_invalid")
        if polygons is None and boxes is None:
            raise PaddleRuntimeContractError("paddle_ocr_result_geometry_missing")
        if polygons is not None and not isinstance(polygons, (list, tuple)):
            raise PaddleRuntimeContractError("paddle_ocr_result_rec_polys_invalid")
        if boxes is not None and not isinstance(boxes, (list, tuple)):
            raise PaddleRuntimeContractError("paddle_ocr_result_rec_boxes_invalid")
        if len(texts) != len(scores):
            raise PaddleRuntimeContractError("paddle_ocr_result_length_mismatch")
        if polygons is not None and len(polygons) not in {0, len(texts)}:
            raise PaddleRuntimeContractError("paddle_ocr_result_polygon_length_mismatch")
        if boxes is not None and len(boxes) not in {0, len(texts)}:
            raise PaddleRuntimeContractError("paddle_ocr_result_box_length_mismatch")
        for index, (text, score) in enumerate(zip(texts, scores)):
            if not isinstance(text, str):
                raise PaddleRuntimeContractError("paddle_ocr_result_text_invalid")
            # PaddleOCR 3.7 can return an aligned detection row whose
            # recognition text is empty.  It is not a text observation and
            # must not invalidate the other, independently anchored rows.
            if not text.strip():
                continue
            try:
                confidence = float(score)
            except (TypeError, ValueError) as error:
                raise PaddleRuntimeContractError("paddle_ocr_result_score_invalid") from error
            if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                raise PaddleRuntimeContractError("paddle_ocr_result_score_invalid")
            polygon = None
            if polygons and polygons[index] is not None:
                raw_polygon = _to_list(polygons[index])
                if not isinstance(raw_polygon, (list, tuple)):
                    raise PaddleRuntimeContractError("paddle_ocr_result_polygon_invalid")
                polygon = [[float(point[0]), float(point[1])] for point in raw_polygon if isinstance(point, (list, tuple)) and len(point) == 2]
                if len(polygon) != len(raw_polygon) or len(polygon) < 3:
                    raise PaddleRuntimeContractError("paddle_ocr_result_polygon_invalid")
            raw_box = boxes[index] if boxes else None
            if polygon is None and raw_box is not None:
                box = _to_list(raw_box)
                if not isinstance(box, (list, tuple)) or len(box) != 4:
                    raise PaddleRuntimeContractError("paddle_ocr_result_box_invalid")
                left, top, right, bottom = [float(item) for item in box]
                if not right > left or not bottom > top:
                    raise PaddleRuntimeContractError("paddle_ocr_result_box_invalid")
                polygon = [[left, top], [right, top], [right, bottom], [left, bottom]]
            if polygon is None:
                raise PaddleRuntimeContractError("paddle_ocr_result_geometry_missing")
            rows.append(
                {
                    "text": text,
                    "bbox": _bbox_from_polygon(polygon),
                    "polygon": polygon,
                    "confidence": confidence,
                    "kind": "text",
                    "result_shape": "paddleocr-3.7-ocr-result-rec-fields-v1",
                    "page_index": page_index,
                    "sequence": index,
                }
            )
    if require_mixed_language:
        _validate_mixed_language_health(rows)
    return rows


class _HtmlTableParser(HTMLParser):
    """Parse the bounded HTML subset emitted by PP-StructureV3."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.table_count = 0
        self._table_open = False
        self._row_index: int | None = None
        self._current_cell: dict[str, Any] | None = None
        self.rows: list[list[dict[str, Any]]] = []
        self.cells: list[dict[str, Any]] = []

    @staticmethod
    def _span(value: Any, code: str) -> int:
        if value is None or value == "":
            return 1
        if isinstance(value, bool):
            raise PaddleRuntimeContractError(code)
        try:
            text = str(value).strip()
            if not text.isdigit():
                raise ValueError
            number = int(text)
        except (TypeError, ValueError) as error:
            raise PaddleRuntimeContractError(code) from error
        if number < 1:
            raise PaddleRuntimeContractError(code)
        return number

    def _is_occupied(self, row_index: int, column_index: int) -> bool:
        return any(
            int(cell["row_index"]) <= row_index < int(cell["row_index"]) + int(cell["row_span"])
            and int(cell["column_index"]) <= column_index < int(cell["column_index"]) + int(cell["column_span"])
            for cell in self.cells
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attributes = {str(key).casefold(): value for key, value in attrs}
        if tag == "table":
            if self._table_open:
                raise PaddleRuntimeContractError("ppstructure_html_nested_table")
            self.table_count += 1
            self._table_open = True
            return
        if tag == "tr":
            if not self._table_open or self._row_index is not None or self._current_cell is not None:
                raise PaddleRuntimeContractError("ppstructure_html_row_shape_invalid")
            self._row_index = len(self.rows)
            self.rows.append([])
            return
        if tag not in {"td", "th"}:
            return
        if self._row_index is None or self._current_cell is not None:
            raise PaddleRuntimeContractError("ppstructure_html_cell_shape_invalid")
        row_span = self._span(attributes.get("rowspan"), "ppstructure_html_rowspan_invalid")
        column_span = self._span(attributes.get("colspan"), "ppstructure_html_colspan_invalid")
        column_index = 0
        while any(
            self._is_occupied(self._row_index, candidate)
            for candidate in range(column_index, column_index + column_span)
        ):
            column_index += 1
        if any(
            self._is_occupied(self._row_index + row_offset, column_index + column_offset)
            for row_offset in range(row_span)
            for column_offset in range(column_span)
        ):
            raise PaddleRuntimeContractError("ppstructure_html_cell_overlap")
        self._current_cell = {
            "row_index": self._row_index,
            "column_index": column_index,
            "row_span": row_span,
            "column_span": column_span,
            "text_parts": [],
        }

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "br" and self._current_cell is not None:
            self._current_cell["text_parts"].append(" ")
        else:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell["text_parts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"td", "th"}:
            if self._current_cell is None:
                raise PaddleRuntimeContractError("ppstructure_html_cell_end_invalid")
            cell = dict(self._current_cell)
            cell["text"] = " ".join("".join(cell.pop("text_parts", [])).split())
            self.cells.append(cell)
            self.rows[int(cell["row_index"])].append(cell)
            self._current_cell = None
            return
        if tag == "tr":
            if self._row_index is None or self._current_cell is not None or not self.rows[self._row_index]:
                raise PaddleRuntimeContractError("ppstructure_html_row_empty_or_unclosed")
            self._row_index = None
            return
        if tag == "table":
            if not self._table_open or self._row_index is not None or self._current_cell is not None:
                raise PaddleRuntimeContractError("ppstructure_html_table_unclosed")
            self._table_open = False

    def finish(self) -> tuple[list[dict[str, Any]], int, int]:
        if self.table_count != 1 or self._table_open or self._row_index is not None or self._current_cell is not None:
            raise PaddleRuntimeContractError("ppstructure_html_table_shape_invalid")
        if not self.rows or not self.cells:
            raise PaddleRuntimeContractError("ppstructure_html_cells_missing")
        row_count = len(self.rows)
        column_count = max(
            int(cell["column_index"]) + int(cell["column_span"]) for cell in self.cells
        )
        covered = {
            (row_index, column_index)
            for cell in self.cells
            for row_index in range(int(cell["row_index"]), int(cell["row_index"]) + int(cell["row_span"]))
            for column_index in range(int(cell["column_index"]), int(cell["column_index"]) + int(cell["column_span"]))
        }
        if len(covered) != row_count * column_count:
            raise PaddleRuntimeContractError("ppstructure_html_topology_gap")
        return self.cells, row_count, column_count


def _adapter_bbox(value: Any, code: str = "ppstructure_bbox_invalid") -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise PaddleRuntimeContractError(code)
    try:
        bbox = [float(item) for item in value]
    except (TypeError, ValueError) as error:
        raise PaddleRuntimeContractError(code) from error
    if any(not math.isfinite(item) for item in bbox) or not bbox[0] < bbox[2] or not bbox[1] < bbox[3]:
        raise PaddleRuntimeContractError(code)
    return bbox


def _bbox_inside(inner: Sequence[float], outer: Sequence[float], tolerance: float = 1.5) -> bool:
    return (
        float(inner[0]) >= float(outer[0]) - tolerance
        and float(inner[1]) >= float(outer[1]) - tolerance
        and float(inner[2]) <= float(outer[2]) + tolerance
        and float(inner[3]) <= float(outer[3]) + tolerance
    )


def _bbox_intersection_area(left: Sequence[float], right: Sequence[float]) -> float:
    return max(0.0, min(float(left[2]), float(right[2])) - max(float(left[0]), float(right[0]))) * max(
        0.0, min(float(left[3]), float(right[3])) - max(float(left[1]), float(right[1]))
    )


def _adapter_conflict(*, source_sha256: str, physical_page: int, conflict_type: str, object_ids: Sequence[str] = (), details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    row = {
        "schema_version": PADDLE_RESULT_ADAPTER_SCHEMA,
        "conflict_id": stable_id("pvc", source_sha256, physical_page, conflict_type, sorted(object_ids), dict(details or {})),
        "source_sha256": source_sha256,
        "physical_page": int(physical_page),
        "conflict_type": conflict_type,
        "object_ids": sorted(set(str(value) for value in object_ids)),
        "details": dict(details or {}),
        "candidate_only": True,
        "review_required": True,
        "promotion": False,
    }
    row["conflict_sha256"] = sha256_json({key: value for key, value in row.items() if key != "conflict_sha256"})
    return row


def _adapter_gap(*, source_sha256: str, physical_page: int, gap_type: str, reason: str, bbox: Sequence[float] | None = None, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    row = {
        "schema_version": PADDLE_RESULT_ADAPTER_SCHEMA,
        "gap_id": stable_id("pvg", source_sha256, physical_page, gap_type, reason, list(bbox) if bbox else None, dict(details or {})),
        "source_sha256": source_sha256,
        "physical_page": int(physical_page),
        "gap_type": gap_type,
        "reason": reason,
        "bbox": list(bbox) if bbox is not None else None,
        "details": dict(details or {}),
        "candidate_only": True,
        "review_required": True,
        "promotion": False,
    }
    row["gap_sha256"] = sha256_json({key: value for key, value in row.items() if key != "gap_sha256"})
    return row


def _table_ocr_alignment(table_ocr_pred: Mapping[str, Any], cells: Sequence[Mapping[str, Any]]) -> tuple[dict[int, str], list[int]]:
    if not table_ocr_pred:
        return {}, []
    texts = table_ocr_pred.get("rec_texts")
    boxes = table_ocr_pred.get("rec_boxes")
    if not isinstance(texts, list) or not isinstance(boxes, list) or len(texts) != len(boxes):
        raise PaddleRuntimeContractError("ppstructure_table_ocr_shape_invalid")
    assignments: dict[int, list[tuple[float, float, str]]] = {}
    unmatched: list[int] = []
    for index, (text, raw_box) in enumerate(zip(texts, boxes)):
        if not isinstance(text, str):
            raise PaddleRuntimeContractError("ppstructure_table_ocr_text_invalid")
        box = _adapter_bbox(raw_box, "ppstructure_table_ocr_bbox_invalid")
        if not text.strip():
            continue
        center = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
        scored = []
        for cell_index, cell in enumerate(cells):
            cell_box = cell["bbox"]
            inside = cell_box[0] <= center[0] <= cell_box[2] and cell_box[1] <= center[1] <= cell_box[3]
            overlap = _bbox_intersection_area(box, cell_box)
            if inside or overlap > 0.0:
                scored.append((0 if inside else 1, -overlap, cell_index))
        if not scored:
            unmatched.append(index)
            continue
        _inside, _negative_overlap, cell_index = sorted(scored)[0]
        assignments.setdefault(cell_index, []).append((box[1], box[0], text.strip()))
    return {
        cell_index: " ".join(text for _y, _x, text in sorted(values))
        for cell_index, values in assignments.items()
    }, unmatched


def _html_table_grid(*, table: Mapping[str, Any], table_index: int, table_bbox: Sequence[float] | None, source_sha256: str, physical_page: int, table_visual_object_id: str, crop_sha256: str | None) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    from visual_semantics import build_table_grid

    conflicts: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    pred_html = table.get("pred_html")
    cell_box_list = table.get("cell_box_list")
    table_ocr_pred = table.get("table_ocr_pred")
    if not isinstance(pred_html, str) or not pred_html.strip() or not isinstance(cell_box_list, list) or not isinstance(table_ocr_pred, Mapping):
        raise PaddleRuntimeContractError("ppstructure_table_result_fields_invalid")
    if table_bbox is None:
        gaps.append(_adapter_gap(source_sha256=source_sha256, physical_page=physical_page, gap_type="table-bbox", reason="table_layout_bbox_missing", details={"table_index": table_index}))
        return None, gaps, conflicts
    table_box = _adapter_bbox(table_bbox, "ppstructure_table_bbox_invalid")
    parser = _HtmlTableParser()
    try:
        parser.feed(pred_html)
        parser.close()
        html_cells, row_count, column_count = parser.finish()
    except (PaddleRuntimeContractError, Exception) as error:
        if isinstance(error, PaddleRuntimeContractError):
            raise
        raise PaddleRuntimeContractError("ppstructure_html_parse_failed") from error
    boxes = [_adapter_bbox(value, "ppstructure_cell_bbox_invalid") for value in cell_box_list]
    if len(boxes) != len(html_cells):
        gaps.append(_adapter_gap(source_sha256=source_sha256, physical_page=physical_page, gap_type="table-cell", reason="html_cell_box_count_mismatch", bbox=table_box, details={"table_index": table_index, "html_cell_count": len(html_cells), "cell_box_count": len(boxes)}))
        return None, gaps, conflicts
    for index, box in enumerate(boxes):
        if not _bbox_inside(box, table_box):
            conflicts.append(_adapter_conflict(source_sha256=source_sha256, physical_page=physical_page, conflict_type="cell-bbox-outside-table", object_ids=[table_visual_object_id], details={"table_index": table_index, "cell_index": index, "cell_bbox": box, "table_bbox": table_box}))
            gaps.append(_adapter_gap(source_sha256=source_sha256, physical_page=physical_page, gap_type="table-cell", reason="cell_bbox_outside_table", bbox=table_box, details={"table_index": table_index, "cell_index": index}))
            return None, gaps, conflicts
        for previous in boxes[:index]:
            if _bbox_intersection_area(box, previous) > 1e-6:
                conflicts.append(_adapter_conflict(source_sha256=source_sha256, physical_page=physical_page, conflict_type="cell-bbox-overlap", object_ids=[table_visual_object_id], details={"table_index": table_index, "cell_index": index}))
                gaps.append(_adapter_gap(source_sha256=source_sha256, physical_page=physical_page, gap_type="table-cell", reason="cell_bbox_overlap", bbox=table_box, details={"table_index": table_index, "cell_index": index}))
                return None, gaps, conflicts
    ocr_by_cell, unmatched_ocr = _table_ocr_alignment(dict(table_ocr_pred), [{"bbox": box} for box in boxes])
    if unmatched_ocr:
        conflicts.append(_adapter_conflict(source_sha256=source_sha256, physical_page=physical_page, conflict_type="table-ocr-unmatched", object_ids=[table_visual_object_id], details={"table_index": table_index, "unmatched_ocr_indices": unmatched_ocr}))
    table_id = str(table.get("table_region_id") or table.get("table_id") or f"table-{table_index}")
    cells: list[dict[str, Any]] = []
    for cell_index, (html_cell, box) in enumerate(zip(html_cells, boxes)):
        html_text = str(html_cell.get("text") or "").strip() or None
        ocr_text = ocr_by_cell.get(cell_index)
        if html_text and ocr_text and " ".join(html_text.split()) != " ".join(ocr_text.split()):
            conflicts.append(_adapter_conflict(source_sha256=source_sha256, physical_page=physical_page, conflict_type="cell-text-source-disagreement", object_ids=[table_visual_object_id], details={"table_index": table_index, "cell_index": cell_index, "html_text_sha256": sha256_json(html_text), "ocr_text_sha256": sha256_json(ocr_text), "resolution": "review-required"}))
        text_candidate = html_text or ocr_text
        cell_id = stable_id("cell", source_sha256, physical_page, table_id, cell_index, box, text_candidate)
        cells.append({
            "cell_id": cell_id,
            "row_id": f"row-{int(html_cell['row_index'])}",
            "column_id": f"column-{int(html_cell['column_index'])}",
            "row_span": int(html_cell["row_span"]),
            "column_span": int(html_cell["column_span"]),
            "text_candidate": text_candidate,
            "value_candidate": None,
            "unit_candidate": None,
            "symbol_candidate": None,
            "bbox": box,
            "typed_candidates": {"value": None, "unit": None, "symbol": None, "confidence": "candidate"},
            "source_anchor": {"source_sha256": source_sha256, "table_visual_object_id": table_visual_object_id, "cell_id": cell_id, "physical_page": int(physical_page), "bbox": box, "anchor_id": stable_id("anchor", source_sha256, physical_page, table_id, cell_id)},
            "crop_sha256": crop_sha256,
        })
    rows = [{"row_id": f"row-{index}", "index": index} for index in range(row_count)]
    columns = [{"column_id": f"column-{index}", "index": index, "label": None} for index in range(column_count)]
    grid = build_table_grid(
        source_sha256=source_sha256,
        table_visual_object_id=table_visual_object_id,
        physical_page=physical_page,
        rows=rows,
        columns=columns,
        cells=cells,
        topology_evidence=[{
            "backend": PADDLE_STRUCTURE_V3_BACKEND,
            "table_id": table_id,
            "bbox": table_box,
            "pred_html_sha256": sha256_json(pred_html),
            "table_ocr_pred_sha256": sha256_json(table_ocr_pred),
            "html_cell_count": len(html_cells),
            "cell_box_count": len(boxes),
            "geometry_precision": "exact-from-runtime-result",
            "adapter_protocol": PADDLE_RESULT_ADAPTER_PROTOCOL,
            "candidate_only": True,
            "review_required": "table-location-one-visual-reviewer; table-values-two-visual-reviewers",
        }],
    )
    return grid, gaps, conflicts


def _normalize_ppstructure_overall_ocr_result(
    payload: Mapping[str, Any],
    *,
    source_sha256: str,
    physical_page: int,
    require_mixed_language: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalize PaddleOCR 3.7 ``overall_ocr_res`` with positional proof.

    The four recognition arrays are a positional contract.  Empty text is
    retained as an explicit filtered gap only after every text/score/polygon/
    box entry is present, typed, and aligned.  No empty/unknown/misaligned
    entry is silently dropped.
    """

    fields = ("rec_texts", "rec_scores", "rec_polys", "rec_boxes")
    if any(field not in payload for field in fields):
        raise PaddleRuntimeContractError("paddle_structure_ocr_result_fields_missing")
    values: dict[str, Any] = {}
    for field in fields:
        try:
            value = _to_list(payload.get(field))
        except PaddleRuntimeContractError:
            raise
        if value is None or not isinstance(value, list):
            raise PaddleRuntimeContractError(f"paddle_structure_ocr_result_{field}_invalid")
        values[field] = value
    lengths = {len(values[field]) for field in fields}
    if len(lengths) != 1:
        raise PaddleRuntimeContractError("paddle_structure_ocr_result_length_mismatch")

    rows: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for index, (text, score, raw_polygon, raw_box) in enumerate(
        zip(values["rec_texts"], values["rec_scores"], values["rec_polys"], values["rec_boxes"])
    ):
        if not isinstance(text, str):
            raise PaddleRuntimeContractError("paddle_structure_ocr_result_text_invalid")
        try:
            confidence = float(score)
        except (TypeError, ValueError) as error:
            raise PaddleRuntimeContractError("paddle_structure_ocr_result_score_invalid") from error
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise PaddleRuntimeContractError("paddle_structure_ocr_result_score_invalid")
        if not isinstance(raw_polygon, list) or len(raw_polygon) < 3:
            raise PaddleRuntimeContractError("paddle_structure_ocr_result_polygon_invalid")
        polygon: list[list[float]] = []
        for point in raw_polygon:
            if not isinstance(point, list) or len(point) != 2:
                raise PaddleRuntimeContractError("paddle_structure_ocr_result_polygon_invalid")
            try:
                x, y = float(point[0]), float(point[1])
            except (TypeError, ValueError) as error:
                raise PaddleRuntimeContractError("paddle_structure_ocr_result_polygon_invalid") from error
            if not math.isfinite(x) or not math.isfinite(y):
                raise PaddleRuntimeContractError("paddle_structure_ocr_result_polygon_invalid")
            polygon.append([x, y])
        polygon_bbox = _bbox_from_polygon(polygon)
        box = _adapter_bbox(raw_box, "paddle_structure_ocr_result_box_invalid")
        if any(abs(float(left) - float(right)) > 1.5 for left, right in zip(polygon_bbox, box)):
            raise PaddleRuntimeContractError("paddle_structure_ocr_result_geometry_mismatch")
        if not text.strip():
            gaps.append(
                _adapter_gap(
                    source_sha256=source_sha256,
                    physical_page=physical_page,
                    gap_type="ocr-empty-text",
                    reason="empty_text_filtered_with_aligned_geometry",
                    bbox=box,
                    details={
                        "ocr_index": index,
                        "confidence": confidence,
                        "candidate_status": "filtered-candidate",
                        "alignment": "rec_texts-rec_scores-rec_polys-rec_boxes",
                    },
                )
            )
            continue
        rows.append(
            {
                "text": text,
                "bbox": box,
                "polygon": polygon,
                "confidence": confidence,
                "kind": "text",
                "result_shape": "paddleocr-3.7-ocr-result-rec-fields-v1",
                "page_index": int(payload.get("page_index", 0)) if isinstance(payload.get("page_index", 0), int) and not isinstance(payload.get("page_index", 0), bool) else 0,
                "sequence": index,
            }
        )
    if require_mixed_language:
        _validate_mixed_language_health(rows)
    return rows, gaps


def normalize_ppstructure_v3_result(
    result: Any,
    *,
    source_sha256: str,
    physical_page: int,
    table_visual_object_id: str,
    crop_sha256: str | None = None,
    image_dimensions: Mapping[str, Any] | None = None,
    require_mixed_language: bool = False,
) -> dict[str, Any]:
    """Adapt the real PaddleX 3.7 PP-StructureV3 JSON shape.

    Only the observed LayoutParsingResult fields are accepted.  A table grid
    is emitted only when HTML topology, cell boxes, table OCR and table layout
    can be joined by deterministic order and geometry.  Gaps/conflicts are
    explicit otherwise; no synthetic cell bbox is invented.
    """

    if not _SHA256_RE.fullmatch(str(source_sha256)):
        raise PaddleRuntimeContractError("result_source_sha256_invalid")
    if isinstance(result, (list, tuple)):
        if len(result) != 1:
            raise PaddleRuntimeContractError("paddle_structure_batch_shape_unknown")
        root = _as_mapping(result[0])
    else:
        root = _as_mapping(result)
    required_keys = {"parsing_res_list", "layout_det_res", "overall_ocr_res", "table_res_list"}
    if not required_keys.issubset(root):
        raise PaddleRuntimeContractError("ppstructure_result_shape_unknown")
    width = height = None
    if isinstance(image_dimensions, Mapping):
        try:
            width, height = float(image_dimensions["width"]), float(image_dimensions["height"])
        except (KeyError, TypeError, ValueError) as error:
            raise PaddleRuntimeContractError("paddle_result_image_dimensions_invalid") from error
        if width <= 0 or height <= 0:
            raise PaddleRuntimeContractError("paddle_result_image_dimensions_invalid")

    layout_boxes: list[dict[str, Any]] = []
    layout_value = root.get("layout_det_res")
    if layout_value is not None:
        if not isinstance(layout_value, Mapping) or not isinstance(layout_value.get("boxes"), list):
            raise PaddleRuntimeContractError("ppstructure_layout_result_shape_unknown")
        for index, raw_box in enumerate(layout_value["boxes"]):
            if not isinstance(raw_box, Mapping):
                raise PaddleRuntimeContractError("ppstructure_layout_box_invalid")
            bbox = _adapter_bbox(raw_box.get("coordinate", raw_box.get("bbox")), "ppstructure_layout_bbox_invalid")
            if width is not None and not _bbox_inside(bbox, [0.0, 0.0, width, height], tolerance=0.01):
                raise PaddleRuntimeContractError("ppstructure_layout_bbox_outside_crop")
            label_value = raw_box.get("label", raw_box.get("block_label"))
            if not isinstance(label_value, str) or not label_value.strip():
                raise PaddleRuntimeContractError("ppstructure_layout_label_missing")
            score_value = raw_box.get("score", raw_box.get("confidence", 0.0))
            try:
                score = float(score_value)
            except (TypeError, ValueError) as error:
                raise PaddleRuntimeContractError("ppstructure_layout_score_invalid") from error
            if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                raise PaddleRuntimeContractError("ppstructure_layout_score_invalid")
            layout_boxes.append({"index": index, "bbox": bbox, "label": label_value.strip().casefold().replace("_", "-"), "score": score})
    parsing_value = root.get("parsing_res_list")
    parsing_blocks: list[dict[str, Any]] = []
    if parsing_value is not None:
        if not isinstance(parsing_value, list):
            raise PaddleRuntimeContractError("ppstructure_parsing_result_shape_unknown")
        for raw_block in parsing_value:
            if not isinstance(raw_block, Mapping):
                raise PaddleRuntimeContractError("ppstructure_parsing_block_invalid")
            if not {"block_bbox", "block_label", "block_content"}.issubset(raw_block):
                raise PaddleRuntimeContractError("ppstructure_parsing_block_fields_missing")
            bbox = _adapter_bbox(raw_block["block_bbox"], "ppstructure_parsing_bbox_invalid")
            label = raw_block["block_label"]
            if not isinstance(label, str) or not label.strip():
                raise PaddleRuntimeContractError("ppstructure_parsing_label_invalid")
            content = raw_block["block_content"]
            if not isinstance(content, str):
                raise PaddleRuntimeContractError("ppstructure_parsing_content_invalid")
            parsing_blocks.append({"bbox": bbox, "label": label.strip().casefold().replace("_", "-"), "content": content})
    if not layout_boxes and parsing_blocks:
        layout_boxes = [{"index": index, "bbox": block["bbox"], "label": block["label"], "score": 0.0} for index, block in enumerate(parsing_blocks)]

    kind_map = {
        "table": "table",
        "figure": "figure",
        "picture": "picture",
        "image": "picture",
        "chart": "chart",
        "plot": "chart",
        "formula": "equation",
        "equation": "equation",
        "annotation": "annotation",
        "caption": "caption",
        "diagram": "diagram",
    }
    ignored_layout_labels = {"text", "doc-title", "paragraph-title", "header", "footer", "footnote", "list", "reference", "code", "seal"}
    visual_candidates: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for block in layout_boxes:
        label = str(block["label"])
        if label in ignored_layout_labels:
            continue
        kind = kind_map.get(label)
        if kind is None:
            gaps.append(_adapter_gap(source_sha256=source_sha256, physical_page=physical_page, gap_type="layout-label", reason="unknown_layout_label", bbox=block["bbox"], details={"label": label, "layout_index": block["index"]}))
            continue
        visual_id = stable_id("vo", source_sha256, physical_page, PADDLE_STRUCTURE_V3_BACKEND, kind, block["index"], block["bbox"])
        visual_candidates.append({
            "candidate_id": visual_id,
            "adapter_local_candidate_id": visual_id,
            "source_sha256": source_sha256,
            "physical_page": int(physical_page),
            "kind": kind,
            "label": label,
            "bbox": list(block["bbox"]),
            "confidence": float(block["score"]),
            "geometry_precision": "exact-from-runtime-result",
            "content_sha256": sha256_json({"label": label, "bbox": block["bbox"], "content": next((row["content"] for row in parsing_blocks if row["label"] == label and row["bbox"] == block["bbox"]), "")}),
            "candidate_only": True,
            "promotion": False,
            "verified_gold": False,
            "review_required": "visual-localization-one-reviewer; visual-values-two-reviewers" if kind in {"table", "chart", "equation"} else "visual-localization-one-reviewer",
        })

    observations: list[dict[str, Any]] = []
    overall_value = root.get("overall_ocr_res")
    if not isinstance(overall_value, Mapping):
        raise PaddleRuntimeContractError("ppstructure_overall_ocr_result_shape_unknown")
    ocr_payload = dict(overall_value)
    if "rec_polys" not in ocr_payload and "dt_polys" in ocr_payload:
        ocr_payload["rec_polys"] = ocr_payload["dt_polys"]
    observations, ocr_gaps = _normalize_ppstructure_overall_ocr_result(
        ocr_payload,
        source_sha256=source_sha256,
        physical_page=physical_page,
        require_mixed_language=require_mixed_language,
    )
    gaps.extend(ocr_gaps)

    table_results_value = root.get("table_res_list", [])
    if not isinstance(table_results_value, list):
        raise PaddleRuntimeContractError("ppstructure_table_result_list_invalid")
    table_blocks = [row for row in layout_boxes if row["label"] == "table"]
    if not table_blocks and parsing_blocks:
        table_blocks = [row for row in parsing_blocks if row["label"] == "table"]
    table_grids: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for table_index, table_value in enumerate(table_results_value):
        if not isinstance(table_value, Mapping):
            raise PaddleRuntimeContractError("ppstructure_table_result_invalid")
        table_bbox = table_blocks[table_index]["bbox"] if table_index < len(table_blocks) else None
        table_seed = table_blocks[table_index].get("index", table_index) if table_index < len(table_blocks) else table_index
        table_id = stable_id("vo", source_sha256, physical_page, PADDLE_STRUCTURE_V3_BACKEND, "table", table_seed, table_bbox or table_index)
        grid, table_gaps, table_conflicts = _html_table_grid(table=dict(table_value), table_index=table_index, table_bbox=table_bbox, source_sha256=source_sha256, physical_page=physical_page, table_visual_object_id=table_id, crop_sha256=crop_sha256)
        gaps.extend(table_gaps)
        conflicts.extend(table_conflicts)
        if grid is not None:
            table_grids.append(grid)
    core = {
        "schema_version": PADDLE_RESULT_ADAPTER_SCHEMA,
        "protocol": PADDLE_RESULT_ADAPTER_PROTOCOL,
        "result_shape": "paddlex-3.7-layout-parsing-result-v1",
        "observations": observations,
        "visual_candidates": visual_candidates,
        "table_grids": table_grids,
        "gaps": gaps,
        "conflicts": conflicts,
        "candidate_only": True,
        "promotion": False,
        "verified_gold": False,
    }
    core["normalized_result_sha256"] = sha256_json(core)
    return {
        **core,
        "raw_result": root,
        "raw_result_sha256": sha256_json(root),
        "raw_result_keys": sorted(root),
    }


def normalize_technical_chart_v1_result(result: Any) -> dict[str, Any]:
    """Normalize only the observed local ChartParsing result mapping.

    ChartParsing returns a one-item result whose JSON payload contains a
    result string. The adapter accepts pipe-delimited rows and explicit
    axis:, ticks:, legend:, series:, unit: or annotation: lines. It never
    infers chart semantics from ordinary prose or numeric-looking cells.
    """

    values = result if isinstance(result, (list, tuple)) else [result]
    if len(values) != 1:
        raise PaddleRuntimeContractError("chart_result_shape_unknown")
    root = _as_mapping(values[0])
    result_value = root.get("result")
    if not isinstance(result_value, str):
        raise PaddleRuntimeContractError("chart_result_field_missing")
    text = result_value.strip()
    if not text:
        raise PaddleRuntimeContractError("chart_result_empty")
    fence = chr(96) * 3
    lines = [line.strip() for line in text.splitlines() if line.strip() and line.strip() not in {fence, fence + "text"}]
    explicit: dict[str, list[str]] = {
        "axis": [],
        "ticks": [],
        "legend": [],
        "series": [],
        "unit": [],
        "annotations": [],
    }
    table_lines: list[list[str]] = []
    for line in lines:
        match = re.match(r"(?i)^(axis|axes|tick|ticks|legend|series|unit|annotation|annotations)\s*:\s*(.*)$", line)
        if match:
            label = match.group(1).casefold()
            key = "axis" if label in {"axis", "axes"} else "ticks" if label in {"tick", "ticks"} else "annotations" if label in {"annotation", "annotations"} else label
            if match.group(2).strip():
                explicit[key].append(match.group(2).strip())
            continue
        if "|" in line:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if cells and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells):
                continue
            if any(cells):
                table_lines.append(cells)
    rows: list[dict[str, Any]] = []
    headers = table_lines[0] if table_lines else []
    if len(table_lines) > 1 and headers:
        for row_index, values_row in enumerate(table_lines[1:]):
            cells: list[dict[str, Any]] = []
            for column_index, value in enumerate(values_row):
                if not value:
                    continue
                column = headers[column_index] if column_index < len(headers) and headers[column_index] else f"column-{column_index}"
                cell = {
                    "cell_id": f"chart-cell-{row_index}-{column_index}",
                    "column": column,
                    "text_candidate": value,
                    "confidence": "candidate",
                }
                if re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?%?", value):
                    cell["value_candidate"] = value
                    if explicit["unit"]:
                        cell["unit_candidate"] = explicit["unit"][0]
                cells.append(cell)
            if cells:
                rows.append({"row_id": f"chart-row-{row_index}", "cells": cells})
    chart = {
        "axis": list(explicit["axis"]),
        "ticks": list(explicit["ticks"]),
        "legend": list(explicit["legend"]),
        "series": list(explicit["series"]),
        "unit": list(explicit["unit"]),
        "annotations": list(explicit["annotations"]),
        "value_candidates": rows,
    }
    gaps: list[dict[str, Any]] = []
    for field in ("axis", "ticks", "legend", "series", "unit", "annotations"):
        if not chart[field]:
            gaps.append({
                "gap_type": f"chart_{field}_not_explicit",
                "reason": "field-not-explicitly-supported-by-result",
                "review_required": True,
            })
    if not rows:
        gaps.append({
            "gap_type": "chart_table_rows_missing",
            "reason": "pipe-delimited-header-and-row-not-observed",
            "review_required": True,
        })
    core = {
        "schema_version": "tkc.technical-chart-result/v0.1",
        "protocol": "technical-chart-result-adapter-v0.1",
        "result_shape": "paddleocr-3.7-chart-parsing-result-v1",
        "chart": chart,
        "chart_rows": rows,
        "gaps": gaps,
        "conflicts": [],
        "candidate_only": True,
        "promotion": False,
        "verified_gold": False,
    }
    core["normalized_result_sha256"] = sha256_json(core)
    return {
        **core,
        "raw_result": root,
        "raw_result_sha256": sha256_json(root),
        "raw_result_keys": sorted(root),
    }


def _require_int(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PaddleRuntimeContractError(code)
    return value


def normalize_ppstructure_v3_tables(
    result: Any,
    *,
    source_sha256: str,
    physical_page: int,
    table_visual_object_id: str,
    crop_sha256: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize only an explicit structured table/cell result contract.

    Raw PP-Structure layout parsing prose or PP-OCR line text is intentionally
    not accepted here.  A real table result must provide ``structured_tables``
    with rows, columns, cell indices and spans.  This is also the synthetic
    fixture contract used by the tests while the local model inventory lacks
    PP-StructureV3 assets.
    """

    root = _as_mapping(result)
    tables = root.get("structured_tables")
    if not isinstance(tables, list):
        raise PaddleRuntimeContractError("ppstructure_table_result_shape_unknown")
    if not _SHA256_RE.fullmatch(source_sha256):
        raise PaddleRuntimeContractError("table_source_sha256_invalid")
    grids: list[dict[str, Any]] = []
    from visual_semantics import build_table_grid

    for table_index, table_value in enumerate(tables):
        if not isinstance(table_value, Mapping):
            raise PaddleRuntimeContractError("ppstructure_table_invalid")
        table = dict(table_value)
        for key in ("table_id", "bbox", "rows", "columns", "cells"):
            if key not in table:
                raise PaddleRuntimeContractError(f"ppstructure_table_field_missing:{key}")
        rows_value, columns_value, cells_value = table["rows"], table["columns"], table["cells"]
        if not isinstance(rows_value, list) or not isinstance(columns_value, list) or not isinstance(cells_value, list):
            raise PaddleRuntimeContractError("ppstructure_table_topology_shape_invalid")
        rows: list[dict[str, Any]] = []
        row_ids: dict[int, str] = {}
        for row_index, row_value in enumerate(rows_value):
            if not isinstance(row_value, Mapping):
                raise PaddleRuntimeContractError("ppstructure_row_invalid")
            index = _require_int(row_value.get("index", row_index), "ppstructure_row_index_invalid")
            row_id = row_value.get("row_id", f"row-{index}")
            if not isinstance(row_id, str) or not row_id:
                raise PaddleRuntimeContractError("ppstructure_row_id_invalid")
            if index in row_ids or row_id in row_ids.values():
                raise PaddleRuntimeContractError("ppstructure_row_duplicate")
            row_ids[index] = row_id
            rows.append({"row_id": row_id, "index": index})
        if sorted(row_ids) != list(range(len(row_ids))):
            raise PaddleRuntimeContractError("ppstructure_row_indices_not_contiguous")
        columns: list[dict[str, Any]] = []
        column_ids: dict[int, str] = {}
        for column_index, column_value in enumerate(columns_value):
            if not isinstance(column_value, Mapping):
                raise PaddleRuntimeContractError("ppstructure_column_invalid")
            index = _require_int(column_value.get("index", column_index), "ppstructure_column_index_invalid")
            column_id = column_value.get("column_id", f"column-{index}")
            if not isinstance(column_id, str) or not column_id:
                raise PaddleRuntimeContractError("ppstructure_column_id_invalid")
            if index in column_ids or column_id in column_ids.values():
                raise PaddleRuntimeContractError("ppstructure_column_duplicate")
            column_ids[index] = column_id
            columns.append({"column_id": column_id, "index": index, "label": column_value.get("label")})
        if sorted(column_ids) != list(range(len(column_ids))):
            raise PaddleRuntimeContractError("ppstructure_column_indices_not_contiguous")
        cells: list[dict[str, Any]] = []
        for cell_index, cell_value in enumerate(cells_value):
            if not isinstance(cell_value, Mapping):
                raise PaddleRuntimeContractError("ppstructure_cell_invalid")
            cell = dict(cell_value)
            for key in ("row_index", "column_index", "row_span", "column_span", "bbox"):
                if key not in cell:
                    raise PaddleRuntimeContractError(f"ppstructure_cell_field_missing:{key}")
            row_index = _require_int(cell["row_index"], "ppstructure_cell_row_index_invalid")
            column_index = _require_int(cell["column_index"], "ppstructure_cell_column_index_invalid")
            row_span = _require_int(cell["row_span"], "ppstructure_cell_row_span_invalid")
            column_span = _require_int(cell["column_span"], "ppstructure_cell_column_span_invalid")
            if row_span < 1 or column_span < 1 or row_index not in row_ids or column_index not in column_ids:
                raise PaddleRuntimeContractError("ppstructure_cell_span_or_index_invalid")
            if row_index + row_span > len(rows) or column_index + column_span > len(columns):
                raise PaddleRuntimeContractError("ppstructure_cell_span_out_of_bounds")
            bbox = cell.get("bbox")
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                raise PaddleRuntimeContractError("ppstructure_cell_bbox_invalid")
            bbox = [float(value) for value in bbox]
            if not (bbox[0] < bbox[2] and bbox[1] < bbox[3]):
                raise PaddleRuntimeContractError("ppstructure_cell_bbox_invalid")
            cell_id = cell.get("cell_id", f"cell-{cell_index}")
            if not isinstance(cell_id, str) or not cell_id:
                raise PaddleRuntimeContractError("ppstructure_cell_id_invalid")
            if any(existing.get("cell_id") == cell_id for existing in cells):
                raise PaddleRuntimeContractError("ppstructure_cell_duplicate")
            typed = cell.get("typed_candidates")
            typed = dict(typed) if isinstance(typed, Mapping) else {}
            unknown = set(typed) - {"value", "unit", "symbol", "confidence"}
            if unknown:
                raise PaddleRuntimeContractError("ppstructure_cell_typed_candidates_unknown")
            value_candidate = cell.get("value", cell.get("value_candidate", typed.get("value")))
            unit_candidate = cell.get("unit", cell.get("unit_candidate", typed.get("unit")))
            symbol_candidate = cell.get("symbol", cell.get("symbol_candidate", typed.get("symbol")))
            confidence = typed.get("confidence", cell.get("confidence", "candidate"))
            if isinstance(confidence, (int, float)):
                confidence = "high" if confidence >= 0.9 else "medium" if confidence >= 0.7 else "low"
            if confidence not in {"candidate", "low", "medium", "high"}:
                raise PaddleRuntimeContractError("ppstructure_cell_confidence_invalid")
            cells.append(
                {
                    "cell_id": cell_id,
                    "row_id": row_ids[row_index],
                    "column_id": column_ids[column_index],
                    "row_span": row_span,
                    "column_span": column_span,
                    "text_candidate": cell.get("text", cell.get("text_candidate")),
                    "value_candidate": value_candidate,
                    "unit_candidate": unit_candidate,
                    "symbol_candidate": symbol_candidate,
                    "bbox": bbox,
                    "typed_candidates": {"value": value_candidate, "unit": unit_candidate, "symbol": symbol_candidate, "confidence": confidence},
                    "source_anchor": {"source_sha256": source_sha256, "table_visual_object_id": table_visual_object_id, "cell_id": cell_id, "physical_page": int(physical_page), "bbox": bbox, "anchor_id": stable_id("anchor", source_sha256, physical_page, cell_id)},
                    "crop_sha256": crop_sha256,
                }
            )
        occupied: set[tuple[int, int]] = set()
        for cell in cells:
            row_index = next(index for index, value in row_ids.items() if value == cell["row_id"])
            column_index = next(index for index, value in column_ids.items() if value == cell["column_id"])
            for row_index_value in range(row_index, row_index + cell["row_span"]):
                for column_index_value in range(column_index, column_index + cell["column_span"]):
                    slot = (row_index_value, column_index_value)
                    if slot in occupied:
                        raise PaddleRuntimeContractError("ppstructure_cell_overlap")
                    occupied.add(slot)
        table_visual_id = table.get("table_visual_object_id")
        if not isinstance(table_visual_id, str) or not table_visual_id:
            table_visual_id = table_visual_object_id if len(tables) == 1 else stable_id(
                "vo", source_sha256, physical_page, PADDLE_STRUCTURE_V3_BACKEND, str(table["table_id"])
            )
        for cell in cells:
            cell["source_anchor"]["table_visual_object_id"] = table_visual_id
        grid = build_table_grid(
            source_sha256=source_sha256,
            table_visual_object_id=table_visual_id,
            physical_page=physical_page,
            rows=rows,
            columns=columns,
            cells=cells,
            topology_evidence=[{"backend": PADDLE_STRUCTURE_V3_BACKEND, "table_id": str(table["table_id"]), "bbox": list(table["bbox"]), "contract": "ppstructure-structured-tables-v1"}],
        )
        grids.append(grid)
    return grids


def run_external_worker(
    contract: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    timeout_seconds: int = 120,
    workspace_root: Path | None = None,
    private_diagnostic_path: Path | None = None,
) -> dict[str, Any]:
    """Run one fixed JSONL request without exposing a source PDF."""

    validate_runtime_contract(contract)
    if request.get("backend_id") != contract.get("backend_id"):
        raise PaddleRuntimeContractError("worker_request_backend_mismatch")
    if request.get("input_kind") != "raster-crop":
        raise PaddleRuntimeContractError("worker_input_kind_must_be_raster_crop")
    forbidden_keys = {"source_path", "source_pdf", "pdf_path", "pdf_bytes"}
    if forbidden_keys.intersection(request):
        raise PaddleRuntimeContractError("worker_source_pdf_input_forbidden")
    input_value = request.get("image_path")
    if not isinstance(input_value, str):
        raise PaddleRuntimeContractError("worker_crop_path_required")
    input_path = Path(input_value).expanduser()
    if input_path.is_symlink() or not input_path.is_file():
        raise PaddleRuntimeContractError("worker_crop_missing_or_symlink")
    input_path = input_path.resolve()
    expected_input_sha256 = request.get("input_sha256")
    input_sha256 = sha256_file(input_path)
    if input_sha256 != expected_input_sha256:
        raise PaddleRuntimeContractError("worker_crop_hash_mismatch")
    root = (workspace_root or input_path.parent).expanduser().resolve()
    if not _is_contained(input_path, root):
        raise PaddleRuntimeContractError("worker_crop_workspace_escape")
    if root == input_path:
        raise PaddleRuntimeContractError("worker_input_output_overlap")
    request_row = {"schema_version": OCR_PROTOCOL_SCHEMA, **dict(request)}
    request_bytes = (json.dumps(request_row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    interpreter = Path(str(contract["interpreter"]))
    worker = Path(str(contract["worker_path"]))
    environment = _minimal_env(root, interpreter)
    start = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    raw_stderr = b""
    try:
        process = subprocess.Popen(
            [str(interpreter), str(worker), "--backend", str(contract["backend_id"])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(root),
            env=environment,
            start_new_session=(os.name == "posix"),
        )
        stdout, raw_stderr = process.communicate(input=request_bytes, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        partial_stderr = error.stderr or b""
        if process is not None:
            process.kill()
            _remainder_stdout, remainder_stderr = process.communicate()
            raw_stderr = (partial_stderr or b"") + (remainder_stderr or b"")
        _write_private_runtime_diagnostic(
            private_diagnostic_path,
            backend_id=str(contract.get("backend_id")),
            stderr=raw_stderr,
            stderr_audit=classify_paddle_stderr(raw_stderr),
            errors=[{"error_code": "worker_timeout"}],
            runtime_events=[],
            process={"returncode": process.returncode if process is not None else None, "timed_out": True},
        )
        raise PaddleRuntimeContractError("worker_timeout") from error
    except OSError as error:
        _write_private_runtime_diagnostic(
            private_diagnostic_path,
            backend_id=str(contract.get("backend_id")),
            stderr=raw_stderr,
            stderr_audit=classify_paddle_stderr(raw_stderr),
            errors=[{"error_code": "worker_process_failed"}],
            runtime_events=[],
            process={"returncode": process.returncode if process is not None else None, "timed_out": False},
        )
        raise PaddleRuntimeContractError("worker_process_failed") from error
    elapsed_ms = round((time.monotonic() - start) * 1000.0, 3)
    if process is None or process.returncode != 0:
        # stderr is intentionally not returned: it may contain source paths,
        # credentials, model paths, or engine internals.  If an operator needs
        # forensic bytes, that belongs in a separately access-controlled local
        # scratch receipt, never in the canonical/public exception.
        _write_private_runtime_diagnostic(
            private_diagnostic_path,
            backend_id=str(contract.get("backend_id")),
            stderr=raw_stderr,
            stderr_audit=classify_paddle_stderr(raw_stderr),
            errors=[{"error_code": "worker_process_failed"}],
            runtime_events=[],
            process={"returncode": process.returncode if process is not None else None, "timed_out": False},
        )
        raise PaddleRuntimeContractError("worker_process_failed")
    response_lines = [line for line in stdout.splitlines() if line.strip()]
    if len(response_lines) != 1:
        raise PaddleRuntimeContractError("worker_jsonl_response_shape_invalid")
    response_bytes = response_lines[0] + b"\n"
    try:
        response = json.loads(response_lines[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PaddleRuntimeContractError("worker_jsonl_response_invalid") from error
    if not isinstance(response, dict):
        raise PaddleRuntimeContractError("worker_jsonl_response_object_required")
    if response.get("schema_version") != OCR_PROTOCOL_SCHEMA or response.get("request_id") != request.get("request_id") or response.get("backend_id") != contract.get("backend_id"):
        raise PaddleRuntimeContractError("worker_jsonl_response_binding_invalid")
    runtime_events: list[dict[str, Any]] = []
    private_event = response.pop("_runtime_private_diagnostic", None)
    if isinstance(private_event, Mapping):
        runtime_events.append(dict(private_event))
    if response.get("status") != "ok":
        _write_private_runtime_diagnostic(
            private_diagnostic_path,
            backend_id=str(contract.get("backend_id")),
            stderr=raw_stderr,
            stderr_audit=classify_paddle_stderr(raw_stderr),
            errors=[{"request_id": request.get("request_id"), "error_code": str(response.get("error_code") or "worker_backend_error")}],
            runtime_events=runtime_events,
            process={"returncode": process.returncode, "timed_out": False},
        )
        raise PaddleRuntimeContractError(str(response.get("error_code") or "worker_backend_error"))
    inventory = response.get("runtime_inventory")
    expected_inventory = contract.get("runtime_inventory")
    if isinstance(inventory, Mapping) and isinstance(expected_inventory, Mapping):
        if inventory.get("distributions") != expected_inventory.get("distributions"):
            raise PaddleRuntimeContractError("worker_runtime_inventory_drift")
        if inventory.get("inventory_sha256") != expected_inventory.get("inventory_sha256"):
            raise PaddleRuntimeContractError("worker_runtime_inventory_receipt_drift")
    response["runtime_contract_sha256"] = contract["contract_sha256"]
    response["worker_receipt"] = {
        "protocol": PADDLE_RUNTIME_PROTOCOL,
        "interpreter": contract["interpreter_resolved"],
        "worker_path": contract["worker_path"],
        "worker_sha256": contract["worker_sha256"],
        "request_sha256": sha256_bytes(request_bytes),
        "response_sha256": _stable_response_sha256(response),
        "response_bytes": len(response_bytes),
        "elapsed_ms": elapsed_ms,
        "process_isolated": True,
        "network_isolation": contract["network"],
        "runtime_contract_sha256": contract["contract_sha256"],
        "source_pdf_passed": False,
        "input_crop_sha256": input_sha256,
        "candidate_only": True,
        "promotion": False,
    }
    _write_private_runtime_diagnostic(
        private_diagnostic_path,
        backend_id=str(contract.get("backend_id")),
        stderr=raw_stderr,
        stderr_audit=classify_paddle_stderr(raw_stderr),
        errors=[],
        runtime_events=runtime_events,
        process={"returncode": process.returncode, "timed_out": False},
    )
    return response


def run_external_worker_batch(
    contract: Mapping[str, Any],
    requests: Sequence[Mapping[str, Any]],
    *,
    timeout_seconds: float = 120.0,
    per_request_timeout_seconds: float | None = None,
    allow_request_timeout_overrides: bool = False,
    max_requests: int | None = None,
    workspace_root: Path | None = None,
    private_diagnostic_path: Path | None = None,
) -> dict[str, Any]:
    """Run a bounded crop-only JSONL session in one isolated worker.

    The worker process owns one backend engine and receives a finite list of
    crop requests.  The parent owns the hard total timeout and never sends a
    source PDF or source path.  A malformed/failed request is isolated in the
    returned error list so successful requests remain auditable.
    """

    validate_runtime_contract(contract)
    rows = list(requests)
    if max_requests is not None and (
        isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 0
    ):
        raise PaddleRuntimeContractError("worker_batch_max_requests_invalid")
    if timeout_seconds is not None and (
        isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds)) or float(timeout_seconds) < 0
    ):
        raise PaddleRuntimeContractError("worker_batch_timeout_invalid")
    if per_request_timeout_seconds is not None and (
        isinstance(per_request_timeout_seconds, bool)
        or not isinstance(per_request_timeout_seconds, (int, float))
        or not math.isfinite(float(per_request_timeout_seconds))
        or float(per_request_timeout_seconds) < 0
    ):
        raise PaddleRuntimeContractError("worker_batch_request_timeout_invalid")
    started = time.monotonic()
    if not rows:
        receipt = {
            "schema_version": VISUAL_BATCH_RECEIPT_SCHEMA,
            "protocol": SELECTIVE_VISUAL_PROTOCOL,
            "batch": True,
            "calls": 0,
            "pages": 0,
            "regions": 0,
            "elapsed_seconds": 0.0,
            "bytes": 0,
            "objects": 0,
            "status": "completed",
            "model_load_calls": 0,
            "source_pdf_passed": False,
            "candidate_only": True,
            "promotion": False,
            "items": [],
        }
        receipt["batch_receipt_sha256"] = sha256_json({key: value for key, value in receipt.items() if key != "batch_receipt_sha256"})
        validate_visual_batch_receipt(receipt)
        private_audit = classify_paddle_stderr(b"")
        private_diagnostic = _write_private_runtime_diagnostic(
            private_diagnostic_path,
            backend_id=str(contract.get("backend_id")),
            stderr=b"",
            stderr_audit=private_audit,
            errors=[],
            runtime_events=[],
            process={"returncode": 0, "timed_out": False},
        )
        return {
            "responses": [],
            "errors": [],
            "receipt": receipt,
            "private_audit": private_audit,
            "private_diagnostic": private_diagnostic,
            "runtime_events": [],
        }
    bounded = rows if max_requests is None else rows[:max_requests]
    errors: list[dict[str, Any]] = []
    runtime_events: list[dict[str, Any]] = []
    request_bytes_rows: list[tuple[dict[str, Any], bytes, str]] = []
    root_default = Path(str(contract.get("model_root"))).expanduser().resolve()
    root = (workspace_root or root_default).expanduser().resolve()
    interpreter = Path(str(contract["interpreter"]))
    worker = Path(str(contract["worker_path"]))
    for index, raw in enumerate(bounded):
        request = dict(raw) if isinstance(raw, Mapping) else {}
        forbidden_keys = {"source_path", "source_pdf", "pdf_path", "pdf_bytes"}
        if forbidden_keys.intersection(request):
            errors.append({"index": index, "request_id": request.get("request_id"), "error_code": "worker_source_pdf_input_forbidden"})
            continue
        if request.get("backend_id") != contract.get("backend_id"):
            errors.append({"index": index, "request_id": request.get("request_id"), "error_code": "worker_request_backend_mismatch"})
            continue
        if request.get("runtime_contract_sha256") != contract.get("contract_sha256"):
            errors.append({"index": index, "request_id": request.get("request_id"), "error_code": "worker_runtime_contract_binding_invalid"})
            continue
        if request.get("input_kind") != "raster-crop":
            errors.append({"index": index, "request_id": request.get("request_id"), "error_code": "worker_input_kind_must_be_raster_crop"})
            continue
        input_value = request.get("image_path")
        if not isinstance(input_value, str):
            errors.append({"index": index, "request_id": request.get("request_id"), "error_code": "worker_crop_path_required"})
            continue
        input_path = Path(input_value).expanduser()
        if input_path.is_symlink() or not input_path.is_file():
            errors.append({"index": index, "request_id": request.get("request_id"), "error_code": "worker_crop_missing_or_symlink"})
            continue
        input_path = input_path.resolve()
        if not _is_contained(input_path, root):
            errors.append({"index": index, "request_id": request.get("request_id"), "error_code": "worker_crop_workspace_escape"})
            continue
        expected_input_sha256 = request.get("input_sha256")
        input_sha256 = sha256_file(input_path)
        if input_sha256 != expected_input_sha256:
            errors.append({"index": index, "request_id": request.get("request_id"), "error_code": "worker_crop_hash_mismatch"})
            continue
        request_timeout = request.get("request_timeout_seconds") if allow_request_timeout_overrides else per_request_timeout_seconds
        if request_timeout is None:
            request_timeout = per_request_timeout_seconds
        if request_timeout is not None and (
            isinstance(request_timeout, bool)
            or not isinstance(request_timeout, (int, float))
            or not math.isfinite(float(request_timeout))
            or float(request_timeout) < 0
        ):
            errors.append({"index": index, "request_id": request.get("request_id"), "error_code": "worker_batch_request_timeout_invalid"})
            continue
        request["request_timeout_seconds"] = request_timeout
        request_row = {"schema_version": OCR_PROTOCOL_SCHEMA, **request}
        encoded = (json.dumps(request_row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        request_bytes_rows.append((request_row, encoded, input_sha256))
    if max_requests is not None and len(rows) > max_requests:
        for index in range(max_requests, len(rows)):
            request = rows[index]
            errors.append({"index": index, "request_id": request.get("request_id") if isinstance(request, Mapping) else None, "error_code": "worker_batch_max_requests_exceeded"})
    if not request_bytes_rows:
        elapsed = max(0.0, time.monotonic() - started)
        budget_error_codes = {"worker_batch_timeout", "worker_batch_max_requests_exceeded", "worker_request_timeout", "worker_batch_request_timeout"}
        receipt_status = "paused-budget-exhausted" if any(str(row.get("error_code")) in budget_error_codes for row in errors) else "paused-batch-failed" if errors else "completed"
        invalid_items: list[dict[str, Any]] = []
        for error in errors:
            index = error.get("index")
            raw = rows[index] if isinstance(index, int) and 0 <= index < len(rows) and isinstance(rows[index], Mapping) else {}
            request_id = raw.get("request_id") or error.get("request_id") or f"invalid-{index if index is not None else len(invalid_items)}"
            input_sha = raw.get("input_sha256") if isinstance(raw.get("input_sha256"), str) and _SHA256_RE.fullmatch(raw.get("input_sha256")) else None
            invalid_items.append({"request_id": str(request_id), "input_sha256": input_sha, "request_sha256": None, "status": "error", "error_code": str(error.get("error_code") or "worker_batch_request_invalid"), "response_sha256": None})
        receipt = {
            "schema_version": VISUAL_BATCH_RECEIPT_SCHEMA,
            "protocol": SELECTIVE_VISUAL_PROTOCOL,
            "batch": True,
            "calls": 0,
            "pages": 0,
            "regions": len(errors),
            "elapsed_seconds": elapsed,
            "bytes": 0,
            "objects": 0,
            "status": receipt_status,
            "model_load_calls": 0,
            "source_pdf_passed": False,
            "candidate_only": True,
            "promotion": False,
            "items": invalid_items,
        }
        receipt["batch_receipt_sha256"] = sha256_json({key: value for key, value in receipt.items() if key != "batch_receipt_sha256"})
        validate_visual_batch_receipt(receipt)
        private_audit = classify_paddle_stderr(b"")
        private_diagnostic = _write_private_runtime_diagnostic(
            private_diagnostic_path,
            backend_id=str(contract.get("backend_id")),
            stderr=b"",
            stderr_audit=private_audit,
            errors=errors,
            runtime_events=[],
            process={"returncode": 0, "timed_out": False},
        )
        return {
            "responses": [],
            "errors": errors,
            "receipt": receipt,
            "private_audit": private_audit,
            "private_diagnostic": private_diagnostic,
            "runtime_events": [],
        }
    payload = b"".join(encoded for _row, encoded, _sha in request_bytes_rows)
    environment = _minimal_env(root, interpreter)
    process: subprocess.Popen[bytes] | None = None
    raw_stdout = b""
    raw_stderr = b""
    process_started = time.monotonic()
    timed_out = False
    try:
        process = subprocess.Popen(
            [str(interpreter), str(worker), "--backend", str(contract["backend_id"])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(root),
            env=environment,
            start_new_session=(os.name == "posix"),
        )
        try:
            raw_stdout, raw_stderr = process.communicate(input=payload, timeout=float(timeout_seconds))
        except subprocess.TimeoutExpired as error:
            timed_out = True
            partial = error.output or b""
            partial_stderr = error.stderr or b""
            if process is not None:
                process.kill()
                remainder, remainder_stderr = process.communicate()
                raw_stdout = (partial or b"") + (remainder or b"")
                raw_stderr = (partial_stderr or b"") + (remainder_stderr or b"")
    except OSError as error:
        raise PaddleRuntimeContractError("worker_batch_process_failed") from error
    if process is None:
        raise PaddleRuntimeContractError("worker_batch_process_failed")
    response_lines = [line for line in raw_stdout.splitlines() if line.strip()]
    protocol_noise = [line for line in response_lines if _safe_json_object(line) is None]
    if protocol_noise:
        # stdout is the fixed JSONL protocol channel.  Library banners,
        # warnings, progress output, or any other non-JSON line make the batch
        # non-canonical.  Record only a stable code; never expose the line.
        errors.append(
            {
                "index": None,
                "request_id": None,
                "error_code": "worker_batch_stdout_protocol_noise",
            }
        )
    by_request_id: dict[str, dict[str, Any]] = {}
    for line in response_lines:
        try:
            response = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(response, dict):
            continue
        request_id = response.get("request_id")
        if isinstance(request_id, str):
            by_request_id[request_id] = response
    responses: list[dict[str, Any]] = []
    observed_model_load_calls = max(
        (int(row.get("model_load_calls", 0) or 0) for row in by_request_id.values() if isinstance(row, Mapping)),
        default=0,
    )
    expected_ids = {str(row.get("request_id")) for row, _encoded, _sha in request_bytes_rows}
    response_bytes_by_id = {
        str(json.loads(line.decode("utf-8")).get("request_id")): line + b"\n"
        for line in response_lines
        if line.strip()
        and _safe_json_object(line) is not None
        and isinstance(_safe_json_object(line).get("request_id"), str)
    }
    for index, (request, encoded, input_sha256) in enumerate(request_bytes_rows):
        request_id = request.get("request_id")
        response = by_request_id.get(str(request_id))
        if not isinstance(response, dict):
            errors.append({"index": index, "request_id": request_id, "error_code": "worker_batch_timeout" if timed_out else "worker_batch_response_missing"})
            continue
        if response.get("schema_version") != OCR_PROTOCOL_SCHEMA or response.get("backend_id") != contract.get("backend_id"):
            errors.append({"index": index, "request_id": request_id, "error_code": "worker_batch_response_binding_invalid"})
            continue
        if response.get("runtime_contract_sha256") != contract.get("contract_sha256"):
            errors.append({"index": index, "request_id": request_id, "error_code": "worker_runtime_contract_response_binding_invalid"})
            continue
        if response.get("input_sha256") not in {None, input_sha256}:
            errors.append({"index": index, "request_id": request_id, "error_code": "worker_batch_response_hash_invalid"})
            continue
        private_event = response.pop("_runtime_private_diagnostic", None)
        if isinstance(private_event, Mapping):
            runtime_events.append(dict(private_event))
        response_line = response_bytes_by_id.get(str(request_id), b"")
        response["runtime_contract_sha256"] = contract["contract_sha256"]
        response["worker_receipt"] = {
            "protocol": PADDLE_RUNTIME_PROTOCOL,
            "interpreter": contract["interpreter_resolved"],
            "worker_path": contract["worker_path"],
            "worker_sha256": contract["worker_sha256"],
            "request_sha256": sha256_bytes(encoded),
            "response_sha256": _stable_response_sha256(response),
            "response_bytes": len(response_line),
            "elapsed_ms": round((time.monotonic() - process_started) * 1000.0, 3),
            "process_isolated": True,
            "batch": True,
            "network_isolation": contract["network"],
            "runtime_contract_sha256": contract["contract_sha256"],
            "source_pdf_passed": False,
            "input_crop_sha256": input_sha256,
            "candidate_only": True,
            "promotion": False,
        }
        if response.get("status") != "ok":
            errors.append({
                "index": index,
                "request_id": request_id,
                "error_code": str(response.get("error_code") or "worker_backend_error"),
                "model_load_calls": int(response.get("model_load_calls", observed_model_load_calls) or 0),
            })
            continue
        inventory = response.get("runtime_inventory")
        expected_inventory = contract.get("runtime_inventory")
        if not isinstance(inventory, Mapping) or not isinstance(expected_inventory, Mapping) or inventory.get("distributions") != expected_inventory.get("distributions") or inventory.get("inventory_sha256") != expected_inventory.get("inventory_sha256"):
            errors.append({"index": index, "request_id": request_id, "error_code": "worker_runtime_inventory_drift"})
            continue
        responses.append(response)
        observed_model_load_calls = max(observed_model_load_calls, int(response.get("model_load_calls", 0) or 0))
    elapsed = max(0.0, time.monotonic() - started)
    object_count = sum(
        len(row.get("observations", [])) + len(row.get("table_grids", []))
        + len(row.get("visual_candidates", []))
        for row in responses
        if isinstance(row, Mapping)
    )
    budget_error_codes = {"worker_batch_timeout", "worker_batch_max_requests_exceeded", "worker_request_timeout", "worker_batch_request_timeout"}
    receipt_status = "paused-budget-exhausted" if timed_out or any(str(row.get("error_code")) in budget_error_codes for row in errors) else "paused-batch-failed" if errors else "completed"
    receipt = {
        "schema_version": VISUAL_BATCH_RECEIPT_SCHEMA,
        "protocol": SELECTIVE_VISUAL_PROTOCOL,
        "batch": True,
        "calls": len(request_bytes_rows),
        "pages": len({int(row.get("physical_page")) for row, _encoded, _sha in request_bytes_rows if row.get("physical_page") is not None}),
        "regions": len(request_bytes_rows),
        "elapsed_seconds": elapsed,
        "bytes": len(payload),
        "objects": object_count,
        "status": receipt_status,
        "model_load_calls": observed_model_load_calls,
        "source_pdf_passed": False,
        "candidate_only": True,
        "promotion": False,
    }
    responses_by_id = {str(response.get("request_id")): response for response in responses if isinstance(response, Mapping)}
    receipt["items"] = [
        {
            "request_id": str(row.get("request_id")),
            "input_sha256": row.get("input_sha256"),
            "request_sha256": sha256_bytes(encoded),
            "status": "ok" if str(row.get("request_id")) in responses_by_id else "error",
            "error_code": next((error.get("error_code") for error in errors if error.get("request_id") == row.get("request_id")), None),
            "response_sha256": (responses_by_id.get(str(row.get("request_id")), {}).get("worker_receipt") or {}).get("response_sha256"),
        }
        for row, encoded, _sha in request_bytes_rows
        if isinstance(row, Mapping)
    ] + [
        {
            "request_id": str(row.get("request_id")) if isinstance(row, Mapping) else None,
            "input_sha256": row.get("input_sha256") if isinstance(row, Mapping) else None,
            "request_sha256": None,
            "status": "error",
            "error_code": error.get("error_code"),
            "response_sha256": None,
        }
        for row, error in ((raw, error) for error in errors if error.get("request_id") is not None for raw in bounded if isinstance(raw, Mapping) and raw.get("request_id") == error.get("request_id") and not any(str(valid.get("request_id")) == str(error.get("request_id")) for valid, _encoded, _sha in request_bytes_rows))
    ]
    receipt["batch_receipt_sha256"] = sha256_json({key: value for key, value in receipt.items() if key != "batch_receipt_sha256"})
    validate_visual_batch_receipt(receipt)
    private_audit = classify_paddle_stderr(raw_stderr)
    private_diagnostic = _write_private_runtime_diagnostic(
        private_diagnostic_path,
        backend_id=str(contract.get("backend_id")),
        stderr=raw_stderr,
        stderr_audit=private_audit,
        errors=errors,
        runtime_events=runtime_events,
        process={"returncode": process.returncode, "timed_out": timed_out},
    )
    return {
        "responses": responses,
        "errors": errors,
        "receipt": receipt,
        "private_audit": private_audit,
        "private_diagnostic": private_diagnostic,
        "runtime_events": runtime_events,
    }


def validate_visual_batch_receipt(receipt: Mapping[str, Any]) -> None:
    """Validate the emitted bounded-batch receipt and its shipped schema.

    The runtime intentionally does not require the optional ``jsonschema``
    dependency.  It loads the bundled schema for the protocol constants and
    performs the small closed-shape/hash checks needed at this boundary.
    """

    schema_path = Path(__file__).resolve().parents[1] / "assets" / "schemas" / "visual-batch-receipt.schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PaddleRuntimeContractError("visual_batch_receipt_schema_unavailable") from error
    if not isinstance(receipt, Mapping) or receipt.get("schema_version") != schema.get("properties", {}).get("schema_version", {}).get("const"):
        raise PaddleRuntimeContractError("visual_batch_receipt_schema_invalid")
    required = schema.get("required", [])
    if any(key not in receipt for key in required):
        raise PaddleRuntimeContractError("visual_batch_receipt_required_field_missing")
    if receipt.get("protocol") != schema.get("properties", {}).get("protocol", {}).get("const") or receipt.get("batch") is not True:
        raise PaddleRuntimeContractError("visual_batch_receipt_protocol_invalid")
    if receipt.get("source_pdf_passed") is not False or receipt.get("candidate_only") is not True or receipt.get("promotion") is not False:
        raise PaddleRuntimeContractError("visual_batch_receipt_policy_invalid")
    load_calls = receipt.get("model_load_calls")
    if isinstance(load_calls, bool) or not isinstance(load_calls, int) or not 0 <= load_calls <= 1:
        raise PaddleRuntimeContractError("visual_batch_receipt_model_load_calls_invalid")
    items = receipt.get("items")
    if not isinstance(items, list):
        raise PaddleRuntimeContractError("visual_batch_receipt_items_invalid")
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("request_id"), str) or item.get("status") not in {"ok", "error"}:
            raise PaddleRuntimeContractError("visual_batch_receipt_item_invalid")
        for key in ("input_sha256", "request_sha256", "response_sha256"):
            value = item.get(key)
            if value is not None and (not isinstance(value, str) or not _SHA256_RE.fullmatch(value)):
                raise PaddleRuntimeContractError("visual_batch_receipt_item_hash_invalid")
        error_code = item.get("error_code")
        if error_code is not None and (not isinstance(error_code, str) or not error_code):
            raise PaddleRuntimeContractError("visual_batch_receipt_item_error_invalid")
    receipt_hash = receipt.get("batch_receipt_sha256")
    if not isinstance(receipt_hash, str) or not _SHA256_RE.fullmatch(receipt_hash) or receipt_hash != sha256_json({key: value for key, value in receipt.items() if key != "batch_receipt_sha256"}):
        raise PaddleRuntimeContractError("visual_batch_receipt_hash_invalid")


def _safe_json_object(line: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


__all__ = [
    "PADDLE_RUNTIME_PROTOCOL",
    "PADDLE_RUNTIME_PROFILE_SCHEMA",
    "PADDLE_RESULT_ADAPTER_SCHEMA",
    "PADDLE_RESULT_ADAPTER_PROTOCOL",
    "PADDLE_OCR_BACKEND",
    "PADDLE_STRUCTURE_BACKEND",
    "PADDLE_STRUCTURE_V3_BACKEND",
    "PADDLE_CHART_BACKEND",
    "PADDLE_RUNTIME_PRIVATE_DIAGNOSTIC_SCHEMA",
    "DEFAULT_PADDLE_DISTRIBUTIONS",
    "REQUIRED_MODEL_DIRS",
    "TECHNICAL_TEXT_PROFILE_ID",
    "TECHNICAL_TABLE_PROFILE_ID",
    "LEGACY_TECHNICAL_CHART_PROFILE_ID",
    "TECHNICAL_CHART_PROFILE_ID",
    "PROFILE_MODEL_DIRS",
    "PROFILE_MODEL_NAMES",
    "PROFILE_SWITCHES",
    "PaddleRuntimeContractError",
    "canonical_json",
    "sha256_bytes",
    "sha256_file",
    "sha256_json",
    "stable_id",
    "profile_definition",
    "build_technical_table_profile",
    "build_technical_chart_profile",
    "build_legacy_technical_chart_profile",
    "freeze_external_runtime_contract",
    "validate_runtime_contract",
    "probe_external_runtime",
    "build_crop_mapping",
    "crop_render_png",
    "build_crop_coordinate_transform",
    "normalize_ppocrv6_result",
    "normalize_ppstructure_v3_result",
    "normalize_technical_chart_v1_result",
    "normalize_ppstructure_v3_tables",
    "run_external_worker",
    "run_external_worker_batch",
    "classify_paddle_stderr",
    "validate_private_runtime_diagnostic",
    "validate_visual_batch_receipt",
]
