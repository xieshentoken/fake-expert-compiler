#!/usr/bin/env python3
"""Build and validate ephemeral semantic workpacks for technical PDF promotion."""

from __future__ import annotations

import hashlib
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import unicodedata
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from expert_skill_contract import (
    KNOWLEDGE_TYPES,
    SCHEMA_ID,
    SHA256_RE,
    SLUG_RE,
    load_json,
    load_jsonl,
    sha256_file,
)
from formula_execution import FORMULA_AST_SCHEMA, FormulaContractError, sha256_json as formula_sha256_json, validate_formula_ast
from pdf_structure import detect_printed_page_label, normalize_text, stable_id
from heading_locator import locator_bundle_sha256
from compiler_version import (
    SCAN_PDF_COMPILER_VERSION,
    SEMANTIC_ASSURANCE_COMPILER_VERSION,
    SEMANTIC_ASSURANCE_PROTOCOL,
    VISUAL_SEMANTICS_COMPILER_VERSION,
)
from scanned_pdf_ocr import (
    ScanOcrError,
    _load_scan_ir,
    build_scan_evidence_locator,
)
from semantic_assurance import (
    ATTESTATION_SCHEMA,
    assurance_manifest_block,
    enrich_review_material,
    initialize_workpack_assurance,
    is_assurance_enabled,
    load_workpack_assurance,
    materialize_workpack_assurance,
    validate_attestations,
    validate_workpack_assurance,
    write_promoted_assurance,
)
from visual_semantics import (
    VISUAL_ASSURANCE_MANIFEST_SCHEMA,
    VISUAL_OBJECT_SCHEMA,
    VISUAL_PROTOCOL,
    VISUAL_REVIEW_PROTOCOL,
    extract_visual_candidates,
    sha256_file as visual_sha256_file,
    validate_visual_bundle,
    validate_visual_review_attestations,
    validate_visual_assurance_manifest,
    visual_assurance_manifest,
    write_visual_bundle,
)


WORKPACK_SCHEMA = "tkc.semantic-workpack/v0.1"
UNIT_SCHEMA = "tkc.semantic-unit/v0.1"
DRAFT_SCHEMA = "tkc.semantic-draft/v0.1"
REVIEW_SCHEMA = "tkc.semantic-review/v0.1"
VISUAL_RECEIPT_SCHEMA = "tkc.visual-receipt/v0.1"
REVIEW_PLAN_SCHEMA = "tkc.review-plan/v0.1"
FRAGMENTS_SCHEMA = "tkc.review-fragments/v0.1"
REVIEW_PROTOCOL = "separate-source-render-review-v1"
ASSURANCE_REVIEW_PROTOCOL = SEMANTIC_ASSURANCE_PROTOCOL
SEMANTIC_REVIEW_METHOD = "fresh-source-inspection"
VISUAL_REVIEW_METHOD = "fresh-render-inspection"
SEMANTIC_REVIEW_CHECKS = {
    "evidence_span_checked",
    "support_completeness_checked",
    "scope_and_applicability_checked",
    "conflict_checked",
}
VISUAL_REVIEW_CHECKS = {
    "page_asset_opened",
    "task_resolutions_checked",
}
NORMALIZATION_PROFILE = "unicode-nfkc-collapse-whitespace-v1"
ORIGINS = {
    "source-explicit",
    "source-paraphrase",
    "compiler-derived",
    "knowledge-gap",
}
DISPOSITIONS = {"promote", "context-only", "non-knowledge", "reject"}
RELATION_TYPES = {
    "is_a",
    "part_of",
    "depends_on",
    "derived_from",
    "causes",
    "supports",
    "contradicts",
    "valid_when",
    "fails_when",
    "used_for",
    "equivalent_to",
    "example_of",
    "related_to",
}
FORMULA_RELATIONS = {"identical", "notation-normalized", "derived", "conflicted"}
PROHIBITED_DRAFT_KEYS = {
    "text",
    "raw_text",
    "source_text",
    "page_text",
    "quote",
    "excerpt",
    "review_status",
    "verified",
    "human_reviewed",
}
FIGURE_PATTERN = re.compile(r"\b(?:Figure|Fig\.)\s*(\d+(?:\.\d+)+)", re.IGNORECASE)
TABLE_PATTERN = re.compile(r"\bTable\s*(\d+(?:\.\d+)+)", re.IGNORECASE)
NUMBER_PATTERN = re.compile(r"(?<![A-Za-z_])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
ISSUE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class SemanticPromotionError(RuntimeError):
    """Stable workpack failure."""


@dataclass(frozen=True)
class PromotionIssue:
    severity: str
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_png(path: Path) -> bool:
    """Reject signature-only files and require a CRC-valid, decodable PNG stream."""

    try:
        data = path.read_bytes()
    except OSError:
        return False
    if len(data) < 45 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return False

    offset = 8
    first_chunk = True
    seen_idat = False
    idat_closed = False
    seen_iend = False
    seen_plte = False
    idat_parts: list[bytes] = []
    header: tuple[int, int, int, int, int] | None = None
    known_critical = {b"IHDR", b"PLTE", b"IDAT", b"IEND"}
    while offset < len(data):
        if offset + 12 > len(data):
            return False
        length = int.from_bytes(data[offset : offset + 4], "big")
        kind = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(data) or len(kind) != 4:
            return False
        payload = data[offset + 8 : offset + 8 + length]
        expected_crc = int.from_bytes(data[offset + 8 + length : end], "big")
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            return False
        if first_chunk:
            if kind != b"IHDR" or length != 13:
                return False
            first_chunk = False
        if kind == b"IHDR":
            if header is not None or length != 13:
                return False
            width = int.from_bytes(payload[0:4], "big")
            height = int.from_bytes(payload[4:8], "big")
            bit_depth, color_type, compression, filtering, interlace = payload[8:13]
            allowed_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if (
                width < 1
                or height < 1
                or bit_depth not in allowed_depths.get(color_type, set())
                or compression != 0
                or filtering != 0
                or interlace not in {0, 1}
            ):
                return False
            header = (width, height, bit_depth, color_type, interlace)
        elif kind == b"PLTE":
            if seen_idat or length == 0 or length % 3 or length > 768:
                return False
            seen_plte = True
        elif kind == b"IDAT":
            if header is None or idat_closed:
                return False
            seen_idat = True
            idat_parts.append(payload)
        elif kind == b"IEND":
            if length != 0 or not seen_idat or end != len(data):
                return False
            seen_iend = True
        elif kind and kind[0] & 0x20 == 0 and kind not in known_critical:
            return False
        if seen_idat and kind not in {b"IDAT", b"IEND"}:
            idat_closed = True
        offset = end
        if seen_iend:
            break

    if header is None or not seen_iend:
        return False
    width, height, bit_depth, color_type, interlace = header
    if color_type == 3 and not seen_plte:
        return False
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    bits_per_pixel = channels * bit_depth
    passes = (
        [(0, 0, 1, 1)]
        if interlace == 0
        else [
            (0, 0, 8, 8),
            (4, 0, 8, 8),
            (0, 4, 4, 8),
            (2, 0, 4, 4),
            (0, 2, 2, 4),
            (1, 0, 2, 2),
            (0, 1, 1, 2),
        ]
    )
    row_layout: list[tuple[int, int]] = []
    expected_size = 0
    for start_x, start_y, step_x, step_y in passes:
        pass_width = 0 if width <= start_x else (width - start_x + step_x - 1) // step_x
        pass_height = 0 if height <= start_y else (height - start_y + step_y - 1) // step_y
        if not pass_width or not pass_height:
            continue
        row_size = (pass_width * bits_per_pixel + 7) // 8
        row_layout.append((pass_height, row_size))
        expected_size += pass_height * (row_size + 1)
    if expected_size < 1 or expected_size > 512 * 1024 * 1024:
        return False
    try:
        decoded = zlib.decompress(b"".join(idat_parts))
    except zlib.error:
        return False
    if len(decoded) != expected_size:
        return False
    cursor = 0
    for pass_height, row_size in row_layout:
        for _ in range(pass_height):
            if decoded[cursor] > 4:
                return False
            cursor += row_size + 1
    return cursor == len(decoded)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _ensure_empty_output(output: Path) -> Path:
    output = output.expanduser().resolve()
    if output.exists():
        if not output.is_dir():
            raise SemanticPromotionError(f"output_not_directory: {output}")
        if any(output.iterdir()):
            raise SemanticPromotionError(f"output_not_empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    os.chmod(output, 0o700)
    return output


def _resolve_workpack_path(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative:
        raise SemanticPromotionError("workpack_path_invalid")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise SemanticPromotionError(f"unsafe_workpack_path: {relative}")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise SemanticPromotionError(f"unsafe_workpack_path: {relative}") from error
    return resolved


def _review_authorization(plan: dict[str, Any]) -> str:
    payload = {
        "attestation_level": plan.get("attestation_level"),
        "issued_by": plan.get("issued_by"),
        "issued_at": plan.get("issued_at"),
        "source_sha256": plan.get("source_sha256"),
        "workpack_id": plan.get("workpack_id"),
        "bundle_sha256": plan.get("bundle_sha256"),
        "proposer_instances": plan.get("proposer_instances"),
        "reviewer_instances": plan.get("reviewer_instances"),
        "semantic_item_count": plan.get("semantic_item_count"),
        "visual_page_count": plan.get("visual_page_count"),
        "gap_resolution_policy": plan.get("gap_resolution_policy"),
    }
    # Preserve verification of pre-v0.3.1 plans while binding the stronger
    # review protocol whenever a new plan declares it.
    if "review_protocol" in plan:
        payload["review_protocol"] = plan.get("review_protocol")
        payload["review_session_id"] = plan.get("review_session_id")
    if "visual_review_protocol" in plan:
        payload["visual_review_protocol"] = plan.get("visual_review_protocol")
    if "semantic_assurance" in plan:
        payload["semantic_assurance"] = plan.get("semantic_assurance")
    if "visual_review_scope" in plan:
        payload["visual_review_scope"] = plan.get("visual_review_scope")
        payload["visual_candidate_task_count"] = plan.get("visual_candidate_task_count")
        payload["visual_queued_task_count"] = plan.get("visual_queued_task_count")
        payload["visual_deferred_task_count"] = plan.get("visual_deferred_task_count")
        payload["visual_deferred_task_ids_sha256"] = plan.get("visual_deferred_task_ids_sha256")
    return sha256_json(payload)


def _validate_review_trace(
    issues: list[PromotionIssue],
    value: Any,
    *,
    path: str,
    expected_method: str,
    expected_session: str,
    required_checks: set[str],
) -> None:
    """Validate host-recorded review workflow metadata.

    These fields are tamper-evident workflow receipts, not proof of human
    identity. They make a review record describe the inspection that the host
    actually requested and prevent the old blanket auto-accept scaffold from
    silently entering a v0.3.1 promotion.
    """

    if not isinstance(value, dict):
        _add_issue(issues, "error", "review_protocol_invalid", path, "Review trace is required by the active protocol.")
        return
    if value.get("review_session_id") != expected_session:
        _add_issue(issues, "error", "review_protocol_invalid", f"{path}.review_session_id", "Review session does not match the frozen plan.")
    if value.get("review_method") != expected_method:
        _add_issue(issues, "error", "review_protocol_invalid", f"{path}.review_method", f"Expected {expected_method!r}.")
    checks = value.get("review_checks")
    if not isinstance(checks, dict) or any(checks.get(name) is not True for name in required_checks):
        _add_issue(
            issues,
            "error",
            "review_checks_missing",
            f"{path}.review_checks",
            f"Required host-recorded checks: {sorted(required_checks)}.",
        )


def _load_ir(ir_root: Path) -> dict[str, Any]:
    ir_root = ir_root.expanduser().resolve()
    required = {
        "source": "source.json",
        "preflight": "preflight.json",
        "document_map": "document-map.json",
        "anchor_candidates": "anchor-candidates.jsonl",
        "visual_review": "visual-review.json",
    }
    receipt_files = {
        "parser_receipt": "parser-receipt.json",
        "layout_candidates": "layout-candidates.jsonl",
        "render_receipts": "render-receipts.jsonl",
    }
    missing = [filename for filename in required.values() if not (ir_root / filename).is_file()]
    if missing:
        raise SemanticPromotionError(f"ir_missing_files: {missing}")
    receipt_presence = {
        key: (ir_root / filename).is_file() for key, filename in receipt_files.items()
    }
    if any(receipt_presence.values()) and not all(receipt_presence.values()):
        missing_receipts = [
            filename for key, filename in receipt_files.items() if not receipt_presence[key]
        ]
        raise SemanticPromotionError(f"ir_receipt_files_incomplete: {missing_receipts}")
    result: dict[str, Any] = {}
    for key, filename in required.items():
        path = ir_root / filename
        result[key] = load_jsonl(path) if filename.endswith(".jsonl") else load_json(path)
    if (ir_root / "heading-overrides.jsonl").is_file():
        result["heading_overrides"] = load_jsonl(ir_root / "heading-overrides.jsonl")
        required = {**required, "heading_overrides": "heading-overrides.jsonl"}
    else:
        result["heading_overrides"] = []
    heading_files = {
        "heading_candidates": "heading-candidates.jsonl",
        "heading_resolutions": "heading-resolutions.jsonl",
    }
    heading_presence = {
        key: (ir_root / filename).is_file() for key, filename in heading_files.items()
    }
    if any(heading_presence.values()) and not all(heading_presence.values()):
        missing_heading_files = [
            filename for key, filename in heading_files.items() if not heading_presence[key]
        ]
        raise SemanticPromotionError(f"heading_locator_files_incomplete: {missing_heading_files}")
    if all(heading_presence.values()):
        result.update(
            {
                key: load_jsonl(ir_root / filename)
                for key, filename in heading_files.items()
            }
        )
        required = {**required, **heading_files}
    else:
        result.update({"heading_candidates": None, "heading_resolutions": None})
    locator_override_path = ir_root / "heading-locator-overrides.jsonl"
    if locator_override_path.is_file():
        result["heading_locator_overrides"] = load_jsonl(locator_override_path)
        required = {**required, "heading_locator_overrides": "heading-locator-overrides.jsonl"}
    else:
        result["heading_locator_overrides"] = []
    if all(receipt_presence.values()):
        for key, filename in receipt_files.items():
            path = ir_root / filename
            result[key] = load_jsonl(path) if filename.endswith(".jsonl") else load_json(path)
        required = {**required, **receipt_files}
    else:
        result.update({"parser_receipt": None, "layout_candidates": None, "render_receipts": None})
    result["file_hashes"] = {
        filename: sha256_file(ir_root / filename) for filename in required.values()
    }
    try:
        scan = _load_scan_ir(ir_root)
    except ScanOcrError as error:
        raise SemanticPromotionError(str(error)) from error
    result["scan_candidate"] = scan or None
    if scan:
        scan_files = scan["scan_ir"].get("file_refs", {})
        result["file_hashes"].update(
            {
                "scan-ir.json": sha256_file(ir_root / "scan-ir.json"),
                **{
                    filename: sha256_file(ir_root / filename)
                    for filename in scan_files.values()
                    if isinstance(filename, str)
                },
            }
        )
    return result


def _load_source(source_path: Path) -> tuple[Any, str, str]:
    try:
        from pypdf import PdfReader, __version__ as pypdf_version
    except ModuleNotFoundError as error:
        raise SemanticPromotionError(
            "pypdf is required; use the bundled workspace Python or install pypdf"
        ) from error
    source_path = source_path.expanduser().resolve()
    if not source_path.is_file():
        raise SemanticPromotionError(f"source_missing: {source_path}")
    source_hash = sha256_file(source_path)
    try:
        reader = PdfReader(str(source_path))
    except Exception as error:
        raise SemanticPromotionError(f"malformed_pdf: {error}") from error
    if reader.is_encrypted:
        raise SemanticPromotionError("unsupported_encrypted_pdf")
    return reader, source_hash, f"pypdf-{pypdf_version}"


def _scan_candidate_page_texts(
    scan_candidate: dict[str, Any] | None,
    *,
    transcript_path: Path | None = None,
) -> dict[int, str]:
    """Project only the selected primary OCR transcript into local workpack text.

    This is intentionally a proposal-time projection.  It does not alter the
    canonical PDF IR or turn OCR into an evidence anchor.  Challenger/baseline
    text stays in the conflict ledger and visual-review inputs.
    """

    if not scan_candidate:
        return {}
    scan_ir = scan_candidate.get("scan_ir", {})
    primary = scan_ir.get("backend_selection", {}).get("primary")
    if not isinstance(primary, str):
        raise SemanticPromotionError("scan_candidate_primary_backend_missing")
    if transcript_path is not None:
        rows = load_jsonl(transcript_path)
    else:
        rows = scan_candidate.get("transcripts", [])
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("backend_id") != primary:
            continue
        page = row.get("physical_page")
        text = row.get("normalized_text")
        if not isinstance(page, int) or not isinstance(text, str) or not text:
            continue
        grouped.setdefault(page, []).append(row)
    result: dict[int, str] = {}
    for page, page_rows in grouped.items():
        page_rows.sort(key=lambda row: (row.get("span", {}).get("start", 0), row.get("id", "")))
        result[page] = normalize_text(" ".join(str(row["normalized_text"]) for row in page_rows))
    return result


def _page_texts_for_ir(
    reader: Any,
    ir: dict[str, Any],
    scope_start: int,
    scope_end: int,
) -> dict[int, str]:
    page_texts = {
        page: normalize_text(reader.pages[page - 1].extract_text() or "")
        for page in range(scope_start, scope_end + 1)
    }
    scan_candidate = ir.get("scan_candidate")
    if scan_candidate:
        page_texts.update(_scan_candidate_page_texts(scan_candidate))
    return page_texts


def _page_texts_for_workpack(
    reader: Any,
    root: Path,
    manifest: dict[str, Any],
    scope_start: int,
    scope_end: int,
) -> dict[int, str]:
    page_texts = {
        page: normalize_text(reader.pages[page - 1].extract_text() or "")
        for page in range(scope_start, scope_end + 1)
    }
    scan_candidate = manifest.get("scan_candidate")
    if isinstance(scan_candidate, dict):
        transcript_value = scan_candidate.get("transcripts")
        transcript_path = (
            _resolve_workpack_path(root, transcript_value)
            if isinstance(transcript_value, str)
            else None
        )
        page_texts.update(
            _scan_candidate_page_texts(
                {"scan_ir": scan_candidate}, transcript_path=transcript_path
            )
        )
    return page_texts


def _load_workpack_scan_evidence(
    root: Path,
    manifest: dict[str, Any],
    *,
    require_hardened: bool = False,
) -> dict[str, Any] | None:
    """Load and hash-check the private scan sidecars used by promotion."""

    scan_manifest = manifest.get("scan_candidate")
    if not isinstance(scan_manifest, dict):
        if require_hardened:
            raise SemanticPromotionError("scan_candidate_manifest_missing")
        return None
    required = {
        "transcripts": "scan-candidate-transcripts.jsonl",
        "observations": "scan-candidate-observations.jsonl",
        "conflicts": "scan-candidate-conflicts.jsonl",
    }
    hardened = scan_manifest.get("schema_version") == "tkc.scanned-pdf-workpack-candidate/v0.2"
    if hardened:
        required.update(
            {
                "backend_receipts": "scan-candidate-backend-receipts.jsonl",
                "transforms": "scan-candidate-transforms.jsonl",
                "render_receipts": "scan-candidate-render-receipts.jsonl",
                "runtime_contracts": "scan-candidate-runtime-contracts.jsonl",
            }
        )
    elif require_hardened:
        raise SemanticPromotionError("scan_candidate_hardening_required")
    component_hashes = scan_manifest.get("component_sha256", {})
    values: dict[str, Any] = {}
    for key, default_name in required.items():
        relative = scan_manifest.get(key, default_name)
        try:
            path = _resolve_workpack_path(root, relative)
        except SemanticPromotionError as error:
            raise SemanticPromotionError(f"scan_candidate_sidecar_path_invalid:{key}") from error
        if not path.is_file():
            raise SemanticPromotionError(f"scan_candidate_sidecar_missing:{key}")
        expected = component_hashes.get(key)
        if hardened and (
            not isinstance(expected, str)
            or not SHA256_RE.fullmatch(expected)
            or sha256_file(path) != expected
        ):
            raise SemanticPromotionError(f"scan_candidate_sidecar_stale:{key}")
        if key == "runtime_contracts":
            values[key] = load_jsonl(path)
        else:
            values[key] = load_jsonl(path)
    if hardened:
        primary = scan_manifest.get("backend_selection", {}).get("primary")
        if not isinstance(primary, str) or not primary:
            raise SemanticPromotionError("scan_candidate_primary_backend_missing")
        values["primary_backend"] = primary
        values["manifest"] = scan_manifest
    return values


def _validate_ir_lineage(
    ir: dict[str, Any], source_hash: str, *, allow_scan_candidates: bool = False
) -> tuple[str, int, int, bool]:
    source = ir["source"]
    preflight = ir["preflight"]
    document_map = ir["document_map"]
    source_id = source.get("source_id")
    if source.get("sha256") != source_hash:
        raise SemanticPromotionError("source_hash_mismatch")
    if not isinstance(source_id, str) or not source_id:
        raise SemanticPromotionError("ir_source_id_missing")
    for name, value in (
        ("preflight", preflight),
        ("document_map", document_map),
        ("visual_review", ir["visual_review"]),
    ):
        if value.get("source_id") != source_id:
            raise SemanticPromotionError(f"ir_source_id_mismatch: {name}")
    for index, candidate in enumerate(ir["anchor_candidates"]):
        if candidate.get("source_id") != source_id:
            raise SemanticPromotionError(f"ir_source_id_mismatch: anchor-candidates:{index + 1}")
    scan_candidate = ir.get("scan_candidate")
    scan_mode = bool(scan_candidate)
    if scan_mode:
        if not isinstance(scan_candidate, dict):
            raise SemanticPromotionError("scan_candidate_invalid")
        scan_root = scan_candidate.get("root")
        scan_ir = scan_candidate.get("scan_ir")
        if not isinstance(scan_root, Path) or not isinstance(scan_ir, dict):
            raise SemanticPromotionError("scan_candidate_invalid")
        if scan_ir.get("compiler_version") != SCAN_PDF_COMPILER_VERSION:
            raise SemanticPromotionError("scan_candidate_compiler_version_invalid")
        component_hashes = scan_ir.get("component_sha256")
        file_refs = scan_ir.get("file_refs")
        if not isinstance(component_hashes, dict) or not isinstance(file_refs, dict):
            raise SemanticPromotionError("scan_candidate_receipts_missing")
        for key, expected in component_hashes.items():
            filename = file_refs.get(key)
            if (
                not isinstance(filename, str)
                or Path(filename).name != filename
                or not isinstance(expected, str)
                or not SHA256_RE.fullmatch(expected)
                or not (scan_root / filename).is_file()
                or sha256_file(scan_root / filename) != expected
            ):
                raise SemanticPromotionError(f"scan_candidate_component_stale: {key}")
        scan_source = scan_root / "source.json"
        if not scan_source.is_file():
            raise SemanticPromotionError("scan_candidate_source_receipt_missing")
        scan_source_record = load_json(scan_source)
        scan_receipts = scan_source_record.get("pdf_ir_receipts", {})
        scan_ir_path = scan_root / "scan-ir.json"
        if (
            scan_source_record.get("sha256") != source_hash
            or scan_receipts.get("scan_ir_sha256") != sha256_file(scan_ir_path)
            or scan_receipts.get("scan_component_sha256") != component_hashes
            or scan_ir.get("quality_routing_sha256")
            != sha256_json(scan_candidate.get("quality"))
        ):
            raise SemanticPromotionError("scan_candidate_receipt_binding_invalid")
    if preflight.get("eligible_for_current_compiler") is not True or preflight.get("pages_needing_ocr"):
        if not allow_scan_candidates or not scan_candidate:
            if preflight.get("pages_needing_ocr"):
                raise SemanticPromotionError("unsupported_scanned_pdf")
            raise SemanticPromotionError("source_not_eligible_for_current_compiler")
        scan_ir = scan_candidate.get("scan_ir") if isinstance(scan_candidate, dict) else None
        if not isinstance(scan_ir, dict) or scan_ir.get("source_sha256") != source_hash:
            raise SemanticPromotionError("scan_candidate_source_mismatch")
        if scan_ir.get("candidate_policy", {}).get("canonical_anchor_creation") != "forbidden":
            raise SemanticPromotionError("scan_candidate_policy_invalid")
        if scan_ir.get("candidate_policy", {}).get("formula_executable") is not False:
            raise SemanticPromotionError("scan_candidate_formula_capability_invalid")
        expected_ocr_pages = sorted(preflight.get("page_routing", {}).get("ocr_required_pages", []))
        if scan_ir.get("ocr_pages") != expected_ocr_pages:
            raise SemanticPromotionError("scan_candidate_route_mismatch")
    scope = preflight.get("scope")
    if not isinstance(scope, dict):
        raise SemanticPromotionError("ir_scope_missing")
    start_page = scope.get("start_page")
    end_page = scope.get("end_page")
    if not isinstance(start_page, int) or not isinstance(end_page, int):
        raise SemanticPromotionError("ir_scope_invalid")
    if document_map.get("scope") != scope:
        raise SemanticPromotionError("ir_scope_mismatch")
    if not isinstance(start_page, int) or not isinstance(end_page, int):
        raise SemanticPromotionError("ir_scope_invalid")

    # v0.1 IR was deliberately source-text-free but predated layout/parser/render
    # receipts. Preserve it as an explicit compatibility input; the workpack still
    # obtains fresh visual assets before reviewed promotion. New reconstruction
    # always emits the stronger v0.2 receipt set below.
    if ir["parser_receipt"] is None:
        if preflight.get("schema_version") != "tkc.pdf-ir/v0.1":
            raise SemanticPromotionError("ir_receipts_missing")
        return source_id, start_page, end_page, scan_mode

    expected_pages = list(range(start_page, end_page + 1))
    receipts = source.get("pdf_ir_receipts")
    if not isinstance(receipts, dict):
        raise SemanticPromotionError("ir_receipts_missing")
    parser_receipt = ir["parser_receipt"]
    layout_candidates = ir["layout_candidates"]
    render_receipts = ir["render_receipts"]
    if (
        parser_receipt.get("source_sha256") != source_hash
        or parser_receipt.get("scope") != scope
        or parser_receipt.get("layout_candidates_sha256") != sha256_json(layout_candidates)
        or receipts.get("parser_receipt_sha256") != sha256_json(parser_receipt)
        or receipts.get("layout_candidates_sha256") != sha256_json(layout_candidates)
        or receipts.get("render_receipts_sha256") != sha256_json(render_receipts)
    ):
        raise SemanticPromotionError("ir_receipt_hash_mismatch")
    for field, value in (
        ("preflight_sha256", preflight),
        ("document_map_sha256", document_map),
        ("visual_review_sha256", ir["visual_review"]),
        ("anchor_candidates_sha256", ir["anchor_candidates"]),
    ):
        if receipts.get(field) != sha256_json(value):
            raise SemanticPromotionError("ir_receipt_hash_mismatch")
    heading_overrides = ir.get("heading_overrides", [])
    if heading_overrides:
        if receipts.get("heading_overrides_sha256") != sha256_json(heading_overrides):
            raise SemanticPromotionError("ir_receipt_hash_mismatch")
        bound = {
            (
                segment.get("title"),
                segment.get("heading_override", {}).get("before_title"),
            )
            for segment in document_map.get("segments", [])
            if isinstance(segment, dict)
            and isinstance(segment.get("heading_override"), dict)
        }
        expected = {
            (row.get("new_title"), row.get("before_title"))
            for row in heading_overrides
        }
        if bound != expected:
            raise SemanticPromotionError("ir_heading_override_unbound")
    elif receipts.get("heading_overrides_sha256") is not None:
        raise SemanticPromotionError("ir_receipt_hash_mismatch")
    if preflight.get("schema_version") == "tkc.pdf-ir/v0.3":
        heading_candidates = ir.get("heading_candidates")
        heading_resolutions = ir.get("heading_resolutions")
        if not isinstance(heading_candidates, list) or not isinstance(heading_resolutions, list):
            raise SemanticPromotionError("heading_locator_files_missing")
        if receipts.get("heading_candidates_sha256") != sha256_json(heading_candidates):
            raise SemanticPromotionError("heading_candidate_hash_mismatch")
        if receipts.get("heading_resolutions_sha256") != sha256_json(heading_resolutions):
            raise SemanticPromotionError("heading_resolution_hash_mismatch")
        locator_overrides = ir.get("heading_locator_overrides", [])
        if locator_overrides:
            if receipts.get("heading_locator_overrides_sha256") != sha256_json(locator_overrides):
                raise SemanticPromotionError("heading_locator_overrides_hash_mismatch")
        elif receipts.get("heading_locator_overrides_sha256") is not None:
            raise SemanticPromotionError("heading_locator_overrides_hash_mismatch")
        candidate_by_id = {
            row.get("candidate_id"): row
            for row in heading_candidates
            if isinstance(row, dict) and isinstance(row.get("candidate_id"), str)
        }
        for index, row in enumerate(heading_candidates):
            if not isinstance(row, dict) or row.get("source_sha256") != source_hash:
                raise SemanticPromotionError(f"heading_candidate_invalid:{index + 1}")
            candidate_hash = sha256_json(
                {key: value for key, value in row.items() if key != "candidate_sha256"}
            )
            if row.get("candidate_sha256") != candidate_hash:
                raise SemanticPromotionError(f"heading_candidate_hash_invalid:{index + 1}")
        for index, row in enumerate(heading_resolutions):
            if not isinstance(row, dict) or row.get("source_sha256") != source_hash:
                raise SemanticPromotionError(f"heading_resolution_invalid:{index + 1}")
            if row.get("resolution_sha256") != sha256_json(
                {key: value for key, value in row.items() if key != "resolution_sha256"}
            ):
                raise SemanticPromotionError(f"heading_resolution_hash_invalid:{index + 1}")
            selected_id = row.get("selected_candidate_id")
            if row.get("status") == "resolved":
                candidate = candidate_by_id.get(selected_id)
                if candidate is None or candidate.get("segment_id") != row.get("segment_id"):
                    raise SemanticPromotionError(f"heading_resolution_selected_candidate_invalid:{index + 1}")
                if row.get("selected_candidate_sha256") != candidate.get("candidate_sha256"):
                    raise SemanticPromotionError(f"heading_resolution_selected_hash_invalid:{index + 1}")
        if not heading_resolutions:
            raise SemanticPromotionError("heading_resolutions_empty")
    parser = parser_receipt.get("layout_parser")
    if (
        not isinstance(parser, dict)
        or not isinstance(parser.get("id"), str)
        or not isinstance(parser.get("version"), str)
    ):
        raise SemanticPromotionError("ir_parser_receipt_invalid")
    parser_pages = parser_receipt.get("pages")
    if not isinstance(parser_pages, list) or [row.get("physical_page") for row in parser_pages if isinstance(row, dict)] != expected_pages:
        raise SemanticPromotionError("ir_parser_page_coverage_invalid")
    page_dimensions: dict[int, dict[str, Any]] = {}
    for row in parser_pages:
        if not isinstance(row, dict):
            raise SemanticPromotionError("ir_parser_page_coverage_invalid")
        page = row.get("physical_page")
        dimensions = row.get("page_dimensions_points")
        status = row.get("coordinate_status")
        if (
            not isinstance(page, int)
            or not isinstance(dimensions, dict)
            or not isinstance(dimensions.get("width"), (int, float))
            or not isinstance(dimensions.get("height"), (int, float))
            or dimensions["width"] <= 0
            or dimensions["height"] <= 0
            or not isinstance(status, str)
        ):
            raise SemanticPromotionError("ir_parser_page_invalid")
        page_dimensions[page] = dimensions
    candidates_by_page: dict[int, list[dict[str, Any]]] = {page: [] for page in expected_pages}
    for index, candidate in enumerate(layout_candidates):
        if not isinstance(candidate, dict):
            raise SemanticPromotionError(f"ir_layout_candidate_invalid: {index + 1}")
        page = candidate.get("physical_page")
        bbox = candidate.get("bbox")
        if (
            candidate.get("source_sha256") != source_hash
            or candidate.get("parser_id") != parser["id"]
            or candidate.get("parser_version") != parser["version"]
            or page not in candidates_by_page
            or not isinstance(bbox, list)
            or len(bbox) != 4
            or any(not isinstance(value, (int, float)) for value in bbox)
            or not isinstance(candidate.get("content_sha256"), str)
            or not SHA256_RE.fullmatch(candidate["content_sha256"])
        ):
            raise SemanticPromotionError(f"ir_layout_candidate_invalid: {index + 1}")
        left, top, right, bottom = bbox
        dimensions = page_dimensions[page]
        if (
            left < 0
            or top < 0
            or right < left
            or bottom < top
            or right > dimensions["width"] + 0.01
            or bottom > dimensions["height"] + 0.01
        ):
            raise SemanticPromotionError(f"ir_layout_candidate_bbox_invalid: {index + 1}")
        candidates_by_page[page].append(candidate)
    for row in parser_pages:
        page = row["physical_page"]
        if row.get("candidate_count") != len(candidates_by_page[page]) or row.get("candidates_sha256") != sha256_json(candidates_by_page[page]):
            raise SemanticPromotionError(f"ir_parser_candidate_binding_invalid: page={page}")

    if render_receipts:
        if receipts.get("render_receipt_status") != "complete":
            raise SemanticPromotionError("ir_render_receipt_status_invalid")
        render_pages = [row.get("physical_page") for row in render_receipts if isinstance(row, dict)]
        if render_pages != expected_pages:
            raise SemanticPromotionError("ir_render_page_coverage_invalid")
        for index, receipt in enumerate(render_receipts):
            renderer = receipt.get("renderer") if isinstance(receipt, dict) else None
            if (
                not isinstance(receipt, dict)
                or receipt.get("source_sha256") != source_hash
                or not isinstance(receipt.get("render_sha256"), str)
                or not SHA256_RE.fullmatch(receipt["render_sha256"])
                or not isinstance(renderer, dict)
                or renderer.get("id") != "pdftoppm"
                or renderer.get("format") != "png"
                or not isinstance(renderer.get("version"), str)
                or not isinstance(renderer.get("dpi"), int)
            ):
                raise SemanticPromotionError(f"ir_render_receipt_invalid: {index + 1}")
    elif receipts.get("render_receipt_status") != "deferred":
        raise SemanticPromotionError("ir_render_receipt_status_invalid")
    return source_id, start_page, end_page, scan_mode


def _locate_headings(
    segments: list[dict[str, Any]], page_texts: dict[int, str]
) -> list[dict[str, Any]]:
    locations: list[dict[str, Any]] = []
    for index, segment in enumerate(segments):
        title = normalize_text(str(segment.get("title", "")))
        pages = segment.get("physical_pages")
        if not title or not isinstance(pages, list) or not pages or not isinstance(pages[0], int):
            raise SemanticPromotionError(f"segment_invalid: {index + 1}")
        page = pages[0]
        text = page_texts.get(page, "")
        positions: list[int] = []
        cursor = 0
        while True:
            position = text.find(title, cursor)
            if position < 0:
                break
            positions.append(position)
            cursor = position + len(title)
        count = len(positions)
        if count == 0:
            raise SemanticPromotionError(
                f"heading_locator_missing: segment={segment.get('id')} page={page} title={title!r}"
            )
        running_head = re.match(r"^\s+\d{1,4}\s+", text[len(title) :])
        if count == 2 and positions[0] == 0 and running_head:
            # Some books repeat the current section title in a page-top running
            # head, followed by the printed page number.  This layout signal is
            # narrow enough to select the later body heading while preserving
            # fail-closed behavior for ordinary duplicate headings.
            start = positions[1]
        elif count > 1:
            raise SemanticPromotionError(
                f"heading_locator_ambiguous: segment={segment.get('id')} page={page} count={count}"
            )
        else:
            start = positions[0]
        locations.append(
            {
                "segment_index": index,
                "segment_id": segment["id"],
                "title": title,
                "page": page,
                "start": start,
                "end": start + len(title),
            }
        )
    for earlier, later in zip(locations, locations[1:]):
        if (later["page"], later["start"]) < (earlier["page"], earlier["start"]):
            raise SemanticPromotionError("heading_order_invalid")
    return locations


def _locate_headings_from_resolutions(
    segments: list[dict[str, Any]],
    page_texts: dict[int, str],
    heading_candidates: list[dict[str, Any]],
    heading_resolutions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Consume only the hash-locked locator resolution; never search titles again."""

    candidates_by_id = {
        row.get("candidate_id"): row
        for row in heading_candidates
        if isinstance(row, dict) and isinstance(row.get("candidate_id"), str)
    }
    resolutions_by_segment = {
        row.get("segment_id"): row
        for row in heading_resolutions
        if isinstance(row, dict) and isinstance(row.get("segment_id"), str)
    }
    unresolved = [
        row
        for row in heading_resolutions
        if isinstance(row, dict) and row.get("status") != "resolved"
    ]
    if unresolved:
        return [], [], unresolved
    ordered_segments = sorted(
        segments,
        key=lambda segment: (
            int(
                candidates_by_id.get(
                    resolutions_by_segment.get(segment.get("id"), {}).get("selected_candidate_id"),
                    {},
                ).get("physical_page")
                or 10**9
            ),
            int(
                (
                    candidates_by_id.get(
                        resolutions_by_segment.get(segment.get("id"), {}).get("selected_candidate_id"),
                        {},
                    ).get("char_span")
                    or [10**9, 10**9]
                )[0]
            ),
            int(resolutions_by_segment.get(segment.get("id"), {}).get("structure_order") or 0),
            str(segment.get("id")),
        ),
    )
    locations: list[dict[str, Any]] = []
    for index, segment in enumerate(ordered_segments):
        segment_id = segment.get("id")
        resolution = resolutions_by_segment.get(segment_id)
        if not isinstance(resolution, dict):
            unresolved.append(
                {
                    "segment_id": segment_id,
                    "failure_code": "heading_locator_missing",
                    "status": "heading_resolution_required",
                    "requested_title": segment.get("title"),
                }
            )
            continue
        candidate_id = resolution.get("selected_candidate_id")
        candidate = candidates_by_id.get(candidate_id)
        if not isinstance(candidate, dict) or candidate.get("segment_id") != segment_id:
            unresolved.append(
                {
                    "segment_id": segment_id,
                    "resolution_id": resolution.get("resolution_id"),
                    "failure_code": "heading_resolution_selected_candidate_invalid",
                    "status": "heading_resolution_required",
                    "requested_title": segment.get("title"),
                    "candidate_ids": resolution.get("candidate_ids", []),
                }
            )
            continue
        page = candidate.get("physical_page")
        span = candidate.get("char_span")
        page_text = page_texts.get(page)
        if (
            not isinstance(page, int)
            or not isinstance(span, list)
            or len(span) != 2
            or not all(isinstance(value, int) for value in span)
            or not isinstance(page_text, str)
            or span[0] < 0
            or span[1] <= span[0]
            or span[1] > len(page_text)
            or candidate.get("page_text_sha256") != sha256_text(page_text)
            or candidate.get("span_sha256") != sha256_text(page_text[span[0]:span[1]])
            or candidate.get("candidate_sha256") != sha256_json(
                {key: value for key, value in candidate.items() if key != "candidate_sha256"}
            )
            or resolution.get("selected_candidate_sha256") != candidate.get("candidate_sha256")
        ):
            raise SemanticPromotionError(f"heading_resolution_binding_invalid: segment={segment_id}")
        locations.append(
            {
                "segment_index": index,
                "segment_id": segment_id,
                "resolution_id": resolution.get("resolution_id"),
                "candidate_id": candidate_id,
                "title": normalize_text(str(segment.get("title", ""))),
                "raw_title": candidate.get("raw_title"),
                "method": candidate.get("candidate_method"),
                "page": page,
                "start": span[0],
                "end": span[1],
            }
        )
    return locations, ordered_segments, unresolved


def _write_structure_resolution_tasks(
    output: Path,
    source_hash: str,
    ir: dict[str, Any],
    unresolved: list[dict[str, Any]],
) -> Path:
    rows: list[dict[str, Any]] = []
    candidates = {
        row.get("candidate_id"): row
        for row in ir.get("heading_candidates", [])
        if isinstance(row, dict) and isinstance(row.get("candidate_id"), str)
    }
    resolutions = {
        row.get("resolution_id"): row
        for row in ir.get("heading_resolutions", [])
        if isinstance(row, dict) and isinstance(row.get("resolution_id"), str)
    }
    for row in unresolved:
        resolution_id = row.get("resolution_id")
        resolution = resolutions.get(resolution_id, row)
        candidate_ids = list(resolution.get("candidate_ids", row.get("candidate_ids", [])))
        task = {
            "schema_version": "tkc.heading-resolution-task/v0.2",
            "task_id": stable_id("hrt", source_hash, resolution_id or row.get("segment_id"), candidate_ids),
            "source_sha256": source_hash,
            "segment_id": row.get("segment_id") or resolution.get("segment_id"),
            "resolution_id": resolution_id,
            "requested_title": resolution.get("requested_title") or row.get("requested_title"),
            "failure_code": resolution.get("failure_code") or row.get("failure_code") or "heading_locator_missing",
            "candidate_ids": candidate_ids,
            "candidate_hashes": {
                candidate_id: candidates[candidate_id].get("candidate_sha256")
                for candidate_id in candidate_ids
                if candidate_id in candidates
            },
            "candidate_context": [
                {
                    "candidate_id": candidate_id,
                    "physical_page": candidates[candidate_id].get("physical_page"),
                    "raw_title": candidates[candidate_id].get("raw_title"),
                    "role": candidates[candidate_id].get("role"),
                    "line_sha256": candidates[candidate_id].get("line_sha256"),
                    "render_sha256": candidates[candidate_id].get("render_sha256"),
                    "parser": candidates[candidate_id].get("parser"),
                }
                for candidate_id in candidate_ids
                if candidate_id in candidates
            ],
            "allowed_actions": ["heading-locator-override", "title-correction-and-rebuild"],
            "reviewer_required": True,
            "status": "pending-independent-structure-review",
            "execution_allowed": False,
        }
        task["task_sha256"] = sha256_json(task)
        rows.append(task)
    path = output / "structure-resolution-required.jsonl"
    _write_jsonl(path, rows)
    return path


def _trim_interval(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _span_record(
    page: int,
    text: str,
    start: int,
    end: int,
    *,
    role: str | None = None,
    extractable: bool = True,
) -> dict[str, Any] | None:
    start, end = _trim_interval(text, start, end)
    if start >= end:
        return None
    span_text = text[start:end]
    record: dict[str, Any] = {
        "page": page,
        "start": start,
        "end": end,
        "page_text_sha256": sha256_text(text),
        "span_sha256": sha256_text(span_text),
        "text": span_text,
        "extractable": extractable,
    }
    if role is not None:
        record["role"] = role
    return record


def _focus_spans(
    page_texts: dict[int, str],
    start_page: int,
    start_offset: int,
    end_page: int,
    end_offset: int,
) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for page in range(start_page, end_page + 1):
        text = page_texts[page]
        start = start_offset if page == start_page else 0
        end = end_offset if page == end_page else len(text)
        record = _span_record(page, text, start, end)
        if record is not None:
            spans.append(record)
    return spans


def _context_spans(
    page_texts: dict[int, str],
    scope_start: int,
    scope_end: int,
    focus_spans: list[dict[str, Any]],
    boundary_start: tuple[int, int],
    boundary_end: tuple[int, int],
    context_characters: int,
) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    start_page, start_offset = boundary_start
    end_page, end_offset = boundary_end

    remaining = context_characters
    page = start_page
    offset = start_offset
    previous_parts: list[dict[str, Any]] = []
    while remaining > 0 and page >= scope_start:
        text = page_texts[page]
        current_end = offset if page == start_page else len(text)
        current_start = max(0, current_end - remaining)
        record = _span_record(
            page,
            text,
            current_start,
            current_end,
            role="previous-tail",
            extractable=False,
        )
        if record is not None:
            previous_parts.append(record)
            remaining -= record["end"] - record["start"]
        page -= 1
        offset = len(page_texts[page]) if page >= scope_start else 0
    contexts.extend(reversed(previous_parts))

    remaining = context_characters
    page = end_page
    offset = end_offset
    while remaining > 0 and page <= scope_end:
        text = page_texts[page]
        current_start = offset if page == end_page else 0
        current_end = min(len(text), current_start + remaining)
        record = _span_record(
            page,
            text,
            current_start,
            current_end,
            role="next-head",
            extractable=False,
        )
        if record is not None:
            contexts.append(record)
            remaining -= record["end"] - record["start"]
        page += 1
        offset = 0

    focus_keys = {
        (span["page"], span["start"], span["end"]) for span in focus_spans
    }
    return [
        span
        for span in contexts
        if (span["page"], span["start"], span["end"]) not in focus_keys
    ]


def _visual_tasks(
    preflight: dict[str, Any],
    visual_review: dict[str, Any],
    page_texts: dict[int, str],
    source_id: str,
    scan_quality: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    preflight_pages = {
        row.get("physical_page"): row
        for row in preflight.get("pages", [])
        if isinstance(row, dict) and isinstance(row.get("physical_page"), int)
    }
    tasks: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()

    def add(page: int, kind: str, label: str, reasons: list[str]) -> None:
        key = (page, kind, label)
        if key in seen:
            return
        seen.add(key)
        tasks.append(
            {
                "id": stable_id("vis", source_id, page, kind, label),
                "page": page,
                "kind": kind,
                "candidate_label": label,
                "required": True,
                "reason_codes": sorted(set(reasons)),
                "presence_classification": "unclassified",
                "bbox_normalized": None,
                "page_asset": None,
                "page_asset_sha256": None,
            }
        )

    for row in visual_review.get("pages", []):
        if not isinstance(row, dict) or not isinstance(row.get("physical_page"), int):
            continue
        page = row["physical_page"]
        reasons = [item for item in row.get("reasons", []) if isinstance(item, str)]
        page_row = preflight_pages.get(page, {})
        labels = page_row.get("equation_labels", [])
        for label in labels if isinstance(labels, list) else []:
            if isinstance(label, str):
                add(page, "equation", label.strip("() "), reasons)
        text = page_texts.get(page, "")
        for label in FIGURE_PATTERN.findall(text):
            add(page, "figure-check", label, reasons + ["figure_presence_requires_review"])
        for label in TABLE_PATTERN.findall(text):
            add(page, "table-check", label, reasons + ["table_layout_requires_review"])
        if "suspicious_glyph_decoding" in reasons:
            add(page, "glyph-check", "page-glyphs", reasons)
        if "math_font_present" in reasons:
            add(page, "math-page-check", "unlabelled-math", reasons)
        if "raster_image_present" in reasons:
            add(page, "image-check", "page-images", reasons)
        if not any(task["page"] == page for task in tasks):
            add(page, "page-layout-check", "page-layout", reasons)
    if isinstance(scan_quality, dict):
        for row in scan_quality.get("pages", []):
            if not isinstance(row, dict) or not isinstance(row.get("physical_page"), int):
                continue
            if row.get("route") != "ocr-candidate":
                continue
            page = row["physical_page"]
            reasons = [item for item in row.get("reasons", []) if isinstance(item, str)]
            if any("formula" in reason for reason in reasons):
                add(
                    page,
                    "scan-formula-check",
                    "scan-formula-candidates",
                    reasons + ["scan_formula_candidate_requires_full_visual_review"],
                )
            if any("table" in reason for reason in reasons):
                add(
                    page,
                    "scan-table-check",
                    "scan-table-candidates",
                    reasons + ["scan_table_candidate_requires_full_visual_review"],
                )
            if "low_confidence" in reasons:
                add(page, "scan-confidence-check", "scan-low-confidence", reasons)
            if "ocr_conflict_or_duplicate" in reasons:
                add(page, "scan-conflict-check", "scan-conflicts", reasons)
            if "unit_visual_review_required" in reasons:
                add(page, "scan-unit-check", "scan-unit-candidates", reasons)
    return sorted(tasks, key=lambda row: (row["page"], row["kind"], row["candidate_label"]))


def _render_visual_pages(
    tasks: list[dict[str, Any]],
    source_path: Path,
    output: Path,
    pdftoppm: Path,
    dpi: int,
) -> dict[str, Any]:
    pdftoppm = pdftoppm.expanduser().resolve()
    if not pdftoppm.is_file():
        resolved = shutil.which(str(pdftoppm))
        if not resolved:
            raise SemanticPromotionError(f"pdftoppm_missing: {pdftoppm}")
        pdftoppm = Path(resolved)
    try:
        version = subprocess.run(
            [str(pdftoppm), "-v"],
            check=False,
            capture_output=True,
            text=True,
        )
        renderer_version = (version.stderr or version.stdout).strip().splitlines()[0]
    except OSError as error:
        raise SemanticPromotionError(f"pdftoppm_failed: {error}") from error

    render_root = output / "renders"
    render_root.mkdir(parents=True, exist_ok=True)
    os.chmod(render_root, 0o700)
    pages = sorted({task["page"] for task in tasks})
    assets: dict[int, tuple[str, str]] = {}
    for page in pages:
        relative = f"renders/page-{page:04d}.png"
        target = output / relative
        prefix = target.with_suffix("")
        completed = subprocess.run(
            [
                str(pdftoppm),
                "-f",
                str(page),
                "-l",
                str(page),
                "-singlefile",
                "-png",
                "-r",
                str(dpi),
                str(source_path),
                str(prefix),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0 or not target.is_file():
            raise SemanticPromotionError(
                f"pdf_render_failed: page={page} detail={completed.stderr.strip()}"
            )
        os.chmod(target, 0o600)
        assets[page] = (relative, sha256_file(target))
    for task in tasks:
        relative, asset_hash = assets[task["page"]]
        task["page_asset"] = relative
        task["page_asset_sha256"] = asset_hash
    return {
        "renderer": "pdftoppm",
        "renderer_version": renderer_version,
        "dpi": dpi,
        "format": "png",
    }


def _visual_bundle_for_workpack(
    source_path: Path,
    source_sha256: str,
    tasks: list[dict[str, Any]],
    *,
    render_profile: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project bounded, candidate-only visual objects into the workpack.

    Existing callers may intentionally build a proposal workpack without a
    renderer.  That remains a valid proposal-only pause; the visual bundle then
    records a deterministic missing-render conflict and cannot enter review or
    promotion.  A rendered workpack uses the pypdf/native baseline and binds every
    object to the exact page render hash already attached to visual tasks.
    """

    pages = sorted({int(task["page"]) for task in tasks if isinstance(task, dict) and isinstance(task.get("page"), int)})
    if not pages:
        return {
            "schema_version": VISUAL_PROTOCOL,
            "source_sha256": source_sha256,
            "pages": [],
            "objects": [],
            "relations": [],
            "table_grids": [],
            "conflicts": [],
            "render_receipts": [],
            "candidate_only": True,
            "whole_book_completeness_claimed": False,
        }
    page_assets = {
        page: next((task for task in tasks if task.get("page") == page and isinstance(task.get("page_asset_sha256"), str)), None)
        for page in pages
    }
    if any(not isinstance(task, dict) for task in page_assets.values()):
        return {
            "schema_version": VISUAL_PROTOCOL,
            "source_sha256": source_sha256,
            "pages": pages,
            "objects": [],
            "relations": [],
            "table_grids": [],
            "conflicts": [
                {
                    "schema_version": "tkc.visual-conflict/v0.1",
                    "visual_conflict_id": stable_id("vc", source_sha256, page, "missing", "render") ,
                    "source_sha256": source_sha256,
                    "physical_page": page,
                    "conflict_type": "missing",
                    "object_ids": [],
                    "details": {"code": "visual_render_receipt_required", "page": page},
                    "status": "candidate",
                    "conflict_sha256": sha256_json({
                        "schema_version": "tkc.visual-conflict/v0.1",
                        "visual_conflict_id": stable_id("vc", source_sha256, page, "missing", "render"),
                        "source_sha256": source_sha256,
                        "physical_page": page,
                        "conflict_type": "missing",
                        "object_ids": [],
                        "details": {"code": "visual_render_receipt_required", "page": page},
                        "status": "candidate",
                    }),
                }
                for page in pages
            ],
            "render_receipts": [],
            "candidate_only": True,
            "whole_book_completeness_claimed": False,
        }
    render_receipts = {
        page: {
            "source_sha256": source_sha256,
            "physical_page": page,
            "renderer": "pdftoppm",
            "renderer_version": (render_profile or {}).get("renderer_version", "workpack-bound"),
            "dpi": int((render_profile or {}).get("dpi", 150)),
            "rotation": 0,
            "format": "png",
            "render_sha256": str(page_assets[page]["page_asset_sha256"]),
        }
        for page in pages
    }
    try:
        return extract_visual_candidates(
            source_path,
            pages=pages,
            render_receipts=render_receipts,
            render_dpi=int((render_profile or {}).get("dpi", 150)),
            parser_receipt={"id": "pypdf-native-baseline", "version": "pypdf-6.10.0", "candidate_only": True},
            backend_receipt={"id": "reviewer-locator-confirmed-bbox", "version": "v0.1", "candidate_only": True},
            model_receipt={"id": "none", "available": False, "download": False},
            config_receipt={"id": "workpack-native-visual-baseline-v0.1", "candidate_only": True},
        )
    except Exception as error:
        # Missing or malformed candidate geometry must remain an explicit conflict;
        # never silently fall back to a page-level visual fact.
        conflicts = []
        for page in pages:
            detail = {"code": str(error).split(":", 1)[0], "message": str(error), "page": page}
            base = {
                "schema_version": "tkc.visual-conflict/v0.1",
                "visual_conflict_id": stable_id("vc", source_sha256, page, "missing", detail["code"]),
                "source_sha256": source_sha256,
                "physical_page": page,
                "conflict_type": "missing",
                "object_ids": [],
                "details": detail,
                "status": "candidate",
            }
            base["conflict_sha256"] = sha256_json(base)
            conflicts.append(base)
        return {"schema_version": VISUAL_PROTOCOL, "source_sha256": source_sha256, "pages": pages, "objects": [], "relations": [], "table_grids": [], "conflicts": conflicts, "render_receipts": list(render_receipts.values()), "candidate_only": True, "whole_book_completeness_claimed": False, "unavailable": True}


def _attach_visual_ids_to_tasks(tasks: list[dict[str, Any]], bundle: Mapping[str, Any]) -> None:
    objects = [row for row in bundle.get("objects", []) if isinstance(row, dict)]
    relations = [row for row in bundle.get("relations", []) if isinstance(row, dict)]
    grids = [row for row in bundle.get("table_grids", []) if isinstance(row, dict)]
    for task in tasks:
        page = task.get("page")
        kind = str(task.get("kind", ""))
        matches = [row for row in objects if row.get("physical_page") == page]
        if kind.startswith("figure"):
            matches = [row for row in matches if row.get("type") in {"figure", "diagram", "plot", "caption"}]
        elif kind.startswith("table"):
            matches = [row for row in matches if row.get("type") in {"table", "table_cell", "caption"}]
        elif kind.startswith("equation") or kind.startswith("glyph") or kind.startswith("math"):
            matches = [row for row in matches if row.get("type") == "equation_display"]
        elif kind.startswith("page"):
            matches = []
        task["visual_object_ids"] = sorted(str(row["visual_object_id"]) for row in matches if isinstance(row.get("visual_object_id"), str))
        # ``table_cell_ids`` are grid cell IDs, not visual-object IDs.  This
        # keeps a numeric/table claim bound to the exact row/column cell while
        # the object list still carries the cell's geometry/provenance.
        matched_table_ids = {
            str(row.get("visual_object_id"))
            for row in matches
            if row.get("type") == "table"
            and isinstance(row.get("visual_object_id"), str)
        }
        task["table_cell_ids"] = sorted(
            str(cell.get("cell_id"))
            for grid in grids
            if grid.get("physical_page") == page
            and (not matched_table_ids or grid.get("table_visual_object_id") in matched_table_ids)
            for cell in grid.get("cells", [])
            if isinstance(cell, dict) and isinstance(cell.get("cell_id"), str)
        )
        task["series_ids"] = sorted(str(row["visual_object_id"]) for row in matches if row.get("type") == "series" and isinstance(row.get("visual_object_id"), str))
        matched_object_ids = {str(row.get("visual_object_id")) for row in matches if isinstance(row.get("visual_object_id"), str)}
        matched_relations = [
            row for row in relations
            if row.get("source_visual_object_id") in matched_object_ids
            or row.get("target_visual_object_id") in matched_object_ids
        ]
        task["visual_relation_ids"] = sorted(str(row.get("visual_relation_id")) for row in matched_relations if isinstance(row.get("visual_relation_id"), str))
        task["relation_sha256s"] = sorted(str(row.get("relation_sha256")) for row in matched_relations if isinstance(row.get("relation_sha256"), str))
        matched_grid_rows = [
            row for row in grids
            if row.get("table_visual_object_id") in matched_object_ids
            or any(str(cell.get("cell_id")) in set(task["table_cell_ids"]) for cell in row.get("cells", []) if isinstance(cell, dict))
        ]
        task["table_grid_ids"] = sorted(str(row.get("table_grid_id")) for row in matched_grid_rows if isinstance(row.get("table_grid_id"), str))
        task["table_grid_sha256s"] = sorted(str(row.get("grid_sha256")) for row in matched_grid_rows if isinstance(row.get("grid_sha256"), str))
        task["crop_sha256s"] = sorted(str(row.get("crop_sha256")) for row in matches if isinstance(row.get("crop_sha256"), str))
        task["bbox_sha256s"] = sorted(sha256_json(row.get("bbox")) for row in matches if isinstance(row.get("bbox"), list))


def build_semantic_workpack(
    source_path: Path,
    ir_root: Path,
    output: Path,
    *,
    context_characters: int = 800,
    max_focus_characters: int = 20000,
    pdftoppm: Path | None = None,
    render_dpi: int = 150,
    allow_scan_candidates: bool = False,
    semantic_assurance: bool = True,
) -> dict[str, Any]:
    if context_characters < 0 or max_focus_characters < 1:
        raise SemanticPromotionError("invalid_workpack_limits")
    output = _ensure_empty_output(output)
    ir = _load_ir(ir_root)
    reader, source_hash, extractor = _load_source(source_path)
    source_id, scope_start, scope_end, scan_mode = _validate_ir_lineage(
        ir, source_hash, allow_scan_candidates=allow_scan_candidates
    )
    if scope_end > len(reader.pages):
        raise SemanticPromotionError("ir_scope_out_of_source")

    page_texts = _page_texts_for_ir(reader, ir, scope_start, scope_end)
    scan_candidate_for_pages = ir.get("scan_candidate") or {}
    manual_scan_pages = set(
        scan_candidate_for_pages.get("scan_ir", {}).get("manual_review_pages", [])
        if isinstance(scan_candidate_for_pages, dict)
        else []
    )
    if any(not text for page, text in page_texts.items() if page not in manual_scan_pages):
        raise SemanticPromotionError("empty_native_text_in_eligible_scope")

    segments = ir["document_map"].get("segments")
    if not isinstance(segments, list) or not segments:
        raise SemanticPromotionError("ir_segments_missing")
    structure_status = "legacy-string-locator"
    structure_resolution_sha256 = None
    if (
        isinstance(ir.get("heading_candidates"), list)
        and isinstance(ir.get("heading_resolutions"), list)
    ):
        structure_status = "heading-locator-v0.2"
        locations, ordered_segments, unresolved = _locate_headings_from_resolutions(
            segments,
            page_texts,
            ir["heading_candidates"],
            ir["heading_resolutions"],
        )
        structure_resolution_sha256 = locator_bundle_sha256(
            ir["heading_candidates"], ir["heading_resolutions"]
        )
        if unresolved:
            task_path = _write_structure_resolution_tasks(output, source_hash, ir, unresolved)
            if not semantic_assurance:
                code = unresolved[0].get("failure_code") or "heading_locator_ambiguous"
                raise SemanticPromotionError(f"{code}: structured_task={task_path}")
            raise SemanticPromotionError(f"heading_resolution_required: structured_task={task_path}")
        segments = ordered_segments
    else:
        locations = _locate_headings(segments, page_texts)
    candidates_by_segment = {
        row.get("segment_id"): row
        for row in ir["anchor_candidates"]
        if isinstance(row, dict) and isinstance(row.get("segment_id"), str)
    }
    if set(candidates_by_segment) != {segment.get("id") for segment in segments}:
        raise SemanticPromotionError("candidate_segment_coverage_mismatch")
    heading_candidates_by_id = {
        row.get("candidate_id"): row
        for row in (ir.get("heading_candidates") or [])
        if isinstance(row, dict) and isinstance(row.get("candidate_id"), str)
    }

    tasks = _visual_tasks(
        ir["preflight"],
        ir["visual_review"],
        page_texts,
        source_id,
        (ir.get("scan_candidate") or {}).get("quality")
        if isinstance(ir.get("scan_candidate"), dict)
        else None,
    )
    render_profile: dict[str, Any] | None = None
    if pdftoppm is not None:
        render_profile = _render_visual_pages(
            tasks,
            source_path.expanduser().resolve(),
            output,
            pdftoppm,
            render_dpi,
        )

    visual_bundle = _visual_bundle_for_workpack(
        source_path.expanduser().resolve(),
        source_hash,
        tasks,
        render_profile=render_profile,
    )
    _attach_visual_ids_to_tasks(tasks, visual_bundle)

    parent_by_id = {segment["id"]: segment.get("parent_id") for segment in segments}
    title_by_id = {segment["id"]: segment["title"] for segment in segments}
    for ancestor in ir["document_map"].get("context_ancestors", []):
        if isinstance(ancestor, dict) and isinstance(ancestor.get("id"), str):
            title_by_id[ancestor["id"]] = str(ancestor.get("title", ancestor["id"]))
    units: list[dict[str, Any]] = []
    unit_files: list[str] = []
    for index, (segment, location) in enumerate(zip(segments, locations)):
        next_location = locations[index + 1] if index + 1 < len(locations) else None
        boundary_start = (location["page"], location["end"])
        if scan_mode:
            reviewed_pages = segment.get("physical_pages")
            if (
                not isinstance(reviewed_pages, list)
                or not reviewed_pages
                or any(not isinstance(page, int) for page in reviewed_pages)
                or reviewed_pages != list(range(reviewed_pages[0], reviewed_pages[-1] + 1))
                or location["page"] not in reviewed_pages
            ):
                raise SemanticPromotionError(
                    f"scan_segment_page_envelope_invalid: segment={segment['id']}"
                )
            boundary_end = (
                reviewed_pages[-1],
                len(page_texts[reviewed_pages[-1]]),
            )
        else:
            boundary_end = (
                (next_location["page"], next_location["start"])
                if next_location is not None
                else (scope_end, len(page_texts[scope_end]))
            )
        focus = _focus_spans(
            page_texts,
            boundary_start[0],
            boundary_start[1],
            boundary_end[0],
            boundary_end[1],
        )
        total_focus = sum(span["end"] - span["start"] for span in focus)
        if total_focus > max_focus_characters:
            raise SemanticPromotionError(
                f"focus_unit_too_large: segment={segment['id']} characters={total_focus}"
            )
        context = _context_spans(
            page_texts,
            scope_start,
            scope_end,
            focus,
            boundary_start,
            boundary_end,
            context_characters,
        )
        focus_pages = {span["page"] for span in focus}
        linked_tasks = [task["id"] for task in tasks if task["page"] in focus_pages]
        ancestors: list[str] = []
        parent = parent_by_id.get(segment["id"])
        while isinstance(parent, str) and parent in title_by_id:
            ancestors.append(title_by_id[parent])
            parent = parent_by_id.get(parent)
        ancestors.reverse()
        unit_id = stable_id(
            "swu",
            source_id,
            segment["id"],
            [(span["page"], span["start"], span["end"], span["span_sha256"]) for span in focus],
        )
        candidate = candidates_by_segment[segment["id"]]
        default_classification = "knowledge-candidate"
        if not focus:
            default_classification = "structural-only"
        elif re.search(r"\b(?:Bibliography|References|Index)\b", segment["title"], re.IGNORECASE):
            default_classification = "non-knowledge"
        unit = {
            "schema_version": UNIT_SCHEMA,
            "unit_id": unit_id,
            "segment_id": segment["id"],
            "candidate_id": candidate["id"],
            "title": segment["title"],
            "ancestor_path": ancestors,
            "default_classification": default_classification,
            "heading_locator": {
                "resolution_id": location.get("resolution_id"),
                "candidate_id": location.get("candidate_id"),
                "page": location["page"],
                "start": location["start"],
                "end": location["end"],
                "match": location.get("method", "unique-normalized-exact"),
            },
            "focus_spans": focus,
            "context_spans": context,
            "visual_task_ids": linked_tasks,
            "output_contract": DRAFT_SCHEMA,
        }
        selected_heading = heading_candidates_by_id.get(location.get("candidate_id"))
        if isinstance(selected_heading, dict) and selected_heading.get("source_kind") == "scan-ocr-candidate":
            scan_locator = selected_heading.get("scan_locator")
            if not isinstance(scan_locator, dict):
                raise SemanticPromotionError(f"scan_heading_locator_missing: segment={segment['id']}")
            resolution_by_id = {
                row.get("resolution_id"): row
                for row in (ir.get("heading_resolutions") or [])
                if isinstance(row, dict)
            }
            scan_locator = dict(scan_locator)
            scan_review = resolution_by_id.get(location.get("resolution_id"), {}).get("scan_review")
            if not isinstance(scan_review, dict):
                raise SemanticPromotionError(f"scan_heading_review_missing: segment={segment['id']}")
            scan_locator["review_attestation_sha256"] = scan_review["attestation_sha256"]
            scan_locator["reviewer_instance"] = scan_review["reviewer_instance"]
            scan_locator["review_session_id"] = scan_review["review_session_id"]
            for key in (
                "proposer_instance",
                "review_plan_sha256",
                "review_input_sha256",
                "candidate_sha256",
                "external_fragment_sha256",
                "review_independence_basis",
            ):
                if key in scan_review:
                    scan_locator[key] = scan_review[key]
            unit["heading_locator"]["source_kind"] = "scan-ocr-candidate"
            unit["heading_locator"]["scan_locator"] = scan_locator
        relative = f"units/{unit_id}.json"
        _write_json(output / relative, unit)
        unit_files.append(relative)
        units.append(
            {
                "unit_id": unit_id,
                "segment_id": segment["id"],
                "candidate_id": candidate["id"],
                "path": relative,
                "default_classification": default_classification,
                "focus_character_count": total_focus,
            }
        )

    task_path = output / "visual-tasks.jsonl"
    _write_jsonl(task_path, tasks)
    visual_paths = write_visual_bundle(output, visual_bundle)
    pages_path = output / "page-index.jsonl"
    _write_jsonl(
        pages_path,
        [
            {
                "page": page,
                "page_text_sha256": sha256_text(text),
                "character_count": len(text),
                "pdf_page_label": str(reader.page_labels[page - 1]),
            }
            for page, text in page_texts.items()
        ],
    )
    (output / "drafts").mkdir(parents=True, exist_ok=True)
    os.chmod(output / "drafts", 0o700)
    _write_jsonl(output / "review-records.jsonl", [])
    _write_jsonl(output / "visual-receipts.jsonl", [])
    if semantic_assurance:
        initialize_workpack_assurance(output)
    scan_manifest: dict[str, Any] | None = None
    if scan_mode:
        scan_candidate = ir.get("scan_candidate")
        if not isinstance(scan_candidate, dict):
            raise SemanticPromotionError("scan_candidate_missing")
        primary = scan_candidate["scan_ir"]["backend_selection"]["primary"]
        scan_transcripts = [
            row
            for row in scan_candidate.get("transcripts", [])
            if isinstance(row, dict) and row.get("backend_id") == primary
        ]
        scan_observations = [
            row
            for row in scan_candidate.get("observations", [])
            if isinstance(row, dict) and row.get("backend", {}).get("id") == primary
        ]
        scan_conflicts = scan_candidate.get("conflicts", [])
        scan_backend_receipts = scan_candidate.get("backend_receipts", [])
        scan_transforms = scan_candidate.get("transforms", [])
        scan_root = scan_candidate["root"]
        scan_render_receipts = load_jsonl(scan_root / "render-receipts.jsonl")
        scan_runtime_contracts = scan_candidate["scan_ir"].get("runtime_contracts", [])
        _write_jsonl(output / "scan-candidate-transcripts.jsonl", scan_transcripts)
        _write_jsonl(output / "scan-candidate-observations.jsonl", scan_observations)
        _write_jsonl(output / "scan-candidate-conflicts.jsonl", scan_conflicts)
        _write_jsonl(output / "scan-candidate-backend-receipts.jsonl", scan_backend_receipts)
        _write_jsonl(output / "scan-candidate-transforms.jsonl", scan_transforms)
        _write_jsonl(output / "scan-candidate-render-receipts.jsonl", scan_render_receipts)
        _write_jsonl(output / "scan-candidate-runtime-contracts.jsonl", scan_runtime_contracts)
        reviewed_structure = scan_candidate["scan_ir"].get("reviewed_scan_structure")
        scan_manifest = {
            "schema_version": "tkc.scanned-pdf-workpack-candidate/v0.2",
            "source_sha256": source_hash,
            "scan_ir_sha256": sha256_file(scan_root / "scan-ir.json"),
            "backend_selection": scan_candidate["scan_ir"]["backend_selection"],
            "ocr_pages": scan_candidate["scan_ir"]["ocr_pages"],
            "transcripts": "scan-candidate-transcripts.jsonl",
            "observations": "scan-candidate-observations.jsonl",
            "conflicts": "scan-candidate-conflicts.jsonl",
            "backend_receipts": "scan-candidate-backend-receipts.jsonl",
            "transforms": "scan-candidate-transforms.jsonl",
            "render_receipts": "scan-candidate-render-receipts.jsonl",
            "runtime_contracts": "scan-candidate-runtime-contracts.jsonl",
            "component_sha256": {
                "transcripts": sha256_file(output / "scan-candidate-transcripts.jsonl"),
                "observations": sha256_file(output / "scan-candidate-observations.jsonl"),
                "conflicts": sha256_file(output / "scan-candidate-conflicts.jsonl"),
                "backend_receipts": sha256_file(output / "scan-candidate-backend-receipts.jsonl"),
                "transforms": sha256_file(output / "scan-candidate-transforms.jsonl"),
                "render_receipts": sha256_file(output / "scan-candidate-render-receipts.jsonl"),
                "runtime_contracts": sha256_file(output / "scan-candidate-runtime-contracts.jsonl"),
            },
            "structure_review_sha256": reviewed_structure.get("structure_sha256") if isinstance(reviewed_structure, dict) else None,
            "structure_review_status": reviewed_structure.get("status") if isinstance(reviewed_structure, dict) else None,
            "structure_reviewer": reviewed_structure.get("reviewer") if isinstance(reviewed_structure, dict) else None,
            "candidate_only": True,
            "promotable": False,
            "executable": False,
        }
    workpack_identity = [
        source_id,
        source_hash,
        scope_start,
        scope_end,
        ir["file_hashes"],
        NORMALIZATION_PROFILE,
    ]
    if semantic_assurance:
        workpack_identity.append(SEMANTIC_ASSURANCE_PROTOCOL)
    workpack_id = stable_id("swp", *workpack_identity)
    manifest = {
        "schema_version": WORKPACK_SCHEMA,
        "workpack_id": workpack_id,
        "source": {
            "source_id": source_id,
            "sha256": source_hash,
            "scope": {"start_page": scope_start, "end_page": scope_end},
        },
        "pdf_ir": {
            "schema_version": ir["preflight"].get("schema_version"),
            "file_hashes": ir["file_hashes"],
            **(
                {
                    "receipt_status": "bound-v0.2",
                    "parser_receipt_sha256": sha256_json(ir["parser_receipt"]),
                    "layout_parser": ir["parser_receipt"]["layout_parser"],
                    "render_receipts_sha256": sha256_json(ir["render_receipts"]),
                }
                if ir["parser_receipt"] is not None
                else {"receipt_status": "legacy-unbound"}
            ),
        },
        "structure": {
            "protocol": structure_status,
            "status": "resolved",
            "heading_resolution_bundle_sha256": structure_resolution_sha256,
            "unresolved_resolution_count": 0,
        },
        **({"scan_candidate": scan_manifest} if scan_manifest is not None else {}),
        **(
            {"semantic_assurance": assurance_manifest_block()}
            if semantic_assurance
            else {}
        ),
        "hash_profile": {
            "normalization": NORMALIZATION_PROFILE,
            "offset_unit": "unicode-code-point",
            "interval": "[start,end)",
            "extractor": extractor,
        },
        "policy": {
            "distribution": "ephemeral-local",
            "shareable": False,
            "contains_source_content": True,
            "final_skill_must_exclude_workpack": True,
            "model_may_only_propose": True,
            "executable_capability_allowed": False,
        },
        "limits": {
            "context_characters": context_characters,
            "max_focus_characters": max_focus_characters,
        },
        "render_profile": render_profile,
        "page_index": "page-index.jsonl",
        "visual_tasks": "visual-tasks.jsonl",
        "visual_semantics": {
            "protocol": VISUAL_PROTOCOL,
            "candidate_only": True,
            "whole_book_completeness_claimed": False,
            "review_attestation_required": bool(visual_bundle.get("objects")),
            "objects": visual_paths["objects"],
            "relations": visual_paths["relations"],
            "table_grids": visual_paths["table_grids"],
            "conflicts": visual_paths["conflicts"],
            "render_receipts": [
                {
                    "physical_page": int(row.get("physical_page")),
                    "render_sha256": row.get("render_sha256"),
                    "dpi": row.get("dpi"),
                    "rotation": row.get("rotation", 0),
                }
                for row in visual_bundle.get("render_receipts", [])
                if isinstance(row, dict)
            ],
            "unavailable": bool(visual_bundle.get("unavailable", False)),
        },
        "units": units,
        "drafts_directory": "drafts",
    }
    _write_json(output / "workpack.json", manifest)
    return manifest


def _add_issue(
    issues: list[PromotionIssue],
    severity: str,
    code: str,
    path: str,
    message: str,
) -> None:
    issues.append(PromotionIssue(severity, code, path, message))


def _recursive_prohibited_keys(value: Any, path: str = "") -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else key
            if key in PROHIBITED_DRAFT_KEYS:
                hits.append((child, key))
            hits.extend(_recursive_prohibited_keys(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            hits.extend(_recursive_prohibited_keys(item, f"{path}[{index}]"))
    return hits


def _contains_interval(container: dict[str, Any], page: int, start: int, end: int) -> bool:
    return (
        container.get("page") == page
        and isinstance(container.get("start"), int)
        and isinstance(container.get("end"), int)
        and container["start"] <= start < end <= container["end"]
    )


def _validate_span_against_source(
    span: dict[str, Any],
    page_texts: dict[int, str],
    focus: list[dict[str, Any]],
    issues: list[PromotionIssue],
    path: str,
) -> None:
    page = span.get("page")
    start = span.get("start")
    end = span.get("end")
    expected_hash = span.get("span_sha256")
    if not isinstance(page, int) or page not in page_texts:
        _add_issue(issues, "error", "evidence_scope_escape", path, "Unknown source page.")
        return
    if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(page_texts[page]):
        _add_issue(issues, "error", "evidence_span_invalid", path, "Invalid [start,end) interval.")
        return
    if not any(_contains_interval(container, page, start, end) for container in focus):
        _add_issue(
            issues,
            "error",
            "evidence_scope_escape",
            path,
            "Evidence must remain inside an extractable focus span, not context.",
        )
    actual_hash = sha256_text(page_texts[page][start:end])
    if expected_hash != actual_hash:
        _add_issue(
            issues,
            "error",
            "evidence_hash_mismatch",
            path,
            f"Expected recomputed span hash {actual_hash}.",
        )


def _draft_object_ref(unit_id: str, local_id: str) -> str:
    return f"{unit_id}:{local_id}"


def _load_workpack_drafts(root: Path, manifest: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    try:
        drafts_root = _resolve_workpack_path(
            root, manifest.get("drafts_directory", "drafts")
        )
    except SemanticPromotionError:
        return {}, {}
    drafts: dict[str, dict[str, Any]] = {}
    paths: dict[str, str] = {}
    if not drafts_root.is_dir():
        return drafts, paths
    for path in sorted(drafts_root.glob("*.json")):
        try:
            draft = load_json(path)
        except Exception:
            continue
        if isinstance(draft, dict) and isinstance(draft.get("unit_id"), str):
            drafts[draft["unit_id"]] = draft
            paths[draft["unit_id"]] = path.relative_to(root).as_posix()
    return drafts, paths


def validate_semantic_workpack(
    source_path: Path,
    workpack_root: Path,
    *,
    require_complete_drafts: bool = True,
) -> tuple[list[PromotionIssue], dict[str, Any]]:
    root = workpack_root.expanduser().resolve()
    issues: list[PromotionIssue] = []
    manifest_path = root / "workpack.json"
    if not manifest_path.is_file():
        return [PromotionIssue("error", "workpack_missing", str(manifest_path), "Missing workpack.json.")], {}
    try:
        manifest = load_json(manifest_path)
    except Exception as error:
        return [PromotionIssue("error", "workpack_invalid_json", str(manifest_path), str(error))], {}
    if not isinstance(manifest, dict) or manifest.get("schema_version") != WORKPACK_SCHEMA:
        _add_issue(issues, "error", "workpack_schema_invalid", "workpack.json", "Unexpected schema.")
        return issues, {}
    try:
        reader, source_hash, extractor = _load_source(source_path)
    except SemanticPromotionError as error:
        _add_issue(issues, "error", "source_unavailable", str(source_path), str(error))
        return issues, {}
    source_record = manifest.get("source")
    if not isinstance(source_record, dict) or source_record.get("sha256") != source_hash:
        _add_issue(issues, "error", "source_hash_mismatch", "workpack.source", "Source SHA-256 differs.")
        return issues, {}
    scope = source_record.get("scope")
    if not isinstance(scope, dict) or not isinstance(scope.get("start_page"), int) or not isinstance(scope.get("end_page"), int):
        _add_issue(issues, "error", "workpack_scope_invalid", "workpack.source.scope", "Invalid scope.")
        return issues, {}
    scope_start, scope_end = scope["start_page"], scope["end_page"]
    if not 1 <= scope_start <= scope_end <= len(reader.pages):
        _add_issue(issues, "error", "workpack_scope_invalid", "workpack.source.scope", "Scope escapes PDF.")
        return issues, {}
    try:
        page_texts = _page_texts_for_workpack(
            reader, root, manifest, scope_start, scope_end
        )
    except (SemanticPromotionError, OSError, ValueError) as error:
        _add_issue(
            issues,
            "error",
            "scan_candidate_texts_invalid",
            "workpack.scan_candidate",
            str(error),
        )
        page_texts = {
            page: normalize_text(reader.pages[page - 1].extract_text() or "")
            for page in range(scope_start, scope_end + 1)
        }
    try:
        scan_evidence = _load_workpack_scan_evidence(
            root,
            manifest,
            require_hardened=isinstance(manifest.get("scan_candidate"), dict)
            and manifest["scan_candidate"].get("schema_version")
            == "tkc.scanned-pdf-workpack-candidate/v0.2",
        )
    except SemanticPromotionError as error:
        scan_evidence = None
        _add_issue(issues, "error", "scan_candidate_evidence_invalid", "workpack.scan_candidate", str(error))
    hash_profile = manifest.get("hash_profile")
    if not isinstance(hash_profile, dict) or hash_profile.get("normalization") != NORMALIZATION_PROFILE:
        _add_issue(issues, "error", "hash_profile_unsupported", "workpack.hash_profile", "Unexpected normalization.")
    elif hash_profile.get("extractor") != extractor:
        _add_issue(
            issues,
            "warning",
            "extractor_runtime_changed",
            "workpack.hash_profile.extractor",
            f"Workpack used {hash_profile.get('extractor')}; validation used {extractor}.",
        )
    policy = manifest.get("policy")
    if not isinstance(policy, dict) or policy.get("model_may_only_propose") is not True or policy.get("executable_capability_allowed") is not False:
        _add_issue(issues, "error", "capability_premature", "workpack.policy", "Phase 2 must be proposal-only and non-executable.")

    try:
        tasks = load_jsonl(
            _resolve_workpack_path(
                root, manifest.get("visual_tasks", "visual-tasks.jsonl")
            )
        )
    except Exception as error:
        _add_issue(issues, "error", "visual_tasks_invalid", "visual-tasks.jsonl", str(error))
        tasks = []
    task_by_id: dict[str, dict[str, Any]] = {}
    page_asset_bindings: dict[int, tuple[Any, Any]] = {}
    for index, task in enumerate(tasks):
        path = f"visual-tasks.jsonl:{index + 1}"
        if not isinstance(task, dict) or not isinstance(task.get("id"), str):
            _add_issue(issues, "error", "visual_task_invalid", path, "Task ID is required.")
            continue
        if task["id"] in task_by_id:
            _add_issue(issues, "error", "visual_task_duplicate", path, task["id"])
        task_by_id[task["id"]] = task
        page = task.get("page")
        if not isinstance(page, int) or page not in page_texts:
            _add_issue(issues, "error", "visual_task_scope_escape", path, "Task page escapes scope.")
        asset_path = task.get("page_asset")
        asset_hash = task.get("page_asset_sha256")
        if asset_path is None:
            if asset_hash is not None:
                _add_issue(
                    issues,
                    "error",
                    "render_binding_incomplete",
                    path,
                    "A render hash cannot be declared without its page asset.",
                )
        else:
            if isinstance(page, int) and asset_path != f"renders/page-{page:04d}.png":
                _add_issue(
                    issues,
                    "error",
                    "render_path_noncanonical",
                    path,
                    f"Expected renders/page-{page:04d}.png for physical page {page}.",
                )
            asset = root / str(asset_path)
            try:
                asset.resolve().relative_to(root)
            except ValueError:
                _add_issue(issues, "error", "unsafe_render_path", path, str(asset_path))
            else:
                if not asset.is_file() or not isinstance(asset_hash, str) or sha256_file(asset) != asset_hash:
                    _add_issue(issues, "error", "render_hash_mismatch", path, str(asset_path))
                elif not _is_png(asset):
                    _add_issue(
                        issues,
                        "error",
                        "render_format_invalid",
                        path,
                        f"Expected a decodable PNG envelope: {asset_path}",
                    )
        if isinstance(page, int) and page in page_texts:
            binding = (asset_path, asset_hash)
            previous = page_asset_bindings.setdefault(page, binding)
            if previous != binding:
                _add_issue(
                    issues,
                    "error",
                    "visual_page_asset_mismatch",
                    path,
                    "All visual tasks for one physical page must share the same canonical render asset and hash.",
                )

    visual_manifest = manifest.get("visual_semantics")
    visual_bundle: dict[str, Any] | None = None
    if isinstance(visual_manifest, dict) and visual_manifest.get("protocol") == VISUAL_PROTOCOL:
        visual_rows: dict[str, list[Any]] = {}
        visual_key_paths = {
            "objects": "visual-objects.jsonl",
            "relations": "visual-relations.jsonl",
            "table_grids": "table-grids.jsonl",
            "conflicts": "visual-conflicts.jsonl",
        }
        for key, default_path in visual_key_paths.items():
            relative = visual_manifest.get(key, {}).get("path", default_path) if isinstance(visual_manifest.get(key), dict) else default_path
            try:
                visual_rows[key] = load_jsonl(_resolve_workpack_path(root, relative))
            except Exception as error:
                _add_issue(issues, "error", "visual_contract_missing", str(relative), str(error))
                visual_rows[key] = []
        visual_bundle = {
            "schema_version": VISUAL_PROTOCOL,
            "source_sha256": source_hash,
            **visual_rows,
        }
        for visual_issue in validate_visual_bundle(visual_bundle, source_sha256=source_hash, require_candidate_only=True):
            _add_issue(issues, visual_issue.severity, visual_issue.code, visual_issue.path, visual_issue.message)
        object_ids = {str(row.get("visual_object_id")) for row in visual_rows["objects"] if isinstance(row, dict)}
        table_cell_ids = {
            str(cell.get("cell_id"))
            for grid in visual_rows["table_grids"]
            if isinstance(grid, dict)
            for cell in grid.get("cells", [])
            if isinstance(cell, dict) and isinstance(cell.get("cell_id"), str)
        }
        for index, task in enumerate(tasks):
            for field in ("visual_object_ids", "series_ids"):
                values = task.get(field, [])
                if not isinstance(values, list) or any(str(value) not in object_ids for value in values):
                    _add_issue(issues, "error", "visual_task_object_unresolved", f"visual-tasks.jsonl:{index + 1}.{field}", "Task references an absent visual object.")
            values = task.get("table_cell_ids", [])
            if not isinstance(values, list) or any(str(value) not in table_cell_ids for value in values):
                _add_issue(issues, "error", "visual_task_cell_unresolved", f"visual-tasks.jsonl:{index + 1}.table_cell_ids", "Task references an absent table-grid cell.")

    units_index = manifest.get("units")
    if not isinstance(units_index, list):
        _add_issue(issues, "error", "unit_index_invalid", "workpack.units", "Expected an array.")
        units_index = []
    units: dict[str, dict[str, Any]] = {}
    candidate_ids: set[str] = set()
    for index, entry in enumerate(units_index):
        path = f"workpack.units[{index}]"
        if not isinstance(entry, dict) or not isinstance(entry.get("unit_id"), str) or not isinstance(entry.get("path"), str):
            _add_issue(issues, "error", "unit_index_entry_invalid", path, "unit_id and path are required.")
            continue
        try:
            unit_path = _resolve_workpack_path(root, entry["path"])
        except SemanticPromotionError:
            _add_issue(issues, "error", "unsafe_unit_path", path, entry["path"])
            continue
        if not unit_path.is_file():
            _add_issue(issues, "error", "unit_missing", entry["path"], "Unit file is missing.")
            continue
        try:
            unit = load_json(unit_path)
        except Exception as error:
            _add_issue(issues, "error", "unit_invalid_json", entry["path"], str(error))
            continue
        unit_id = entry["unit_id"]
        if not isinstance(unit, dict) or unit.get("schema_version") != UNIT_SCHEMA or unit.get("unit_id") != unit_id:
            _add_issue(issues, "error", "unit_schema_invalid", entry["path"], "Unit identity/schema mismatch.")
            continue
        if unit_id in units:
            _add_issue(issues, "error", "unit_duplicate", path, unit_id)
        units[unit_id] = unit
        candidate_id = unit.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id in candidate_ids:
            _add_issue(issues, "error", "candidate_coverage_invalid", entry["path"], str(candidate_id))
        else:
            candidate_ids.add(candidate_id)
        for kind in ("focus_spans", "context_spans"):
            spans = unit.get(kind)
            if not isinstance(spans, list):
                _add_issue(issues, "error", "unit_spans_invalid", f"{entry['path']}.{kind}", "Expected an array.")
                continue
            for span_index, span in enumerate(spans):
                span_path = f"{entry['path']}.{kind}[{span_index}]"
                if not isinstance(span, dict):
                    _add_issue(issues, "error", "unit_span_invalid", span_path, "Expected an object.")
                    continue
                page, start, end = span.get("page"), span.get("start"), span.get("end")
                if not isinstance(page, int) or page not in page_texts or not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(page_texts.get(page, "")):
                    _add_issue(issues, "error", "unit_span_invalid", span_path, "Invalid source interval.")
                    continue
                actual_text = page_texts[page][start:end]
                if span.get("text") != actual_text or span.get("span_sha256") != sha256_text(actual_text):
                    _add_issue(issues, "error", "unit_span_stale", span_path, "Stored text/hash differs from source.")
                if span.get("page_text_sha256") != sha256_text(page_texts[page]):
                    _add_issue(issues, "error", "page_text_hash_mismatch", span_path, "Page text hash differs.")
                expected_extractable = kind == "focus_spans"
                if span.get("extractable") is not expected_extractable:
                    _add_issue(issues, "error", "unit_span_policy_invalid", span_path, "Extractable flag differs from span role.")
        for task_id in unit.get("visual_task_ids", []):
            if task_id not in task_by_id:
                _add_issue(issues, "error", "visual_task_unresolved", entry["path"], str(task_id))

    try:
        drafts_root = _resolve_workpack_path(
            root, manifest.get("drafts_directory", "drafts")
        )
    except SemanticPromotionError as error:
        _add_issue(
            issues,
            "error",
            "unsafe_drafts_path",
            "workpack.drafts_directory",
            str(error),
        )
        drafts_root = root / "__invalid_drafts_directory__"
    draft_files = sorted(drafts_root.glob("*.json")) if drafts_root.is_dir() else []
    drafts: dict[str, dict[str, Any]] = {}
    draft_path_by_unit: dict[str, str] = {}
    all_objects: dict[str, tuple[dict[str, Any], str]] = {}
    dispositions: dict[str, str] = {}
    for draft_path in draft_files:
        relative = draft_path.relative_to(root).as_posix()
        try:
            draft = load_json(draft_path)
        except Exception as error:
            _add_issue(issues, "error", "draft_invalid_json", relative, str(error))
            continue
        if not isinstance(draft, dict) or draft.get("schema_version") != DRAFT_SCHEMA:
            _add_issue(issues, "error", "draft_schema_invalid", relative, "Unexpected schema.")
            continue
        unit_id = draft.get("unit_id")
        if not isinstance(unit_id, str) or unit_id not in units:
            _add_issue(issues, "error", "draft_unit_unresolved", relative, str(unit_id))
            continue
        if unit_id in drafts:
            _add_issue(issues, "error", "draft_unit_duplicate", relative, unit_id)
        drafts[unit_id] = draft
        draft_path_by_unit[unit_id] = relative
        for key_path, key in _recursive_prohibited_keys(draft):
            _add_issue(
                issues,
                "error",
                "draft_source_text_leak",
                f"{relative}.{key_path}",
                f"Prohibited field {key!r}; drafts may return coordinates, not source text or self-review.",
            )
        proposer = draft.get("proposer_instance")
        if not isinstance(proposer, str) or not proposer:
            _add_issue(issues, "error", "proposer_identity_missing", relative, "proposer_instance is required.")
        disposition = draft.get("candidate_disposition")
        if not isinstance(disposition, dict) or disposition.get("action") not in DISPOSITIONS or not isinstance(disposition.get("reason"), str) or not disposition.get("reason"):
            _add_issue(issues, "error", "candidate_disposition_invalid", relative, "Action and reason are required.")
            action = "invalid"
        else:
            action = disposition["action"]
        dispositions[unit_id] = action
        objects = draft.get("objects")
        if not isinstance(objects, list):
            _add_issue(issues, "error", "draft_objects_invalid", relative, "objects must be an array.")
            objects = []
        if action == "promote" and not objects and not draft.get("gaps") and not draft.get("conflicts"):
            _add_issue(issues, "error", "empty_promoted_unit", relative, "Promote requires an object, gap, or conflict.")
        if action in {"context-only", "non-knowledge", "reject"} and objects:
            _add_issue(issues, "error", "disposition_object_conflict", relative, "Non-promoted units cannot emit knowledge objects.")
        local_seen: set[str] = set()
        unit = units[unit_id]
        focus = unit.get("focus_spans", [])
        for object_index, obj in enumerate(objects):
            object_path = f"{relative}.objects[{object_index}]"
            if not isinstance(obj, dict):
                _add_issue(issues, "error", "proposal_schema_invalid", object_path, "Expected an object.")
                continue
            local_id = obj.get("local_id")
            if not isinstance(local_id, str) or not local_id or local_id in local_seen:
                _add_issue(issues, "error", "proposal_id_invalid", object_path, str(local_id))
                continue
            local_seen.add(local_id)
            reference = _draft_object_ref(unit_id, local_id)
            all_objects[reference] = (obj, object_path)
            if obj.get("type") not in KNOWLEDGE_TYPES:
                _add_issue(issues, "error", "proposal_type_invalid", object_path, str(obj.get("type")))
            if obj.get("origin") not in ORIGINS - {"knowledge-gap"}:
                _add_issue(issues, "error", "claim_origin_misclassified", object_path, str(obj.get("origin")))
            for field in ("title", "statement"):
                if not isinstance(obj.get(field), str) or not obj.get(field):
                    _add_issue(issues, "error", "proposal_schema_invalid", f"{object_path}.{field}", "Non-empty string required.")
            for field in ("assumptions", "valid_when", "fails_when"):
                if not isinstance(obj.get(field), list) or any(not isinstance(item, str) for item in obj.get(field, [])):
                    _add_issue(issues, "error", "proposal_schema_invalid", f"{object_path}.{field}", "Array of strings required.")
            evidence = obj.get("evidence_spans")
            if not isinstance(evidence, list) or not evidence:
                _add_issue(issues, "error", "proposal_unanchored", object_path, "At least one evidence span is required.")
                evidence = []
            for span_index, span in enumerate(evidence):
                if not isinstance(span, dict):
                    _add_issue(issues, "error", "evidence_span_invalid", f"{object_path}.evidence_spans[{span_index}]", "Expected an object.")
                    continue
                _validate_span_against_source(
                    span,
                    page_texts,
                    focus,
                    issues,
                    f"{object_path}.evidence_spans[{span_index}]",
                )
            applicability = obj.get("applicability")
            if not isinstance(applicability, dict):
                _add_issue(issues, "error", "applicability_missing", object_path, "Structured applicability is required.")
            else:
                pages = applicability.get("physical_pages")
                excludes = applicability.get("excluded_conclusions")
                if not isinstance(pages, list) or len(pages) != 2 or any(not isinstance(page, int) for page in pages) or pages[0] < scope_start or pages[1] > scope_end or pages[0] > pages[1]:
                    _add_issue(issues, "error", "applicability_overbroad", object_path, "Applicability pages escape source scope.")
                if not isinstance(excludes, list) or any(not isinstance(item, str) for item in excludes):
                    _add_issue(issues, "error", "applicability_missing", object_path, "excluded_conclusions must be explicit.")
            task_ids = obj.get("visual_task_ids")
            if not isinstance(task_ids, list) or any(task_id not in task_by_id for task_id in task_ids):
                _add_issue(issues, "error", "visual_task_unresolved", object_path, "Unknown visual task ID.")
                task_ids = []
            evidence_pages = {
                span.get("page")
                for span in evidence
                if isinstance(span, dict) and isinstance(span.get("page"), int)
            }
            mismatched_tasks = [
                task_id
                for task_id in task_ids
                if task_by_id[task_id].get("page") not in evidence_pages
            ]
            if mismatched_tasks:
                _add_issue(
                    issues,
                    "error",
                    "visual_task_evidence_page_mismatch",
                    object_path,
                    f"Visual tasks must be on an evidence-span page: {mismatched_tasks}",
                )
            formula = obj.get("formula")
            if obj.get("type") == "Equation":
                if not isinstance(formula, dict):
                    _add_issue(issues, "error", "formula_contract_missing", object_path, "Equation proposals require formula metadata.")
                else:
                    relation = formula.get("representation_relation")
                    if relation not in FORMULA_RELATIONS:
                        _add_issue(issues, "error", "formula_contract_invalid", object_path, "Unknown representation relation.")
                    source_notation = formula.get("source_notation")
                    normalized_notation = formula.get("normalized_notation")
                    derived_notation = formula.get("derived_notation")
                    if obj.get("origin") == "source-explicit" and not isinstance(source_notation, str):
                        _add_issue(issues, "error", "formula_source_notation_missing", object_path, "Source-explicit equation needs source_notation.")
                    if obj.get("origin") == "compiler-derived" and not isinstance(derived_notation, str):
                        _add_issue(issues, "error", "compiler_inference_misattributed", object_path, "Compiler-derived equation needs derived_notation.")
                    if relation == "identical" and isinstance(source_notation, str) and isinstance(normalized_notation, str) and normalize_text(source_notation) != normalize_text(normalized_notation):
                        _add_issue(issues, "error", "formula_silent_mutation", object_path, "Non-identical forms cannot be labeled identical.")
                    ast_value = formula.get("ast")
                    if ast_value is not None:
                        try:
                            validate_formula_ast(ast_value)
                        except FormulaContractError as error:
                            _add_issue(issues, "error", "formula_ast_invalid", object_path, str(error))
                formula_task_kinds = {
                    task_by_id[task_id].get("kind") for task_id in task_ids if task_id in task_by_id
                }
                if not formula_task_kinds & {"equation", "glyph-check", "math-page-check"}:
                    _add_issue(issues, "error", "formula_visual_receipt_missing", object_path, "Equation must link a formula visual task.")
            elif formula is not None and not isinstance(formula, dict):
                _add_issue(issues, "error", "formula_contract_invalid", object_path, "formula must be an object or null.")
            if isinstance(obj.get("statement"), str) and obj.get("origin") != "compiler-derived":
                statement_numbers = set(NUMBER_PATTERN.findall(obj["statement"]))
                if statement_numbers and evidence:
                    evidence_numbers: set[str] = set()
                    for span in evidence:
                        if isinstance(span, dict) and isinstance(span.get("page"), int) and isinstance(span.get("start"), int) and isinstance(span.get("end"), int):
                            evidence_numbers.update(NUMBER_PATTERN.findall(page_texts[span["page"]][span["start"]:span["end"]]))
                    untraced = statement_numbers - evidence_numbers
                    if untraced:
                        _add_issue(issues, "warning", "numeric_token_untraced", object_path, f"Numbers absent from evidence spans: {sorted(untraced)}")

        for field in ("relations", "conflicts", "gaps"):
            if not isinstance(draft.get(field), list):
                _add_issue(issues, "error", "draft_collection_invalid", f"{relative}.{field}", "Expected an array.")

    if require_complete_drafts:
        missing = set(units) - set(drafts)
        if missing:
            _add_issue(issues, "error", "draft_coverage_incomplete", "drafts", f"Missing units: {sorted(missing)}")
    if set(drafts) - set(units):
        _add_issue(issues, "error", "draft_unit_unresolved", "drafts", "Draft references an unknown unit.")

    all_relations: list[tuple[dict[str, Any], str]] = []
    all_conflicts: list[tuple[dict[str, Any], str]] = []
    for unit_id, draft in drafts.items():
        relative = draft_path_by_unit[unit_id]
        conflict_local_ids: set[str] = set()
        for index, relation in enumerate(draft.get("relations", [])):
            all_relations.append((relation, f"{relative}.relations[{index}]"))
        for index, conflict in enumerate(draft.get("conflicts", [])):
            conflict_path = f"{relative}.conflicts[{index}]"
            all_conflicts.append((conflict, conflict_path))
            local_id = conflict.get("local_id") if isinstance(conflict, dict) else None
            if (
                not isinstance(local_id, str)
                or not local_id
                or local_id in conflict_local_ids
            ):
                _add_issue(
                    issues,
                    "error",
                    "conflict_id_invalid",
                    conflict_path,
                    "Conflict local_id must be non-empty and unique within its unit.",
                )
            else:
                conflict_local_ids.add(local_id)
        for index, gap in enumerate(draft.get("gaps", [])):
            gap_path = f"{relative}.gaps[{index}]"
            if not isinstance(gap, dict) or gap.get("origin") != "knowledge-gap" or not isinstance(gap.get("statement"), str) or not gap.get("statement"):
                _add_issue(issues, "error", "knowledge_gap_invalid", gap_path, "Gap requires origin and statement.")
                continue
            evidence = gap.get("evidence_spans")
            if not isinstance(evidence, list) or not evidence:
                _add_issue(
                    issues,
                    "error",
                    "knowledge_gap_evidence_missing",
                    gap_path,
                    "A unit-local gap observation requires a source-bound focus span.",
                )
                continue
            focus = units[unit_id].get("focus_spans", [])
            for span_index, span in enumerate(evidence):
                if not isinstance(span, dict):
                    _add_issue(
                        issues,
                        "error",
                        "evidence_span_invalid",
                        f"{gap_path}.evidence_spans[{span_index}]",
                        "Expected an object.",
                    )
                    continue
                _validate_span_against_source(
                    span,
                    page_texts,
                    focus,
                    issues,
                    f"{gap_path}.evidence_spans[{span_index}]",
                )

    for relation, path in all_relations:
        if not isinstance(relation, dict):
            _add_issue(issues, "error", "relation_invalid", path, "Expected an object.")
            continue
        source_ref = relation.get("source_ref")
        target_ref = relation.get("target_ref")
        if source_ref not in all_objects or target_ref not in all_objects:
            _add_issue(issues, "error", "relation_object_unresolved", path, "Relation endpoints must resolve.")
        if relation.get("type") not in RELATION_TYPES:
            _add_issue(issues, "error", "relation_type_invalid", path, str(relation.get("type")))

    conflicted_refs: set[str] = set()
    for conflict, path in all_conflicts:
        if not isinstance(conflict, dict) or not isinstance(conflict.get("local_id"), str) or not isinstance(conflict.get("topic"), str):
            _add_issue(issues, "error", "conflict_invalid", path, "Conflict identity/topic required.")
            continue
        claims = conflict.get("claim_refs")
        if (
            not isinstance(claims, list)
            or len(claims) < 2
            or any(not isinstance(ref, str) for ref in claims)
            or len(claims) != len(set(claims))
            or any(ref not in all_objects for ref in claims)
        ):
            _add_issue(issues, "error", "conflict_claims_invalid", path, "At least two distinct resolved claim refs required.")
        else:
            conflicted_refs.update(claims)
        if conflict.get("recommended_status") not in {"unresolved", "mitigated"}:
            _add_issue(issues, "error", "conflict_status_invalid", path, "Phase 2 may propose unresolved or mitigated only.")
        if conflict.get("requires_review") is not True:
            _add_issue(issues, "error", "conflict_review_missing", path, "Conflict review must be required.")

    for reference, (obj, path) in all_objects.items():
        formula = obj.get("formula")
        if isinstance(formula, dict) and (
            formula.get("representation_relation") == "conflicted"
            or formula.get("dimension_check") == "inconsistent"
        ) and reference not in conflicted_refs:
            _add_issue(issues, "error", "formula_dimension_conflict_unrecorded", path, "Conflicted/inconsistent formula requires a conflict proposal.")

    summary = {
        "workpack_id": manifest.get("workpack_id"),
        "source_id": source_record.get("source_id"),
        "scope": scope,
        "unit_count": len(units),
        "draft_count": len(drafts),
        "object_count": len(all_objects),
        "relation_count": len(all_relations),
        "conflict_count": len(all_conflicts),
        "visual_task_count": len(tasks),
        "rendered_visual_task_count": sum(bool(task.get("page_asset")) for task in tasks),
        "visual_object_count": len(visual_bundle.get("objects", [])) if isinstance(visual_bundle, dict) else 0,
        "visual_relation_count": len(visual_bundle.get("relations", [])) if isinstance(visual_bundle, dict) else 0,
        "table_grid_count": len(visual_bundle.get("table_grids", [])) if isinstance(visual_bundle, dict) else 0,
        "visual_conflict_count": len(visual_bundle.get("conflicts", [])) if isinstance(visual_bundle, dict) else 0,
        "candidate_dispositions": {
            action: sum(value == action for value in dispositions.values())
            for action in sorted(DISPOSITIONS)
        },
    }
    return sorted(issues, key=lambda item: (item.severity != "error", item.code, item.path)), summary


def _review_material(
    source_hash: str,
    manifest: dict[str, Any],
    units: dict[str, dict[str, Any]],
    tasks: list[dict[str, Any]],
    drafts: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    objects: dict[str, dict[str, Any]] = {}
    object_proposers: dict[str, str] = {}
    queue: list[dict[str, Any]] = []

    for unit_id in sorted(drafts):
        draft = drafts[unit_id]
        proposer = str(draft.get("proposer_instance", ""))
        for obj in draft.get("objects", []):
            if not isinstance(obj, dict) or not isinstance(obj.get("local_id"), str):
                continue
            reference = _draft_object_ref(unit_id, obj["local_id"])
            objects[reference] = obj
            object_proposers[reference] = proposer

    def add_item(
        item_ref: str,
        item_kind: str,
        unit_id: str,
        proposer: str,
        value: dict[str, Any],
        dependencies: list[str],
    ) -> None:
        item_hash = sha256_json(value)
        dependency_hashes = {
            reference: sha256_json(objects[reference])
            for reference in sorted(dependencies)
            if reference in objects
        }
        review_input = {
            "source_sha256": source_hash,
            "workpack_id": manifest.get("workpack_id"),
            "item_ref": item_ref,
            "item_kind": item_kind,
            "item_sha256": item_hash,
            "dependency_sha256": dependency_hashes,
        }
        queue.append(
            {
                "item_ref": item_ref,
                "item_kind": item_kind,
                "unit_id": unit_id,
                "proposer_instance": proposer,
                "item_sha256": item_hash,
                "dependency_refs": sorted(
                    reference for reference in dependencies if reference in objects
                ),
                "review_input_sha256": sha256_json(review_input),
            }
        )

    for unit_id in sorted(drafts):
        draft = drafts[unit_id]
        proposer = str(draft.get("proposer_instance", ""))
        for obj in draft.get("objects", []):
            if isinstance(obj, dict) and isinstance(obj.get("local_id"), str):
                reference = _draft_object_ref(unit_id, obj["local_id"])
                add_item(reference, "object", unit_id, proposer, obj, [])
        for index, relation in enumerate(draft.get("relations", [])):
            if isinstance(relation, dict):
                dependencies = [
                    reference
                    for reference in (relation.get("source_ref"), relation.get("target_ref"))
                    if isinstance(reference, str)
                ]
                add_item(f"rel:{unit_id}:{index}", "relation", unit_id, proposer, relation, dependencies)
        for index, conflict in enumerate(draft.get("conflicts", [])):
            if isinstance(conflict, dict):
                dependencies = [
                    reference for reference in conflict.get("claim_refs", []) if isinstance(reference, str)
                ]
                local = conflict.get("local_id", str(index))
                add_item(f"conf:{unit_id}:{local}", "conflict", unit_id, proposer, conflict, dependencies)
        for index, gap in enumerate(draft.get("gaps", [])):
            if isinstance(gap, dict):
                add_item(f"gap:{unit_id}:{index}", "gap", unit_id, proposer, gap, [])

    proposals_by_page: dict[int, set[str]] = {}
    proposers_by_page: dict[int, set[str]] = {}
    for reference, obj in objects.items():
        pages = {
            span.get("page")
            for span in obj.get("evidence_spans", [])
            if isinstance(span, dict) and isinstance(span.get("page"), int)
        }
        for page in pages:
            proposals_by_page.setdefault(page, set()).add(reference)
            proposers_by_page.setdefault(page, set()).add(object_proposers[reference])

    tasks_by_page: dict[int, list[dict[str, Any]]] = {}
    for task in tasks:
        if isinstance(task, dict) and isinstance(task.get("page"), int):
            tasks_by_page.setdefault(task["page"], []).append(task)
    visual_queue: list[dict[str, Any]] = []
    object_hashes = {reference: sha256_json(obj) for reference, obj in objects.items()}
    for page in sorted(tasks_by_page):
        page_tasks = sorted(tasks_by_page[page], key=lambda task: task["id"])
        proposal_refs = sorted(proposals_by_page.get(page, set()))
        visual_object_ids = sorted({str(identifier) for task in page_tasks for identifier in task.get("visual_object_ids", []) if isinstance(identifier, str)})
        table_cell_ids = sorted({str(identifier) for task in page_tasks for identifier in task.get("table_cell_ids", []) if isinstance(identifier, str)})
        series_ids = sorted({str(identifier) for task in page_tasks for identifier in task.get("series_ids", []) if isinstance(identifier, str)})
        high_risk_visual = bool(table_cell_ids or series_ids or any(str(task.get("kind", "")).startswith(("equation", "table", "scan-table", "plot")) for task in page_tasks))
        visual_input = {
            "source_sha256": source_hash,
            "workpack_id": manifest.get("workpack_id"),
            "page": page,
            "tasks": [
                {
                    "id": task["id"],
                    "kind": task.get("kind"),
                    "candidate_label": task.get("candidate_label"),
                    "reason_codes": task.get("reason_codes"),
                    "page_asset_sha256": task.get("page_asset_sha256"),
                    "visual_object_ids": sorted(task.get("visual_object_ids", [])),
                    "table_cell_ids": sorted(task.get("table_cell_ids", [])),
                    "series_ids": sorted(task.get("series_ids", [])),
                    "visual_relation_ids": sorted(task.get("visual_relation_ids", [])),
                    "relation_sha256s": sorted(task.get("relation_sha256s", [])),
                    "table_grid_ids": sorted(task.get("table_grid_ids", [])),
                    "table_grid_sha256s": sorted(task.get("table_grid_sha256s", [])),
                    "crop_sha256s": sorted(task.get("crop_sha256s", [])),
                    "bbox_sha256s": sorted(task.get("bbox_sha256s", [])),
                }
                for task in page_tasks
            ],
            "proposal_sha256": {
                reference: object_hashes[reference] for reference in proposal_refs
            },
        }
        visual_queue.append(
            {
                "page": page,
                "visual_task_ids": [task["id"] for task in page_tasks],
                "proposal_refs": proposal_refs,
                "proposer_instances": sorted(proposers_by_page.get(page, set())),
                "page_asset": page_tasks[0].get("page_asset"),
                "page_asset_sha256": page_tasks[0].get("page_asset_sha256"),
                "full_page_sha256": page_tasks[0].get("page_asset_sha256"),
                "render_sha256": page_tasks[0].get("page_asset_sha256"),
                "render_dpi": int(manifest.get("render_profile", {}).get("dpi", 150)) if isinstance(manifest.get("render_profile"), dict) else 150,
                "render_rotation": 0,
                "visual_object_ids": visual_object_ids,
                "table_cell_ids": table_cell_ids,
                "series_ids": series_ids,
                "visual_relation_ids": sorted({str(identifier) for task in page_tasks for identifier in task.get("visual_relation_ids", []) if isinstance(identifier, str)}),
                "relation_sha256s": sorted({str(value) for task in page_tasks for value in task.get("relation_sha256s", []) if isinstance(value, str)}),
                "table_grid_ids": sorted({str(identifier) for task in page_tasks for identifier in task.get("table_grid_ids", []) if isinstance(identifier, str)}),
                "table_grid_sha256s": sorted({str(value) for task in page_tasks for value in task.get("table_grid_sha256s", []) if isinstance(value, str)}),
                "crop_sha256s": sorted({str(value) for task in page_tasks for value in task.get("crop_sha256s", []) if isinstance(value, str)}),
                "bbox_sha256s": sorted({str(value) for task in page_tasks for value in task.get("bbox_sha256s", []) if isinstance(value, str)}),
                "required_reviewer_count": 2 if high_risk_visual else 1,
                "review_input_sha256": sha256_json(visual_input),
            }
        )
    bundle_hash = sha256_json(
        {
            "source_sha256": source_hash,
            "workpack_id": manifest.get("workpack_id"),
            "semantic_queue": queue,
            "visual_queue": visual_queue,
        }
    )
    return queue, visual_queue, bundle_hash


def _review_material_for_manifest(
    source_hash: str,
    manifest: dict[str, Any],
    units: dict[str, dict[str, Any]],
    tasks: list[dict[str, Any]],
    drafts: dict[str, dict[str, Any]],
    *,
    workpack_root: Path | None = None,
    materialize_assurance: bool = False,
    visual_review_scope: str = "all",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Dispatch legacy and v0.5 review material without assurance inflation."""

    if visual_review_scope not in {"all", "referenced"}:
        raise SemanticPromotionError("visual_review_scope_invalid")
    review_tasks = tasks
    if visual_review_scope == "referenced":
        referenced_task_ids = {
            task_id
            for draft in drafts.values()
            for obj in draft.get("objects", [])
            if isinstance(obj, dict)
            for task_id in obj.get("visual_task_ids", [])
            if isinstance(task_id, str)
        }
        review_tasks = [
            task
            for task in tasks
            if isinstance(task, dict) and task.get("id") in referenced_task_ids
        ]
    queue, visual_queue, bundle_hash = _review_material(
        source_hash, manifest, units, review_tasks, drafts
    )
    if not is_assurance_enabled(manifest):
        return queue, visual_queue, bundle_hash
    if workpack_root is None:
        raise SemanticPromotionError("semantic_assurance_root_required")
    if materialize_assurance:
        try:
            materialize_workpack_assurance(
                workpack_root, manifest, units, tasks, drafts
            )
        except ValueError as error:
            raise SemanticPromotionError(str(error)) from error
    assurance_issues, _ = validate_workpack_assurance(
        workpack_root, manifest, units, tasks, drafts
    )
    if assurance_issues:
        first = assurance_issues[0]
        raise SemanticPromotionError(f"{first.code}: {first.path}")
    assertions, _, _, _ = load_workpack_assurance(workpack_root, manifest)
    return enrich_review_material(
        queue,
        visual_queue,
        [row for row in assertions if isinstance(row, dict)],
        tasks,
        source_sha256=source_hash,
        workpack_id=str(manifest.get("workpack_id", "")),
    )


def prepare_review_plan(
    source_path: Path,
    workpack_root: Path,
    reviewer_instances: list[str],
    *,
    visual_review_scope: str = "all",
) -> dict[str, Any]:
    base_issues, _ = validate_semantic_workpack(
        source_path, workpack_root, require_complete_drafts=True
    )
    errors = [issue for issue in base_issues if issue.severity == "error"]
    if errors:
        raise SemanticPromotionError(
            f"proposal_gate_failed: {errors[0].code} {errors[0].path}"
        )
    reviewers = sorted({reviewer for reviewer in reviewer_instances if reviewer})
    if not reviewers:
        raise SemanticPromotionError("reviewer_instances_required")
    root = workpack_root.expanduser().resolve()
    manifest = load_json(root / "workpack.json")
    reader, source_hash, _ = _load_source(source_path)
    del reader
    units = {
        entry["unit_id"]: load_json(_resolve_workpack_path(root, entry["path"]))
        for entry in manifest["units"]
    }
    tasks = load_jsonl(_resolve_workpack_path(root, manifest["visual_tasks"]))
    drafts, _ = _load_workpack_drafts(root, manifest)
    queue, visual_queue, bundle_hash = _review_material_for_manifest(
        source_hash,
        manifest,
        units,
        tasks,
        drafts,
        workpack_root=root,
        materialize_assurance=True,
        visual_review_scope=visual_review_scope,
    )
    missing_render_pages = [
        row["page"]
        for row in visual_queue
        if not isinstance(row.get("page_asset"), str)
        or not SHA256_RE.fullmatch(str(row.get("page_asset_sha256", "")))
    ]
    if missing_render_pages:
        raise SemanticPromotionError(
            f"visual_review_assets_required: pages={missing_render_pages}"
        )
    proposers = sorted(
        {
            str(draft.get("proposer_instance"))
            for draft in drafts.values()
            if draft.get("proposer_instance")
        }
    )
    if set(reviewers) & set(proposers):
        raise SemanticPromotionError("reviewer_not_independent")
    if is_assurance_enabled(manifest):
        required = max(
            (int(row.get("required_reviewer_count", 2)) for row in queue),
            default=1,
        )
        required = max(
            required,
            max((int(row.get("required_reviewer_count", 1)) for row in visual_queue), default=1),
        )
        if len(reviewers) < required:
            raise SemanticPromotionError(
                f"reviewer_coverage_insufficient: required={required} registered={len(reviewers)}"
            )
    _write_jsonl(root / "review-queue.jsonl", queue)
    _write_jsonl(root / "visual-review-queue.jsonl", visual_queue)
    plan = {
        "schema_version": REVIEW_PLAN_SCHEMA,
        "attestation_level": "host-orchestrator-recorded-not-cryptographic",
        "issued_by": "host-orchestrator",
        "issued_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "source_sha256": source_hash,
        "workpack_id": manifest["workpack_id"],
        "bundle_sha256": bundle_hash,
        "proposer_instances": proposers,
        "reviewer_instances": reviewers,
        "semantic_item_count": len(queue),
        "visual_page_count": len(visual_queue),
        "visual_review_scope": visual_review_scope,
        "visual_candidate_task_count": len(tasks),
        "visual_queued_task_count": sum(len(row["visual_task_ids"]) for row in visual_queue),
        "visual_deferred_task_count": len(tasks) - sum(len(row["visual_task_ids"]) for row in visual_queue),
        "visual_deferred_task_ids_sha256": sha256_json(
            sorted(
                set(str(task.get("id")) for task in tasks if isinstance(task, dict))
                - {
                    str(task_id)
                    for row in visual_queue
                    for task_id in row.get("visual_task_ids", [])
                }
            )
        ),
        "gap_resolution_policy": "explicit-package-scope-v1",
        "review_protocol": (
            ASSURANCE_REVIEW_PROTOCOL
            if is_assurance_enabled(manifest)
            else REVIEW_PROTOCOL
        ),
        "semantic_queue": "review-queue.jsonl",
        "visual_queue": "visual-review-queue.jsonl",
    }
    if (
        isinstance(manifest.get("visual_semantics"), dict)
        and manifest["visual_semantics"].get("review_attestation_required") is True
    ):
        # The v0.2 fragment protocol is opt-in per workpack so legacy v0.6
        # page-receipt fixtures remain readable, while any normalized visual
        # object route is forced through exact object-level attestations.
        plan["visual_review_protocol"] = VISUAL_REVIEW_PROTOCOL
        plan["visual_reviewer_requirements"] = dict(sorted(_visual_review_requirements(visual_queue).items()))
    plan["review_session_id"] = stable_id(
        "rws",
        bundle_hash,
        plan["issued_at"],
        ",".join(reviewers),
    )
    plan["authorization_sha256"] = _review_authorization(plan)
    if is_assurance_enabled(manifest):
        assertions, support, coverage, _ = load_workpack_assurance(root, manifest)
        plan["semantic_assurance"] = {
            "protocol": SEMANTIC_ASSURANCE_PROTOCOL,
            "compiler_version": SEMANTIC_ASSURANCE_COMPILER_VERSION,
            "assertion_count": len(assertions),
            "support_count": len(support),
            "coverage_count": len(coverage),
            "assertion_bundle_sha256": sha256_json(assertions),
            "support_bundle_sha256": sha256_json(support),
            "coverage_bundle_sha256": sha256_json(coverage),
        }
        # The authorization already binds the queue bundle, which itself binds
        # all assurance hashes. Recompute after adding the descriptive block so
        # callers never mistake it for an unsigned mutable annotation.
        plan["authorization_sha256"] = _review_authorization(plan)
    _write_json(root / "review-plan.json", plan)
    return plan


def _fragment_path_outside_workpack(fragment: Path, root: Path) -> Path:
    resolved = fragment.expanduser().resolve()
    if not resolved.is_file():
        raise SemanticPromotionError(f"review_fragment_missing: {resolved}")
    try:
        resolved.relative_to(root)
    except ValueError:
        return resolved
    raise SemanticPromotionError("review_fragment_inside_workpack")


def _normalize_rationale(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _review_blanket_anomalies(
    semantic_records: dict[str, dict[str, Any]],
    visual_records: dict[int, dict[str, Any]],
) -> list[str]:
    """Detect template-style blanket review patterns (single shared content)."""
    anomalies: list[str] = []
    accepted = [
        record
        for record in semantic_records.values()
        if record.get("verdict") == "accepted"
    ]
    if len(accepted) >= 2:
        accepted_rationales = {
            _normalize_rationale(record.get("rationale")) for record in accepted
        }
        if len(accepted_rationales) < 2:
            anomalies.append("semantic:single-shared-accepted-rationale")
            if all(record.get("verdict") == "accepted" for record in semantic_records.values()):
                confidences = {
                    sha256_json(record.get("confidence")) for record in accepted
                }
                if len(confidences) == 1:
                    anomalies.append("semantic:identical-accepted-confidence")
    if len(semantic_records) >= 2 and len(
        {
            _normalize_rationale(record.get("rationale"))
            for record in semantic_records.values()
        }
    ) < 2:
        anomalies.append("semantic:single-shared-rationale")
    verified = [
        receipt
        for receipt in visual_records.values()
        if receipt.get("verdict") == "verified"
    ]
    if len(verified) >= 2:
        observations = {
            _normalize_rationale(resolution.get("observation"))
            for receipt in verified
            for resolution in receipt.get("task_resolutions", [])
            if isinstance(resolution, dict)
        }
        if len(observations) < 2:
            anomalies.append("visual:single-shared-observation")
    return anomalies


def _quarantine_review_material(
    root: Path, code: str, detail: dict[str, Any]
) -> Path:
    """Fail closed: move review material into a quarantine receipt directory."""
    stamp = (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace(":", "")
    )
    target = root / "quarantine" / f"{code}-{stamp}"
    target.mkdir(parents=True, exist_ok=False)
    moved: list[str] = []
    for name in (
        "review-records.jsonl",
        "review-attestations.jsonl",
        "visual-receipts.jsonl",
        "review-fragments.json",
    ):
        source = root / name
        if source.is_file():
            source.rename(target / name)
            moved.append(name)
    fragments_dir = root / "review-fragments"
    if fragments_dir.is_dir():
        fragments_dir.rename(target / "review-fragments")
        moved.append("review-fragments")
    (target / "quarantine-receipt.json").write_text(
        json.dumps(
            {
                "schema_version": "tkc.quarantine-receipt/v0.1",
                "code": code,
                "detail": detail,
                "quarantined_at": stamp,
                "moved": sorted(moved),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def _write_fragment_provenance(
    root: Path,
    plan: dict[str, Any],
    semantic_fragments: list[Path],
    visual_fragments: list[Path],
    semantic_path: Path,
    visual_path: Path,
) -> None:
    """Copy external fragments into the workpack and record their provenance."""
    fragments_dir = root / "review-fragments"
    if fragments_dir.is_dir():
        shutil.rmtree(fragments_dir)
    fragments_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(fragments_dir, 0o700)
    manifest_rows: dict[str, list[dict[str, Any]]] = {
        "semantic_fragments": [],
        "visual_fragments": [],
    }
    reviewers: set[str] = set()
    for kind, fragments in (
        ("semantic_fragments", semantic_fragments),
        ("visual_fragments", visual_fragments),
    ):
        for fragment in fragments:
            resolved = _fragment_path_outside_workpack(fragment, root)
            content = resolved.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            rows = load_jsonl(resolved)
            copy = fragments_dir / f"{digest}.jsonl"
            copy.write_bytes(content)
            row = {
                "filename": copy.name,
                "sha256": digest,
                "record_count": len(rows),
            }
            if kind == "semantic_fragments":
                row["item_refs"] = sorted(
                    str(record.get("item_ref"))
                    for record in rows
                    if isinstance(record, dict) and isinstance(record.get("item_ref"), str)
                )
            else:
                row["pages"] = sorted(
                    int(record.get("page"))
                    for record in rows
                    if isinstance(record, dict) and isinstance(record.get("page"), int)
                )
                row["visual_object_ids"] = sorted(
                    {
                        str(identifier)
                        for record in rows
                        if isinstance(record, dict)
                        for identifier in record.get("visual_object_ids", [])
                        if isinstance(identifier, str)
                    }
                )
                row["protocols"] = sorted(
                    {
                        str(record.get("schema_version"))
                        for record in rows
                        if isinstance(record, dict) and record.get("schema_version")
                    }
                )
            row["reviewer_instances"] = sorted(
                {
                    str(record.get("reviewer_instance"))
                    for record in rows
                    if isinstance(record, dict) and isinstance(record.get("reviewer_instance"), str)
                }
            )
            reviewers.update(row["reviewer_instances"])
            manifest_rows[kind].append(row)
    merged_at = (
        dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    )
    _write_json(
        root / "review-fragments.json",
        {
            "schema_version": FRAGMENTS_SCHEMA,
            "workpack_id": plan.get("workpack_id"),
            "merged_at": merged_at,
            "reviewer_instances": sorted(reviewers),
            "semantic_fragments": manifest_rows["semantic_fragments"],
            "visual_fragments": manifest_rows["visual_fragments"],
            "merged_semantic_sha256": sha256_file(semantic_path),
            "merged_visual_sha256": sha256_file(visual_path),
        },
    )


def _load_workpack_visual_bundle(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Load the normalized visual candidate rows from a bounded workpack."""

    visual_manifest = manifest.get("visual_semantics")
    if not isinstance(visual_manifest, dict) or visual_manifest.get("protocol") != VISUAL_PROTOCOL:
        raise SemanticPromotionError("visual_contract_missing")
    rows: dict[str, list[Any]] = {}
    defaults = {
        "objects": "visual-objects.jsonl",
        "relations": "visual-relations.jsonl",
        "table_grids": "table-grids.jsonl",
        "conflicts": "visual-conflicts.jsonl",
    }
    for key, default in defaults.items():
        entry = visual_manifest.get(key)
        relative = entry.get("path", default) if isinstance(entry, dict) else default
        try:
            rows[key] = load_jsonl(_resolve_workpack_path(root, relative))
        except Exception as error:
            raise SemanticPromotionError(f"visual_contract_missing:{relative}") from error
    bundle = {"schema_version": VISUAL_PROTOCOL, "source_sha256": manifest.get("source", {}).get("sha256"), **rows}
    return bundle


def _visual_review_requirements(visual_queue: Iterable[dict[str, Any]]) -> dict[str, int]:
    requirements: dict[str, int] = {}
    for row in visual_queue:
        required = max(1, int(row.get("required_reviewer_count", 1)))
        for identifier in row.get("visual_object_ids", []):
            if isinstance(identifier, str):
                requirements[identifier] = max(requirements.get(identifier, 0), required)
    return requirements


def merge_review_fragments(
    source_path: Path,
    workpack_root: Path,
    semantic_fragments: list[Path],
    visual_fragments: list[Path],
) -> dict[str, Any]:
    """Mechanically merge complete, registered review fragments and fail closed."""

    root = workpack_root.expanduser().resolve()
    plan = load_json(root / "review-plan.json")
    if not isinstance(plan, dict) or plan.get("schema_version") != REVIEW_PLAN_SCHEMA:
        raise SemanticPromotionError("review_plan_invalid")
    registered = set(plan.get("reviewer_instances", []))
    if plan.get("authorization_sha256") != _review_authorization(plan):
        raise SemanticPromotionError("reviewer_identity_unverifiable")
    semantic_queue = load_jsonl(
        _resolve_workpack_path(
            root, plan.get("semantic_queue", "review-queue.jsonl")
        )
    )
    visual_queue = load_jsonl(
        _resolve_workpack_path(
            root, plan.get("visual_queue", "visual-review-queue.jsonl")
        )
    )
    semantic_by_ref = {row["item_ref"]: row for row in semantic_queue}
    visual_by_page = {row["page"]: row for row in visual_queue}
    if not semantic_fragments or (visual_by_page and not visual_fragments):
        raise SemanticPromotionError("review_fragments_required")

    assurance_protocol = plan.get("review_protocol") == ASSURANCE_REVIEW_PROTOCOL
    attestation_records: list[dict[str, Any]] = []
    semantic_records: dict[str, dict[str, Any]] = {}
    if assurance_protocol:
        for fragment in semantic_fragments:
            resolved = _fragment_path_outside_workpack(fragment, root)
            for record in load_jsonl(resolved):
                if not isinstance(record, dict):
                    raise SemanticPromotionError("review_attestation_invalid")
                attestation_records.append(record)
        attestation_issues, semantic_records, _ = validate_attestations(
            attestation_records, semantic_queue, plan
        )
        if attestation_issues:
            first = attestation_issues[0]
            raise SemanticPromotionError(f"{first.code}: {first.path}")
    else:
        for fragment in semantic_fragments:
            resolved = _fragment_path_outside_workpack(fragment, root)
            for record in load_jsonl(resolved):
                item_ref = record.get("item_ref") if isinstance(record, dict) else None
                if item_ref not in semantic_by_ref:
                    raise SemanticPromotionError(f"review_item_unresolved: {item_ref}")
                if item_ref in semantic_records:
                    raise SemanticPromotionError(f"review_receipt_duplicate: {item_ref}")
                queue_row = semantic_by_ref[item_ref]
                if record.get("schema_version") != REVIEW_SCHEMA:
                    raise SemanticPromotionError(f"review_receipt_invalid: {item_ref}")
                record = dict(record)
                record.setdefault("item_kind", queue_row["item_kind"])
                if record.get("reviewer_instance") not in registered:
                    raise SemanticPromotionError(f"reviewer_identity_unverifiable: {item_ref}")
                if record.get("reviewer_instance") == queue_row["proposer_instance"]:
                    raise SemanticPromotionError(f"reviewer_not_independent: {item_ref}")
                for field in (
                    "proposer_instance",
                    "item_sha256",
                    "review_input_sha256",
                ):
                    if record.get(field) != queue_row[field]:
                        raise SemanticPromotionError(
                            f"review_receipt_stale: {item_ref}.{field}"
                        )
                if record.get("item_kind") != queue_row["item_kind"]:
                    raise SemanticPromotionError(f"review_item_kind_mismatch: {item_ref}")
                semantic_records[item_ref] = record
        missing_items = set(semantic_by_ref) - set(semantic_records)
        if missing_items:
            raise SemanticPromotionError(
                f"review_receipt_missing: {sorted(missing_items)[:5]}"
            )

    visual_records: dict[int, dict[str, Any]] = {}
    visual_attestation_records: list[dict[str, Any]] = []
    visual_protocol = plan.get("visual_review_protocol")
    if visual_protocol == VISUAL_REVIEW_PROTOCOL:
        try:
            visual_bundle = _load_workpack_visual_bundle(root, load_json(root / "workpack.json"))
        except (OSError, ValueError, SemanticPromotionError) as error:
            raise SemanticPromotionError(f"visual_contract_missing: {error}") from error
        visual_objects = [row for row in visual_bundle.get("objects", []) if isinstance(row, dict)]
        visual_relations = [row for row in visual_bundle.get("relations", []) if isinstance(row, dict)]
        visual_grids = [row for row in visual_bundle.get("table_grids", []) if isinstance(row, dict)]
        requirements = plan.get("visual_reviewer_requirements") if isinstance(plan.get("visual_reviewer_requirements"), dict) else _visual_review_requirements(visual_queue)
        review_plan_sha256 = sha256_json(plan)
        for fragment in visual_fragments:
            resolved = _fragment_path_outside_workpack(fragment, root)
            for record in load_jsonl(resolved):
                if not isinstance(record, dict):
                    raise SemanticPromotionError("visual_review_attestation_invalid")
                visual_attestation_records.append(record)
        visual_issues = validate_visual_review_attestations(
            visual_attestation_records,
            visual_objects,
            review_plan_sha256=review_plan_sha256,
            source_sha256=str(plan.get("source_sha256")),
            proposer_instances=plan.get("proposer_instances", []),
            required_reviewers=requirements,
            table_grids=visual_grids,
            visual_relations=visual_relations,
            expected_review_session_id=str(plan.get("review_session_id")),
            registered_reviewers=plan.get("reviewer_instances", []),
        )
        if visual_issues:
            first = visual_issues[0]
            raise SemanticPromotionError(f"{first.code}: {first.path}")
    else:
        for fragment in visual_fragments:
            resolved = _fragment_path_outside_workpack(fragment, root)
            for receipt in load_jsonl(resolved):
                page = receipt.get("page") if isinstance(receipt, dict) else None
                if page not in visual_by_page:
                    raise SemanticPromotionError(f"visual_receipt_page_unresolved: {page}")
                if page in visual_records:
                    raise SemanticPromotionError(f"visual_receipt_duplicate: page={page}")
                queue_row = visual_by_page[page]
                if receipt.get("schema_version") != VISUAL_RECEIPT_SCHEMA:
                    raise SemanticPromotionError(f"visual_receipt_invalid: page={page}")
                if receipt.get("reviewer_instance") not in registered:
                    raise SemanticPromotionError(
                        f"reviewer_identity_unverifiable: page={page}"
                    )
                if receipt.get("reviewer_instance") in set(
                    queue_row["proposer_instances"]
                ):
                    raise SemanticPromotionError(f"reviewer_not_independent: page={page}")
                for field in ("review_input_sha256", "page_asset_sha256"):
                    if receipt.get(field) != queue_row[field]:
                        raise SemanticPromotionError(
                            f"formula_visual_receipt_stale: page={page}.{field}"
                        )
                resolutions = receipt.get("task_resolutions")
                resolution_ids = [
                    row.get("task_id") for row in resolutions if isinstance(row, dict)
                ] if isinstance(resolutions, list) else []
                if len(resolution_ids) != len(set(resolution_ids)) or set(resolution_ids) != set(
                    queue_row["visual_task_ids"]
                ):
                    raise SemanticPromotionError(
                        f"visual_task_coverage_incomplete: page={page}"
                    )
                visual_records[page] = receipt
        missing_pages = set(visual_by_page) - set(visual_records)
        if missing_pages:
            raise SemanticPromotionError(
                f"formula_visual_receipt_missing: pages={sorted(missing_pages)}"
            )

    anomalies = _review_blanket_anomalies(semantic_records, visual_records)
    if anomalies:
        raise SemanticPromotionError(
            f"review_blanket_pattern: {','.join(anomalies)}"
        )

    semantic_path = root / "review-records.jsonl"
    attestation_path = root / "review-attestations.jsonl"
    visual_path = root / "visual-receipts.jsonl"
    visual_attestation_path = root / "visual-review-attestations.jsonl"
    previous_semantic = semantic_path.read_bytes() if semantic_path.is_file() else None
    previous_attestations = (
        attestation_path.read_bytes() if attestation_path.is_file() else None
    )
    previous_visual = visual_path.read_bytes() if visual_path.is_file() else None
    previous_visual_attestations = visual_attestation_path.read_bytes() if visual_attestation_path.is_file() else None
    _write_jsonl(
        semantic_path,
        [semantic_records[item_ref] for item_ref in sorted(semantic_records)],
    )
    _write_jsonl(
        visual_path,
        [visual_records[page] for page in sorted(visual_records)],
    )
    if visual_protocol == VISUAL_REVIEW_PROTOCOL:
        _write_jsonl(
            visual_attestation_path,
            sorted(
                visual_attestation_records,
                key=lambda row: (str(row.get("attestation_id")), str(row.get("reviewer_instance"))),
            ),
        )
    if assurance_protocol:
        _write_jsonl(
            attestation_path,
            sorted(
                attestation_records,
                key=lambda row: (str(row.get("item_ref")), str(row.get("reviewer_instance"))),
            ),
        )
    _write_fragment_provenance(
        root,
        plan,
        semantic_fragments,
        visual_fragments,
        attestation_path if assurance_protocol else semantic_path,
        visual_attestation_path if visual_protocol == VISUAL_REVIEW_PROTOCOL else visual_path,
    )
    issues, summary = validate_reviewed_workpack(source_path, root)
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        (root / "review-fragments.json").unlink(missing_ok=True)
        fragments_dir = root / "review-fragments"
        if fragments_dir.is_dir():
            shutil.rmtree(fragments_dir)
        if previous_semantic is None:
            semantic_path.unlink(missing_ok=True)
        else:
            semantic_path.write_bytes(previous_semantic)
        if assurance_protocol:
            if previous_attestations is None:
                attestation_path.unlink(missing_ok=True)
            else:
                attestation_path.write_bytes(previous_attestations)
        if previous_visual is None:
            visual_path.unlink(missing_ok=True)
        else:
            visual_path.write_bytes(previous_visual)
        if visual_protocol == VISUAL_REVIEW_PROTOCOL:
            if previous_visual_attestations is None:
                visual_attestation_path.unlink(missing_ok=True)
            else:
                visual_attestation_path.write_bytes(previous_visual_attestations)
        raise SemanticPromotionError(
            f"reviewed_gate_failed: {errors[0].code} {errors[0].path}"
        )
    return summary


def _validate_fragment_manifest(
    issues: list[PromotionIssue],
    root: Path,
    workpack_manifest: dict[str, Any],
    reviewers: set[str],
    records: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
    visual_attestations: list[dict[str, Any]] | None = None,
) -> None:
    """Require every merged review row to be attributable to an external fragment."""
    path = root / "review-fragments.json"
    if not path.is_file():
        if records or receipts or visual_attestations:
            _add_issue(
                issues,
                "error",
                "review_fragments_manifest_missing",
                "review-fragments.json",
                "Merged review material requires a fragment provenance manifest.",
            )
        return
    try:
        fragments = load_json(path)
    except Exception as error:
        _add_issue(
            issues,
            "error",
            "review_fragments_manifest_invalid",
            "review-fragments.json",
            str(error),
        )
        return
    if not isinstance(fragments, dict) or fragments.get("schema_version") != FRAGMENTS_SCHEMA:
        _add_issue(
            issues,
            "error",
            "review_fragments_manifest_invalid",
            "review-fragments.json",
            "Unexpected fragment manifest schema.",
        )
        return
    if fragments.get("workpack_id") != workpack_manifest.get("workpack_id"):
        _add_issue(
            issues,
            "error",
            "review_fragments_manifest_invalid",
            "review-fragments.json.workpack_id",
            "Fragment manifest is bound to another workpack.",
        )
    manifest_reviewers = fragments.get("reviewer_instances")
    if isinstance(manifest_reviewers, list) and set(manifest_reviewers) - reviewers:
        _add_issue(
            issues,
            "error",
            "reviewer_identity_unverifiable",
            "review-fragments.json.reviewer_instances",
            "Fragment reviewers are not registered in the review plan.",
        )
    fragment_rows: dict[str, list[dict[str, Any]]] = {}
    for kind in ("semantic_fragments", "visual_fragments"):
        rows = fragments.get(kind)
        if not isinstance(rows, list):
            _add_issue(
                issues,
                "error",
                "review_fragments_manifest_invalid",
                f"review-fragments.json.{kind}",
                "Expected a fragment list.",
            )
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            digest = row.get("sha256")
            filename = row.get("filename")
            count = row.get("record_count")
            if (
                not isinstance(digest, str)
                or not SHA256_RE.fullmatch(digest)
                or not isinstance(filename, str)
                or not filename
                or not isinstance(count, int)
                or count < 0
            ):
                _add_issue(
                    issues,
                    "error",
                    "review_fragments_manifest_invalid",
                    "review-fragments.json",
                    "Fragment digest, filename, or record count invalid.",
                )
                continue
            copy = root / "review-fragments" / filename
            if not copy.is_file():
                _add_issue(
                    issues,
                    "error",
                    "review_fragments_manifest_invalid",
                    f"review-fragments/{filename}",
                    "Fragment copy missing.",
                )
                continue
            if sha256_file(copy) != digest:
                _add_issue(
                    issues,
                    "error",
                    "review_fragments_manifest_invalid",
                    f"review-fragments/{filename}",
                    "Fragment copy digest mismatch.",
                )
                continue
            try:
                loaded = load_jsonl(copy)
            except Exception as error:
                _add_issue(
                    issues,
                    "error",
                    "review_fragments_manifest_invalid",
                    f"review-fragments/{filename}",
                    str(error),
                )
                continue
            if len(loaded) != count:
                _add_issue(
                    issues,
                    "error",
                    "review_fragments_manifest_invalid",
                    f"review-fragments/{filename}",
                    "Fragment record count mismatch.",
                )
            for record in loaded:
                fragment_rows.setdefault(sha256_json(record), []).append(record)
    merged_keys = [sha256_json(record) for record in records] + [
        sha256_json(receipt) for receipt in receipts
    ] + [
        sha256_json(attestation)
        for attestation in (visual_attestations or [])
    ]
    if any(key not in fragment_rows for key in merged_keys) or len(fragment_rows) != len(
        set(merged_keys)
    ):
        _add_issue(
            issues,
            "error",
            "review_fragments_manifest_mismatch",
            "review-records.jsonl",
            "Merged review material is not fully attributable to the recorded fragments.",
        )


def validate_reviewed_workpack(
    source_path: Path,
    workpack_root: Path,
) -> tuple[list[PromotionIssue], dict[str, Any]]:
    issues, summary = validate_semantic_workpack(
        source_path, workpack_root, require_complete_drafts=True
    )
    if any(issue.severity == "error" for issue in issues):
        return issues, summary
    root = workpack_root.expanduser().resolve()
    manifest = load_json(root / "workpack.json")
    _, source_hash, _ = _load_source(source_path)
    units = {
        entry["unit_id"]: load_json(_resolve_workpack_path(root, entry["path"]))
        for entry in manifest["units"]
    }
    tasks = load_jsonl(_resolve_workpack_path(root, manifest["visual_tasks"]))
    task_by_id = {
        str(task.get("id")): task
        for task in tasks
        if isinstance(task, dict) and isinstance(task.get("id"), str)
    }
    drafts, _ = _load_workpack_drafts(root, manifest)
    plan_path = root / "review-plan.json"
    if not plan_path.is_file():
        _add_issue(issues, "error", "review_receipt_missing", "review-plan.json", "Prepare an orchestrator review plan.")
        return sorted(issues, key=lambda item: (item.severity != "error", item.code, item.path)), summary
    plan = load_json(plan_path)
    if not isinstance(plan, dict) or plan.get("schema_version") != REVIEW_PLAN_SCHEMA:
        _add_issue(issues, "error", "reviewer_identity_unverifiable", "review-plan.json", "Invalid review plan.")
        return sorted(issues, key=lambda item: (item.severity != "error", item.code, item.path)), summary
    try:
        queue, visual_queue, bundle_hash = _review_material_for_manifest(
            source_hash,
            manifest,
            units,
            tasks,
            drafts,
            workpack_root=root,
            visual_review_scope=str(plan.get("visual_review_scope", "all")),
        )
    except SemanticPromotionError as error:
        detail = str(error)
        code = detail.split(":", 1)[0]
        _add_issue(issues, "error", code, "semantic_assurance", detail)
        return sorted(
            issues,
            key=lambda item: (item.severity != "error", item.code, item.path),
        ), summary
    queue_by_ref = {row["item_ref"]: row for row in queue}
    visual_by_page = {row["page"]: row for row in visual_queue}
    if (
        plan.get("attestation_level")
        != "host-orchestrator-recorded-not-cryptographic"
        or plan.get("issued_by") != "host-orchestrator"
        or not isinstance(plan.get("issued_at"), str)
        or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
            plan["issued_at"],
        )
        or plan.get("authorization_sha256") != _review_authorization(plan)
    ):
        _add_issue(
            issues,
            "error",
            "reviewer_identity_unverifiable",
            "review-plan.json",
            "Review authorization metadata is invalid or changed after issuance.",
        )
    protocol = plan.get("review_protocol")
    visual_review_protocol = plan.get("visual_review_protocol")
    if visual_review_protocol is not None and visual_review_protocol != VISUAL_REVIEW_PROTOCOL:
        _add_issue(
            issues,
            "error",
            "visual_review_protocol_invalid",
            "review-plan.json.visual_review_protocol",
            f"Unsupported visual review protocol: {visual_review_protocol!r}.",
        )
    protocol_enabled = protocol in {REVIEW_PROTOCOL, ASSURANCE_REVIEW_PROTOCOL}
    if protocol is not None and protocol not in {REVIEW_PROTOCOL, ASSURANCE_REVIEW_PROTOCOL}:
        _add_issue(
            issues,
            "error",
            "review_protocol_invalid",
            "review-plan.json.review_protocol",
            f"Unsupported review protocol: {protocol!r}.",
        )
    review_session_id = plan.get("review_session_id")
    if protocol_enabled and (not isinstance(review_session_id, str) or not review_session_id):
        _add_issue(
            issues,
            "error",
            "review_protocol_invalid",
            "review-plan.json.review_session_id",
            "The active review protocol requires a non-empty session ID.",
        )
    if protocol_enabled and plan.get("review_session_id") != stable_id(
        "rws",
        bundle_hash,
        plan.get("issued_at"),
        ",".join(sorted(plan.get("reviewer_instances", [])))
        if isinstance(plan.get("reviewer_instances"), list)
        else "",
    ):
        _add_issue(
            issues,
            "error",
            "review_protocol_invalid",
            "review-plan.json.review_session_id",
            "The review session ID is not bound to this plan, bundle, timestamp, and reviewer registry.",
        )
    if plan.get("source_sha256") != source_hash or plan.get("workpack_id") != manifest.get("workpack_id") or plan.get("bundle_sha256") != bundle_hash:
        _add_issue(issues, "error", "review_receipt_stale", "review-plan.json", "Proposal or source inputs changed after plan issuance.")
    if "visual_review_scope" in plan:
        queued_task_ids = {
            str(task_id)
            for row in visual_queue
            for task_id in row.get("visual_task_ids", [])
        }
        candidate_task_ids = {
            str(task.get("id"))
            for task in tasks
            if isinstance(task, dict) and isinstance(task.get("id"), str)
        }
        deferred_task_ids = sorted(candidate_task_ids - queued_task_ids)
        if (
            plan.get("semantic_item_count") != len(queue)
            or plan.get("visual_page_count") != len(visual_queue)
            or plan.get("visual_candidate_task_count") != len(candidate_task_ids)
            or plan.get("visual_queued_task_count") != len(queued_task_ids)
            or plan.get("visual_deferred_task_count") != len(deferred_task_ids)
            or plan.get("visual_deferred_task_ids_sha256") != sha256_json(deferred_task_ids)
        ):
            _add_issue(
                issues,
                "error",
                "review_receipt_stale",
                "review-plan.json.visual_review_scope",
                "Visual review-scope counts or deferred-task binding changed after plan issuance.",
            )
    reviewers = set(plan.get("reviewer_instances", [])) if isinstance(plan.get("reviewer_instances"), list) else set()
    proposers = set(plan.get("proposer_instances", [])) if isinstance(plan.get("proposer_instances"), list) else set()
    if not reviewers or reviewers & proposers:
        _add_issue(issues, "error", "reviewer_not_independent", "review-plan.json", "Reviewer must differ from all proposers.")

    try:
        records = load_jsonl(root / "review-records.jsonl")
    except Exception as error:
        _add_issue(issues, "error", "review_receipt_missing", "review-records.jsonl", str(error))
        records = []
    attestation_records: list[dict[str, Any]] = []
    if protocol == ASSURANCE_REVIEW_PROTOCOL:
        try:
            raw_attestations = load_jsonl(root / "review-attestations.jsonl")
        except Exception as error:
            _add_issue(
                issues,
                "error",
                "review_attestation_invalid",
                "review-attestations.jsonl",
                str(error),
            )
            raw_attestations = []
        attestation_records = [
            row for row in raw_attestations if isinstance(row, dict)
        ]
        attestation_issues, expected_leads, _ = validate_attestations(
            raw_attestations, queue, plan
        )
        for issue in attestation_issues:
            _add_issue(
                issues, issue.severity, issue.code, issue.path, issue.message
            )
        actual_leads = {
            str(row.get("item_ref")): row
            for row in records
            if isinstance(row, dict) and isinstance(row.get("item_ref"), str)
        }
        if expected_leads != actual_leads:
            _add_issue(
                issues,
                "error",
                "review_attestation_invalid",
                "review-records.jsonl",
                "Lead semantic records do not match the canonical attestation projection.",
            )
    records_by_ref: dict[str, list[dict[str, Any]]] = {}
    accepted_refs: set[str] = set()
    rejected_refs: set[str] = set()
    gap_resolutions: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        path = f"review-records.jsonl:{index + 1}"
        if not isinstance(record, dict) or record.get("schema_version") != REVIEW_SCHEMA:
            _add_issue(issues, "error", "review_receipt_invalid", path, "Unexpected schema.")
            continue
        item_ref = record.get("item_ref")
        if item_ref not in queue_by_ref:
            _add_issue(issues, "error", "review_item_unresolved", path, str(item_ref))
            continue
        records_by_ref.setdefault(item_ref, []).append(record)
        queue_row = queue_by_ref[item_ref]
        if record.get("item_kind") not in {None, queue_row["item_kind"]}:
            _add_issue(
                issues,
                "error",
                "review_item_kind_mismatch",
                path,
                str(record.get("item_kind")),
            )
        reviewer = record.get("reviewer_instance")
        if reviewer not in reviewers:
            _add_issue(issues, "error", "reviewer_identity_unverifiable", path, str(reviewer))
        if reviewer == queue_row["proposer_instance"]:
            _add_issue(issues, "error", "reviewer_not_independent", path, str(reviewer))
        if record.get("proposer_instance") != queue_row["proposer_instance"]:
            _add_issue(issues, "error", "review_receipt_stale", path, "Proposer binding differs.")
        if record.get("item_sha256") != queue_row["item_sha256"] or record.get("review_input_sha256") != queue_row["review_input_sha256"]:
            _add_issue(issues, "error", "review_receipt_stale", path, "Item fingerprint differs.")
        if protocol_enabled:
            required_checks = set(SEMANTIC_REVIEW_CHECKS)
            item_value = record.get("item_kind")
            if item_value == "object":
                proposal = next(
                    (
                        obj
                        for obj in drafts.get(queue_row["unit_id"], {}).get("objects", [])
                        if isinstance(obj, dict)
                        and f"{queue_row['unit_id']}:{obj.get('local_id')}" == item_ref
                    ),
                    None,
                )
                if isinstance(proposal, dict) and isinstance(proposal.get("formula"), dict):
                    required_checks.add("formula_checked")
            _validate_review_trace(
                issues,
                record,
                path=path,
                expected_method=SEMANTIC_REVIEW_METHOD,
                expected_session=str(plan.get("review_session_id")),
                required_checks=required_checks,
            )
        verdict = record.get("verdict")
        if verdict not in {"accepted", "rejected", "quarantined"}:
            _add_issue(issues, "error", "review_verdict_invalid", path, str(verdict))
        issue_codes = record.get("issue_codes")
        if (
            not isinstance(issue_codes, list)
            or any(
                not isinstance(code, str) or not ISSUE_CODE_RE.fullmatch(code)
                for code in issue_codes or []
            )
            or len(issue_codes) != len(set(issue_codes))
        ):
            _add_issue(
                issues,
                "error",
                "review_issue_code_invalid",
                path,
                "issue_codes must be unique stable snake_case identifiers.",
            )
            issue_codes = []
        if not isinstance(record.get("rationale"), str) or not record.get("rationale"):
            _add_issue(
                issues,
                "error",
                "review_rationale_missing",
                path,
                "Independent review requires a non-empty rationale.",
            )
        if verdict == "accepted":
            if record.get("evidence_support") != "full":
                _add_issue(issues, "error", "claim_partially_supported", path, "Accepted items require full evidence support.")
            confidence = record.get("confidence")
            if not isinstance(confidence, dict) or any(
                not isinstance(confidence.get(field), (int, float))
                or isinstance(confidence.get(field), bool)
                or not 0 <= confidence[field] <= 1
                for field in ("extraction", "interpretation")
            ):
                _add_issue(issues, "error", "review_confidence_invalid", path, "Independent confidence is required.")
            accepted_refs.add(item_ref)
        else:
            if not issue_codes:
                _add_issue(
                    issues,
                    "error",
                    "review_receipt_invalid",
                    path,
                    "Rejected or quarantined items require at least one issue code.",
                )
            rejected_refs.add(item_ref)
        if queue_row["item_kind"] == "gap":
            resolution = record.get("gap_resolution")
            if not isinstance(resolution, dict):
                _add_issue(
                    issues,
                    "error",
                    "knowledge_gap_resolution_missing",
                    path,
                    "The package-scope gap disposition is required.",
                )
            else:
                gap_resolutions[item_ref] = resolution
        elif record.get("gap_resolution") is not None:
            _add_issue(
                issues,
                "error",
                "knowledge_gap_resolution_invalid",
                path,
                "Only gap reviews may carry gap_resolution.",
            )
    for item_ref in queue_by_ref:
        rows = records_by_ref.get(item_ref, [])
        if len(rows) != 1:
            _add_issue(issues, "error", "review_receipt_missing" if not rows else "review_receipt_duplicate", item_ref, "Exactly one semantic review record is required.")
    if plan.get("gap_resolution_policy") != "explicit-package-scope-v1":
        _add_issue(
            issues,
            "error",
            "knowledge_gap_resolution_missing",
            "review-plan.json",
            "Phase 2 requires explicit package-scope gap review.",
        )
    for item_ref, resolution in gap_resolutions.items():
        record = records_by_ref[item_ref][0]
        status = resolution.get("status")
        resolver_refs = resolution.get("resolved_by_refs")
        if (
            status not in {"unresolved_in_package", "resolved_elsewhere"}
            or not isinstance(resolver_refs, list)
            or resolver_refs != sorted(set(resolver_refs))
            or any(not isinstance(reference, str) for reference in resolver_refs)
        ):
            _add_issue(
                issues,
                "error",
                "knowledge_gap_resolution_invalid",
                item_ref,
                "Invalid status or resolver reference set.",
            )
            continue
        if status == "unresolved_in_package":
            if resolver_refs or record.get("verdict") != "accepted":
                _add_issue(
                    issues,
                    "error",
                    "knowledge_gap_resolution_invalid",
                    item_ref,
                    "An unresolved package gap must be accepted with no resolver.",
                )
            continue
        if (
            not resolver_refs
            or record.get("verdict") != "rejected"
            or "knowledge_gap_resolved_elsewhere" not in record.get("issue_codes", [])
        ):
            _add_issue(
                issues,
                "error",
                "knowledge_gap_resolution_invalid",
                item_ref,
                "A resolved local gap must be rejected with its stable issue code and resolver.",
            )
            continue
        gap_unit = queue_by_ref[item_ref]["unit_id"]
        for resolver_ref in resolver_refs:
            resolver = queue_by_ref.get(resolver_ref)
            resolver_records = records_by_ref.get(resolver_ref, [])
            if (
                not isinstance(resolver, dict)
                or resolver.get("item_kind") != "object"
                or resolver.get("unit_id") == gap_unit
                or len(resolver_records) != 1
                or resolver_records[0].get("verdict") != "accepted"
                or resolver_records[0].get("evidence_support") != "full"
            ):
                _add_issue(
                    issues,
                    "error",
                    "knowledge_gap_resolution_unresolved",
                    item_ref,
                    f"Resolver is not an accepted full-support object in another unit: {resolver_ref}",
                )
    for item_ref in accepted_refs:
        missing_dependencies = set(queue_by_ref[item_ref].get("dependency_refs", [])) - accepted_refs
        if missing_dependencies:
            _add_issue(
                issues,
                "error",
                "review_dependency_not_accepted",
                item_ref,
                f"Accepted item depends on non-accepted items: {sorted(missing_dependencies)}",
            )

    receipts_by_page: dict[int, list[dict[str, Any]]] = {}
    task_resolution: dict[str, str] = {}
    task_page_verdict: dict[str, str] = {}
    visual_objects: list[dict[str, Any]] = []
    visual_relations: list[dict[str, Any]] = []
    visual_grids: list[dict[str, Any]] = []
    visual_attestation_records: list[dict[str, Any]] = []
    if visual_review_protocol == VISUAL_REVIEW_PROTOCOL:
        try:
            visual_bundle = _load_workpack_visual_bundle(root, manifest)
            visual_objects = [row for row in visual_bundle.get("objects", []) if isinstance(row, dict)]
            visual_relations = [row for row in visual_bundle.get("relations", []) if isinstance(row, dict)]
            visual_grids = [row for row in visual_bundle.get("table_grids", []) if isinstance(row, dict)]
            visual_attestation_records = load_jsonl(root / "visual-review-attestations.jsonl")
        except Exception as error:
            _add_issue(issues, "error", "visual_review_attestation_missing", "visual-review-attestations.jsonl", str(error))
        requirements = plan.get("visual_reviewer_requirements") if isinstance(plan.get("visual_reviewer_requirements"), dict) else _visual_review_requirements(visual_queue)
        if visual_objects:
            visual_issues = validate_visual_review_attestations(
                visual_attestation_records,
                visual_objects,
                review_plan_sha256=sha256_json(plan),
                source_sha256=source_hash,
                proposer_instances=plan.get("proposer_instances", []),
                required_reviewers=requirements,
                table_grids=visual_grids,
                visual_relations=visual_relations,
                expected_review_session_id=str(plan.get("review_session_id")),
                registered_reviewers=plan.get("reviewer_instances", []),
            )
            for issue in visual_issues:
                _add_issue(issues, issue.severity, issue.code, issue.path, issue.message)
        visual_object_attestations = {
            str(identifier): [row for row in visual_attestation_records if isinstance(row, dict) and identifier in row.get("visual_object_ids", [])]
            for identifier in {str(row.get("visual_object_id")) for row in visual_objects}
        }
        for task in tasks:
            identifiers = [str(value) for value in task.get("visual_object_ids", []) if isinstance(value, str)]
            if identifiers and all(
                any(row.get("verdict") == "accepted" for row in visual_object_attestations.get(identifier, []))
                for identifier in identifiers
            ):
                task_resolution[task["id"]] = "supported"
                task_page_verdict[task["id"]] = "verified"
    try:
        receipts = load_jsonl(root / "visual-receipts.jsonl") if visual_review_protocol != VISUAL_REVIEW_PROTOCOL else []
    except Exception as error:
        _add_issue(issues, "error", "formula_visual_receipt_missing", "visual-receipts.jsonl", str(error))
        receipts = []
    for index, receipt in enumerate(receipts):
        path = f"visual-receipts.jsonl:{index + 1}"
        if not isinstance(receipt, dict) or receipt.get("schema_version") != VISUAL_RECEIPT_SCHEMA:
            _add_issue(issues, "error", "visual_receipt_invalid", path, "Unexpected schema.")
            continue
        page = receipt.get("page")
        if page not in visual_by_page:
            _add_issue(issues, "error", "visual_receipt_page_unresolved", path, str(page))
            continue
        receipts_by_page.setdefault(page, []).append(receipt)
        queue_row = visual_by_page[page]
        reviewer = receipt.get("reviewer_instance")
        if reviewer not in reviewers:
            _add_issue(issues, "error", "reviewer_identity_unverifiable", path, str(reviewer))
        if reviewer in set(queue_row["proposer_instances"]):
            _add_issue(issues, "error", "reviewer_not_independent", path, str(reviewer))
        if receipt.get("review_input_sha256") != queue_row["review_input_sha256"]:
            _add_issue(issues, "error", "formula_visual_receipt_stale", path, "Visual input fingerprint differs.")
        if receipt.get("page_asset_sha256") != queue_row["page_asset_sha256"]:
            _add_issue(issues, "error", "formula_visual_receipt_stale", path, "Render fingerprint differs.")
        if protocol_enabled:
            _validate_review_trace(
                issues,
                receipt,
                path=path,
                expected_method=VISUAL_REVIEW_METHOD,
                expected_session=str(plan.get("review_session_id")),
                required_checks=set(VISUAL_REVIEW_CHECKS),
            )
        verdict = receipt.get("verdict")
        if verdict not in {"verified", "contradicted", "unresolved", "not-needed"}:
            _add_issue(issues, "error", "visual_receipt_invalid", path, str(verdict))
        if verdict == "not-needed" and queue_row["proposal_refs"]:
            _add_issue(issues, "error", "visual_not_needed_unsupported", path, "Promoted proposals exist on this page.")
        resolutions = receipt.get("task_resolutions")
        if not isinstance(resolutions, list):
            _add_issue(issues, "error", "visual_receipt_invalid", path, "task_resolutions must be an array.")
            resolutions = []
        seen_tasks: set[str] = set()
        for resolution in resolutions:
            if not isinstance(resolution, dict) or resolution.get("task_id") not in queue_row["visual_task_ids"]:
                _add_issue(issues, "error", "visual_task_unresolved", path, str(resolution))
                continue
            task_id = resolution["task_id"]
            if task_id in seen_tasks:
                _add_issue(
                    issues,
                    "error",
                    "visual_task_duplicate",
                    path,
                    str(task_id),
                )
                continue
            seen_tasks.add(task_id)
            task_verdict = resolution.get("verdict")
            if task_verdict not in {"supported", "not-present", "contradicted", "unresolved"}:
                _add_issue(issues, "error", "visual_task_verdict_invalid", path, str(task_verdict))
            if not isinstance(resolution.get("observation"), str) or not resolution.get("observation"):
                _add_issue(issues, "error", "visual_observation_missing", path, str(task_id))
            task_resolution[task_id] = task_verdict
            task_page_verdict[task_id] = str(verdict)
        if seen_tasks != set(queue_row["visual_task_ids"]):
            _add_issue(issues, "error", "visual_task_coverage_incomplete", path, "Every page task requires a disposition.")
    if visual_review_protocol != VISUAL_REVIEW_PROTOCOL:
        for page in visual_by_page:
            rows = receipts_by_page.get(page, [])
            if len(rows) != 1:
                _add_issue(issues, "error", "formula_visual_receipt_missing" if not rows else "visual_receipt_duplicate", f"page:{page}", "Exactly one visual receipt per routed page is required.")
    _validate_fragment_manifest(
        issues,
        root,
        manifest,
        reviewers,
        attestation_records
        if protocol == ASSURANCE_REVIEW_PROTOCOL
        else records,
        receipts,
        visual_attestation_records if visual_review_protocol == VISUAL_REVIEW_PROTOCOL else None,
    )
    anomalies = _review_blanket_anomalies(
        {
            str(record.get("item_ref")): record
            for record in records
            if isinstance(record, dict) and isinstance(record.get("item_ref"), str)
        },
        {
            int(receipt.get("page")): receipt
            for receipt in receipts
            if isinstance(receipt, dict) and isinstance(receipt.get("page"), int)
        },
    )
    if anomalies:
        blanket_code = (
            "visual_review_blanket_pattern"
            if all(anomaly.startswith("visual:") for anomaly in anomalies)
            else "review_blanket_pattern"
        )
        _add_issue(
            issues,
            "error",
            blanket_code,
            "review-records.jsonl",
            f"Template-style blanket review pattern: {','.join(anomalies)}",
        )
        _quarantine_review_material(
            root,
            blanket_code,
            {"anomalies": anomalies, "review_item_count": len(records), "visual_receipt_count": len(receipts)},
        )

    object_by_ref: dict[str, dict[str, Any]] = {}
    accepted_conflict_refs: set[str] = set()
    for unit_id, draft in drafts.items():
        for obj in draft.get("objects", []):
            if isinstance(obj, dict) and isinstance(obj.get("local_id"), str):
                object_by_ref[_draft_object_ref(unit_id, obj["local_id"])] = obj
        for index, conflict in enumerate(draft.get("conflicts", [])):
            if not isinstance(conflict, dict):
                continue
            local = conflict.get("local_id", str(index))
            conflict_ref = f"conf:{unit_id}:{local}"
            if conflict_ref in accepted_refs:
                accepted_conflict_refs.update(
                    reference
                    for reference in conflict.get("claim_refs", [])
                    if isinstance(reference, str)
                )
    for reference in accepted_refs & set(object_by_ref):
        obj = object_by_ref[reference]
        if obj.get("type") in {"Procedure", "DecisionRule"}:
            _add_issue(
                issues,
                "error",
                "capability_premature",
                reference,
                "Phase 2 may not promote procedure or decision contracts.",
            )
        linked_tasks = obj.get("visual_task_ids", [])
        # Legacy workpacks retain their page-receipt compatibility path.  The
        # exact-object support gate is exclusive to the new v0.2 visual
        # semantics protocol and must not break earlier review contracts.
        if isinstance(linked_tasks, list):
            if visual_review_protocol == VISUAL_REVIEW_PROTOCOL:
                page_only_tasks = [
                    task_id
                    for task_id in linked_tasks
                    if isinstance(task_id, str)
                    and isinstance(task_by_id.get(task_id), dict)
                    and not (
                        task_by_id[task_id].get("visual_object_ids")
                        or task_by_id[task_id].get("table_cell_ids")
                        or task_by_id[task_id].get("series_ids")
                    )
                ]
                if page_only_tasks:
                    _add_issue(
                        issues,
                        "error",
                        "visual_fact_object_support_missing",
                        reference,
                        f"New visual-semantics workpacks cannot promote page-only visual tasks: {page_only_tasks}",
                    )
            unsupported = [
                task_id
                for task_id in linked_tasks
                if task_resolution.get(task_id) != "supported"
                or task_page_verdict.get(task_id) != "verified"
            ]
            if unsupported:
                _add_issue(
                    issues,
                    "error",
                    "visual_claim_support_missing",
                    reference,
                    f"Accepted proposal has unverified visual tasks: {unsupported}",
                )
        if obj.get("type") == "Equation":
            if not linked_tasks or not any(
                task_resolution.get(task_id) == "supported"
                and task_page_verdict.get(task_id) == "verified"
                for task_id in linked_tasks
            ):
                _add_issue(issues, "error", "formula_visual_receipt_missing", reference, "Accepted equation lacks a supported visual task.")
        formula = obj.get("formula")
        if isinstance(formula, dict) and (
            formula.get("representation_relation") == "conflicted"
            or formula.get("dimension_check") == "inconsistent"
        ) and reference not in accepted_conflict_refs:
            _add_issue(
                issues,
                "error",
                "conflict_omitted",
                reference,
                "Accepted conflicted formula lacks an accepted conflict record.",
            )

    summary.update(
        {
            "review_plan_bundle_sha256": bundle_hash,
            "review_item_count": len(queue),
            "accepted_review_item_count": len(accepted_refs),
            "rejected_or_quarantined_item_count": len(rejected_refs),
            "visual_receipt_count": len(receipts),
            "visual_attestation_count": len(visual_attestation_records),
            "visual_review_protocol": visual_review_protocol,
            "reviewed": not any(issue.severity == "error" for issue in issues),
        }
    )
    return sorted(issues, key=lambda item: (item.severity != "error", item.code, item.path)), summary


def _proposal_items(
    drafts: dict[str, dict[str, Any]],
) -> dict[str, tuple[str, dict[str, Any]]]:
    items: dict[str, tuple[str, dict[str, Any]]] = {}
    for unit_id in sorted(drafts):
        draft = drafts[unit_id]
        for obj in draft.get("objects", []):
            if isinstance(obj, dict) and isinstance(obj.get("local_id"), str):
                items[_draft_object_ref(unit_id, obj["local_id"])] = (unit_id, obj)
        for index, relation in enumerate(draft.get("relations", [])):
            if isinstance(relation, dict):
                items[f"rel:{unit_id}:{index}"] = (unit_id, relation)
        for index, conflict in enumerate(draft.get("conflicts", [])):
            if isinstance(conflict, dict):
                local = conflict.get("local_id", str(index))
                items[f"conf:{unit_id}:{local}"] = (unit_id, conflict)
        for index, gap in enumerate(draft.get("gaps", [])):
            if isinstance(gap, dict):
                items[f"gap:{unit_id}:{index}"] = (unit_id, gap)
    return items


def _word_tokens(value: str) -> list[str]:
    return re.findall(r"[^\W_]+(?:[-'][^\W_]+)*", value.casefold(), re.UNICODE)


def _cjk_stream(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value)
        if (
            "\u3400" <= character <= "\u4dbf"
            or "\u4e00" <= character <= "\u9fff"
            or "\uf900" <= character <= "\ufaff"
            or "\u3040" <= character <= "\u30ff"
            or "\uac00" <= character <= "\ud7af"
        )
    )


def _iter_strings(value: Any, path: str = "output") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key in sorted(value, key=str):
            yield from _iter_strings(value[key], f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_strings(item, f"{path}[{index}]")


def _verbatim_window(
    source_texts: Iterable[str],
    values: Iterable[tuple[str, str]],
    *,
    window: int = 21,
    cjk_window: int = 24,
) -> tuple[str, str] | None:
    source_values = list(source_texts)
    source_windows: set[tuple[str, ...]] = set()
    source_cjk_windows: set[str] = set()
    for source_text in source_values:
        source_tokens = _word_tokens(source_text)
        source_windows.update(
            tuple(source_tokens[index : index + window])
            for index in range(max(0, len(source_tokens) - window + 1))
        )
        source_cjk = _cjk_stream(source_text)
        source_cjk_windows.update(
            source_cjk[index : index + cjk_window]
            for index in range(max(0, len(source_cjk) - cjk_window + 1))
        )
    for path, value in values:
        tokens = _word_tokens(value)
        for index in range(len(tokens) - window + 1):
            candidate = tuple(tokens[index : index + window])
            if candidate in source_windows:
                return path, " ".join(candidate)
        cjk = _cjk_stream(value)
        for index in range(len(cjk) - cjk_window + 1):
            candidate_cjk = cjk[index : index + cjk_window]
            if candidate_cjk in source_cjk_windows:
                return path, candidate_cjk
    return None


def _write_text_secure(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)


def promote_reviewed_workpack(
    source_path: Path,
    workpack_root: Path,
    output: Path,
    *,
    name: str,
    display_name: str,
    domain: str,
    source_title: str | None = None,
) -> dict[str, Any]:
    """Promote independently reviewed proposals into a non-executable draft Skill."""

    if not SLUG_RE.fullmatch(name):
        raise SemanticPromotionError("invalid_package_name")
    if not SLUG_RE.fullmatch(domain):
        raise SemanticPromotionError("invalid_domain")
    if not display_name.strip():
        raise SemanticPromotionError("display_name_required")
    issues, reviewed_summary = validate_reviewed_workpack(source_path, workpack_root)
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        raise SemanticPromotionError(
            f"reviewed_gate_failed: {errors[0].code} {errors[0].path}"
        )

    root = workpack_root.expanduser().resolve()
    source = source_path.expanduser().resolve()
    reader, source_hash, extractor = _load_source(source)
    manifest = load_json(root / "workpack.json")
    plan = load_json(root / "review-plan.json")
    units = {
        entry["unit_id"]: load_json(_resolve_workpack_path(root, entry["path"]))
        for entry in manifest["units"]
    }
    tasks = load_jsonl(_resolve_workpack_path(root, manifest["visual_tasks"]))
    task_by_id = {
        task["id"]: task
        for task in tasks
        if isinstance(task, dict) and isinstance(task.get("id"), str)
    }
    drafts, _ = _load_workpack_drafts(root, manifest)
    proposal_items = _proposal_items(drafts)
    queue, _, bundle_hash = _review_material_for_manifest(
        source_hash,
        manifest,
        units,
        tasks,
        drafts,
        workpack_root=root,
        visual_review_scope=str(plan.get("visual_review_scope", "all")),
    )
    queue_by_ref = {row["item_ref"]: row for row in queue}
    review_records = load_jsonl(root / "review-records.jsonl")
    review_by_ref = {
        record["item_ref"]: record
        for record in review_records
        if isinstance(record, dict) and isinstance(record.get("item_ref"), str)
    }
    assurance_attestations = (
        load_jsonl(root / "review-attestations.jsonl")
        if is_assurance_enabled(manifest)
        else []
    )
    attestations_by_ref: dict[str, list[dict[str, Any]]] = {}
    for attestation in assurance_attestations:
        if isinstance(attestation, dict) and isinstance(attestation.get("item_ref"), str):
            attestations_by_ref.setdefault(attestation["item_ref"], []).append(
                attestation
            )
    visual_receipts = load_jsonl(root / "visual-receipts.jsonl")
    receipt_by_page = {
        receipt["page"]: receipt
        for receipt in visual_receipts
        if isinstance(receipt, dict) and isinstance(receipt.get("page"), int)
    }
    visual_protocol = plan.get("visual_review_protocol")
    visual_bundle: dict[str, Any] | None = None
    visual_attestations: list[dict[str, Any]] = []
    promoted_visual_objects: list[dict[str, Any]] = []
    promoted_visual_relations: list[dict[str, Any]] = []
    promoted_table_grids: list[dict[str, Any]] = []
    promoted_visual_conflicts: list[dict[str, Any]] = []
    visual_manifest: dict[str, Any] | None = None
    visual_attestations_by_object: dict[str, list[dict[str, Any]]] = {}
    if visual_protocol == VISUAL_REVIEW_PROTOCOL:
        frozen_visual_requirements = plan.get("visual_reviewer_requirements")
        if not isinstance(frozen_visual_requirements, dict) or not frozen_visual_requirements:
            raise SemanticPromotionError("visual_reviewer_requirements_missing")
        visual_bundle = _load_workpack_visual_bundle(root, manifest)
        visual_attestations = load_jsonl(root / "visual-review-attestations.jsonl")
        accepted_visual_ids = {
            str(identifier)
            for row in visual_attestations
            if isinstance(row, dict) and row.get("verdict") == "accepted"
            for identifier in row.get("visual_object_ids", [])
            if isinstance(identifier, str)
        }
        for row in visual_attestations:
            if isinstance(row, dict):
                for identifier in row.get("visual_object_ids", []):
                    if isinstance(identifier, str) and row.get("verdict") == "accepted":
                        visual_attestations_by_object.setdefault(identifier, []).append(row)
        promoted_visual_objects = [
            {**dict(row), "status": "promoted"}
            for row in visual_bundle.get("objects", [])
            if isinstance(row, dict) and row.get("visual_object_id") in accepted_visual_ids
        ]
        promoted_visual_relations = [
            {**dict(row), "status": "promoted"}
            for row in visual_bundle.get("relations", [])
            if isinstance(row, dict)
            and row.get("source_visual_object_id") in accepted_visual_ids
            and row.get("target_visual_object_id") in accepted_visual_ids
        ]
        promoted_table_grids = [
            {**dict(row), "status": "promoted"}
            for row in visual_bundle.get("table_grids", [])
            if isinstance(row, dict) and row.get("table_visual_object_id") in accepted_visual_ids
        ]
        promoted_visual_conflicts = [
            {**dict(row), "object_ids": [identifier for identifier in row.get("object_ids", []) if identifier in accepted_visual_ids]}
            for row in visual_bundle.get("conflicts", [])
            if isinstance(row, dict) and all(identifier in accepted_visual_ids for identifier in row.get("object_ids", []))
        ]
        for row in promoted_visual_objects:
            row["object_sha256"] = sha256_json({key: value for key, value in row.items() if key != "object_sha256"})
        for row in promoted_visual_relations:
            row["relation_sha256"] = sha256_json({key: value for key, value in row.items() if key not in {"relation_sha256", "status"}})
        for row in promoted_table_grids:
            row["grid_sha256"] = sha256_json({key: value for key, value in row.items() if key not in {"grid_sha256", "status"}})
        for row in promoted_visual_conflicts:
            row["conflict_sha256"] = sha256_json({key: value for key, value in row.items() if key != "conflict_sha256"})
        visual_manifest = visual_assurance_manifest(
            source_sha256=source_hash,
            objects=promoted_visual_objects,
            relations=promoted_visual_relations,
            grids=promoted_table_grids,
            conflicts=promoted_visual_conflicts,
            attestations=visual_attestations,
            whole_book_completeness_claimed=False,
            review_plan_sha256=sha256_json(plan),
            review_session_id=str(plan.get("review_session_id")),
            registered_reviewers=plan.get("reviewer_instances", []),
            required_reviewers=frozen_visual_requirements,
        )
    accepted_refs = {
        item_ref
        for item_ref, record in review_by_ref.items()
        if record.get("verdict") == "accepted"
    }
    source_id = manifest["source"]["source_id"]
    scope = manifest["source"]["scope"]
    page_texts = _page_texts_for_workpack(
        reader,
        root,
        manifest,
        scope["start_page"],
        scope["end_page"],
    )
    scan_evidence = _load_workpack_scan_evidence(
        root,
        manifest,
        require_hardened=(
            isinstance(manifest.get("scan_candidate"), dict)
            and manifest["scan_candidate"].get("schema_version")
            == "tkc.scanned-pdf-workpack-candidate/v0.2"
        ),
    )
    scan_manifest = (
        manifest.get("scan_candidate")
        if isinstance(manifest.get("scan_candidate"), dict)
        else None
    )

    object_refs = {
        item_ref
        for item_ref in accepted_refs
        if queue_by_ref[item_ref]["item_kind"] == "object"
    }
    object_id_by_ref: dict[str, str] = {}
    promoted_id_by_ref: dict[str, str] = {}
    object_values: dict[str, dict[str, Any]] = {}
    for reference in sorted(object_refs):
        _, obj = proposal_items[reference]
        evidence_key = tuple(
            sorted(
                (
                    int(span["page"]),
                    int(span["start"]),
                    int(span["end"]),
                    str(span["span_sha256"]),
                )
                for span in obj["evidence_spans"]
            )
        )
        object_id = stable_id(
            "ko",
            source_id,
            obj["type"],
            obj["origin"],
            normalize_text(obj["statement"]),
            evidence_key,
        )
        if object_id in object_values:
            raise SemanticPromotionError(f"promotion_object_collision: {object_id}")
        object_id_by_ref[reference] = object_id
        promoted_id_by_ref[reference] = object_id
        object_values[object_id] = obj

    def review_binding(reference: str) -> dict[str, Any]:
        review = review_by_ref[reference]
        binding = {
            "attestation_level": plan["attestation_level"],
            "reviewer_instance": review["reviewer_instance"],
            "item_sha256": review["item_sha256"],
            "review_input_sha256": review["review_input_sha256"],
            "review_bundle_sha256": bundle_hash,
        }
        if is_assurance_enabled(manifest):
            rows = sorted(
                attestations_by_ref.get(reference, []),
                key=lambda row: str(row.get("reviewer_instance")),
            )
            binding["reviewer_instances"] = [
                row["reviewer_instance"] for row in rows
            ]
            binding["attestation_ids"] = [row["attestation_id"] for row in rows]
            binding["semantic_assurance_protocol"] = SEMANTIC_ASSURANCE_PROTOCOL
        if visual_protocol == VISUAL_REVIEW_PROTOCOL and reference in proposal_items:
            proposal = proposal_items[reference][1]
            task_ids = [value for value in proposal.get("visual_task_ids", []) if isinstance(value, str)]
            task_rows = [task_by_id[task_id] for task_id in task_ids if task_id in task_by_id]
            visual_ids = sorted({str(value) for task in task_rows for value in task.get("visual_object_ids", []) if isinstance(value, str)})
            cell_ids = sorted({str(value) for task in task_rows for value in task.get("table_cell_ids", []) if isinstance(value, str)})
            series_ids = sorted({str(value) for task in task_rows for value in task.get("series_ids", []) if isinstance(value, str)})
            if task_ids:
                binding["visual_object_ids"] = visual_ids
                binding["table_cell_ids"] = cell_ids
                binding["series_ids"] = series_ids
                binding["visual_relation_ids"] = sorted({str(value) for task in task_rows for value in task.get("visual_relation_ids", []) if isinstance(value, str)})
                binding["relation_sha256s"] = sorted({str(value) for task in task_rows for value in task.get("relation_sha256s", []) if isinstance(value, str)})
                binding["table_grid_ids"] = sorted({str(value) for task in task_rows for value in task.get("table_grid_ids", []) if isinstance(value, str)})
                binding["table_grid_sha256s"] = sorted({str(value) for task in task_rows for value in task.get("table_grid_sha256s", []) if isinstance(value, str)})
                binding["render_sha256s"] = sorted({str(task.get("page_asset_sha256")) for task in task_rows if isinstance(task.get("page_asset_sha256"), str)})
                binding["crop_sha256s"] = sorted({str(value) for task in task_rows for value in task.get("crop_sha256s", []) if isinstance(value, str)})
                binding["bbox_sha256s"] = sorted({str(value) for task in task_rows for value in task.get("bbox_sha256s", []) if isinstance(value, str)})
                binding["visual_review_attestation_ids"] = sorted({str(row.get("attestation_id")) for identifier in visual_ids for row in visual_attestations_by_object.get(identifier, []) if isinstance(row.get("attestation_id"), str)})
                binding["visual_review_protocol"] = VISUAL_REVIEW_PROTOCOL
        return binding

    anchor_groups: dict[
        tuple[str, tuple[tuple[int, int, int, str], ...]], dict[str, Any]
    ] = {}
    anchor_ids_by_object: dict[str, list[str]] = {object_id: [] for object_id in object_values}
    for reference in sorted(object_refs):
        unit_id, obj = proposal_items[reference]
        span_key = tuple(
            sorted(
                (
                    int(span["page"]),
                    int(span["start"]),
                    int(span["end"]),
                    str(span["span_sha256"]),
                )
                for span in obj["evidence_spans"]
            )
        )
        group_key = (str(units[unit_id]["segment_id"]), span_key)
        group = anchor_groups.setdefault(
            group_key,
            {
                "unit_id": unit_id,
                "segment_id": units[unit_id]["segment_id"],
                "spans": span_key,
                "supports": [],
                "visual_task_ids": set(),
            },
        )
        object_id = object_id_by_ref[reference]
        group["supports"].append(object_id)
        group["visual_task_ids"].update(obj.get("visual_task_ids", []))

    anchors: list[dict[str, Any]] = []
    for (segment_id, span_key), group in sorted(
        anchor_groups.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        anchor_id = stable_id("ev", source_id, segment_id, span_key)
        pages = sorted({span[0] for span in span_key})
        text_spans = [
            {
                "page": page,
                "start": start,
                "end": end,
                "page_text_sha256": sha256_text(page_texts[page]),
                "span_sha256": span_hash,
            }
            for page, start, end, span_hash in span_key
        ]
        excerpt_sha256 = sha256_text(
            "\f".join(page_texts[page][start:end] for page, start, end, _ in span_key)
        )
        locator: dict[str, Any] = {
            "hash_scope": "normalized native text spans",
            "extractor": extractor,
            "normalization": NORMALIZATION_PROFILE,
            "offset_unit": "unicode-code-point",
            "interval": "[start,end)",
            "review_bundle_sha256": bundle_hash,
        }
        scan_locator: dict[str, Any] | None = None
        scan_pages = (
            set(scan_manifest.get("ocr_pages", []))
            if isinstance(scan_manifest, dict)
            else set()
        )
        scan_span_rows = [
            {
                "page": page,
                "start": start,
                "end": end,
                "page_text_sha256": sha256_text(page_texts[page]),
                "span_sha256": span_hash,
            }
            for page, start, end, span_hash in span_key
            if page in scan_pages
        ]
        if scan_span_rows:
            if (
                not isinstance(scan_evidence, dict)
                or not isinstance(scan_manifest, dict)
                or scan_manifest.get("schema_version")
                != "tkc.scanned-pdf-workpack-candidate/v0.2"
                or scan_manifest.get("structure_review_status") != "accepted"
            ):
                raise SemanticPromotionError(
                    f"scan_structure_review_required: segment={segment_id}"
                )
            heading_scan_locator = units[unit_id].get("heading_locator", {}).get("scan_locator")
            structure_reviewer = scan_manifest.get("structure_reviewer")
            if not isinstance(heading_scan_locator, dict) or not isinstance(structure_reviewer, dict):
                raise SemanticPromotionError(f"scan_anchor_review_binding_missing: segment={segment_id}")
            scan_review_binding = {
                "proposer_instance": heading_scan_locator.get("proposer_instance")
                or structure_reviewer.get("proposer_instance"),
                "reviewer_instance": heading_scan_locator.get("reviewer_instance")
                or structure_reviewer.get("reviewer_instance"),
                "review_session_id": heading_scan_locator.get("review_session_id")
                or structure_reviewer.get("review_session_id"),
                "review_plan_sha256": heading_scan_locator.get("review_plan_sha256")
                or structure_reviewer.get("review_plan_sha256"),
                "review_input_sha256": heading_scan_locator.get("review_input_sha256"),
                "candidate_sha256": heading_scan_locator.get("candidate_sha256"),
                "external_fragment_sha256": heading_scan_locator.get("external_fragment_sha256")
                or structure_reviewer.get("external_fragment_sha256"),
                "review_attestation_sha256": heading_scan_locator.get("review_attestation_sha256")
                or structure_reviewer.get("attestation_sha256"),
                "review_independence_basis": heading_scan_locator.get("review_independence_basis")
                or structure_reviewer.get("review_independence_basis"),
            }
            if any(value is None for value in scan_review_binding.values()):
                raise SemanticPromotionError(f"scan_anchor_review_binding_missing: segment={segment_id}")
            try:
                scan_locator = build_scan_evidence_locator(
                    source_sha256=source_hash,
                    evidence_spans=scan_span_rows,
                    page_texts=page_texts,
                    transcripts=scan_evidence["transcripts"],
                    observations=scan_evidence["observations"],
                    backend_receipts=scan_evidence["backend_receipts"],
                    transforms=scan_evidence["transforms"],
                    render_receipts=scan_evidence["render_receipts"],
                    primary_backend=scan_evidence["primary_backend"],
                    excerpt_sha256=excerpt_sha256,
                    review_binding=scan_review_binding,
                )
            except ScanOcrError as error:
                raise SemanticPromotionError(
                    f"scan_anchor_evidence_binding_invalid: segment={segment_id}: {error}"
                ) from error
            locator["hash_scope"] = "reviewed OCR transcript spans"
            locator["extractor"] = "scan-ocr-projection-v0.1"
        visual_task_ids = sorted(group["visual_task_ids"])
        visual_receipt_pages: list[int] = []
        if visual_task_ids:
            visual_receipt_pages = sorted(
                {task_by_id[task_id]["page"] for task_id in visual_task_ids}
            )
            receipt_hashes = sorted(
                {
                    sha256_json(receipt_by_page[page])
                    for page in visual_receipt_pages
                }
            )
            locator["visual_receipts_sha256"] = sha256_json(receipt_hashes)
        if isinstance(scan_locator, dict):
            scan_span_by_key = {
                (row["page"], row["start"], row["end"], row["span_sha256"]): row
                for row in scan_locator.get("evidence_spans", [])
                if isinstance(row, dict)
            }
            for text_span in text_spans:
                bound = scan_span_by_key.get(
                    (
                        text_span["page"],
                        text_span["start"],
                        text_span["end"],
                        text_span["span_sha256"],
                    )
                )
                if bound is not None:
                    text_span.update(
                        {
                            "scan_evidence_span_id": bound["id"],
                            "transcript_span_ids": list(bound["transcript_span_ids"]),
                            "observation_ids": list(bound["observation_ids"]),
                            "bbox_pdf_points": list(bound["bbox_pdf_points"]),
                            "bbox_sha256": bound["bbox_sha256"],
                        }
                    )
        supports = sorted(group["supports"])
        anchor = {
            "schema_version": SCHEMA_ID,
            "id": anchor_id,
            "source_id": source_id,
            "pages": pages,
            "printed_page_labels": [
                detect_printed_page_label(reader.pages[page - 1].extract_text() or "")
                or str(reader.page_labels[page - 1])
                for page in pages
            ],
            "segment_id": segment_id,
            "locator": locator,
            "text_spans": text_spans,
            "excerpt_sha256": excerpt_sha256,
            "supports": supports,
        }
        if isinstance(scan_locator, dict):
            anchor["scan_locator"] = dict(scan_locator)
        if visual_receipt_pages:
            anchor["visual_receipt_pages"] = visual_receipt_pages
        anchors.append(anchor)
        for object_id in supports:
            anchor_ids_by_object[object_id].append(anchor_id)

    promoted_objects: list[dict[str, Any]] = []
    generated_text: list[tuple[str, str]] = []
    for reference in sorted(object_refs, key=lambda item: object_id_by_ref[item]):
        object_id = object_id_by_ref[reference]
        _, proposal = proposal_items[reference]
        review = review_by_ref[reference]
        proposal_formula = proposal.get("formula")
        promoted_formula = None
        if isinstance(proposal_formula, dict):
            promoted_formula = {
                field: proposal_formula.get(field)
                for field in (
                    "source_notation",
                    "normalized_notation",
                    "derived_notation",
                    "representation_relation",
                    "dimension_check",
                )
            }
            if isinstance(proposal_formula.get("ast"), dict):
                promoted_formula["ast_schema_version"] = FORMULA_AST_SCHEMA
                promoted_formula["ast"] = proposal_formula["ast"]
                promoted_formula["ast_sha256"] = formula_sha256_json(proposal_formula["ast"])
        promoted = {
            "schema_version": SCHEMA_ID,
            "id": object_id,
            "type": proposal["type"],
            "origin": proposal["origin"],
            "title": proposal["title"],
            "statement": proposal["statement"],
            "source_ids": [source_id],
            "evidence_ids": sorted(anchor_ids_by_object[object_id]),
            "assumptions": proposal["assumptions"],
            "valid_when": proposal["valid_when"],
            "fails_when": proposal["fails_when"],
            "applicability": proposal["applicability"],
            "formula": promoted_formula,
            "confidence": review["confidence"],
            "review_binding": review_binding(reference),
            "status": "independently-reviewed-draft",
        }
        if visual_protocol == VISUAL_REVIEW_PROTOCOL:
            task_ids = [value for value in proposal.get("visual_task_ids", []) if isinstance(value, str)]
            task_rows = [task_by_id[task_id] for task_id in task_ids if task_id in task_by_id]
            visual_ids = sorted({str(value) for task in task_rows for value in task.get("visual_object_ids", []) if isinstance(value, str)})
            cell_ids = sorted({str(value) for task in task_rows for value in task.get("table_cell_ids", []) if isinstance(value, str)})
            series_ids = sorted({str(value) for task in task_rows for value in task.get("series_ids", []) if isinstance(value, str)})
            if task_ids:
                # Preserve the fact that this semantic object depends on a
                # visual task even when the task has no exact object binding;
                # validation then emits visual_fact_object_support_missing
                # instead of silently falling back to page-level support.
                promoted["visual_task_ids"] = sorted(set(task_ids))
                promoted["visual_object_ids"] = visual_ids
                promoted["table_cell_ids"] = cell_ids
                promoted["series_ids"] = series_ids
                promoted["visual_relation_ids"] = sorted({str(value) for task in task_rows for value in task.get("visual_relation_ids", []) if isinstance(value, str)})
                promoted["relation_sha256s"] = sorted({str(value) for task in task_rows for value in task.get("relation_sha256s", []) if isinstance(value, str)})
                promoted["table_grid_ids"] = sorted({str(value) for task in task_rows for value in task.get("table_grid_ids", []) if isinstance(value, str)})
                promoted["table_grid_sha256s"] = sorted({str(value) for task in task_rows for value in task.get("table_grid_sha256s", []) if isinstance(value, str)})
                promoted["render_sha256s"] = sorted({str(task.get("page_asset_sha256")) for task in task_rows if isinstance(task.get("page_asset_sha256"), str)})
                promoted["crop_sha256s"] = sorted({str(value) for task in task_rows for value in task.get("crop_sha256s", []) if isinstance(value, str)})
                promoted["bbox_sha256s"] = sorted({str(value) for task in task_rows for value in task.get("bbox_sha256s", []) if isinstance(value, str)})
                promoted["visual_review_attestation_ids"] = sorted({str(row.get("attestation_id")) for identifier in visual_ids for row in visual_attestations_by_object.get(identifier, []) if isinstance(row.get("attestation_id"), str)})
        promoted_objects.append(promoted)
        for field in ("title", "statement"):
            generated_text.append((f"{object_id}.{field}", str(promoted[field])))
        for field in ("assumptions", "valid_when", "fails_when"):
            generated_text.extend(
                (f"{object_id}.{field}[{index}]", value)
                for index, value in enumerate(promoted[field])
            )
        if promoted_formula is not None:
            generated_text.extend(
                (f"{object_id}.formula.{field}", value)
                for field, value in promoted_formula.items()
                if isinstance(value, str)
            )

    promoted_relations: list[dict[str, Any]] = []
    for reference in sorted(accepted_refs):
        if queue_by_ref[reference]["item_kind"] != "relation":
            continue
        _, proposal = proposal_items[reference]
        source_object_id = object_id_by_ref[proposal["source_ref"]]
        target_object_id = object_id_by_ref[proposal["target_ref"]]
        relation_id = stable_id(
            "relation",
            source_id,
            source_object_id,
            proposal["type"],
            target_object_id,
        )
        if relation_id in promoted_id_by_ref.values():
            raise SemanticPromotionError(f"promotion_relation_collision: {relation_id}")
        promoted_id_by_ref[reference] = relation_id
        promoted_relations.append(
            {
                "schema_version": SCHEMA_ID,
                "id": relation_id,
                "source_id": source_object_id,
                "type": proposal["type"],
                "target_id": target_object_id,
                "review_binding": review_binding(reference),
            }
        )
    promoted_relations.sort(key=lambda row: (row["source_id"], row["type"], row["target_id"]))

    promoted_conflicts: list[dict[str, Any]] = []
    for reference in sorted(accepted_refs):
        if queue_by_ref[reference]["item_kind"] != "conflict":
            continue
        _, proposal = proposal_items[reference]
        claims = []
        for claim_ref in proposal["claim_refs"]:
            object_id = object_id_by_ref[claim_ref]
            obj = object_values[object_id]
            claims.append(
                {
                    "claim_id": object_id,
                    "statement": obj["statement"],
                    "evidence_ids": sorted(anchor_ids_by_object[object_id]),
                    "origin": obj["origin"],
                }
            )
        conflict_id = stable_id(
            "conflict", source_id, proposal["topic"], sorted(object_id_by_ref[ref] for ref in proposal["claim_refs"])
        )
        if conflict_id in promoted_id_by_ref.values():
            raise SemanticPromotionError(f"promotion_conflict_collision: {conflict_id}")
        promoted_id_by_ref[reference] = conflict_id
        promoted_conflicts.append(
            {
                "schema_version": SCHEMA_ID,
                "id": conflict_id,
                "topic": proposal["topic"],
                "claims": claims,
                "possible_causes": proposal.get("possible_causes", []),
                "status": "unresolved",
                "requires_review": True,
                "review_binding": review_binding(reference),
            }
        )
        generated_text.append((f"{conflict_id}.topic", proposal["topic"]))

    promoted_gaps: list[dict[str, Any]] = []
    for reference in sorted(accepted_refs):
        if queue_by_ref[reference]["item_kind"] != "gap":
            continue
        unit_id, proposal = proposal_items[reference]
        gap_id = stable_id("gap", source_id, unit_id, proposal["statement"])
        if gap_id in promoted_id_by_ref.values():
            raise SemanticPromotionError(f"promotion_gap_collision: {gap_id}")
        promoted_id_by_ref[reference] = gap_id
        gap_spans = [
            {
                "page": int(span["page"]),
                "start": int(span["start"]),
                "end": int(span["end"]),
                "page_text_sha256": sha256_text(page_texts[int(span["page"])]),
                "span_sha256": str(span["span_sha256"]),
            }
            for span in proposal["evidence_spans"]
        ]
        promoted_gaps.append(
            {
                "schema_version": "tkc.knowledge-gap/v0.1",
                "id": gap_id,
                "origin": "knowledge-gap",
                "statement": proposal["statement"],
                "scope": "package",
                "status": "unresolved",
                "source_id": source_id,
                "source_item_ref": reference,
                "unit_id": unit_id,
                "evidence_spans": gap_spans,
                "excerpt_sha256": sha256_text(
                    "\f".join(
                        page_texts[span["page"]][span["start"] : span["end"]]
                        for span in proposal["evidence_spans"]
                    )
                ),
                "review_binding": review_binding(reference),
            }
        )
        generated_text.append((f"{gap_id}.statement", proposal["statement"]))

    sanitized_review_records = []
    for item_ref in sorted(queue_by_ref):
        queue_row = queue_by_ref[item_ref]
        record = review_by_ref[item_ref]
        sanitized = {
            "schema_version": REVIEW_SCHEMA,
            "item_ref": item_ref,
            "item_kind": queue_row["item_kind"],
            "reviewer_instance": record["reviewer_instance"],
            "proposer_instance": record["proposer_instance"],
            "item_sha256": record["item_sha256"],
            "review_input_sha256": record["review_input_sha256"],
            "verdict": record["verdict"],
            "evidence_support": record.get("evidence_support"),
            "issue_codes": record["issue_codes"],
            "rationale": record["rationale"],
            "confidence": record.get("confidence"),
        }
        for field in ("review_session_id", "review_method", "review_checks"):
            if field in record:
                sanitized[field] = record[field]
        if queue_row["item_kind"] == "gap":
            sanitized["gap_resolution"] = record.get("gap_resolution")
        elif queue_row["item_kind"] == "object":
            proposal = proposal_items[item_ref][1]
            formula = proposal.get("formula") if isinstance(proposal, dict) else None
            if isinstance(formula, dict) and isinstance(formula.get("ast"), dict):
                sanitized["formula_ast_sha256"] = formula_sha256_json(formula["ast"])
        sanitized_review_records.append(sanitized)
        generated_text.append((f"review:{item_ref}.rationale", record["rationale"]))
    sanitized_review_by_ref = {
        record["item_ref"]: record for record in sanitized_review_records
    }
    sanitized_visual_receipts = []
    for receipt in sorted(visual_receipts, key=lambda row: row["page"]):
        sanitized = {
            "schema_version": VISUAL_RECEIPT_SCHEMA,
            "page": receipt["page"],
            "reviewer_instance": receipt["reviewer_instance"],
            "review_input_sha256": receipt["review_input_sha256"],
            "page_asset_sha256": receipt["page_asset_sha256"],
            "verdict": receipt["verdict"],
            "task_resolutions": receipt["task_resolutions"],
        }
        for field in ("review_session_id", "review_method", "review_checks"):
            if field in receipt:
                sanitized[field] = receipt[field]
        sanitized_visual_receipts.append(sanitized)
        generated_text.extend(
            (
                f"visual:{receipt['page']}:{resolution['task_id']}.observation",
                resolution["observation"],
            )
            for resolution in receipt["task_resolutions"]
        )

    title = source_title or source.stem
    generated_payload = {
        "package_identity": {
            "name": name,
            "display_name": display_name,
            "domain": domain,
            "source_title": title,
            "source_filename": source.name,
        },
        "objects": promoted_objects,
        "relations": promoted_relations,
        "conflicts": promoted_conflicts,
        "knowledge_gaps": promoted_gaps,
        "semantic_reviews": sanitized_review_records,
        "visual_receipts": sanitized_visual_receipts,
    }
    if is_assurance_enabled(manifest):
        assertion_rows, support_rows, coverage_rows, attestation_rows = (
            load_workpack_assurance(root, manifest)
        )
        generated_payload["semantic_assurance"] = {
            "assertions": [
                row
                for row in assertion_rows
                if isinstance(row, dict)
                and row.get("parent_item_ref") in accepted_refs
            ],
            "support": [
                row
                for row in support_rows
                if isinstance(row, dict)
                and row.get("parent_item_ref") in accepted_refs
            ],
            "coverage": coverage_rows,
            "attestations": [
                row
                for row in attestation_rows
                if isinstance(row, dict) and row.get("item_ref") in accepted_refs
            ],
        }
    verbatim = _verbatim_window(
        page_texts.values(),
        _iter_strings(generated_payload),
    )
    if verbatim is not None:
        raise SemanticPromotionError(
            f"source_text_leak: {verbatim[0]} contains a prohibited source window"
        )

    output = _ensure_empty_output(output)
    created_at = plan.get("issued_at")
    if not isinstance(created_at, str) or not created_at:
        raise SemanticPromotionError("review_plan_timestamp_missing")
    printed_scope_labels = [
        detect_printed_page_label(reader.pages[page - 1].extract_text() or "")
        or str(reader.page_labels[page - 1])
        for page in range(scope["start_page"], scope["end_page"] + 1)
    ]
    source_record = {
        "source_id": source_id,
        "title": title,
        "sha256": source_hash,
        "media_type": "application/pdf",
        "size_bytes": source.stat().st_size,
        "original_filename": source.name,
        "included_path": None,
        "scope": {
            "physical_pages": [scope["start_page"], scope["end_page"]],
            "printed_page_labels": printed_scope_labels,
        },
    }
    object_index = [
        {
            "id": obj["id"],
            "type": obj["type"],
            "path": f"references/knowledge/objects/{obj['id']}.json",
        }
        for obj in promoted_objects
    ]
    package_manifest = {
        "schema_version": SCHEMA_ID,
        "package": {
            "id": name,
            "name": display_name,
            "version": "0.7.0" if visual_protocol == VISUAL_REVIEW_PROTOCOL else ("0.5.0" if is_assurance_enabled(manifest) else "0.2.0"),
            "domain": domain,
            "status": "draft",
        },
        "distribution": {
            "source_mode": "source-required",
            "verification": "external-source-required",
        },
        "sources": [source_record],
        "modules": [
            {
                "module_id": f"{name}-core",
                "version": "0.7.0" if visual_protocol == VISUAL_REVIEW_PROTOCOL else ("0.5.0" if is_assurance_enabled(manifest) else "0.2.0"),
                "knowledge_index": "references/knowledge/index.json",
                "exports": sorted(object_values),
                "dependencies": [],
            }
        ],
        "capabilities": {
            "reference": True,
            "decision_support": False,
            "executable": False,
        },
        "build": {
            "compiler": "fake-expert",
            "compiler_version": (
                VISUAL_SEMANTICS_COMPILER_VERSION
                if visual_protocol == VISUAL_REVIEW_PROTOCOL
                else (SEMANTIC_ASSURANCE_COMPILER_VERSION if is_assurance_enabled(manifest) else "0.2.0-phase2")
            ),
            "schema_version": SCHEMA_ID,
            "created_at": created_at,
            "input_fingerprint": bundle_hash,
            "prompt_versions": (
                [
                    "semantic-proposal-v0.1",
                    "atomic-semantic-assurance-v0.2",
                    "risk-tiered-independent-review-v0.1",
                    *(["visual-semantics-v0.1", VISUAL_REVIEW_PROTOCOL] if visual_protocol == VISUAL_REVIEW_PROTOCOL else []),
                ]
                if is_assurance_enabled(manifest)
                else [
                    "semantic-proposal-v0.1",
                    "independent-semantic-review-v0.1",
                ]
            ),
            "model_ids": [],
        },
    }
    if visual_protocol == VISUAL_REVIEW_PROTOCOL and visual_manifest is not None:
        package_manifest["visual_semantics"] = {
            "protocol": VISUAL_PROTOCOL,
            "review_protocol": VISUAL_REVIEW_PROTOCOL,
            "object_schema": VISUAL_OBJECT_SCHEMA,
            "candidate_only": False,
            "whole_book_completeness_claimed": False,
            "assurance_manifest": "references/visual/assurance-manifest.json",
            "source_free": True,
            "pixels_included": False,
        }
    _write_json(output / "manifest.json", package_manifest)
    _write_json(
        output / "references/evidence/source-manifest.json",
        {"schema_version": SCHEMA_ID, "sources": [source_record]},
    )
    _write_json(
        output / "references/knowledge/index.json",
        {"schema_version": SCHEMA_ID, "objects": object_index},
    )
    for obj in promoted_objects:
        _write_json(
            output / f"references/knowledge/objects/{obj['id']}.json", obj
        )
    _write_jsonl(output / "references/evidence/anchors.jsonl", anchors)
    _write_jsonl(output / "references/knowledge/relations.jsonl", promoted_relations)
    _write_jsonl(output / "references/conflicts.jsonl", promoted_conflicts)
    _write_jsonl(output / "references/knowledge-gaps.jsonl", promoted_gaps)
    _write_jsonl(
        output / "references/reviews/semantic-records.jsonl",
        sanitized_review_records,
    )
    _write_jsonl(
        output / "references/reviews/visual-receipts.jsonl",
        sanitized_visual_receipts,
    )
    if visual_protocol == VISUAL_REVIEW_PROTOCOL and visual_manifest is not None:
        _write_jsonl(output / "references/visual/visual-objects.jsonl", promoted_visual_objects)
        _write_jsonl(output / "references/visual/visual-relations.jsonl", promoted_visual_relations)
        _write_jsonl(output / "references/visual/table-grids.jsonl", promoted_table_grids)
        _write_jsonl(output / "references/visual/visual-conflicts.jsonl", promoted_visual_conflicts)
        _write_jsonl(output / "references/visual/review-attestations.jsonl", visual_attestations)
        _write_json(output / "references/visual/assurance-manifest.json", visual_manifest)
    promoted_review_plan = {
        "schema_version": REVIEW_PLAN_SCHEMA,
        "attestation_level": plan["attestation_level"],
        "issued_by": plan["issued_by"],
        "issued_at": plan["issued_at"],
        "source_sha256": source_hash,
        "workpack_id": manifest["workpack_id"],
        "bundle_sha256": bundle_hash,
        "proposer_instances": plan["proposer_instances"],
        "reviewer_instances": plan["reviewer_instances"],
        "semantic_item_count": len(queue_by_ref),
        # The original authorization binds the review queue count.  The v0.2
        # object-attestation route intentionally has no legacy page receipts,
        # so preserve that bound count instead of replacing it with zero.
        "visual_page_count": (
            plan.get("visual_page_count")
            if visual_protocol == VISUAL_REVIEW_PROTOCOL
            else len(visual_receipts)
        ),
        "gap_resolution_policy": plan["gap_resolution_policy"],
        "authorization_sha256": plan["authorization_sha256"],
        "render_profile": manifest.get("render_profile"),
    }
    for field in ("review_protocol", "review_session_id"):
        if field in plan:
            promoted_review_plan[field] = plan[field]
    for field in (
        "visual_review_scope",
        "visual_candidate_task_count",
        "visual_queued_task_count",
        "visual_deferred_task_count",
        "visual_deferred_task_ids_sha256",
    ):
        if field in plan:
            promoted_review_plan[field] = plan[field]
    if visual_protocol == VISUAL_REVIEW_PROTOCOL:
        promoted_review_plan["visual_review_protocol"] = VISUAL_REVIEW_PROTOCOL
        promoted_review_plan["visual_review_plan_sha256"] = sha256_json(plan)
        promoted_review_plan["visual_object_count"] = len(promoted_visual_objects)
        promoted_review_plan["visual_attestation_count"] = len(visual_attestations)
        promoted_review_plan["visual_reviewer_requirements"] = dict(plan.get("visual_reviewer_requirements", {}))
    if is_assurance_enabled(manifest):
        promoted_review_plan["semantic_assurance"] = plan.get(
            "semantic_assurance"
        )
        promoted_review_plan["source_review_plan_sha256"] = sha256_json(plan)
    _write_json(output / "references/reviews/review-plan.json", promoted_review_plan)
    assurance_manifest = None
    if is_assurance_enabled(manifest):
        assurance_manifest = write_promoted_assurance(
            workpack_root=root,
            workpack_manifest=manifest,
            output_root=output,
            plan=plan,
            accepted_item_refs=accepted_refs,
            entity_map={
                item_ref: promoted_id_by_ref.get(item_ref)
                for item_ref in sorted(queue_by_ref)
            },
        )
    _write_json(
        output / "references/procedures/index.json",
        {"schema_version": SCHEMA_ID, "procedures": []},
    )
    _write_json(
        output / "references/decisions/index.json",
        {"schema_version": SCHEMA_ID, "decisions": []},
    )
    _write_jsonl(output / "references/competency-tests.jsonl", [])

    promotion_rows = []
    for item_ref in sorted(queue_by_ref):
        record = review_by_ref[item_ref]
        row = {
            "item_ref": item_ref,
            "item_kind": queue_by_ref[item_ref]["item_kind"],
            "verdict": record["verdict"],
            "issue_codes": record["issue_codes"],
            "item_sha256": queue_by_ref[item_ref]["item_sha256"],
            "review_input_sha256": queue_by_ref[item_ref]["review_input_sha256"],
            "review_record_sha256": sha256_json(sanitized_review_by_ref[item_ref]),
            "reviewer_instance": record["reviewer_instance"],
            "evidence_support": record.get("evidence_support"),
            "rationale": record["rationale"],
            "confidence": record.get("confidence"),
            "promoted_id": promoted_id_by_ref.get(item_ref),
        }
        for field in ("review_session_id", "review_method", "review_checks"):
            if field in record:
                row[field] = record[field]
        if queue_by_ref[item_ref]["item_kind"] == "gap":
            resolution = record.get("gap_resolution")
            row["gap_resolution"] = resolution
            row["resolved_by_ids"] = [
                object_id_by_ref[reference]
                for reference in resolution.get("resolved_by_refs", [])
            ] if isinstance(resolution, dict) else []
        promotion_rows.append(row)
    promotion_report = {
        "schema_version": "tkc.promotion-report/v0.1",
        "source_id": source_id,
        "source_sha256": source_hash,
        "workpack_id": manifest["workpack_id"],
        "review_bundle_sha256": bundle_hash,
        "review_attestation_level": plan["attestation_level"],
        "review_protocol": plan.get("review_protocol"),
        "review_session_id": plan.get("review_session_id"),
        "created_at": created_at,
        "policy": {
            "promoter": "deterministic",
            "source_content_copied": False,
            "source_required": True,
            "decision_support": False,
            "executable": False,
            "publication_ready": False,
        },
        "counts": {
            "review_items": len(queue_by_ref),
            "accepted_items": len(accepted_refs),
            "knowledge_objects": len(promoted_objects),
            "relations": len(promoted_relations),
            "conflicts": len(promoted_conflicts),
            "knowledge_gaps": len(promoted_gaps),
            "resolved_elsewhere_gaps": sum(
                queue_by_ref[item_ref]["item_kind"] == "gap"
                and isinstance(review_by_ref[item_ref].get("gap_resolution"), dict)
                and review_by_ref[item_ref]["gap_resolution"].get("status")
                == "resolved_elsewhere"
                for item_ref in queue_by_ref
            ),
            "anchors": len(anchors),
            **(
                {
                    "visual_objects": len(promoted_visual_objects),
                    "visual_relations": len(promoted_visual_relations),
                    "table_grids": len(promoted_table_grids),
                    "visual_review_attestations": len(visual_attestations),
                }
                if visual_protocol == VISUAL_REVIEW_PROTOCOL
                else {}
            ),
        },
        "items": promotion_rows,
        "review_artifacts": {
            "plan": "references/reviews/review-plan.json",
            "semantic_records": "references/reviews/semantic-records.jsonl",
            "visual_receipts": "references/reviews/visual-receipts.jsonl",
            **(
                {
                    "visual_assurance_manifest": "references/visual/assurance-manifest.json",
                    "visual_objects": "references/visual/visual-objects.jsonl",
                    "visual_relations": "references/visual/visual-relations.jsonl",
                    "table_grids": "references/visual/table-grids.jsonl",
                    "visual_conflicts": "references/visual/visual-conflicts.jsonl",
                    "visual_review_attestations": "references/visual/review-attestations.jsonl",
                }
                if visual_protocol == VISUAL_REVIEW_PROTOCOL
                else {}
            ),
            **(
                {
                    "review_attestations": "references/reviews/review-attestations.jsonl",
                    "semantic_assurance_manifest": "references/semantic/assurance-manifest.json",
                }
                if assurance_manifest is not None
                else {}
            ),
        },
        **(
            {
                "semantic_assurance": {
                    "protocol": assurance_manifest["protocol"],
                    "compiler_version": assurance_manifest["compiler_version"],
                    "selected_scope_complete": assurance_manifest[
                        "selected_scope_complete"
                    ],
                    "whole_book_completeness_claimed": False,
                    "manifest_sha256": sha256_file(
                        output / "references/semantic/assurance-manifest.json"
                    ),
                }
            }
            if assurance_manifest is not None
            else {}
        ),
        **(
            {
                "visual_assurance": {
                    "protocol": visual_manifest["protocol"],
                    "review_protocol": visual_manifest["review_protocol"],
                    "whole_book_completeness_claimed": False,
                    "manifest_sha256": sha256_file(output / "references/visual/assurance-manifest.json"),
                }
            }
            if visual_manifest is not None
            else {}
        ),
        "reviewed_gate": reviewed_summary,
    }
    _write_json(
        output / "references/evidence/promotion-report.json", promotion_report
    )

    skill_text = f"""---
name: {name}
description: Use independently reviewed, source-grounded {display_name} knowledge for bounded technical explanations and trace requests.
---

# {display_name}

This is a Phase 2 reference-only draft. It is not publication-ready and does not
authorize decisions or execution.

## Runtime workflow

1. Read `manifest.json` and keep the answer within the declared source scope.
2. Search `references/knowledge/index.json` for the smallest relevant object set.
3. Read the selected objects, evidence anchors, conflicts, and knowledge gaps.
4. State assumptions, applicability, valid conditions, and failure conditions.
5. Cite both knowledge object IDs and evidence IDs for technical claims.
6. Preserve source-printed and compiler-derived alternatives as distinct claims.
7. Abstain when the package lacks evidence or the request exceeds the source scope.
8. Do not claim decision-support or executable capability.

## Verification boundary

The PDF is not distributed. Re-run external span verification against the source
whose SHA-256 is declared in `manifest.json` before relying on an anchor.
"""
    _write_text_secure(output / "SKILL.md", skill_text)
    _write_text_secure(
        output / "agents/openai.yaml",
        "interface:\n"
        f"  display_name: {json.dumps(display_name)}\n"
        f"  short_description: {json.dumps(f'Reviewed draft {domain} reference knowledge')}\n"
        f"  default_prompt: {json.dumps(f'Use ${name} with knowledge and evidence IDs, and abstain outside scope.')}\n",
    )
    return {
        "package": str(output),
        "package_id": name,
        "source_id": source_id,
        "source_sha256": source_hash,
        "review_bundle_sha256": bundle_hash,
        "counts": promotion_report["counts"],
        "capabilities": package_manifest["capabilities"],
        "status": "draft",
        "publication_ready": False,
    }
