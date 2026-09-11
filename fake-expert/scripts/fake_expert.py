#!/usr/bin/env python3
"""Thin, offline UX entry point for the fake-expert compiler.

The entry point freezes a small private job, reports deterministic gates, and
routes already complete native workpacks to the existing direct-reference
builder.  It deliberately does not author semantic review, Gold, attestations,
promotion, execution, OCR, model runs, downloads, or network calls.
"""

from __future__ import annotations

import argparse
import datetime as datetime_module
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

SCRIPT_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_ROOT.parents[2]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from compiler_version import (  # noqa: E402
    DIRECT_REFERENCE_COMPILER_VERSION,
    FAKE_EXPERT_JOB_PROTOCOL,
    FAKE_EXPERT_JOB_SCHEMA,
    RELEASE_VERSION,
    UX_CLI_PROTOCOL,
    UX_COMPILER_VERSION,
)


PROFILES = ("native-text", "scan-text", "scan-structure", "visual")
NATIVE_TEXT_MINIMUM_CHARACTERS = 40
TARGET_STAGES = ("draft", "reviewed", "ready", "executable")
GOLD_MODES = ("none", "reuse", "create", "update")
PROFILE_BACKENDS: dict[str, tuple[str, ...]] = {
    "native-text": ("pypdf-native",),
    "scan-text": ("paddleocr-ppocrv6", "pymupdf-tesseract", "docling"),
    "scan-structure": ("paddleocr-ppstructure-v3",),
    "visual": ("pdftoppm", "paddleocr-ppstructure-v3", "docling"),
}
AUTHORITATIVE_RUNTIME_BACKENDS = {
    "paddleocr-ppocrv6",
    "paddleocr-ppstructure-v3",
}
JOB_FILE_NAME = "fake-expert-job.json"
JOB_FILE_SUFFIX = ".fake-expert-job.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
FORBIDDEN_SUBMISSION_KEYS = {
    "prediction", "predictions", "expected_answer", "expected_answers",
    "model_output", "model_confidence", "inference_output", "ppstructure",
    "chart2table", "docling", "gold", "hidden_answer",
}

SKILL_ROOT = SCRIPT_ROOT.parent
SKILL_FILE = SKILL_ROOT / "SKILL.md"
COMMON_READING = (
    "references/quickstart.md",
    "references/glossary.md",
    "references/troubleshooting.md",
)
PROFILE_READING: dict[str, tuple[str, ...]] = {
    "native-text": (
        "references/pdf-intermediate-representation.md",
        "references/compilation-workflow.md",
        "references/expert-skill-contract.md",
    ),
    "scan-text": (
        "references/scanned-pdf-ocr.md",
        "references/pdf-intermediate-representation.md",
        "references/phase-7d4-runtime-qualification-implementation-outline.md",
    ),
    "scan-structure": (
        "references/scanned-pdf-ocr.md",
        "references/pdf-intermediate-representation.md",
        "references/phase-7d4-runtime-qualification-implementation-outline.md",
        "references/quality-gates.md",
    ),
    "visual": (
        "references/phase-7a-visual-semantics-implementation-outline.md",
        "references/phase-7d-visual-extraction-implementation-outline.md",
        "references/phase-7d4-runtime-qualification-implementation-outline.md",
    ),
}
STAGE_READING: dict[str, tuple[str, ...]] = {
    "draft": (),
    "reviewed": ("references/semantic-promotion.md", "references/quality-gates.md"),
    "ready": ("references/semantic-promotion.md", "references/quality-gates.md"),
    "executable": (
        "references/semantic-promotion.md",
        "references/quality-gates.md",
        "references/safe-formula-execution.md",
    ),
}
GOLD_READING = ("references/semantic-promotion.md", "references/quality-gates.md")
DOCUMENTED_ERROR_CODES = (
    "workspace_too_broad",
    "job_schema_invalid",
    "job_schema_upgrade_required",
    "job_hash_mismatch",
    "job_drift",
    "required_reference_missing",
    "required_reference_hash_mismatch",
    "source_profile_mismatch",
    "backend_required_for_profile",
    "backend_invalid_for_profile",
    "runtime_qualification_required",
    "runtime_qualification_unverified",
    "semantic_authoring_required",
    "structure_review_required",
    "visual_review_required",
    "gold_review_required",
    "independent_review_required",
    "competency_required",
    "seal_required",
    "draft_output_invalid",
)


class FakeExpertError(ValueError):
    """Stable, user-facing failure from the unified entry point."""


def _forbidden_submission_key(value: Any, *, ignore_declarations: bool = False) -> str | None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            lowered = str(key).casefold().replace("-", "_")
            if ignore_declarations and lowered == "declarations":
                continue
            if lowered in FORBIDDEN_SUBMISSION_KEYS or any(
                token in lowered
                for token in ("model_output", "expected_answer", "hidden_answer", "pp_structure", "chart2table")
            ):
                return str(key)
            found = _forbidden_submission_key(nested, ignore_declarations=ignore_declarations)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _forbidden_submission_key(nested, ignore_declarations=ignore_declarations)
            if found:
                return found
    return None


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _required_reading_paths(profile: str, target_stage: str, gold_mode: str) -> list[str]:
    """Return the deterministic, minimal reference route for one job."""

    ordered = list(COMMON_READING)
    ordered.extend(PROFILE_READING.get(profile, ()))
    ordered.extend(STAGE_READING.get(target_stage, ()))
    if gold_mode != "none":
        ordered.extend(GOLD_READING)
    return list(dict.fromkeys(ordered))


def _reference_path(relative: str) -> Path:
    value = Path(relative)
    if (
        value.is_absolute()
        or value.parts[:1] != ("references",)
        or value.suffix != ".md"
        or any(part in {"", ".", ".."} for part in value.parts)
    ):
        raise FakeExpertError("required_reference_path_invalid")
    path = (SKILL_ROOT / value).resolve()
    if not _within(path, SKILL_ROOT) or path.is_symlink():
        raise FakeExpertError("required_reference_path_invalid")
    return path


def _required_reading_records(profile: str, target_stage: str, gold_mode: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for relative in _required_reading_paths(profile, target_stage, gold_mode):
        path = _reference_path(relative)
        if not path.is_file():
            raise FakeExpertError(f"required_reference_missing:{relative}")
        records.append({"path": relative, "sha256": _sha256_file(path)})
    return records


def _document_contract_report() -> dict[str, Any]:
    """Validate the progressive-reading contract without mutating the Skill."""

    issues: list[str] = []
    try:
        skill_text = SKILL_FILE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        skill_text = ""
        issues.append("skill_document_missing")

    routed = sorted({
        relative
        for profile in PROFILES
        for stage in TARGET_STAGES
        for gold_mode in GOLD_MODES
        for relative in _required_reading_paths(profile, stage, gold_mode)
    })
    for relative in routed:
        try:
            path = _reference_path(relative)
        except FakeExpertError:
            issues.append(f"required_reference_path_invalid:{relative}")
            continue
        if not path.is_file():
            issues.append(f"required_reference_missing:{relative}")
        if relative not in skill_text:
            issues.append(f"required_reference_unrouted:{relative}")

    linked = sorted(set(re.findall(r"references/[A-Za-z0-9._-]+\.md", skill_text)))
    for relative in linked:
        try:
            path = _reference_path(relative)
        except FakeExpertError:
            issues.append(f"linked_reference_path_invalid:{relative}")
            continue
        if not path.is_file():
            issues.append(f"linked_reference_missing:{relative}")

    troubleshooting_path = SKILL_ROOT / "references" / "troubleshooting.md"
    version_matrix_path = SKILL_ROOT / "references" / "version-matrix.md"
    try:
        troubleshooting = troubleshooting_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        troubleshooting = ""
    for code in DOCUMENTED_ERROR_CODES:
        if f"`{code}`" not in troubleshooting:
            issues.append(f"undocumented_error_code:{code}")
    try:
        version_matrix = version_matrix_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        version_matrix = ""
    for token in (RELEASE_VERSION, UX_COMPILER_VERSION, UX_CLI_PROTOCOL, FAKE_EXPERT_JOB_SCHEMA, FAKE_EXPERT_JOB_PROTOCOL):
        if f"`{token}`" not in version_matrix:
            issues.append(f"version_matrix_missing:{token}")
    if f"fake-expert v{RELEASE_VERSION}" not in skill_text:
        issues.append("skill_release_identity_missing")

    return {
        "name": "document_contract",
        "passed": not issues,
        "code": "document_contract_ok" if not issues else "document_contract_invalid",
        "required_reference_count": len(routed),
        "linked_reference_count": len(linked),
        "issues": sorted(set(issues)),
        "meaning": "Required reading, stable errors, and current version identities are machine-checked.",
        "next_action": "continue" if not issues else "repair_document_contract",
    }


def _sha256_path(path: Path) -> str:
    path = path.expanduser()
    if path.is_symlink():
        raise FakeExpertError("symlink_path_forbidden")
    if path.is_file():
        return _sha256_file(path)
    if not path.is_dir():
        raise FakeExpertError("input_missing")
    rows: list[dict[str, str]] = []
    for file_path in _walk_files(path, max_depth=16):
        relative = file_path.relative_to(path).as_posix()
        rows.append({"path": relative, "sha256": _sha256_file(file_path)})
    rows.sort(key=lambda row: row["path"])
    return _sha256_bytes(_canonical_bytes({"files": rows}))


def _read_json(path: Path, code: str = "json_invalid") -> Any:
    if path.is_symlink() or not path.is_file():
        raise FakeExpertError(f"{code}_missing")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FakeExpertError(code) from error


def _write_json(path: Path, value: Any, *, overwrite: bool) -> None:
    if path.is_symlink() or (path.exists() and not overwrite):
        raise FakeExpertError("job_exists" if not overwrite else "job_symlink_forbidden")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _now() -> str:
    return datetime_module.datetime.now(datetime_module.timezone.utc).isoformat(
        timespec="seconds"
    )


def _safe_path(path_value: str | Path, code: str = "path_invalid") -> Path:
    path = Path(path_value).expanduser()
    if "\x00" in str(path) or path.is_symlink():
        raise FakeExpertError(f"{code}:symlink_or_nul")
    return path.resolve()


def _scoped_workspace(path_value: str | Path, *, create: bool) -> Path:
    raw = Path(path_value).expanduser()
    if raw.is_symlink():
        raise FakeExpertError("workspace_symlink_forbidden")
    path = raw.resolve()
    if path in {Path("/"), Path.home().resolve(), PROJECT_ROOT.resolve()}:
        raise FakeExpertError("workspace_too_broad")
    if path.exists() and not path.is_dir():
        raise FakeExpertError("workspace_not_directory")
    if not path.exists():
        if not create:
            raise FakeExpertError("workspace_missing")
        path.mkdir(parents=True, exist_ok=False)
        os.chmod(path, 0o700)
    return path


def _walk_files(root: Path, *, max_depth: int) -> Iterable[Path]:
    root = root.resolve()
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        depth = len(current_path.relative_to(root).parts)
        if depth > max_depth:
            raise FakeExpertError("workspace_depth_limit")
        for directory in list(directories):
            candidate = current_path / directory
            if candidate.is_symlink():
                raise FakeExpertError("workspace_symlink_forbidden")
        for filename in filenames:
            candidate = current_path / filename
            if candidate.is_symlink():
                raise FakeExpertError("workspace_symlink_forbidden")
            yield candidate


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _hash_without(value: Mapping[str, Any], key: str) -> str:
    return _sha256_bytes(_canonical_bytes({k: v for k, v in value.items() if k != key}))


def _file_record(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": _sha256_path(path)}


def _tool_record(name: str) -> dict[str, str]:
    path = SCRIPT_ROOT / name
    return {"path": str(path.resolve()), "sha256": _sha256_file(path)}


def _action(code: str, meaning: str) -> dict[str, str]:
    return {"code": code, "meaning": meaning}


def _reject_unknown(record: Mapping[str, Any], allowed: set[str], code: str) -> None:
    unknown = set(record) - allowed
    if unknown:
        raise FakeExpertError(f"{code}:{sorted(unknown)[0]}")


def _validate_job_shape(job: Any) -> None:
    if not isinstance(job, dict):
        raise FakeExpertError("job_schema_invalid")
    if job.get("schema_version") != FAKE_EXPERT_JOB_SCHEMA:
        if job.get("schema_version") == "tkc.fake-expert-job/v0.1":
            raise FakeExpertError("job_schema_upgrade_required")
        raise FakeExpertError("job_schema_version_invalid")
    required = {
        "schema_version", "protocol", "compiler_version", "job_id", "created_at",
        "profile", "backend", "target_stage", "inputs", "package", "workspace", "gold",
        "required_reading", "state", "outputs", "tool_bindings", "runtime", "policy", "next_action",
        "job_sha256",
    }
    if set(job) != required:
        raise FakeExpertError("job_schema_invalid")
    if job.get("protocol") != FAKE_EXPERT_JOB_PROTOCOL:
        raise FakeExpertError("job_protocol_invalid")
    if job.get("compiler_version") != UX_COMPILER_VERSION:
        raise FakeExpertError("job_compiler_version_invalid")
    if not isinstance(job.get("job_id"), str) or not re.fullmatch(
        r"fxj-[0-9a-f]{20}", job["job_id"]
    ):
        raise FakeExpertError("job_id_invalid")
    if job.get("profile") not in PROFILES or job.get("target_stage") not in TARGET_STAGES:
        raise FakeExpertError("job_selection_invalid")
    if not isinstance(job.get("created_at"), str) or not job["created_at"].strip():
        raise FakeExpertError("job_created_at_invalid")

    backend = job.get("backend")
    if not isinstance(backend, dict) or set(backend) != {"name", "runtime_receipt"}:
        raise FakeExpertError("job_backend_invalid")
    backend_name = backend.get("name")
    if backend_name not in PROFILE_BACKENDS.get(str(job["profile"]), ()):
        raise FakeExpertError("job_backend_invalid")
    if job["profile"] == "native-text" and backend.get("runtime_receipt") is not None:
        raise FakeExpertError("job_backend_runtime_receipt_invalid")
    _validate_file_record(backend.get("runtime_receipt"), allow_none=True)

    inputs = job.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != {"source", "workpack"}:
        raise FakeExpertError("job_inputs_invalid")
    _validate_file_record(inputs["source"], allow_none=False)
    _validate_file_record(inputs["workpack"], allow_none=True)

    package = job.get("package")
    if not isinstance(package, dict) or set(package) != {
        "name", "display_name", "domain", "version"
    }:
        raise FakeExpertError("job_package_invalid")
    if (
        not isinstance(package["name"], str)
        or not NAME_RE.fullmatch(package["name"])
        or not isinstance(package["domain"], str)
        or not NAME_RE.fullmatch(package["domain"])
        or not isinstance(package["display_name"], str)
        or not package["display_name"].strip()
        or not isinstance(package["version"], str)
        or not VERSION_RE.fullmatch(package["version"])
    ):
        raise FakeExpertError("job_package_invalid")
    if not isinstance(job.get("workspace"), str) or not job["workspace"].strip():
        raise FakeExpertError("job_workspace_invalid")

    gold = job.get("gold")
    if not isinstance(gold, dict) or set(gold) != {"mode", "revision"}:
        raise FakeExpertError("job_gold_invalid")
    if gold.get("mode") not in GOLD_MODES:
        raise FakeExpertError("job_gold_mode_invalid")
    _validate_file_record(gold["revision"], allow_none=True)
    if gold["mode"] in {"reuse", "update"} and gold["revision"] is None:
        raise FakeExpertError("job_gold_revision_required")

    reading = job.get("required_reading")
    if not isinstance(reading, list) or not reading:
        raise FakeExpertError("job_required_reading_invalid")
    reading_paths: list[str] = []
    for record in reading:
        if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
            raise FakeExpertError("job_required_reading_invalid")
        relative = record.get("path")
        if not isinstance(relative, str) or not SHA256_RE.fullmatch(str(record.get("sha256"))):
            raise FakeExpertError("job_required_reading_invalid")
        _reference_path(relative)
        reading_paths.append(relative)
    expected_reading = _required_reading_paths(str(job["profile"]), str(job["target_stage"]), str(gold["mode"]))
    if reading_paths != expected_reading or len(reading_paths) != len(set(reading_paths)):
        raise FakeExpertError("job_required_reading_invalid")

    state = job.get("state")
    if not isinstance(state, dict) or not {"status", "stage", "updated_at"}.issubset(state):
        raise FakeExpertError("job_state_invalid")
    if set(state) - {"status", "stage", "updated_at", "code", "history"}:
        raise FakeExpertError("job_state_invalid")
    if state.get("status") not in {"planned", "paused", "completed", "rejected"}:
        raise FakeExpertError("job_state_status_invalid")
    if not isinstance(state.get("stage"), str) or not state["stage"]:
        raise FakeExpertError("job_state_stage_invalid")
    if not isinstance(state.get("updated_at"), str) or not state["updated_at"]:
        raise FakeExpertError("job_state_timestamp_invalid")
    if "history" in state and not isinstance(state["history"], list):
        raise FakeExpertError("job_state_history_invalid")

    outputs = job.get("outputs")
    if not isinstance(outputs, dict) or set(outputs) != {"skill", "job"}:
        raise FakeExpertError("job_outputs_invalid")
    if any(not isinstance(outputs[key], str) or not outputs[key] for key in outputs):
        raise FakeExpertError("job_outputs_invalid")

    tools = job.get("tool_bindings")
    if not isinstance(tools, dict) or set(tools) != {
        "cli", "preflight", "direct_reference", "pipeline"
    }:
        raise FakeExpertError("job_tool_bindings_invalid")
    for record in tools.values():
        _validate_file_record(record, allow_none=False)

    runtime = job.get("runtime")
    if not isinstance(runtime, dict) or set(runtime) != {
        "python", "pypdf", "network_enabled", "model_download", "ocr_invoked"
    } or runtime.get("pypdf") != "6.10.0" or any(
        runtime.get(key) is not False
        for key in ("network_enabled", "model_download", "ocr_invoked")
    ):
        raise FakeExpertError("job_runtime_policy_invalid")

    policy = job.get("policy")
    expected_policy = {
        "auto_install", "auto_download", "auto_backend_fallback", "review_authoring",
        "gold_authoring", "promotion", "execution",
    }
    if not isinstance(policy, dict) or set(policy) != expected_policy or any(
        value is not False for value in policy.values()
    ):
        raise FakeExpertError("job_policy_invalid")
    action = job.get("next_action")
    if not isinstance(action, dict) or set(action) != {"code", "meaning"} or any(
        not isinstance(value, str) or not value for value in action.values()
    ):
        raise FakeExpertError("job_next_action_invalid")
    if not isinstance(job.get("job_sha256"), str) or not SHA256_RE.fullmatch(job["job_sha256"]):
        raise FakeExpertError("job_hash_invalid")


def _validate_file_record(record: Any, *, allow_none: bool) -> None:
    if allow_none and record is None:
        return
    if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
        raise FakeExpertError("job_file_record_invalid")
    if not isinstance(record["path"], str) or not record["path"].strip() or not SHA256_RE.fullmatch(str(record["sha256"])):
        raise FakeExpertError("job_file_record_invalid")


def _load_job(path_value: str | Path) -> tuple[Path, dict[str, Any]]:
    path = _safe_path(path_value, "job_path_invalid")
    job = _read_json(path, "job_invalid")
    _validate_job_shape(job)
    if _hash_without(job, "job_sha256") != job["job_sha256"]:
        raise FakeExpertError("job_hash_mismatch")
    return path, job


def _validate_job_bindings(path: Path, job: Mapping[str, Any]) -> list[str]:
    drift: list[str] = []
    workspace = _safe_path(str(job["workspace"]), "workspace_invalid")
    if path != _safe_path(str(job["outputs"]["job"]), "job_output_path_invalid"):
        drift.append("job_path_binding_mismatch")
    if not _within(path, workspace):
        drift.append("job_outside_workspace")
    skill = _safe_path(str(job["outputs"]["skill"]), "skill_output_path_invalid")
    if not _within(skill, workspace):
        drift.append("skill_outside_workspace")
    source = _safe_path(str(job["inputs"]["source"]["path"]), "source_path_invalid")
    if not source.is_file() or source.is_symlink():
        drift.append("source_missing")
    elif _sha256_file(source) != job["inputs"]["source"]["sha256"]:
        drift.append("source_hash_mismatch")
    for label in ("workpack",):
        record = job["inputs"][label]
        if record is not None:
            candidate = _safe_path(str(record["path"]), f"{label}_path_invalid")
            if not candidate.exists() or candidate.is_symlink():
                drift.append(f"{label}_missing")
            else:
                try:
                    actual = _sha256_path(candidate)
                except FakeExpertError:
                    drift.append(f"{label}_path_invalid")
                else:
                    if actual != record["sha256"]:
                        drift.append(f"{label}_hash_mismatch")
    record = job["gold"]["revision"]
    if record is not None:
        candidate = _safe_path(str(record["path"]), "gold_revision_path_invalid")
        if not candidate.exists() or candidate.is_symlink():
            drift.append("gold_revision_missing")
        else:
            try:
                actual = _sha256_path(candidate)
            except FakeExpertError:
                drift.append("gold_revision_path_invalid")
            else:
                if actual != record["sha256"]:
                    drift.append("gold_revision_hash_mismatch")
    runtime_record = job["backend"]["runtime_receipt"]
    if runtime_record is not None:
        candidate = _safe_path(str(runtime_record["path"]), "runtime_receipt_path_invalid")
        if not candidate.is_file() or candidate.is_symlink():
            drift.append("runtime_receipt_missing")
        elif _sha256_file(candidate) != runtime_record["sha256"]:
            drift.append("runtime_receipt_hash_mismatch")
    for record in job["required_reading"]:
        relative = str(record["path"])
        try:
            candidate = _reference_path(relative)
        except FakeExpertError:
            drift.append(f"required_reference_path_invalid:{relative}")
            continue
        if not candidate.is_file():
            drift.append(f"required_reference_missing:{relative}")
        elif _sha256_file(candidate) != record["sha256"]:
            drift.append(f"required_reference_hash_mismatch:{relative}")
    current_tools = {
        "cli": _tool_record("fake_expert.py"),
        "preflight": _tool_record("environment_preflight.py"),
        "direct_reference": _tool_record("direct_reference_skill.py"),
        "pipeline": _tool_record("pipeline_orchestrator.py"),
    }
    for name, record in current_tools.items():
        if job["tool_bindings"].get(name) != record:
            drift.append(f"tool_hash_mismatch:{name}")
    try:
        current_pypdf = __import__("pypdf")
        if str(getattr(current_pypdf, "__version__", "unknown")) != str(job["runtime"]["pypdf"]):
            drift.append("runtime_pypdf_mismatch")
    except Exception:
        drift.append("runtime_pypdf_unavailable")
    current_python = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if current_python != str(job["runtime"]["python"]):
        drift.append("runtime_python_mismatch")
    return sorted(set(drift))


def _build_job(args: argparse.Namespace, workspace: Path, job_path: Path) -> dict[str, Any]:
    source = _safe_path(args.source, "source_path_invalid")
    if not source.is_file() or source.is_symlink():
        raise FakeExpertError("source_missing")
    if args.profile not in PROFILES:
        raise FakeExpertError("profile_invalid")
    if args.gold_mode not in GOLD_MODES:
        raise FakeExpertError("gold_mode_invalid")
    if args.gold_mode in {"reuse", "update"} and args.gold_revision is None:
        raise FakeExpertError("gold_revision_required")
    if args.gold_mode == "none" and args.gold_revision is not None:
        raise FakeExpertError("gold_revision_without_mode")
    if args.gold_mode == "update":
        raise FakeExpertError("gold_update_requires_existing_base")
    requested_backend = getattr(args, "backend", None)
    allowed_backends = PROFILE_BACKENDS[args.profile]
    if args.profile == "native-text":
        backend_name = "pypdf-native" if requested_backend is None else str(requested_backend)
        if backend_name != "pypdf-native":
            raise FakeExpertError("backend_invalid_for_native_text")
    else:
        if not isinstance(requested_backend, str) or not requested_backend:
            raise FakeExpertError("backend_required_for_profile")
        backend_name = requested_backend
        if backend_name not in allowed_backends:
            raise FakeExpertError("backend_invalid_for_profile")
    name = str(args.name)
    domain = str(args.domain)
    if not NAME_RE.fullmatch(name) or not NAME_RE.fullmatch(domain):
        raise FakeExpertError("package_name_or_domain_invalid")
    display_name = str(args.display_name)
    version = str(args.version)
    if not display_name.strip() or not VERSION_RE.fullmatch(version):
        raise FakeExpertError("package_metadata_invalid")
    workpack_record = None
    if args.workpack is not None:
        workpack = _safe_path(args.workpack, "workpack_path_invalid")
        if not workpack.exists() or workpack.is_symlink():
            raise FakeExpertError("workpack_missing")
        workpack_record = _file_record(workpack)
    gold_record = None
    if args.gold_revision is not None:
        revision = _safe_path(args.gold_revision, "gold_revision_path_invalid")
        if not revision.exists() or revision.is_symlink():
            raise FakeExpertError("gold_revision_missing")
        gold_record = _file_record(revision)
    runtime_record = None
    runtime_receipt_value = getattr(args, "runtime_receipt", None)
    if runtime_receipt_value is not None:
        if args.profile == "native-text":
            raise FakeExpertError("runtime_receipt_not_applicable")
        runtime_receipt = _safe_path(runtime_receipt_value, "runtime_receipt_path_invalid")
        if not runtime_receipt.is_file() or runtime_receipt.is_symlink():
            raise FakeExpertError("runtime_receipt_missing")
        runtime_record = _file_record(runtime_receipt)
    skill = (workspace / f"fake-{name}").resolve()
    if not _within(skill, workspace):
        raise FakeExpertError("skill_outside_workspace")
    source_record = _file_record(source)
    job_id = "fxj-" + _sha256_bytes(
        _canonical_bytes(
            {
                "source": source_record,
                "workspace": str(workspace),
                "profile": args.profile,
                "package": {"name": name, "domain": domain, "version": version},
                "created_at": args.created_at,
            }
        )
    )[:20]
    job: dict[str, Any] = {
        "schema_version": FAKE_EXPERT_JOB_SCHEMA,
        "protocol": FAKE_EXPERT_JOB_PROTOCOL,
        "compiler_version": UX_COMPILER_VERSION,
        "job_id": job_id,
        "created_at": args.created_at,
        "profile": args.profile,
        "backend": {"name": backend_name, "runtime_receipt": runtime_record},
        "target_stage": args.target_stage,
        "inputs": {"source": source_record, "workpack": workpack_record},
        "package": {
            "name": name,
            "display_name": display_name,
            "domain": domain,
            "version": version,
        },
        "workspace": str(workspace),
        "gold": {"mode": args.gold_mode, "revision": gold_record},
        "required_reading": _required_reading_records(args.profile, args.target_stage, args.gold_mode),
        "state": {
            "status": "planned",
            "stage": "plan",
            "updated_at": args.created_at,
            "code": "plan_created",
            "history": [],
        },
        "outputs": {"skill": str(skill), "job": str(job_path.resolve())},
        "tool_bindings": {
            "cli": _tool_record("fake_expert.py"),
            "preflight": _tool_record("environment_preflight.py"),
            "direct_reference": _tool_record("direct_reference_skill.py"),
            "pipeline": _tool_record("pipeline_orchestrator.py"),
        },
        "runtime": {
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "pypdf": "6.10.0",
            "network_enabled": False,
            "model_download": False,
            "ocr_invoked": False,
        },
        "policy": {
            "auto_install": False,
            "auto_download": False,
            "auto_backend_fallback": False,
            "review_authoring": False,
            "gold_authoring": False,
            "promotion": False,
            "execution": False,
        },
        "next_action": _action("run_compile", "Run compile to evaluate the frozen inputs and stop at the first explicit gate."),
        "job_sha256": "",
    }
    job["job_sha256"] = _hash_without(job, "job_sha256")
    _validate_job_shape(job)
    return job


def plan_job(args: argparse.Namespace) -> dict[str, Any]:
    document_contract = _document_contract_report()
    if not document_contract["passed"]:
        raise FakeExpertError("document_contract_invalid")
    workspace = _scoped_workspace(args.workspace, create=True)
    job_path = _safe_path(args.job or (workspace / JOB_FILE_NAME), "job_path_invalid")
    if not _within(job_path, workspace):
        raise FakeExpertError("job_outside_workspace")
    if job_path.exists() or job_path.is_symlink():
        raise FakeExpertError("job_exists")
    job = _build_job(args, workspace, job_path)
    source_profile_gate = _source_profile_gate(job)
    if source_profile_gate:
        job["state"].update({
            "status": source_profile_gate["status"],
            "stage": source_profile_gate["code"],
            "code": source_profile_gate["code"],
            "updated_at": job["created_at"],
        })
        job["next_action"] = source_profile_gate["next_action"]
        job["job_sha256"] = _hash_without(job, "job_sha256")
        _validate_job_shape(job)
    _write_json(job_path, job, overwrite=False)
    result = {
        "status": source_profile_gate["status"] if source_profile_gate else "planned",
        "job": str(job_path),
        "job_id": job["job_id"],
        "job_sha256": job["job_sha256"],
        "profile": job["profile"],
        "backend": job["backend"],
        "target_stage": job["target_stage"],
        "source_sha256": job["inputs"]["source"]["sha256"],
        "next_action": job["next_action"],
        "policy": job["policy"],
        "required_reading": job["required_reading"],
        "release_version": RELEASE_VERSION,
        "compiler_version": UX_COMPILER_VERSION,
    }
    if source_profile_gate:
        result.update({
            "code": source_profile_gate["code"],
            "required_gates": source_profile_gate["required_gates"],
        })
    return result


def _workpack_summary(path: Path) -> dict[str, Any]:
    if not path.is_dir() or path.is_symlink():
        return {"kind": "missing", "complete": False}
    manifest_path = path / "workpack.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return {"kind": "unknown", "complete": False}
    manifest = _read_json(manifest_path, "workpack_manifest_invalid")
    if not isinstance(manifest, dict):
        return {"kind": "unknown", "complete": False}
    units = manifest.get("units")
    drafts = path / str(manifest.get("drafts_directory", "drafts"))
    complete = isinstance(units, list) and bool(units) and drafts.is_dir()
    required = [
        "semantic-assertions.jsonl", "support-matrix.jsonl", "review-plan.json",
        "review-queue.jsonl",
    ]
    files_present = {name: (path / name).is_file() for name in required}
    return {
        "kind": "semantic",
        "complete": complete,
        "unit_count": len(units) if isinstance(units, list) else 0,
        "draft_count": len([p for p in drafts.iterdir() if p.is_file()]) if drafts.is_dir() else 0,
        "required_files": files_present,
        "manifest_source_sha256": (manifest.get("source") or {}).get("sha256"),
    }


def _source_profile_gate(job: Mapping[str, Any]) -> dict[str, Any] | None:
    """Re-run the canonical native source check before any native build route."""

    if job.get("profile") != "native-text":
        return None
    try:
        source_record = job["inputs"]["source"]
        source = _safe_path(str(source_record["path"]), "source_path_invalid")
        current_pypdf = __import__("pypdf")
        if str(getattr(current_pypdf, "__version__", "unknown")) != str(job["runtime"]["pypdf"]):
            reason = "pypdf_runtime_mismatch"
        else:
            import environment_preflight

            report = environment_preflight._check_source(
                str(source), NATIVE_TEXT_MINIMUM_CHARACTERS
            )
            reason = str(report.get("code") or "source_preflight_error")
            if report.get("passed") is True and reason == "source_native_text_ok":
                return None
    except Exception:
        reason = "source_preflight_error"
    return _paused(
        "source_profile_mismatch",
        "The native-text profile requires a valid, parseable PDF with sufficient native text; "
        f"the canonical source check reported {reason}. Choose an explicit scan profile for image-only or low-text input.",
        ["source_profile_mismatch", "select_explicit_scan_profile"],
    )


def _runtime_receipt_present(root: Path) -> bool:
    """Return only a non-empty local receipt hint; it is not qualification."""

    names = {
        "qualification-receipt.json", "runtime-qualification.json",
        "paddle-runtime-qualification.json",
    }
    return any(
        (root / name).is_file()
        and not (root / name).is_symlink()
        and (root / name).stat().st_size > 0
        for name in names
    )


def _scan_structure_review_present(root: Path) -> bool:
    for name in (
        "external-scan-structure-review-fragment.json",
        "scan-structure-review-fragment.json",
        "structure-review.json",
    ):
        if (root / name).is_file() and not (root / name).is_symlink() and (root / name).stat().st_size > 0:
            return True
    return False


def _runtime_qualification_gate(job: Mapping[str, Any]) -> dict[str, Any] | None:
    """Validate only an already supplied receipt; never start a runtime."""

    if job["profile"] not in {"scan-text", "scan-structure", "visual"}:
        return None
    backend = job["backend"]["name"]
    record = job["backend"]["runtime_receipt"]
    if record is None:
        return _paused(
            "runtime_qualification_required",
            "The selected backend needs an existing, explicitly qualified local runtime receipt; no runtime is started automatically.",
            ["runtime_qualification_required"],
        )
    if backend not in AUTHORITATIVE_RUNTIME_BACKENDS:
        return _paused(
            "runtime_qualification_validator_required",
            "This backend has no generic qualification shortcut here; validate it with its existing specialist route before resume.",
            ["runtime_qualification_validator_required"],
        )
    try:
        import paddle_runtime_qualification

        receipt_path = _safe_path(record["path"], "runtime_receipt_path_invalid")
        receipt = _read_json(receipt_path, "runtime_receipt_invalid")
        paddle_runtime_qualification.validate_runtime_qualification(
            receipt,
            backend_id=backend,
            require_qualified=True,
        )
    except Exception:
        return _paused(
            "runtime_qualification_unverified",
            "The supplied runtime receipt did not pass the existing specialist validator; keep this route paused and repair or requalify externally.",
            ["runtime_qualification_unverified"],
        )
    return None


def _accepted_scan_structure_revision(workpack: Path, source_sha256: str) -> bool:
    """Check the narrow, source-bound output of the existing finalize route."""

    revision_path = workpack / "revision.json"
    fragment_path = workpack / "external-scan-structure-review-fragment.json"
    if any(
        path.is_symlink() or not path.is_file() or path.stat().st_size == 0
        for path in (revision_path, fragment_path)
    ):
        return False
    try:
        revision = _read_json(revision_path, "structure_review_revision_invalid")
        fragment = _read_json(fragment_path, "structure_review_fragment_invalid")
        if (
            revision.get("schema_version") != "tkc.scan-structure-review-revision/v0.1"
            or revision.get("status") != "accepted-external-fragment"
            or revision.get("fragment_count") != 1
            or revision.get("fragment_sha256") != _sha256_file(fragment_path)
            or revision.get("revision_sha256") != _hash_without(revision, "revision_sha256")
            or revision.get("candidate_only") is not True
            or revision.get("promotion_allowed") is not False
            or revision.get("release_included") is not False
            or revision.get("mvp_acceptance") is not False
            or fragment.get("schema_version") != "tkc.scanned-pdf-structure-review-fragment/v0.1"
            or fragment.get("provenance") != "external-review-fragment"
        ):
            return False
        plan = fragment.get("review_plan")
        attestation = fragment.get("attestation")
        if not isinstance(plan, Mapping) or plan.get("source_sha256") != source_sha256:
            return False
        if not isinstance(attestation, Mapping):
            return False
        if (
            attestation.get("proposer_instance") != plan.get("proposer_instances", [None])[0]
            or attestation.get("reviewer_instance") != plan.get("reviewer_instances", [None])[0]
            or attestation.get("proposer_instance") == attestation.get("reviewer_instance")
            or attestation.get("source_render_inspected") is not True
            or attestation.get("title_page_order_bbox_evidence_checked") is not True
            or attestation.get("differences_recorded") is not True
            or attestation.get("independent_from_proposer") is not True
            or attestation.get("provided_outside_workbench") is not True
            or attestation.get("compiler_authored") is not False
        ):
            return False
        segments = fragment.get("segments")
        return isinstance(segments, list) and bool(segments) and all(
            isinstance(segment, Mapping)
            and isinstance(segment.get("evidence"), Mapping)
            and segment["evidence"].get("source_sha256") == source_sha256
            for segment in segments
        )
    except Exception:
        return False


def _scan_structure_review_gate(workpack: Path | None, source_sha256: str) -> dict[str, Any]:
    """Keep arbitrary filename hints from satisfying the structure gate."""

    if workpack is None:
        return _paused(
            "structure_review_required",
            "A bounded external scan-structure review is required before scanned evidence can become a locator.",
            ["structure_review_required", "independent_review_required"],
        )
    if not _scan_structure_review_present(workpack):
        return _paused(
            "structure_review_required",
            "A bounded external scan-structure review is required before scanned evidence can become a locator.",
            ["structure_review_required", "independent_review_required"],
        )
    if not _accepted_scan_structure_revision(workpack, source_sha256):
        return _paused(
            "structure_review_validator_required",
            "A structure-review filename is only a hint; run the existing specialist workbench validator and bind its accepted external fragment before resume.",
            ["structure_review_validator_required", "independent_review_required"],
        )
    return _paused(
        "independent_review_required",
        "The accepted scan structure fragment remains subject to the existing independent semantic/support gates.",
        ["independent_review_required", "competency_required", "seal_required"],
    )


def _validate_direct_output(output: Path) -> list[str]:
    try:
        import direct_reference_skill

        errors = direct_reference_skill.validate_direct_reference(output)
    except Exception:
        return ["direct_reference_validator_error"]
    return [str(error) for error in errors]


def _evaluate_job(job_path: Path, job: Mapping[str, Any]) -> dict[str, Any]:
    drift = _validate_job_bindings(job_path, job)
    if drift:
        return {
            "status": "rejected",
            "code": "job_drift",
            "drift": drift,
            "job_id": job["job_id"],
            "next_action": _action("repair_or_replan", "Do not edit a hash-bound job in place; preserve it and create a fresh plan after repairing the input.") ,
            "required_gates": [],
        }
    source_profile_gate = _source_profile_gate(job)
    if source_profile_gate:
        source_profile_gate["job_id"] = job["job_id"]
        return source_profile_gate
    profile = str(job["profile"])
    workpack_record = job["inputs"]["workpack"]
    workpack = _safe_path(workpack_record["path"], "workpack_path_invalid") if workpack_record else None
    gold = job["gold"]
    if gold["mode"] == "update":
        return _paused(
            "gold_update_requires_existing_base",
            "Initial unified jobs cannot perform Gold update; use the existing bind-gold/update route against a validated base package.",
            ["gold_update_requires_existing_base"],
        )
    if gold["mode"] == "reuse" and gold["revision"] is None:
        return _paused(
            "gold_review_required",
            "An existing external Gold revision is required for reuse; the compiler cannot create Gold.",
            ["gold_review_required"],
        )
    runtime_gate = _runtime_qualification_gate(job)
    if profile == "native-text":
        if workpack is None:
            return _paused("semantic_authoring_required", "A complete semantic proposal workpack must be supplied; the compiler cannot author missing semantics.", ["semantic_authoring_required", "independent_review_required"])
        summary = _workpack_summary(workpack)
        if summary["kind"] != "semantic":
            return _paused("workpack_format_unrecognized", "The supplied workpack is not a recognized semantic workpack.", ["workpack_format_unrecognized"])
        if not summary["complete"]:
            return _paused("semantic_authoring_required", "The semantic proposal workpack is incomplete; finish it through the existing workpack route.", ["semantic_authoring_required", "independent_review_required"])
        if job["target_stage"] != "draft":
            return _paused("independent_review_required", "Independent semantic and visual review remains a separate gate; resume cannot promote a Draft.", ["independent_review_required", "competency_required", "seal_required"])
        output = _safe_path(job["outputs"]["skill"], "skill_output_path_invalid")
        if output.exists():
            errors = _validate_direct_output(output)
            if errors:
                return {
                    "status": "rejected",
                    "code": "draft_output_invalid",
                    "job_id": job["job_id"],
                    "issues": errors,
                    "next_action": _action("replan_fresh_output", "The existing output failed the authoritative direct-reference validator; preserve it and create a fresh plan/output path."),
                    "required_gates": ["direct_reference_validation_required"],
                }
            return {"status": "completed", "code": "draft_output_present", "job_id": job["job_id"], "next_action": _action("validate_draft", "The existing direct Reference Draft passed its authoritative validator; independent Review is still required for any higher lifecycle stage."), "required_gates": []}
        return {"status": "ready-to-compile", "code": "direct_reference_build_ready", "job_id": job["job_id"], "next_action": _action("build_direct_reference_draft", "Run the existing deterministic direct-reference builder; it remains draft-only."), "required_gates": ["independent_review_required"], "planned_commands": ["direct_reference_skill.py build"]}
    if runtime_gate:
        return runtime_gate
    if profile in {"scan-text", "scan-structure"}:
        return _scan_structure_review_gate(workpack, str(job["inputs"]["source"]["sha256"]))
    return _paused("visual_review_required", "Visual candidates require the existing human visual workbench and independent review; no visual conclusion is authored here.", ["visual_review_required", "independent_review_required"])


def _paused(code: str, meaning: str, gates: list[str]) -> dict[str, Any]:
    return {
        "status": "paused",
        "code": code,
        "next_action": _action(code, meaning),
        "required_gates": gates,
    }


def _emit_progress(job: Mapping[str, Any], result: Mapping[str, Any], *, event: str) -> None:
    payload = {
        "schema_version": "tkc.fake-expert-progress/v0.1",
        "protocol": UX_CLI_PROTOCOL,
        "event": event,
        "job_id": job.get("job_id"),
        "status": result.get("status"),
        "code": result.get("code"),
        "next_action": (result.get("next_action") or {}).get("code"),
        "required_gate_count": len(result.get("required_gates", [])),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)


def _run_job(args: argparse.Namespace, *, resume: bool) -> dict[str, Any]:
    job_path, job = _load_job(args.job)
    result = _evaluate_job(job_path, job)
    result.update({
        "job": str(job_path),
        "job_sha256": job["job_sha256"],
        "required_reading": job["required_reading"],
        "dry_run": bool(args.dry_run),
    })
    if args.progress:
        _emit_progress(job, result, event="resume-evaluated" if resume else "compile-evaluated")
    if args.dry_run:
        result["would_write"] = []
        if result.get("code") == "direct_reference_build_ready":
            result["would_write"] = [str(_safe_path(job["outputs"]["skill"], "skill_output_path_invalid"))]
        return result
    if result.get("status") == "ready-to-compile" and result.get("code") == "direct_reference_build_ready":
        try:
            import direct_reference_skill

            direct_result = direct_reference_skill.build_direct_reference(
                _safe_path(job["inputs"]["source"]["path"]),
                _safe_path(job["inputs"]["workpack"]["path"]),
                _safe_path(job["outputs"]["skill"]),
                name=str(job["package"]["name"]),
                display_name=str(job["package"]["display_name"]),
                domain=str(job["package"]["domain"]),
                version=str(job["package"]["version"]),
                created_at=str(job["created_at"]),
                gold_mode=str(job["gold"]["mode"]),
                gold_revision=(
                    _safe_path(job["gold"]["revision"]["path"], "gold_revision_path_invalid")
                    if job["gold"]["revision"] is not None
                    else None
                ),
            )
        except Exception as error:
            raise FakeExpertError(f"direct_reference_build_rejected:{error}") from error
        result["status"] = "completed"
        result["code"] = "draft_compiled"
        result["direct_reference"] = {
            "status": direct_result.get("status"),
            "artifact_stage": direct_result.get("artifact_stage"),
            "object_count": direct_result.get("object_count"),
            "anchor_count": direct_result.get("anchor_count"),
        }
        result["next_action"] = _action("validate_draft", "Validate the direct Reference Draft; independent Review is still required for any higher lifecycle stage.")
    elif result.get("status") in {"paused", "ready-to-compile"}:
        result["status"] = "paused"
    elif result.get("status") == "rejected":
        return result
    updated_job = _update_job(job_path, job, result)
    result["job_sha256"] = updated_job["job_sha256"]
    result["job_file_sha256"] = _sha256_file(job_path)
    return result


def _update_job(path: Path, job: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    updated = json.loads(json.dumps(job))
    state = updated["state"]
    now = _now()
    state["status"] = str(result.get("status"))
    state["stage"] = str(result.get("code"))
    state["updated_at"] = now
    state["code"] = str(result.get("code"))
    history = state.setdefault("history", [])
    history.append({"at": now, "status": result.get("status"), "code": result.get("code")})
    updated["next_action"] = result["next_action"]
    updated["job_sha256"] = _hash_without(updated, "job_sha256")
    _write_json(path, updated, overwrite=True)
    return updated


def doctor(args: argparse.Namespace) -> dict[str, Any]:
    import environment_preflight

    report = environment_preflight.build_report(
        renderer=args.pdftoppm,
        source=str(args.source) if args.source else None,
        minimum_native_characters=args.minimum_native_characters,
        profile=args.profile,
        backend=args.backend,
    )
    document_contract = _document_contract_report()
    report["checks"].append(document_contract)
    report["passed"] = bool(report["passed"] and document_contract["passed"])
    report["profile_ready"] = bool(report.get("profile_ready") and document_contract["passed"])
    if not document_contract["passed"]:
        report.setdefault("blocking", []).append("document_contract_invalid")
    report["protocol"] = UX_CLI_PROTOCOL
    report["compiler_version"] = UX_COMPILER_VERSION
    report["required_reading"] = _required_reading_records(args.profile, "draft", "none")
    report["next_action"] = "continue" if report["passed"] else "repair_required_environment_checks"
    return report


def _manifest_candidate(root: Path) -> tuple[str, Path, dict[str, Any]]:
    root = _safe_path(root, "workbench_path_invalid")
    if not root.is_dir() or root.is_symlink():
        raise FakeExpertError("workbench_missing")
    candidates = [
        ("semantic", root / "manifest.json"),
        ("visual", root / "workbench-manifest.json"),
        ("scan-structure", root / "workbench.json"),
        ("scan-gold", root / "scan-gold-manifest.json"),
    ]
    for kind, manifest_path in candidates:
        if not manifest_path.is_file() or manifest_path.is_symlink():
            continue
        value = _read_json(manifest_path, "workbench_manifest_invalid")
        if not isinstance(value, dict):
            continue
        schema = value.get("schema_version")
        if kind == "semantic" and schema == "tkc.semantic-review-workbench/v0.1":
            return kind, manifest_path, value
        if kind == "visual" and isinstance(schema, str) and schema.startswith("tkc.visual-review-workbench-manifest/"):
            return kind, manifest_path, value
        if kind == "scan-structure" and schema == "tkc.scan-structure-review-workbench/v0.1":
            return kind, manifest_path, value
        if kind == "scan-gold" and isinstance(schema, str) and schema.startswith("tkc.scan-gold-review-workbench-manifest/"):
            return kind, manifest_path, value
    raise FakeExpertError("workbench_schema_unrecognized")


def _workbench_entry(kind: str, manifest_path: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    root = manifest_path.parent
    if kind == "scan-structure":
        item_count = len(manifest.get("items", [])) if isinstance(manifest.get("items"), list) else 0
        identifier = manifest.get("workbench_id")
    elif kind == "scan-gold":
        data_path = root / str(manifest.get("data_file", ""))
        data = _read_json(data_path, "scan_gold_data_invalid") if data_path.is_file() else {}
        counts = data.get("item_counts", {}) if isinstance(data, dict) else {}
        item_count = sum(value for value in counts.values() if isinstance(value, int))
        identifier = manifest.get("workbench_instance_id")
    else:
        item_count = manifest.get("item_count")
        identifier = manifest.get("workbench_id") or manifest.get("workbench_instance_id")
    return {
        "kind": kind,
        "path": str(root.resolve()),
        "manifest": str(manifest_path.resolve()),
        "schema_version": manifest.get("schema_version"),
        "protocol": manifest.get("protocol"),
        "compiler_version": manifest.get("compiler_version"),
        "id": identifier,
        "item_count": item_count,
    }


def _validate_workbench_layout(kind: str, root: Path, manifest_path: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    for _ in _walk_files(root, max_depth=16):
        pass
    if kind in {"visual", "scan-structure", "scan-gold"} and manifest.get("manifest_sha256") != _hash_without(manifest, "manifest_sha256"):
        raise FakeExpertError("workbench_manifest_hash_mismatch")
    if kind != "semantic" and "release_included" in manifest and manifest.get("release_included") is not False:
        raise FakeExpertError("workbench_release_policy_invalid")
    if kind == "semantic":
        data_path = root / "data.json"
        data = _read_json(data_path, "semantic_workbench_data_invalid")
        if not isinstance(data, dict) or manifest.get("data", {}).get("sha256") != _sha256_bytes(_canonical_bytes(data)):
            raise FakeExpertError("semantic_workbench_data_hash_mismatch")
        if any(manifest.get(key) is not False for key in ("predictions_included", "hidden_answers_included", "network_required", "release_included")):
            raise FakeExpertError("semantic_workbench_policy_invalid")
        if not (root / "review.html").is_file():
            raise FakeExpertError("workbench_review_html_missing")
    elif kind == "visual":
        data_path = root / "data" / "reviewer-workpack.json"
        data = _read_json(data_path, "visual_workbench_data_invalid")
        if _sha256_file(data_path) != manifest.get("data_file_sha256") or not isinstance(data, dict):
            raise FakeExpertError("visual_workbench_data_binding_invalid")
        policy = manifest.get("ui_policy") if isinstance(manifest.get("ui_policy"), Mapping) else {}
        if any(policy.get(key) is not False for key in ("network_enabled", "external_resources", "prediction_or_model_fields_present")):
            raise FakeExpertError("visual_workbench_policy_invalid")
    elif kind == "scan-structure":
        if not (root / "review.html").is_file() or not (root / "structure-proposal.json").is_file():
            raise FakeExpertError("scan_structure_workbench_layout_invalid")
    else:
        # The scan-Gold module owns the detailed profile/data contract.  This
        # import is lazy so list/plan/doctor never load its heavy dependencies.
        try:
            import scan_gold_review

            scan_gold_review._load_scan_gold_workbench(root)
        except FakeExpertError:
            raise
        except Exception as error:
            raise FakeExpertError(f"scan_gold_workbench_invalid:{error}") from error
    return _workbench_entry(kind, manifest_path, manifest)


def review_list(args: argparse.Namespace) -> dict[str, Any]:
    workspace = _scoped_workspace(args.workspace, create=False)
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    seen: set[Path] = set()
    for file_path in _walk_files(workspace, max_depth=8):
        if file_path.name not in {"manifest.json", "workbench-manifest.json", "workbench.json", "scan-gold-manifest.json"}:
            continue
        try:
            kind, manifest_path, manifest = _manifest_candidate(file_path.parent)
        except FakeExpertError:
            continue
        if manifest_path in seen:
            continue
        seen.add(manifest_path)
        try:
            entry = _workbench_entry(kind, manifest_path, manifest)
        except FakeExpertError as error:
            errors.append({"path": str(manifest_path), "code": str(error)})
            continue
        entries.append(entry)
    entries.sort(key=lambda row: (row["kind"], row["path"]))
    return {"status": "ok" if not errors else "partial", "workspace": str(workspace), "workbenches": entries, "errors": errors, "count": len(entries)}


def review_locate(args: argparse.Namespace) -> dict[str, Any]:
    kind, manifest_path, manifest = _manifest_candidate(args.workbench)
    root = manifest_path.parent
    _validate_workbench_layout(kind, root, manifest_path, manifest)
    html = root / "review.html"
    if html.is_symlink() or not html.is_file():
        raise FakeExpertError("workbench_review_html_missing")
    instruction = None
    declared = manifest.get("instructions", {})
    if isinstance(declared, Mapping) and isinstance(declared.get("path"), str):
        candidate = root / declared["path"]
        if candidate.is_file() and not candidate.is_symlink():
            instruction = candidate
    if instruction is None:
        for candidate in sorted(root.glob("*.md")):
            if not candidate.is_symlink():
                instruction = candidate
                break
    return {
        "status": "ok",
        "kind": kind,
        "workbench": str(root),
        "manifest": str(manifest_path),
        "review_html": str(html.resolve()),
        "instructions": str(instruction.resolve()) if instruction else None,
        "steps": ["Open review_html locally.", "Inspect only the bound source evidence shown by this workbench.", "Export the reviewer submission and validate it with this command."],
        "writes": False,
    }


def review_status(args: argparse.Namespace) -> dict[str, Any]:
    kind, manifest_path, manifest = _manifest_candidate(args.workbench)
    root = manifest_path.parent
    entry = _validate_workbench_layout(kind, root, manifest_path, manifest)
    submission_names = {
        "semantic": "review-submission.json",
        "visual": "review-submission.json",
        "scan-structure": "scan-structure-review-submission.json",
        "scan-gold": "scan-gold-review-submission.json",
    }
    submission = root / submission_names[kind]
    workflow_status = manifest.get("status") or "awaiting-external-review"
    return {
        "status": "ok",
        **entry,
        "workflow_status": workflow_status,
        "transport_status": "verified",
        "submission_present": submission.is_file() and not submission.is_symlink(),
        "next_action": "validate_submission_when_exported" if not submission.exists() else "run_validate_submission",
        "gold_created": False,
        "attestation_created": False,
        "writes": False,
    }


def _validate_semantic_submission(root: Path, submission_path: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    try:
        import semantic_review_workbench
    except Exception as error:
        raise FakeExpertError("semantic_validator_unavailable") from error
    value = _read_json(submission_path, "semantic_submission_invalid")
    if not isinstance(value, dict):
        raise FakeExpertError("semantic_submission_invalid")
    forbidden = _forbidden_submission_key(value, ignore_declarations=True)
    if forbidden is not None:
        raise FakeExpertError(f"semantic_submission_hidden_field:{forbidden}")
    required = {"schema_version", "workbench_id", "reviewer_instance", "review_plan_sha256", "exported_at", "declarations", "items"}
    if set(value) != required or value.get("schema_version") != "tkc.semantic-review-workbench-submission/v0.1":
        raise FakeExpertError("semantic_submission_shape_invalid")
    for key in ("workbench_id", "reviewer_instance", "review_plan_sha256"):
        if value.get(key) != manifest.get(key):
            raise FakeExpertError(f"semantic_submission_binding_invalid:{key}")
    declarations = value.get("declarations")
    expected_declarations = set(semantic_review_workbench.DECLARATIONS)
    if not isinstance(declarations, dict) or set(declarations) != expected_declarations or any(value is not True for value in declarations.values()):
        raise FakeExpertError("semantic_submission_declarations_invalid")
    data = _read_json(root / "data.json", "semantic_workbench_data_invalid")
    expected_refs = list(manifest.get("assigned_item_refs", []))
    queue_by_ref = {
        str(item.get("queue", {}).get("item_ref")): item.get("queue", {})
        for item in data.get("items", []) if isinstance(item, Mapping)
    }
    rows = value.get("items")
    if not isinstance(rows, list) or len(rows) != len(expected_refs):
        raise FakeExpertError("semantic_submission_item_coverage_invalid")
    seen: set[str] = set()
    issues: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("item_ref"), str):
            raise FakeExpertError("semantic_submission_item_invalid")
        ref = row["item_ref"]
        if ref in seen or ref not in expected_refs:
            raise FakeExpertError("semantic_submission_item_set_invalid")
        seen.add(ref)
        if _forbidden_submission_key(row) is not None:
            raise FakeExpertError(f"semantic_submission_item_hidden_field:{ref}")
        queue = queue_by_ref.get(ref, {})
        if row.get("review_input_sha256") != queue.get("review_input_sha256") or row.get("assertion_bundle_sha256") != queue.get("assertion_bundle_sha256"):
            raise FakeExpertError("semantic_submission_item_binding_invalid")
        verdict = row.get("verdict")
        support = row.get("evidence_support")
        issue_codes = row.get("issue_codes")
        confidence = row.get("confidence")
        review_checks = row.get("review_checks")
        differences = row.get("differences")
        if verdict not in {"accepted", "rejected", "quarantined"}:
            issues.append(f"review_verdict_invalid:{ref}")
        if support not in {"full", "partial", "none", "contradicted", "not-applicable"}:
            issues.append(f"review_support_invalid:{ref}")
        if not isinstance(issue_codes, list) or len(issue_codes) != len(set(issue_codes)) or any(
            not isinstance(code, str) or not semantic_review_workbench.ISSUE_CODE_RE.fullmatch(code)
            for code in (issue_codes if isinstance(issue_codes, list) else [])
        ):
            issues.append(f"review_issue_codes_invalid:{ref}")
        if not isinstance(row.get("rationale"), str) or len(row.get("rationale", "").strip()) < 12:
            issues.append(f"review_rationale_incomplete:{ref}")
        if not isinstance(confidence, dict) or set(confidence) != {"extraction", "interpretation"}:
            issues.append(f"review_confidence_invalid:{ref}")
        else:
            for confidence_name in ("extraction", "interpretation"):
                try:
                    confidence_value = float(confidence[confidence_name])
                except (TypeError, ValueError):
                    confidence_value = -1.0
                if not 0 <= confidence_value <= 1:
                    issues.append(f"review_confidence_invalid:{ref}")
        required_checks = set(semantic_review_workbench.REVIEW_CHECKS)
        if queue.get("requires_formula_check") is True:
            required_checks.add("formula_checked")
        if not isinstance(review_checks, dict) or any(review_checks.get(name) is not True for name in required_checks):
            issues.append(f"review_checks_incomplete:{ref}")
        assertion_ids = list(queue.get("assertion_ids", []))
        difference_by_id = {
            difference.get("assertion_id"): difference
            for difference in differences
            if isinstance(difference, dict) and isinstance(difference.get("assertion_id"), str)
        } if isinstance(differences, list) else {}
        if (
            not isinstance(differences, list)
            or len(difference_by_id) != len(differences)
            or set(difference_by_id) != set(assertion_ids)
            or any(
                difference.get("status") not in {"confirmed", "correction-required", "unsupported"}
                or not isinstance(difference.get("observation"), str)
                or len(difference.get("observation", "").strip()) < 12
                for difference in (differences if isinstance(differences, list) else [])
            )
        ):
            issues.append(f"review_differences_invalid:{ref}")
        if verdict == "accepted" and (
            support != "full"
            or issue_codes
            or not isinstance(differences, list)
            or any(difference_by_id.get(assertion_id, {}).get("status") != "confirmed" for assertion_id in assertion_ids)
        ):
            issues.append(f"review_acceptance_inconsistent:{ref}")
        elif verdict in {"rejected", "quarantined"} and not issue_codes:
            issues.append(f"review_issue_codes_required:{ref}")
        if queue.get("item_kind") == "gap" and not isinstance(row.get("gap_resolution"), dict):
            issues.append(f"review_gap_resolution_invalid:{ref}")
    if seen != set(expected_refs):
        raise FakeExpertError("semantic_submission_item_coverage_invalid")
    return {"valid": True, "accepted": not issues, "issue_count": len(issues), "issues": sorted(set(issues))}


def _validate_visual_submission(root: Path, submission_path: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    try:
        import reviewer_workbench
    except Exception as error:
        raise FakeExpertError("visual_validator_unavailable") from error
    value = _read_json(submission_path, "visual_submission_invalid")
    if not isinstance(value, dict):
        raise FakeExpertError("visual_submission_schema_invalid")
    if set(value) != {
        "schema_version", "protocol", "workbench_instance_id", "reviewer_instance",
        "review_session_id", "workpack_identity", "declarations", "items", "exported_at",
    } or value.get("schema_version") != "tkc.visual-review-workbench-submission/v0.1" or value.get("protocol") != "visual-review-workbench-v0.1":
        raise FakeExpertError("visual_submission_schema_invalid")
    if "split" in value:
        raise FakeExpertError("visual_submission_hidden_answer_or_split_field")
    forbidden = reviewer_workbench._forbidden_key_present(value, ignore_declarations=True)
    if forbidden is not None:
        raise FakeExpertError(f"visual_submission_hidden_field:{forbidden}")
    if not isinstance(value.get("exported_at"), str) or not value["exported_at"].strip():
        raise FakeExpertError("visual_submission_exported_at_invalid")
    declarations = value.get("declarations")
    if (
        not isinstance(declarations, Mapping)
        or set(declarations) != set(reviewer_workbench.DECLARATION_KEYS)
        or any(declarations.get(key) is not True for key in reviewer_workbench.DECLARATION_KEYS)
    ):
        raise FakeExpertError("visual_submission_declarations_invalid")
    identity = manifest.get("workpack_identity")
    required_identity = {key: identity.get(key) for key in ("workpack_sha256", "phase7d5a_manifest_sha256", "candidate_items_file_sha256", "split_commitment_sha256", "review_plan_sha256") if isinstance(identity, Mapping) and key in identity}
    if value.get("workpack_identity") != required_identity:
        raise FakeExpertError("visual_submission_identity_mismatch")
    reviewer_id = value.get("reviewer_instance")
    session_id = value.get("review_session_id")
    if not isinstance(reviewer_id, str) or not isinstance(session_id, str):
        raise FakeExpertError("visual_submission_reviewer_identity_invalid")
    expected_instance = reviewer_workbench._reviewer_instance_id(str(identity.get("workpack_sha256")), reviewer_id, session_id)
    if value.get("workbench_instance_id") != expected_instance or value.get("workbench_instance_id") != manifest.get("workbench_instance_id"):
        raise FakeExpertError("visual_submission_instance_binding_invalid")
    data = _read_json(root / "data" / "reviewer-workpack.json", "visual_workbench_data_invalid")
    expected_items = {str(row["item_id"]): row for row in data.get("items", []) if isinstance(row, Mapping) and isinstance(row.get("item_id"), str)}
    rows = value.get("items")
    if not isinstance(rows, list):
        raise FakeExpertError("visual_submission_items_required")
    seen: set[str] = set()
    issues: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("item_id"), str):
            raise FakeExpertError("visual_submission_item_invalid")
        item_id = str(row["item_id"])
        if item_id in seen or item_id not in expected_items:
            raise FakeExpertError("visual_submission_item_set_invalid")
        seen.add(item_id)
        expected = expected_items[item_id]
        input_hashes = {
            "source_sha256": expected.get("source_sha256"),
            "render_sha256": (expected.get("context") or {}).get("sha256"),
            "crop_sha256": (expected.get("crop") or {}).get("sha256"),
            "bbox_sha256": expected.get("bbox_sha256"),
        }
        if row.get("item_sha256") != expected.get("item_sha256") or row.get("review_task_id") != expected.get("review_task_id") or row.get("input_hashes") != input_hashes:
            raise FakeExpertError(f"visual_submission_item_hash_mismatch:{item_id}")
        if "split" in row or reviewer_workbench._forbidden_key_present(row) is not None:
            raise FakeExpertError(f"visual_submission_item_hidden_field:{item_id}")
        review = row.get("review")
        if not isinstance(review, Mapping):
            issues.append(f"review_missing:{item_id}")
        else:
            issues.extend(reviewer_workbench._review_content_issues(item_id, str(expected.get("candidate_type")), review, expected))
    if seen != set(expected_items):
        issues.extend(f"missing_submission_item:{item_id}" for item_id in sorted(set(expected_items) - seen))
    return {"valid": True, "accepted": not issues, "issue_count": len(set(issues)), "issues": sorted(set(issues))}


def _validate_scan_structure_submission(root: Path, submission_path: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    try:
        import scan_structure_review

        value = _read_json(submission_path, "scan_structure_submission_invalid")
        result = scan_structure_review._validate_submission(manifest, value)
    except FakeExpertError:
        raise
    except Exception as error:
        raise FakeExpertError(str(error)) from error
    return {"valid": True, "accepted": bool(result.get("accepted")), "blockers": result.get("blockers", []), "item_count": len(result.get("items", {}))}


def _validate_scan_gold_submission(root: Path, submission_path: Path) -> dict[str, Any]:
    try:
        import scan_gold_review

        facts = scan_gold_review._load_scan_gold_workbench(root)
        result = scan_gold_review._load_scan_gold_submission(submission_path, facts)
    except FakeExpertError:
        raise
    except Exception as error:
        raise FakeExpertError(str(error)) from error
    return {"valid": True, "accepted": not bool(result.get("issues")), "issue_count": len(result.get("issues", [])), "issues": result.get("issues", [])}


def review_validate_submission(args: argparse.Namespace) -> dict[str, Any]:
    kind, manifest_path, manifest = _manifest_candidate(args.workbench)
    root = manifest_path.parent
    _validate_workbench_layout(kind, root, manifest_path, manifest)
    submission = _safe_path(args.submission, "submission_path_invalid")
    if not submission.is_file() or submission.is_symlink():
        raise FakeExpertError("submission_missing")
    if kind == "semantic":
        validation = _validate_semantic_submission(root, submission, manifest)
    elif kind == "visual":
        validation = _validate_visual_submission(root, submission, manifest)
    elif kind == "scan-structure":
        validation = _validate_scan_structure_submission(root, submission, manifest)
    else:
        validation = _validate_scan_gold_submission(root, submission)
    validation.update({
        "status": "valid" if validation.get("valid") else "rejected",
        "kind": kind,
        "workbench": str(root),
        "submission": str(submission),
        "gold_created": False,
        "attestation_created": False,
        "promotion_allowed": False,
        "writes": False,
    })
    return validation


def _human(result: Mapping[str, Any]) -> None:
    if "checks" in result:
        state = "PASS" if result.get("passed") else "FAIL"
        print(f"{state}: doctor profile={result.get('profile')}")
        for check in result.get("checks", []):
            suffix = " (optional)" if check.get("optional") else ""
            print(f"{'PASS' if check.get('passed') else 'FAIL'} {check.get('name')}{suffix}: {check.get('version') or 'unavailable'} [{check.get('code') or 'no-code'}]")
        print(f"next: {result.get('next_action')}")
        return
    status = str(result.get("status", "unknown")).upper()
    print(f"{status}: {result.get('code') or result.get('next_action') or 'ok'}")
    if result.get("job"):
        print(f"job: {result['job']}")
    if result.get("next_action") and isinstance(result["next_action"], Mapping):
        print(f"next: {result['next_action'].get('meaning')}")
    if result.get("count") is not None:
        print(f"count: {result['count']}")


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", dest="json_output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor_parser = sub.add_parser("doctor", help="Read-only environment diagnosis")
    doctor_parser.add_argument("--source", type=Path)
    doctor_parser.add_argument("--profile", choices=PROFILES, default="native-text")
    doctor_parser.add_argument("--backend")
    doctor_parser.add_argument("--pdftoppm", default="pdftoppm")
    doctor_parser.add_argument("--minimum-native-characters", type=int, default=40)
    _add_json_flag(doctor_parser)

    plan_parser = sub.add_parser("plan", help="Create a private hash-bound job")
    plan_parser.add_argument("--source", required=True, type=Path)
    plan_parser.add_argument("--workspace", required=True, type=Path)
    plan_parser.add_argument("--profile", choices=PROFILES, required=True)
    plan_parser.add_argument("--backend", help="Explicit backend identity; required for non-native profiles.")
    plan_parser.add_argument("--runtime-receipt", type=Path, help="Optional existing specialist qualification receipt to hash-bind.")
    plan_parser.add_argument("--target-stage", choices=TARGET_STAGES, default="draft")
    plan_parser.add_argument("--workpack", type=Path)
    plan_parser.add_argument("--gold-mode", choices=GOLD_MODES, default="none")
    plan_parser.add_argument("--gold-revision", type=Path)
    plan_parser.add_argument("--job", type=Path)
    plan_parser.add_argument("--name", default="reference")
    plan_parser.add_argument("--display-name", default="Reference Draft")
    plan_parser.add_argument("--domain", default="technical-reference")
    plan_parser.add_argument("--version", default="0.1.0")
    plan_parser.add_argument("--created-at", default=_now())
    _add_json_flag(plan_parser)

    for name, help_text in (("compile", "Evaluate and compile a job"), ("resume", "Re-evaluate a paused job")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--job", required=True, type=Path)
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--progress", action="store_true", help="Emit structured JSONL progress on stderr")
        _add_json_flag(command)

    status_parser = sub.add_parser("status", help="Inspect one job or an explicit workspace")
    status_group = status_parser.add_mutually_exclusive_group(required=True)
    status_group.add_argument("--job", type=Path)
    status_group.add_argument("--workspace", type=Path)
    status_parser.add_argument("--all", action="store_true", help="With --workspace, inspect only jobs below that workspace")
    _add_json_flag(status_parser)

    review = sub.add_parser("review", help="Read-only workbench discovery and submission validation")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    review_list_parser = review_sub.add_parser("list")
    review_list_parser.add_argument("--workspace", required=True, type=Path)
    _add_json_flag(review_list_parser)
    for name in ("locate", "status"):
        command = review_sub.add_parser(name)
        command.add_argument("--workbench", required=True, type=Path)
        _add_json_flag(command)
    validate = review_sub.add_parser("validate-submission")
    validate.add_argument("--workbench", required=True, type=Path)
    validate.add_argument("--submission", required=True, type=Path)
    _add_json_flag(validate)
    return parser


def _status_job(path: Path) -> dict[str, Any]:
    try:
        job_path, job = _load_job(path)
    except FakeExpertError as error:
        return {"status": "rejected", "job": str(path.resolve()), "code": str(error)}
    drift = _validate_job_bindings(job_path, job)
    source_profile_gate = None if drift else _source_profile_gate(job)
    return {
        "status": "rejected" if drift else (source_profile_gate["status"] if source_profile_gate else job["state"]["status"]),
        "code": "job_drift" if drift else (source_profile_gate["code"] if source_profile_gate else job["state"].get("code", "status_ok")),
        "job": str(job_path),
        "job_id": job["job_id"],
        "job_sha256": job["job_sha256"],
        "profile": job["profile"],
        "backend": job["backend"],
        "target_stage": job["target_stage"],
        "state": job["state"],
        "next_action": source_profile_gate["next_action"] if source_profile_gate else job["next_action"],
        "drift": drift,
        "source_sha256": job["inputs"]["source"]["sha256"],
        "policy": job["policy"],
        "required_reading": job["required_reading"],
    }


def _status_all(workspace_value: str | Path) -> dict[str, Any]:
    workspace = _scoped_workspace(workspace_value, create=False)
    jobs: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for path in _walk_files(workspace, max_depth=8):
        if path.name != JOB_FILE_NAME and not path.name.endswith(JOB_FILE_SUFFIX):
            continue
        result = _status_job(path)
        if result.get("status") == "rejected" and result.get("code"):
            errors.append({"job": str(path), "code": str(result["code"])})
        else:
            jobs.append(result)
    jobs.sort(key=lambda row: row["job"])
    return {"status": "ok" if not errors else "partial", "workspace": str(workspace), "jobs": jobs, "errors": errors, "count": len(jobs)}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = doctor(args)
        elif args.command == "plan":
            result = plan_job(args)
        elif args.command == "compile":
            result = _run_job(args, resume=False)
        elif args.command == "resume":
            result = _run_job(args, resume=True)
        elif args.command == "status":
            if args.workspace is not None and not args.all:
                raise FakeExpertError("status_workspace_requires_all")
            if args.job is not None and args.all:
                raise FakeExpertError("status_all_requires_workspace")
            result = _status_all(args.workspace) if args.workspace is not None else _status_job(args.job)
        elif args.command == "review":
            if args.review_command == "list":
                result = review_list(args)
            elif args.review_command == "locate":
                result = review_locate(args)
            elif args.review_command == "status":
                result = review_status(args)
            else:
                result = review_validate_submission(args)
        else:  # pragma: no cover
            raise FakeExpertError("command_invalid")
        if getattr(args, "json_output", False) or args.command in {"plan", "compile", "resume", "status", "review"}:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            _human(result)
        return 0 if result.get("status") not in {"rejected", "paused"} else (0 if args.command in {"compile", "resume"} else 1)
    except (FakeExpertError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        payload = {"status": "rejected", "error": {"code": str(error), "message": str(error)}}
        if getattr(args, "json_output", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), file=sys.stderr)
        else:
            print(f"REJECTED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
