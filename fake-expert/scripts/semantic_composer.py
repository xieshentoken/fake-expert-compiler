#!/usr/bin/env python3
"""Build and validate source-attributed Phase 7C semantic alignment candidates.

This module deliberately stops at a candidate/review-plan boundary. It never
rewrites child package objects, authors attestations, resolves conflicts, or
creates a source-free consensus.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable

from compiler_version import (
    ALIGNMENT_CANDIDATE_SCHEMA,
    COMPOSER_CONFLICT_SCHEMA,
    COMPOSER_INPUT_SCHEMA,
    COMPOSER_RECEIPT_SCHEMA,
    COMPOSER_REVIEW_ATTESTATION_SCHEMA,
    COMPOSER_REVIEW_PLAN_SCHEMA,
    COMPOSER_REVIEW_PROTOCOL,
    COMPOSER_WORKPACK_SCHEMA,
    CONCEPT_REF_SCHEMA,
    DIFFERENCE_SCHEMA,
    EVIDENCE_BINDING_SCHEMA,
    SEMANTIC_COMPOSER_COMPILER_VERSION,
    SEMANTIC_COMPOSER_PROTOCOL,
    UNIT_MAP_SCHEMA,
)
from expert_skill_contract import (
    Issue,
    load_json,
    load_jsonl,
    sha256_file,
    sha256_json,
    validate_expert_skill,
    write_json,
)
from semantic_assurance import validate_promoted_assurance
from verify_pdf_ir import verify_pdf_ir
from pdf_structure import PdfPipelineError


ATTESTATION_LEVEL = "host-orchestrator-recorded-not-cryptographic"
RESOLUTION_POLICY = "preserve-separate-no-consensus"
SOURCE_LEVELS = {"legacy-source-support", "atomic-supported", "candidate-source"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RFC3339_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)
RELATION_TYPES = {
    "equivalent_candidate",
    "broader_candidate",
    "narrower_candidate",
    "related_candidate",
    "contradicts_candidate",
    "alias_candidate",
}
HIGH_RISK_DIFFERENCE_KINDS = {
    "numeric",
    "unit",
    "dimension",
    "symbol",
    "applicability",
    "assumption",
    "failure_condition",
}
PDF_IR_IDENTITY_ALGORITHM = "pdf-ir-canonical-inventory-v1"
PDF_IR_COMPONENT_ROLES = {
    "source",
    "document_map",
    "parser_receipt",
    "heading_resolutions",
    "structure_task",
}
PDF_IR_STANDARD_BASENAMES = {
    "source": "source.json",
    "document_map": "document-map.json",
    "parser_receipt": "parser-receipt.json",
    "heading_resolutions": "heading-resolutions.jsonl",
    "structure_task": "structure-resolution-required.jsonl",
}


class SemanticComposerError(RuntimeError):
    """Stable fail-closed Composer error."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha256(
        canonical_json({"prefix": prefix, "parts": list(parts)})
    ).hexdigest()
    return f"{prefix}-{digest[:24]}"


def row_hash(row: dict[str, Any], hash_key: str) -> str:
    return sha256_json({key: value for key, value in row.items() if key != hash_key})


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows, key=lambda row: str(row.get(_row_sort_key(row), "")))
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in ordered),
        encoding="utf-8",
    )


def _row_sort_key(row: dict[str, Any]) -> str:
    for key in (
        "concept_ref_id",
        "alignment_candidate_id",
        "difference_id",
        "conflict_id",
        "unit_map_id",
        "binding_id",
        "attestation_id",
        "receipt_id",
    ):
        if isinstance(row.get(key), str):
            return row[key]
    return "id"


def _normalize(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.replace("–", "-").replace("—", "-")
    value = re.sub(r"\s+", "", value)
    return re.sub(r"[^0-9a-z\u3400-\u9fff_+\-./%μΩ]", "", value)


SURFACE_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "for", "from",
    "in", "is", "it", "of", "on", "or", "the", "to", "with", "within",
}
SURFACE_GENERIC_ENGLISH = {
    "field", "magnetic", "device", "concept", "system", "operation", "problem",
    "discussion", "source", "section", "context", "candidate", "current",
}


def _surface_forms(values: Iterable[Any]) -> set[str]:
    """Return typed source-local surface phrases, never prose-derived chars."""
    result: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = unicodedata.normalize("NFKC", value).casefold()
        normalized = normalized.replace("–", "-").replace("—", "-")
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if not normalized:
            continue
        cjk = re.findall(r"[\u3400-\u9fff]", normalized)
        latin_words = [
            token
            for token in re.findall(r"[a-z][a-z0-9_+\-]*", normalized)
            if token not in SURFACE_STOPWORDS and len(token) >= 3
        ]
        if cjk:
            # Chinese one-character overlap such as 磁/场/装/置 is not a
            # typed concept signal. Keep the complete multi-character form.
            if len(cjk) >= 2:
                result.add(re.sub(r"[^0-9a-z\u3400-\u9fff_+\-./%μΩ]", "", normalized))
        elif latin_words:
            # Preserve the source-local phrase boundary for English.  Joining
            # all tokens first made ``tokamak current`` look like one opaque
            # token and weakened the typed-term contract.
            result.add(" ".join(latin_words))
            result.update(
                " ".join(latin_words[index : index + width])
                for width in (2, 3)
                for index in range(0, len(latin_words) - width + 1)
                if all(token not in SURFACE_GENERIC_ENGLISH for token in latin_words[index : index + width])
            )
            # A multi-word typed phrase is not reduced to every constituent
            # word: otherwise a prose title containing only ``plasma`` would
            # spuriously match the explicit alias ``plasma current``.  A
            # single-word label/term remains a valid source-local surface.
            if len(latin_words) == 1:
                result.update(
                    token
                    for token in latin_words
                    if len(token) >= 4 and token not in SURFACE_GENERIC_ENGLISH
                )
    return result


def _tokens(values: Iterable[Any]) -> set[str]:
    """Compatibility name for normalized typed surface forms."""
    return _surface_forms(values)


DEFAULT_ALIAS_MAP: dict[str, list[str]] = {}


UNIT_DEFINITIONS: dict[str, dict[str, Any]] = {
    "m": {"dimension": "length", "scale_to_si": 1.0},
    "cm": {"dimension": "length", "scale_to_si": 0.01},
    "mm": {"dimension": "length", "scale_to_si": 0.001},
    "s": {"dimension": "time", "scale_to_si": 1.0},
    "ms": {"dimension": "time", "scale_to_si": 0.001},
    "A": {"dimension": "current", "scale_to_si": 1.0},
    "kA": {"dimension": "current", "scale_to_si": 1000.0},
    "MA": {"dimension": "current", "scale_to_si": 1000000.0},
    "T": {"dimension": "magnetic_field", "scale_to_si": 1.0},
    "tesla": {"dimension": "magnetic_field", "scale_to_si": 1.0},
    "keV": {"dimension": "energy", "scale_to_si": 1.602176634e-16},
    "eV": {"dimension": "energy", "scale_to_si": 1.602176634e-19},
    "%": {"dimension": "dimensionless", "scale_to_si": 0.01},
    "dimensionless": {"dimension": "dimensionless", "scale_to_si": 1.0},
}


def _merge_alias_map(custom: dict[str, Any] | None) -> dict[str, list[str]]:
    merged = {key: sorted(set(values)) for key, values in DEFAULT_ALIAS_MAP.items()}
    if custom:
        for key, values in custom.items():
            if not isinstance(key, str) or not isinstance(values, list):
                raise SemanticComposerError("alias_map_invalid")
            if any(not isinstance(value, str) or not value for value in values):
                raise SemanticComposerError("alias_map_invalid")
            merged[key] = sorted(set(merged.get(key, []) + values))
    return {key: merged[key] for key in sorted(merged)}


def _alias_groups(values: Iterable[Any], alias_map: dict[str, list[str]]) -> list[str]:
    surfaces = _surface_forms(values)
    groups: list[str] = []
    for group, aliases in alias_map.items():
        alias_surfaces = _surface_forms(aliases)
        if surfaces & alias_surfaces:
            groups.append(group)
    return sorted(set(groups))


def _unit_record(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        name = value
        supplied: dict[str, Any] = {}
    elif isinstance(value, dict):
        name = value.get("name") or value.get("unit")
        supplied = dict(value)
    else:
        raise SemanticComposerError("unit_record_invalid")
    if not isinstance(name, str) or not name:
        raise SemanticComposerError("unit_record_invalid")
    base = UNIT_DEFINITIONS.get(name)
    if base is None:
        dimension = supplied.get("dimension")
        scale = supplied.get("scale_to_si")
        if not isinstance(dimension, str) or not isinstance(scale, (int, float)):
            return {
                "name": name,
                "dimension": None,
                "scale_to_si": None,
                "normalization_status": "unknown",
            }
        base = {"dimension": dimension, "scale_to_si": float(scale)}
    return {
        "name": name,
        "dimension": base["dimension"],
        "scale_to_si": base["scale_to_si"],
        "normalization_status": "known",
    }


def _numeric_tokens(values: Any, fallback_text: str = "") -> list[dict[str, Any]]:
    if isinstance(values, list):
        result: list[dict[str, Any]] = []
        for item in values:
            if isinstance(item, dict) and isinstance(item.get("value"), (int, float, str)):
                row = {"value": str(item["value"])}
                if isinstance(item.get("unit"), str):
                    row["unit"] = item["unit"]
                if isinstance(item.get("evidence_id"), str):
                    row["evidence_id"] = item["evidence_id"]
                result.append(row)
            elif isinstance(item, (int, float, str)):
                result.append({"value": str(item)})
        return sorted(result, key=lambda row: (row.get("value", ""), row.get("unit", "")))
    # Administrative page ranges, section numbers, and prose numerals are
    # not technical numeric assertions. Only explicit typed input can enter
    # numeric differences/conflicts/unit mapping.
    return []


def _symbols(values: Any, fallback_text: str = "") -> list[str]:
    if isinstance(values, list):
        return sorted({str(value) for value in values if isinstance(value, str) and value})
    # Ordinary prose is not a typed symbol declaration. Do not infer symbols
    # from English words; formula/symbol collision review requires explicit
    # typed input from the source-local proposal.
    return []


def _require_sha(value: Any, code: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise SemanticComposerError(code)
    return value


def _safe_ir_component_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise SemanticComposerError("candidate_ir_component_path_invalid")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SemanticComposerError("candidate_ir_component_path_invalid")
    normalized = path.as_posix()
    if normalized != value:
        raise SemanticComposerError("candidate_ir_component_path_invalid")
    return normalized


def _ir_component_records(expected_components: Any) -> list[dict[str, str]]:
    if not isinstance(expected_components, list) or not expected_components:
        raise SemanticComposerError("candidate_ir_components_missing")
    records: list[dict[str, str]] = []
    roles: set[str] = set()
    paths: set[str] = set()
    for component in expected_components:
        if not isinstance(component, dict) or component.get("role") not in PDF_IR_COMPONENT_ROLES:
            raise SemanticComposerError("candidate_ir_component_role_invalid")
        role = str(component["role"])
        path = _safe_ir_component_path(component.get("path"))
        if Path(path).name != PDF_IR_STANDARD_BASENAMES[role]:
            raise SemanticComposerError("candidate_ir_component_basename_invalid")
        sha = _require_sha(component.get("sha256"), "candidate_ir_component_hash_invalid")
        if role in roles or path in paths:
            raise SemanticComposerError("candidate_ir_component_duplicate")
        roles.add(role)
        paths.add(path)
        records.append({"role": role, "path": path, "sha256": sha})
    if roles != PDF_IR_COMPONENT_ROLES:
        raise SemanticComposerError("candidate_ir_component_role_set_invalid")
    source_parent = Path(next(row["path"] for row in records if row["role"] == "source")).parent
    for row in records:
        if row["role"] != "structure_task" and Path(row["path"]).parent != source_parent:
            raise SemanticComposerError("candidate_ir_component_root_mismatch")
    return sorted(records, key=lambda row: (row["role"], row["path"]))


def _canonical_pdf_ir_identity(candidate_ir_root: Path, expected_components: Any) -> dict[str, Any]:
    """Recompute a generic, role-labelled PDF IR component identity.

    The caller supplies the frozen safe-relative paths.  No directory name or
    book-specific layout is embedded in the compiler.
    """
    root = candidate_ir_root.expanduser().resolve()
    if not root.is_dir():
        raise SemanticComposerError("candidate_ir_root_missing")
    records = _ir_component_records(expected_components)
    actual: list[dict[str, str]] = []
    for component in records:
        path = (root / component["path"]).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise SemanticComposerError("candidate_ir_component_path_escape") from error
        if not path.is_file():
            raise SemanticComposerError("candidate_ir_component_missing")
        actual.append({"role": component["role"], "path": component["path"], "sha256": sha256_file(path)})
    canonical = {"algorithm": PDF_IR_IDENTITY_ALGORITHM, "components": actual}
    return {
        "identity_algorithm": PDF_IR_IDENTITY_ALGORITHM,
        "components": actual,
        "ir_sha256": sha256_json(canonical),
    }


def _validate_candidate_ir_binding(
    candidate_ir_root: Path | None,
    expected: dict[str, Any] | None,
    source_sha: str | None,
    candidate_scope: list[int] | None,
    source_path: Path | None,
    pdftoppm: Path | str | None,
    issues: list[Issue],
    path: str,
) -> None:
    if expected is None:
        if candidate_ir_root is not None:
            _issue(issues, "candidate_ir_binding_missing", path, "A candidate IR path was supplied without a frozen IR identity binding.")
        return
    if candidate_ir_root is None:
        _issue(issues, "source_binding_unverifiable", path, "Frozen normalized PDF IR requires --candidate-ir-root for source replay validation.")
        return
    try:
        expected_components = _ir_component_records(expected.get("components"))
        actual = _canonical_pdf_ir_identity(candidate_ir_root, expected_components)
    except SemanticComposerError as error:
        _issue(issues, "candidate_ir_unverifiable", str(candidate_ir_root), str(error))
        return
    if expected.get("identity_algorithm") != PDF_IR_IDENTITY_ALGORITHM:
        _issue(issues, "candidate_ir_identity_algorithm_invalid", path, "Candidate IR identity algorithm is not the frozen canonical inventory protocol.")
    if _ir_component_records(expected.get("components")) != actual["components"]:
        _issue(issues, "candidate_ir_component_mismatch", path, "Candidate IR component path/SHA map differs from the frozen binding.")
    if expected.get("ir_sha256") != actual["ir_sha256"]:
        _issue(issues, "candidate_ir_hash_mismatch", path, "Candidate IR aggregate hash does not recompute from the frozen component map.")
    actual_by_role = {row["role"]: row for row in actual["components"]}
    source_component = (candidate_ir_root.expanduser().resolve() / actual_by_role["source"]["path"]).resolve()
    parser_component = (candidate_ir_root.expanduser().resolve() / actual_by_role["parser_receipt"]["path"]).resolve()
    task_component = (candidate_ir_root.expanduser().resolve() / actual_by_role["structure_task"]["path"]).resolve()
    try:
        source_record = load_json(source_component)
        parser_record = load_json(parser_component)
        task_rows = load_jsonl(task_component)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        _issue(issues, "candidate_ir_unverifiable", str(candidate_ir_root), f"Candidate IR source/task records cannot be read: {error}")
        return
    if not isinstance(source_record, dict) or source_record.get("sha256") != expected.get("source_sha256") or (source_sha and source_record.get("sha256") != source_sha):
        _issue(issues, "candidate_ir_source_mismatch", path, "IR source.json does not bind to the candidate source SHA.")
    parser_scope = parser_record.get("scope") if isinstance(parser_record, dict) else None
    actual_scope = [parser_scope.get("start_page"), parser_scope.get("end_page")] if isinstance(parser_scope, dict) else None
    expected_scope = expected.get("scope", {}).get("physical_pages") if isinstance(expected.get("scope"), dict) else None
    if not isinstance(actual_scope, list) or len(actual_scope) != 2 or not all(isinstance(value, int) for value in actual_scope):
        _issue(issues, "candidate_ir_scope_invalid", path, "IR parser receipt does not expose a valid physical-page scope.")
    else:
        if expected_scope != actual_scope:
            _issue(issues, "candidate_ir_scope_mismatch", path, "Frozen IR scope differs from the parser receipt scope.")
        if candidate_scope is None or len(candidate_scope) != 2 or not all(isinstance(value, int) for value in candidate_scope):
            _issue(issues, "candidate_scope_invalid", path, "Candidate source scope is missing while replaying the normalized IR.")
        elif not (actual_scope[0] <= candidate_scope[0] and candidate_scope[1] <= actual_scope[1]):
            _issue(issues, "candidate_scope_outside_ir", path, "Candidate source scope is not fully covered by the normalized IR scope.")
    matching_tasks = [row for row in task_rows if isinstance(row, dict) and row.get("task_id") == expected.get("heading_task_id")]
    if len(matching_tasks) != 1:
        _issue(issues, "candidate_ir_heading_task_mismatch", path, "Frozen heading task ID is absent or duplicated in the structure task artifact.")
    else:
        task = matching_tasks[0]
        if task.get("task_sha256") != expected.get("heading_task_sha256"):
            _issue(issues, "candidate_ir_heading_task_hash_mismatch", path, "Frozen heading task hash does not match the bound task record.")
        if source_sha and task.get("source_sha256") != source_sha:
            _issue(issues, "candidate_ir_source_mismatch", path, "Heading task source SHA differs from the candidate source.")
        if task.get("status") != "pending-independent-structure-review":
            _issue(issues, "candidate_ir_status_invalid", path, "The bound heading task no longer has its independent structure-review pause status.")
    if source_path is not None:
        ir_root_for_verifier = source_component.parent
        try:
            verification = verify_pdf_ir(
                source_path,
                ir_root_for_verifier,
                pdftoppm=pdftoppm or "pdftoppm",
                docling_allow_network=False,
            )
        except (OSError, PdfPipelineError, ValueError, json.JSONDecodeError) as error:
            _issue(issues, "pdf_ir_verify_failed", str(candidate_ir_root), f"verify_pdf_ir failed closed: {error}")
        else:
            if verification.get("passed") is not True:
                _issue(issues, "pdf_ir_verify_failed", str(candidate_ir_root), "verify_pdf_ir reported one or more stale or inconsistent receipts.")
            if isinstance(verification.get("scope"), dict) and expected_scope != [verification["scope"].get("start_page"), verification["scope"].get("end_page")]:
                _issue(issues, "candidate_ir_scope_mismatch", path, "verify_pdf_ir scope differs from the frozen normalized IR scope.")


def _ensure_rfc3339(value: str) -> None:
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        raise SemanticComposerError("created_at_invalid")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SemanticComposerError("created_at_invalid") from error
    if parsed.tzinfo is None:
        raise SemanticComposerError("created_at_invalid")


def _ensure_empty_output(output: Path) -> Path:
    output = output.expanduser().resolve()
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise SemanticComposerError(f"output_not_empty: {output}")
    else:
        output.mkdir(parents=True)
    return output


def _assert_not_inside(output: Path, inputs: Iterable[Path], code: str) -> None:
    output = output.expanduser().resolve()
    for raw in inputs:
        path = raw.expanduser().resolve()
        if output == path or path in output.parents or output in path.parents:
            raise SemanticComposerError(code)


def _source_id(source_sha: str) -> str:
    return f"src-{source_sha[:16]}"


def _ref_identity(row: dict[str, Any]) -> dict[str, Any]:
    """Frozen identity inputs carried into every downstream candidate ID."""
    return {
        "concept_ref_id": row.get("concept_ref_id"),
        "concept_ref_sha256": row.get("concept_ref_sha256"),
        "source_sha256": row.get("source_sha256"),
        "package_id": row.get("package_id"),
        "workpack_id": row.get("workpack_id"),
        "local_ref": row.get("local_ref"),
        "evidence_binding_ids": sorted(set(row.get("evidence_binding_ids", []))),
        "atomic_assertion_ids": sorted(set(row.get("atomic_assertion_ids", []))),
        "support_sha256s": sorted(set(row.get("support_sha256s", []))),
    }


def _page_geometry(page: Any) -> dict[str, Any]:
    try:
        media = page.mediabox
        crop = page.cropbox
        media_values = [float(media.left), float(media.bottom), float(media.right), float(media.top)]
        crop_values = [float(crop.left), float(crop.bottom), float(crop.right), float(crop.top)]
        rotation = int(page.rotation or 0) % 360
    except Exception as error:
        raise SemanticComposerError("pdf_page_geometry_unavailable") from error
    width = crop_values[2] - crop_values[0]
    height = crop_values[3] - crop_values[1]
    if width <= 0 or height <= 0 or rotation not in {0, 90, 180, 270}:
        raise SemanticComposerError("pdf_page_geometry_invalid")
    display_width, display_height = (height, width) if rotation in {90, 270} else (width, height)
    return {
        "coordinate_space": "pdf-page-top-left-points-v1",
        "source_box_space": "pdf-page-bottom-left-points-v1",
        "media_box": media_values,
        "crop_box": crop_values,
        "rotation_degrees": rotation,
        "display_width": round(display_width, 6),
        "display_height": round(display_height, 6),
        "full_page_bbox": [0.0, 0.0, round(display_width, 6), round(display_height, 6)],
        "transform": {
            "origin": "crop-box",
            "rotation_applied": rotation,
            "y_axis": "top-left",
        },
    }


def _load_pypdf_pages(source: Path, pages: Iterable[int]) -> tuple[int, str, dict[int, str], dict[int, dict[str, Any]]]:
    try:
        from pypdf import PdfReader, __version__ as pypdf_version
    except ImportError as error:
        raise SemanticComposerError("pypdf_unavailable") from error
    if str(pypdf_version) != "6.10.0":
        raise SemanticComposerError("pypdf_version_mismatch")
    try:
        reader = PdfReader(str(source))
    except Exception as error:  # pypdf has several parser-specific exceptions
        raise SemanticComposerError("pdf_parse_failed") from error
    if getattr(reader, "is_encrypted", False):
        raise SemanticComposerError("pdf_encrypted")
    page_numbers = sorted(set(pages))
    if not page_numbers or page_numbers[0] < 1 or page_numbers[-1] > len(reader.pages):
        raise SemanticComposerError("candidate_page_scope_invalid")
    texts: dict[int, str] = {}
    geometries: dict[int, dict[str, Any]] = {}
    for page in page_numbers:
        try:
            pdf_page = reader.pages[page - 1]
            texts[page] = pdf_page.extract_text() or ""
            geometries[page] = _page_geometry(pdf_page)
        except Exception as error:
            raise SemanticComposerError("native_text_extraction_failed") from error
    return len(reader.pages), str(pypdf_version), texts, geometries


def _resolve_candidate_span(text: str, evidence: dict[str, Any]) -> tuple[int, int]:
    start = evidence.get("span_start")
    end = evidence.get("span_end")
    if isinstance(start, int) and isinstance(end, int):
        if 0 <= start < end <= len(text):
            return start, end
        raise SemanticComposerError("candidate_span_invalid")
    match = evidence.get("text_match")
    if not isinstance(match, str) or not match:
        raise SemanticComposerError("candidate_locator_missing")
    occurrence = evidence.get("occurrence", 1)
    if not isinstance(occurrence, int) or occurrence < 1:
        raise SemanticComposerError("candidate_occurrence_invalid")
    cursor = 0
    found: tuple[int, int] | None = None
    for _ in range(occurrence):
        index = text.find(match, cursor)
        if index < 0:
            raise SemanticComposerError("candidate_locator_not_found")
        found = (index, index + len(match))
        cursor = index + max(1, len(match))
    assert found is not None
    return found


def _candidate_evidence(
    *,
    source_sha: str,
    workpack_id: str,
    local_id: str,
    evidence: dict[str, Any],
    page_texts: dict[int, str],
    page_geometries: dict[int, dict[str, Any]],
    pypdf_version: str,
    render_path: Path | None,
) -> dict[str, Any]:
    physical_page = evidence.get("physical_page")
    if not isinstance(physical_page, int) or physical_page not in page_texts:
        raise SemanticComposerError("candidate_physical_page_invalid")
    geometry = page_geometries.get(physical_page)
    if not isinstance(geometry, dict):
        raise SemanticComposerError("pdf_page_geometry_unavailable")
    printed_label = evidence.get("printed_label")
    if not isinstance(printed_label, str) or not printed_label:
        raise SemanticComposerError("printed_label_candidate_missing")
    start, end = _resolve_candidate_span(page_texts[physical_page], evidence)
    span = page_texts[physical_page][start:end]
    content_hash = sha256_text(span)
    expected_hash = evidence.get("content_hash") or evidence.get("span_sha256")
    if expected_hash is not None and expected_hash != content_hash:
        raise SemanticComposerError("candidate_content_hash_mismatch")
    page_hash = sha256_text(unicodedata.normalize("NFKC", page_texts[physical_page]))
    bbox_policy = evidence.get("bbox_policy")
    bbox = evidence.get("bbox")
    if bbox is None:
        if bbox_policy != "full-page-candidate":
            raise SemanticComposerError("bbox_missing")
        bbox = list(geometry["full_page_bbox"])
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or any(not isinstance(value, (int, float)) for value in bbox)
        or bbox[0] < 0
        or bbox[1] < 0
        or bbox[2] <= bbox[0]
        or bbox[3] <= bbox[1]
        or bbox[2] > geometry["display_width"]
        or bbox[3] > geometry["display_height"]
    ):
        raise SemanticComposerError("bbox_invalid")
    if evidence.get("bbox_space", "pdf-page-top-left-points-v1") != "pdf-page-top-left-points-v1":
        raise SemanticComposerError("bbox_space_invalid")
    render_sha = evidence.get("render_sha256")
    crop_sha = evidence.get("crop_sha256")
    if render_path is not None and evidence.get("render_physical_page", physical_page) == physical_page:
        actual_render = sha256_file(render_path)
        if render_sha is not None and render_sha != actual_render:
            raise SemanticComposerError("render_hash_mismatch")
        render_sha = actual_render
        if crop_sha is None and evidence.get("crop_policy") == "full-page":
            crop_sha = render_sha
    if render_sha is not None:
        _require_sha(render_sha, "render_hash_invalid")
    if crop_sha is not None:
        _require_sha(crop_sha, "crop_hash_invalid")
    locator = {
        "physical_page": physical_page,
        "printed_label": printed_label,
        "printed_label_status": evidence.get("printed_label_status", "candidate-only"),
        "bbox": [float(value) for value in bbox],
        "bbox_space": evidence.get("bbox_space", "pdf-page-top-left-points-v1"),
        "page_geometry": geometry,
        "bbox_policy": bbox_policy or "explicit-candidate-bbox",
        "exact_bbox": bbox_policy != "full-page-candidate",
        "locator_precision": "full-page" if bbox_policy == "full-page-candidate" else "candidate-bbox",
        "span_start": start,
        "span_end": end,
        "page_text_sha256": page_hash,
        "text_match_sha256": sha256_text(str(evidence.get("text_match", span))),
        "locator": evidence.get("locator", {}),
        "visual_review_required": bool(render_sha or evidence.get("visual_review_required")),
    }
    evidence_id = evidence.get("evidence_id")
    if not isinstance(evidence_id, str) or not evidence_id:
        evidence_id = stable_id("evlocal", source_sha, workpack_id, local_id, physical_page, content_hash)
    binding_id = stable_id("sebind", source_sha, workpack_id, local_id, evidence_id)
    row = {
        "schema_version": EVIDENCE_BINDING_SCHEMA,
        "binding_id": binding_id,
        "source_sha256": source_sha,
        "package_id": workpack_id,
        "workpack_id": workpack_id,
        "local_ref": f"{workpack_id}:{local_id}",
        "object_ids": [],
        "atomic_assertion_ids": [],
        "support_sha256s": [],
        "evidence_ids": [evidence_id],
        "locator": locator,
        "content_hash": content_hash,
        "parser": "pypdf",
        "parser_version": pypdf_version,
        "render_sha256": render_sha,
        "crop_sha256": crop_sha,
        "review_required": True,
        "input_assurance": "candidate-source",
    }
    row["evidence_binding_sha256"] = row_hash(row, "evidence_binding_sha256")
    return row


def _candidate_concept_refs(
    spec: dict[str, Any],
    source: Path,
    alias_map: dict[str, list[str]],
    render_paths: dict[int, Path] | None,
    candidate_spec_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    source_record = spec.get("source")
    if not isinstance(source_record, dict):
        raise SemanticComposerError("candidate_source_record_invalid")
    source_sha = _require_sha(source_record.get("sha256"), "candidate_source_hash_invalid")
    actual_sha = sha256_file(source)
    if source_sha != actual_sha:
        raise SemanticComposerError("candidate_source_hash_mismatch")
    normalized_pdf_ir = source_record.get("normalized_pdf_ir")
    if normalized_pdf_ir is not None:
        if not isinstance(normalized_pdf_ir, dict):
            raise SemanticComposerError("normalized_pdf_ir_binding_invalid")
        if normalized_pdf_ir.get("source_sha256") != source_sha:
            raise SemanticComposerError("normalized_pdf_ir_source_mismatch")
        if normalized_pdf_ir.get("status") != "heading_resolution_required":
            raise SemanticComposerError("normalized_pdf_ir_status_invalid")
        if normalized_pdf_ir.get("semantic_workpack_built") is not False:
            raise SemanticComposerError("normalized_pdf_workpack_claim_invalid")
        for key in ("ir_sha256", "heading_task_sha256"):
            _require_sha(normalized_pdf_ir.get(key), f"{key}_invalid")
    workpack_id = spec.get("workpack_id")
    if not isinstance(workpack_id, str) or not workpack_id:
        raise SemanticComposerError("candidate_workpack_id_invalid")
    scope = source_record.get("physical_pages") or source_record.get("scope", {}).get("physical_pages")
    if (
        not isinstance(scope, list)
        or len(scope) != 2
        or any(not isinstance(page, int) for page in scope)
        or scope[0] > scope[1]
    ):
        raise SemanticComposerError("candidate_scope_invalid")
    page_count, pypdf_version, page_texts, page_geometries = _load_pypdf_pages(
        source, range(scope[0], scope[1] + 1)
    )
    concepts = spec.get("concepts")
    if not isinstance(concepts, list) or not concepts:
        raise SemanticComposerError("candidate_concepts_missing")
    refs: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    local_ids: set[str] = set()
    for concept in concepts:
        if not isinstance(concept, dict):
            raise SemanticComposerError("candidate_concept_invalid")
        local_id = concept.get("local_id")
        label = concept.get("label")
        if not isinstance(local_id, str) or not local_id or local_id in local_ids:
            raise SemanticComposerError("candidate_concept_id_invalid")
        if not isinstance(label, str) or not label:
            raise SemanticComposerError("candidate_concept_label_invalid")
        local_ids.add(local_id)
        aliases = concept.get("aliases", [])
        terms = [label] + (aliases if isinstance(aliases, list) else [])
        explicit_terms = concept.get("normalized_terms", [])
        if isinstance(explicit_terms, list):
            terms.extend(explicit_terms)
        evidence_rows = concept.get("evidence")
        if not isinstance(evidence_rows, list) or not evidence_rows:
            raise SemanticComposerError("candidate_evidence_missing")
        local_bindings: list[dict[str, Any]] = []
        for evidence in evidence_rows:
            if not isinstance(evidence, dict):
                raise SemanticComposerError("candidate_evidence_invalid")
            binding = _candidate_evidence(
                source_sha=source_sha,
                workpack_id=workpack_id,
                local_id=local_id,
                evidence=evidence,
                page_texts=page_texts,
                page_geometries=page_geometries,
                pypdf_version=pypdf_version,
                render_path=(render_paths or {}).get(int(evidence["physical_page"])) if isinstance(evidence.get("physical_page"), int) else None,
            )
            bindings.append(binding)
            local_bindings.append(binding)
        numeric_tokens = _numeric_tokens(concept.get("numeric_tokens"))
        evidence_ids_for_numeric = {
            evidence_id
            for binding in local_bindings
            for evidence_id in binding.get("evidence_ids", [])
            if isinstance(evidence_id, str)
        }
        if any(item.get("evidence_id") not in evidence_ids_for_numeric for item in numeric_tokens):
            raise SemanticComposerError("numeric_evidence_binding_missing")
        statement_parts = [label]
        for key in ("definition", "assumptions", "applicability", "failure_conditions"):
            value = concept.get(key)
            if isinstance(value, list):
                statement_parts.extend(str(item) for item in value)
            elif isinstance(value, str):
                statement_parts.append(value)
        evidence_binding_ids = [row["binding_id"] for row in local_bindings]
        ref = {
            "schema_version": CONCEPT_REF_SCHEMA,
            "concept_ref_id": stable_id(
                "scref",
                source_sha,
                workpack_id,
                workpack_id,
                local_id,
                candidate_spec_sha256,
                evidence_binding_ids,
            ),
            "source_sha256": source_sha,
            "package_id": workpack_id,
            "package_version": "candidate-workpack",
            "workpack_id": workpack_id,
            "source_role": "candidate-workpack",
            "input_level": "candidate-source",
            "input_assurance": "candidate-source",
            "atomic_support_required": True,
            "local_ref": {"concept_id": local_id, "candidate_input_schema": COMPOSER_INPUT_SCHEMA},
            "label": label,
            "normalized_terms": sorted(_tokens(terms)),
            "alias_groups": _alias_groups(terms, alias_map),
            "symbols": _symbols(concept.get("symbols"), " ".join(statement_parts)),
            "units": sorted(
                (_unit_record(value) for value in (concept.get("units") or [])),
                key=lambda row: (row["name"], str(row.get("dimension"))),
            ),
            "numeric_tokens": numeric_tokens,
            "assumptions": sorted(str(value) for value in (concept.get("assumptions") or []) if isinstance(value, str)),
            "applicability": sorted(str(value) for value in (concept.get("applicability") or []) if isinstance(value, str)),
            "failure_conditions": sorted(str(value) for value in (concept.get("failure_conditions") or []) if isinstance(value, str)),
            "definition_hash": _require_sha(
                concept.get("definition_hash")
                or sha256_json({"label": label, "definition": concept.get("definition"), "terms": sorted(_tokens(terms))}),
                "definition_hash_invalid",
            ),
            "evidence_binding_ids": evidence_binding_ids,
            "atomic_assertion_ids": [],
            "support_sha256s": [],
            "candidate_spec_sha256": candidate_spec_sha256,
            "promotion_eligible": False,
        }
        ref["local_ref"]["candidate_spec_sha256"] = candidate_spec_sha256
        for binding in local_bindings:
            binding["local_ref"] = f"{workpack_id}:{local_id}"
        ref["concept_ref_sha256"] = row_hash(ref, "concept_ref_sha256")
        refs.append(ref)
    candidate_descriptor = {
        "source_id": source_record.get("source_id") or _source_id(source_sha),
        "sha256": source_sha,
        "size_bytes": source.stat().st_size,
        "physical_pages": page_count,
        "scope": {"physical_pages": [scope[0], scope[1]]},
        "parser": "pypdf",
        "parser_version": pypdf_version,
        "printed_label_policy": "candidate-only-unless-visual-or-manual-locator",
        "whole_book_complete_claimed": False,
        "input_level": "candidate-source",
        "atomic_support_required": True,
        "input_assurance": "candidate-source",
        "candidate_spec_sha256": candidate_spec_sha256,
        "promotion_eligible": False,
    }
    if isinstance(normalized_pdf_ir, dict):
        candidate_descriptor["normalized_pdf_ir"] = dict(normalized_pdf_ir)
        candidate_descriptor["structure_review_required"] = True
    return candidate_descriptor, refs, bindings


def _package_source_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise SemanticComposerError("package_sources_missing")
    result: dict[str, dict[str, Any]] = {}
    for source in sources:
        if not isinstance(source, dict):
            raise SemanticComposerError("package_source_record_invalid")
        source_id = source.get("source_id")
        source_sha = source.get("sha256")
        if not isinstance(source_id, str) or not isinstance(source_sha, str) or not SHA256_RE.fullmatch(source_sha):
            raise SemanticComposerError("package_source_record_invalid")
        result[source_id] = source
    return result


def _atomic_assertions_for_object(
    object_id: str,
    entity_map: list[dict[str, Any]],
    assertion_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve a promoted object through its proposal item reference.

    Phase 6A assertion ``parent_item_ref`` values are proposal refs such as
    ``swu-...:main``; they are not required to equal a promoted object ID.
    The explicit entity-map lookup keeps atomic support source-bound and
    makes the mapping independently testable.
    """
    item_ref = next(
        (
            row.get("item_ref")
            for row in entity_map
            if isinstance(row, dict)
            and row.get("promoted_id") == object_id
            and isinstance(row.get("item_ref"), str)
        ),
        None,
    )
    if item_ref is None:
        # A direct proposal-ref identity is permitted only when the
        # assertion parent matches it exactly.
        item_ref = object_id
    return sorted(
        (
            row
            for row in assertion_rows
            if isinstance(row, dict)
            and row.get("parent_item_ref") == item_ref
            and isinstance(row.get("assertion_id"), str)
        ),
        key=lambda row: row["assertion_id"],
    )


def _package_concept_refs(
    package: Path,
    alias_map: dict[str, list[str]],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    package = package.expanduser().resolve()
    if not package.is_dir():
        raise SemanticComposerError(f"package_missing: {package}")
    package_errors = [
        issue
        for issue in validate_expert_skill(package, level="publish")
        if issue.severity == "error"
    ]
    if package_errors:
        raise SemanticComposerError(
            f"package_invalid: {package}:{package_errors[0].code}:{package_errors[0].path}"
        )
    manifest = load_json(package / "manifest.json")
    manifest_sha256 = sha256_file(package / "manifest.json")
    package_record = manifest.get("package")
    if not isinstance(package_record, dict):
        raise SemanticComposerError("package_manifest_invalid")
    package_id = package_record.get("id")
    package_version = package_record.get("version")
    if not isinstance(package_id, str) or not isinstance(package_version, str):
        raise SemanticComposerError("package_identity_invalid")
    source_map = _package_source_map(manifest)
    assurance_issues, assurance = validate_promoted_assurance(package)
    if assurance_issues:
        raise SemanticComposerError(
            f"package_atomic_support_invalid: {assurance_issues[0].code}:{assurance_issues[0].path}"
        )
    atomic_supported = bool(assurance.get("present") and assurance.get("passed"))
    input_level = "atomic-supported" if atomic_supported else "legacy-source-support"
    if not atomic_supported and assurance.get("present"):
        raise SemanticComposerError("package_atomic_support_invalid")
    support_by_assertion: dict[str, dict[str, Any]] = {}
    entity_map: list[dict[str, Any]] = []
    assertion_rows: list[dict[str, Any]] = []
    if atomic_supported:
        assurance_manifest = load_json(package / "references/semantic/assurance-manifest.json")
        entity_map_value = assurance_manifest.get("entity_map") if isinstance(assurance_manifest, dict) else None
        if not isinstance(entity_map_value, list):
            raise SemanticComposerError("package_atomic_entity_map_missing")
        entity_map = [row for row in entity_map_value if isinstance(row, dict)]
        assertion_rows = load_jsonl(package / "references/semantic/semantic-assertions.jsonl")
        support_rows = load_jsonl(package / "references/semantic/support-matrix.jsonl")
        for row in support_rows:
            if isinstance(row, dict) and isinstance(row.get("assertion_id"), str):
                support_by_assertion[row["assertion_id"]] = row
    index = load_json(package / "references/knowledge/index.json")
    entries = index.get("objects") if isinstance(index, dict) else None
    if not isinstance(entries, list) or not entries:
        raise SemanticComposerError("package_knowledge_index_invalid")
    anchors = {
        str(row.get("id")): row
        for row in load_jsonl(package / "references/evidence/anchors.jsonl")
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    refs: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    module_ids = [
        row.get("module_id")
        for row in manifest.get("modules", [])
        if isinstance(row, dict) and isinstance(row.get("module_id"), str)
    ]
    for entry in sorted(entries, key=lambda row: str(row.get("id"))):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not isinstance(entry.get("path"), str):
            raise SemanticComposerError("package_knowledge_index_invalid")
        object_id = entry["id"]
        object_path = (package / entry["path"]).resolve()
        try:
            object_path.relative_to(package)
        except ValueError as error:
            raise SemanticComposerError("package_object_path_escape") from error
        if not object_path.is_file():
            raise SemanticComposerError("package_object_missing")
        object_sha256 = sha256_file(object_path)
        obj = load_json(object_path)
        if not isinstance(obj, dict) or obj.get("id") != object_id:
            raise SemanticComposerError("package_object_invalid")
        source_ids = [value for value in obj.get("source_ids", []) if isinstance(value, str)]
        if not source_ids:
            raise SemanticComposerError("package_object_source_binding_missing")
        source = source_map.get(source_ids[0])
        if source is None:
            raise SemanticComposerError("package_object_source_binding_missing")
        source_sha = _require_sha(source.get("sha256"), "package_source_hash_invalid")
        evidence_ids = [value for value in obj.get("evidence_ids", []) if isinstance(value, str)]
        object_assertions = _atomic_assertions_for_object(
            object_id,
            entity_map if atomic_supported else [],
            assertion_rows if atomic_supported else [],
        )
        local_bindings: list[dict[str, Any]] = []
        for evidence_id in sorted(set(evidence_ids)):
            anchor = anchors.get(evidence_id)
            if anchor is None:
                raise SemanticComposerError("package_evidence_binding_missing")
            pages = anchor.get("pages")
            if not isinstance(pages, list) or not pages or not isinstance(pages[0], int):
                raise SemanticComposerError("package_evidence_locator_missing")
            labels = anchor.get("printed_page_labels")
            printed_label = labels[0] if isinstance(labels, list) and labels and isinstance(labels[0], str) else None
            content_hash = anchor.get("excerpt_sha256") or anchor.get("content_hash")
            if not isinstance(content_hash, str) or not SHA256_RE.fullmatch(content_hash):
                raise SemanticComposerError("package_evidence_hash_missing")
            binding_id = stable_id("sebind", source_sha, package_id, None, object_id, evidence_id)
            binding = {
                "schema_version": EVIDENCE_BINDING_SCHEMA,
                "binding_id": binding_id,
                "source_sha256": source_sha,
                "package_id": package_id,
                "workpack_id": None,
                "local_ref": object_id,
                "object_ids": [object_id],
                "atomic_assertion_ids": [],
                "support_sha256s": [],
                "evidence_ids": [evidence_id],
                "locator": {
                    "physical_page": pages[0],
                    "printed_label": printed_label,
                    "printed_label_status": "legacy-declared" if printed_label else "legacy-unrecorded",
                    "physical_pages": [page for page in pages if isinstance(page, int)],
                    "locator": anchor.get("locator", {}),
                    "segment_id": anchor.get("segment_id"),
                    "source_scope": source.get("scope"),
                    "visual_review_required": False,
                },
                "content_hash": content_hash,
                "parser": "legacy-source-anchor",
                "parser_version": "unrecorded-legacy",
                "render_sha256": None,
                "crop_sha256": None,
                "review_required": True,
                "input_assurance": input_level,
            }
            if atomic_supported:
                binding["atomic_assertion_ids"] = sorted(
                    row["assertion_id"] for row in object_assertions if isinstance(row.get("assertion_id"), str)
                )
                binding["support_sha256s"] = sorted(
                    str(support_by_assertion[assertion_id].get("support_sha256"))
                    for assertion_id in binding["atomic_assertion_ids"]
                    if isinstance(support_by_assertion.get(assertion_id, {}).get("support_sha256"), str)
                )
            binding["evidence_binding_sha256"] = row_hash(binding, "evidence_binding_sha256")
            bindings.append(binding)
            local_bindings.append(binding)
        title = str(obj.get("title") or object_id)
        text_parts = [title, str(obj.get("statement") or "")]
        for key in ("assumptions", "valid_when", "fails_when"):
            value = obj.get(key)
            if isinstance(value, list):
                text_parts.extend(str(item) for item in value)
            elif isinstance(value, str):
                text_parts.append(value)
        parent_assertions = object_assertions
        surface_parts = [title]
        for key in ("aliases", "terms", "normalized_terms"):
            value = obj.get(key)
            if isinstance(value, list):
                surface_parts.extend(str(item) for item in value if isinstance(item, str))
        numeric_tokens = _numeric_tokens(obj.get("numeric_tokens"))
        evidence_ids_for_numeric = set(evidence_ids)
        if any(item.get("evidence_id") not in evidence_ids_for_numeric for item in numeric_tokens):
            raise SemanticComposerError("numeric_evidence_binding_missing")
        evidence_binding_ids = [row["binding_id"] for row in local_bindings]
        atomic_assertion_ids = sorted(
            {
                assertion_id
                for binding in local_bindings
                for assertion_id in binding.get("atomic_assertion_ids", [])
                if isinstance(assertion_id, str)
            }
        )
        support_sha256s = sorted(
            {
                support_sha
                for binding in local_bindings
                for support_sha in binding.get("support_sha256s", [])
                if isinstance(support_sha, str)
            }
        )
        ref = {
            "schema_version": CONCEPT_REF_SCHEMA,
            "concept_ref_id": stable_id(
                "scref",
                source_sha,
                package_id,
                None,
                object_id,
                manifest_sha256,
                object_sha256,
                evidence_binding_ids,
                atomic_assertion_ids,
                support_sha256s,
            ),
            "source_sha256": source_sha,
            "package_id": package_id,
            "package_version": package_version,
            "workpack_id": None,
            "source_role": "reference-package",
            "input_level": input_level,
            "input_assurance": input_level,
            "atomic_support_required": not atomic_supported,
            "local_ref": {
                "object_id": object_id,
                "module_ids": sorted(module_ids),
                "source_ids": sorted(source_ids),
                "package_manifest_sha256": manifest_sha256,
                "object_sha256": object_sha256,
            },
            "label": title,
            "normalized_terms": sorted(_tokens(surface_parts)),
            "alias_groups": _alias_groups(surface_parts, alias_map),
            "symbols": _symbols(obj.get("symbols"), " ".join(text_parts)),
            "units": sorted(
                (_unit_record(value) for value in (obj.get("units") or [])),
                key=lambda row: (row["name"], str(row.get("dimension"))),
            ),
            "numeric_tokens": numeric_tokens,
            "assumptions": sorted(str(value) for value in (obj.get("assumptions") or []) if isinstance(value, str)),
            "applicability": sorted(str(value) for value in (obj.get("valid_when") or []) if isinstance(value, str)),
            "failure_conditions": sorted(str(value) for value in (obj.get("fails_when") or []) if isinstance(value, str)),
            "definition_hash": sha256_json({"title": title, "statement": obj.get("statement"), "type": obj.get("type")}),
            "evidence_binding_ids": evidence_binding_ids,
            "atomic_assertion_ids": atomic_assertion_ids,
            "support_sha256s": support_sha256s,
            "promotion_eligible": False,
        }
        for binding in local_bindings:
            binding["atomic_assertion_ids"] = sorted(
                set(binding["atomic_assertion_ids"])
                | {
                    row["assertion_id"]
                    for row in parent_assertions
                    if isinstance(row.get("assertion_id"), str)
                }
            )
            binding["support_sha256s"] = sorted(set(binding.get("support_sha256s", []))) if atomic_supported else []
            binding["evidence_binding_sha256"] = row_hash(binding, "evidence_binding_sha256")
        ref["concept_ref_sha256"] = row_hash(ref, "concept_ref_sha256")
        refs.append(ref)
    descriptor = {
        "package_id": package_id,
        "package_version": package_version,
        "manifest_sha256": manifest_sha256,
        "source_sha256": sorted({row["source_sha256"] for row in refs}),
        "input_level": input_level,
        "input_assurance": input_level,
        "atomic_support_required": not atomic_supported,
        "promotion_eligible": False,
        "assurance_protocol": assurance.get("protocol", "legacy"),
        "source_ids": sorted(source_map),
    }
    return descriptor, refs, bindings


def _compare_field_values(left: dict[str, Any], right: dict[str, Any], field: str) -> bool:
    left_value = left.get(field) or []
    right_value = right.get(field) or []
    if isinstance(left_value, list) and isinstance(right_value, list):
        return sorted(json.dumps(value, ensure_ascii=False, sort_keys=True) for value in left_value) != sorted(
            json.dumps(value, ensure_ascii=False, sort_keys=True) for value in right_value
        )
    return left_value != right_value


def _unit_pairs(left: dict[str, Any], right: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    result: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for left_unit in left.get("units", []) or []:
        for right_unit in right.get("units", []) or []:
            if isinstance(left_unit, dict) and isinstance(right_unit, dict):
                result.append((left_unit, right_unit))
    return result


def _lexical_score(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_terms = set(left.get("normalized_terms", []))
    right_terms = set(right.get("normalized_terms", []))
    if not left_terms or not right_terms:
        return 0.0
    return round(len(left_terms & right_terms) / max(1, len(left_terms | right_terms)), 6)


def _review_requirement(
    *,
    relation_type: str | None = None,
    difference_kind: str | None = None,
    conflict_kind: str | None = None,
    unit_map: bool = False,
    symbol_collision: bool = False,
) -> dict[str, Any]:
    high_risk = bool(
        symbol_collision
        or unit_map
        or difference_kind in HIGH_RISK_DIFFERENCE_KINDS
        or conflict_kind
        or relation_type in {"equivalent_candidate", "contradicts_candidate"}
    )
    return {
        "risk_tier": "high" if high_risk else "ordinary",
        "required_reviewer_count": 2 if high_risk else 1,
        "reasons": sorted(
            {
                value
                for value in (
                    "relation_semantics" if relation_type else None,
                    difference_kind,
                    conflict_kind,
                    "unit_normalization" if unit_map else None,
                    "symbol_collision" if symbol_collision else None,
                )
                if value
            }
        ),
    }


def _difference_rows(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    source_context: tuple[str, str, str, str],
) -> list[dict[str, Any]]:
    left_ref, right_ref, left_source, right_source = source_context
    rows: list[dict[str, Any]] = []
    for field, kind in (
        ("definition_hash", "definition"),
        ("assumptions", "assumption"),
        ("applicability", "applicability"),
        ("failure_conditions", "failure_condition"),
        ("numeric_tokens", "numeric"),
        ("symbols", "symbol"),
    ):
        if not _compare_field_values(left, right, field):
            continue
        left_value = left.get(field)
        right_value = right.get(field)
        row = {
            "schema_version": DIFFERENCE_SCHEMA,
            "difference_id": stable_id(
                "sdiff",
                left_source,
                right_source,
                _ref_identity(left),
                _ref_identity(right),
                kind,
            ),
            "left_ref": left_ref,
            "right_ref": right_ref,
            "difference_kind": kind,
            "left_value_hash": sha256_json(left_value),
                "right_value_hash": sha256_json(right_value),
                "evidence_binding_ids": sorted(
                    set(left.get("evidence_binding_ids", [])) | set(right.get("evidence_binding_ids", []))
                ),
                "status": "candidate",
            "review_requirement": _review_requirement(difference_kind=kind),
            "resolution": RESOLUTION_POLICY,
        }
        row["difference_sha256"] = row_hash(row, "difference_sha256")
        rows.append(row)
    for left_unit, right_unit in _unit_pairs(left, right):
        if (
            left_unit.get("dimension") != right_unit.get("dimension")
            or left_unit.get("scale_to_si") != right_unit.get("scale_to_si")
            or left_unit.get("name") != right_unit.get("name")
        ):
            kind = "unit" if left_unit.get("dimension") == right_unit.get("dimension") else "dimension"
            row = {
                "schema_version": DIFFERENCE_SCHEMA,
                "difference_id": stable_id(
                    "sdiff",
                    left_source,
                    right_source,
                    _ref_identity(left),
                    _ref_identity(right),
                    kind,
                    left_unit,
                    right_unit,
                ),
                "left_ref": left_ref,
                "right_ref": right_ref,
                "difference_kind": kind,
                "left_value_hash": sha256_json(left_unit),
                "right_value_hash": sha256_json(right_unit),
                "evidence_binding_ids": sorted(
                    set(left.get("evidence_binding_ids", [])) | set(right.get("evidence_binding_ids", []))
                ),
                "status": "candidate",
                "review_requirement": _review_requirement(difference_kind=kind),
                "resolution": RESOLUTION_POLICY,
            }
            row["difference_sha256"] = row_hash(row, "difference_sha256")
            rows.append(row)
    return sorted(rows, key=lambda row: row["difference_id"])


def _conflict_rows(
    differences: list[dict[str, Any]],
    left_ref: str,
    right_ref: str,
    left_source: str,
    right_source: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for difference in differences:
        kind = difference.get("difference_kind")
        if kind not in HIGH_RISK_DIFFERENCE_KINDS:
            continue
        conflict_kind = "dimension" if kind == "dimension" else kind
        row = {
            "schema_version": COMPOSER_CONFLICT_SCHEMA,
            "conflict_id": stable_id(
                "sconflict",
                left_source,
                right_source,
                left_ref,
                right_ref,
                conflict_kind,
                difference["difference_id"],
                difference.get("evidence_binding_ids", []),
            ),
            "left_ref": left_ref,
            "right_ref": right_ref,
            "conflict_kind": conflict_kind,
            "difference_id": difference["difference_id"],
            "evidence_binding_ids": list(difference.get("evidence_binding_ids", [])),
            "status": "candidate",
            "resolution": RESOLUTION_POLICY,
            "review_requirement": _review_requirement(conflict_kind=conflict_kind),
        }
        row["conflict_sha256"] = row_hash(row, "conflict_sha256")
        rows.append(row)
    return sorted(rows, key=lambda row: row["conflict_id"])


def _unit_map_rows(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    left_ref: str,
    right_ref: str,
    left_source: str,
    right_source: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for left_unit, right_unit in _unit_pairs(left, right):
        left_dimension = left_unit.get("dimension")
        right_dimension = right_unit.get("dimension")
        compatible = bool(
            left_dimension
            and right_dimension
            and left_dimension == right_dimension
            and isinstance(left_unit.get("scale_to_si"), (int, float))
            and isinstance(right_unit.get("scale_to_si"), (int, float))
        )
        factor = None
        if compatible:
            factor = round(float(left_unit["scale_to_si"]) / float(right_unit["scale_to_si"]), 15)
        row = {
            "schema_version": UNIT_MAP_SCHEMA,
            "unit_map_id": stable_id(
                "sunit",
                left_source,
                right_source,
                _ref_identity(left),
                _ref_identity(right),
                left_unit,
                right_unit,
            ),
            "left_ref": left_ref,
            "right_ref": right_ref,
            "left_unit": left_unit,
            "right_unit": right_unit,
            "evidence_binding_ids": sorted(
                set(left.get("evidence_binding_ids", [])) | set(right.get("evidence_binding_ids", []))
            ),
            "conversion_factor": factor,
            "dimension_compatible": compatible,
            "status": "candidate" if compatible else "incompatible",
            "review_requirement": _review_requirement(unit_map=True),
            "resolution": RESOLUTION_POLICY,
        }
        row["unit_map_sha256"] = row_hash(row, "unit_map_sha256")
        rows.append(row)
    return sorted(rows, key=lambda row: row["unit_map_id"])


def _relation_type(
    left: dict[str, Any],
    right: dict[str, Any],
    score: float,
    shared_groups: list[str],
    explicit_relation: str | None = None,
) -> str:
    if explicit_relation is not None:
        mapping = {
            "equivalent": "equivalent_candidate",
            "broader": "broader_candidate",
            "narrower": "narrower_candidate",
            "related": "related_candidate",
            "contradicts": "contradicts_candidate",
            "alias": "alias_candidate",
        }
        if explicit_relation not in mapping:
            raise SemanticComposerError("explicit_relation_type_invalid")
        return mapping[explicit_relation]
    # Lexical identity, shared aliases, and token overlap remain ambiguous
    # candidates. Only an explicit proposer relation may request an
    # equivalent/broader/narrower/contradicts candidate.
    return "related_candidate"


def _alignment_rows(
    refs: list[dict[str, Any]],
    *,
    differences: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    unit_maps: list[dict[str, Any]],
    explicit_relations: dict[tuple[str, str], str] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    package_refs = sorted(
        (row for row in refs if row.get("source_role") == "reference-package"),
        key=lambda row: row["concept_ref_id"],
    )
    candidate_refs = sorted(
        (row for row in refs if row.get("source_role") == "candidate-workpack"),
        key=lambda row: row["concept_ref_id"],
    )
    for package_ref in package_refs:
        for candidate_ref in candidate_refs:
            left, right = package_ref, candidate_ref
            shared_groups = sorted(set(left.get("alias_groups", [])) & set(right.get("alias_groups", [])))
            shared_terms = sorted(set(left.get("normalized_terms", [])) & set(right.get("normalized_terms", [])))
            symbol_overlap = sorted(set(left.get("symbols", [])) & set(right.get("symbols", [])))
            score = _lexical_score(left, right)
            explicit_relation = (explicit_relations or {}).get((left["concept_ref_id"], right["concept_ref_id"]))
            if not explicit_relation and not shared_groups and not shared_terms:
                continue
            left_id, right_id = left["concept_ref_id"], right["concept_ref_id"]
            pair_differences = [
                row
                for row in differences
                if {row.get("left_ref"), row.get("right_ref")} == {left_id, right_id}
            ]
            pair_conflicts = [
                row
                for row in conflicts
                if {row.get("left_ref"), row.get("right_ref")} == {left_id, right_id}
            ]
            pair_units = [
                row
                for row in unit_maps
                if {row.get("left_ref"), row.get("right_ref")} == {left_id, right_id}
            ]
            relation = _relation_type(left, right, score, shared_groups, explicit_relation)
            high_risk = bool(pair_conflicts or pair_units or symbol_overlap or relation in {"equivalent_candidate", "contradicts_candidate"})
            review = _review_requirement(
                relation_type=relation,
                conflict_kind="cross_source_conflict" if pair_conflicts else None,
                symbol_collision=bool(symbol_overlap),
                unit_map=bool(pair_units),
            )
            row = {
                "schema_version": ALIGNMENT_CANDIDATE_SCHEMA,
                "alignment_candidate_id": stable_id(
                    "salign",
                    left.get("source_sha256"),
                    right.get("source_sha256"),
                    left.get("package_id"),
                    right.get("package_id"),
                    left.get("workpack_id"),
                    right.get("workpack_id"),
                    _ref_identity(left),
                    _ref_identity(right),
                    relation,
                ),
                "left_ref": left_id,
                "right_ref": right_id,
                "relation_type": relation,
                "semantic_status": "candidate",
                "signals": {
                    "shared_alias_groups": shared_groups,
                    "shared_normalized_terms": shared_terms,
                    "symbol_overlap": symbol_overlap,
                    "lexical_score": score,
                    "lexical_match_is_not_equivalence": True,
                    "explicit_relation_proposal": explicit_relation is not None,
                    "high_risk": high_risk,
                },
                "difference_ids": sorted(row["difference_id"] for row in pair_differences),
                "conflict_ids": sorted(row["conflict_id"] for row in pair_conflicts),
                "unit_map_ids": sorted(row["unit_map_id"] for row in pair_units),
                "review_requirement": review,
                "evidence_binding_ids": sorted(
                    set(left.get("evidence_binding_ids", [])) | set(right.get("evidence_binding_ids", []))
                ),
                "resolution": RESOLUTION_POLICY,
                "promotion_eligible": False,
            }
            row["alignment_candidate_sha256"] = row_hash(row, "alignment_candidate_sha256")
            rows.append(row)
    return sorted(rows, key=lambda row: row["alignment_candidate_id"])


def _explicit_relation_pairs(
    spec: dict[str, Any],
    package_refs: list[dict[str, Any]],
    candidate_refs: list[dict[str, Any]],
) -> dict[tuple[str, str], str]:
    by_object = {
        str(row.get("local_ref", {}).get("object_id")): row["concept_ref_id"]
        for row in package_refs
        if isinstance(row.get("local_ref"), dict) and isinstance(row.get("local_ref", {}).get("object_id"), str)
    }
    by_concept = {
        str(row.get("local_ref", {}).get("concept_id")): row["concept_ref_id"]
        for row in candidate_refs
        if isinstance(row.get("local_ref"), dict) and isinstance(row.get("local_ref", {}).get("concept_id"), str)
    }
    relations = spec.get("relations", [])
    if relations is None:
        return {}
    if not isinstance(relations, list):
        raise SemanticComposerError("explicit_relations_invalid")
    result: dict[tuple[str, str], str] = {}
    for relation in relations:
        if not isinstance(relation, dict):
            raise SemanticComposerError("explicit_relation_invalid")
        left_id = relation.get("package_object_id") or relation.get("left_object_id")
        right_id = relation.get("candidate_concept_id") or relation.get("right_concept_id")
        if not isinstance(left_id, str) or not isinstance(right_id, str):
            raise SemanticComposerError("explicit_relation_binding_missing")
        left_ref = by_object.get(left_id)
        right_ref = by_concept.get(right_id)
        if left_ref is None or right_ref is None:
            raise SemanticComposerError("explicit_relation_binding_missing")
        relation_type = relation.get("relation_type")
        if relation_type not in {"equivalent", "broader", "narrower", "related", "contradicts", "alias"}:
            raise SemanticComposerError("explicit_relation_type_invalid")
        result[(left_ref, right_ref)] = relation_type
    return result


def _eligible_pair(left: dict[str, Any], right: dict[str, Any], explicit: dict[tuple[str, str], str]) -> bool:
    pair = (left.get("concept_ref_id"), right.get("concept_ref_id"))
    if pair in explicit:
        return True
    shared_groups = set(left.get("alias_groups", [])) & set(right.get("alias_groups", []))
    shared_terms = set(left.get("normalized_terms", [])) & set(right.get("normalized_terms", []))
    # A unit or symbol overlap alone is not an explanatory concept signal;
    # it may be a collision between unrelated objects. Such pairs need an
    # explicit proposer relation or a shared alias/term before differences,
    # conflicts, and unit maps are materialized.
    return bool(shared_groups or shared_terms)


def _review_items(
    alignments: list[dict[str, Any]],
    differences: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    unit_maps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in alignments:
        review = row["review_requirement"]
        items.append(
            {
                "item_id": row["alignment_candidate_id"],
                "item_type": "alignment_candidate",
                "required_reviewer_count": review["required_reviewer_count"],
                "risk_tier": review["risk_tier"],
                "reasons": review["reasons"],
                "candidate_sha256": row["alignment_candidate_sha256"],
                "evidence_binding_ids": row["evidence_binding_ids"],
                "status": "pending",
            }
        )
    for collection, item_type, hash_key in (
        (differences, "difference", "difference_sha256"),
        (conflicts, "conflict", "conflict_sha256"),
        (unit_maps, "unit_map", "unit_map_sha256"),
    ):
        for row in collection:
            review = row["review_requirement"]
            items.append(
                {
                    "item_id": row[next(key for key in row if key.endswith("_id") and key != "schema_version")],
                    "item_type": item_type,
                    "required_reviewer_count": review["required_reviewer_count"],
                    "risk_tier": review["risk_tier"],
                    "reasons": review["reasons"],
                    "candidate_sha256": row[hash_key],
                    "evidence_binding_ids": sorted(set(row.get("evidence_binding_ids", []))),
                    "status": "pending",
                }
            )
    return sorted(items, key=lambda row: (row["item_type"], row["item_id"]))


def _build_review_plan(
    *,
    workpack_id: str,
    created_at: str,
    source_shas: list[str],
    items: list[dict[str, Any]],
    reviewer_instances: list[str],
) -> dict[str, Any]:
    plan = {
        "schema_version": COMPOSER_REVIEW_PLAN_SCHEMA,
        "review_plan_id": stable_id("scrplan", workpack_id, source_shas, [row["item_id"] for row in items]),
        "protocol": COMPOSER_REVIEW_PROTOCOL,
        "workpack_id": workpack_id,
        "created_at": created_at,
        "source_sha256": sorted(set(source_shas)),
        "attestation_level": ATTESTATION_LEVEL,
        "reviewer_registry": sorted(set(reviewer_instances)),
        "proposer_instances": [f"compiler:{SEMANTIC_COMPOSER_COMPILER_VERSION}"],
        "items": items,
        "state": "pending-independent-review" if items else "pending-no-candidates",
        "attestation_authoring_allowed": False,
        "promotion_allowed": False,
        "resolution_policy": RESOLUTION_POLICY,
    }
    plan["review_plan_sha256"] = row_hash(plan, "review_plan_sha256")
    return plan


ARTIFACT_PATHS = {
    "concept_refs": "concept-refs.jsonl",
    "evidence_bindings": "evidence-bindings.jsonl",
    "alignment_candidates": "alignment-candidates.jsonl",
    "differences": "differences.jsonl",
    "conflicts": "conflicts.jsonl",
    "unit_maps": "unit-maps.jsonl",
    "review_plan": "review-plan.json",
    "review_attestations": "review-attestations.jsonl",
    "alias_map": "alias-map.json",
    "state": "state.json",
    "receipts": "receipts.jsonl",
}


def _artifact_inventory(root: Path) -> dict[str, dict[str, Any]]:
    inventory: dict[str, dict[str, Any]] = {}
    for key, relative in ARTIFACT_PATHS.items():
        path = root / relative
        if not path.is_file():
            raise SemanticComposerError(f"artifact_missing: {relative}")
        record: dict[str, Any] = {"path": relative, "sha256": sha256_file(path)}
        if path.suffix == ".jsonl":
            record["record_count"] = len(load_jsonl(path))
        inventory[key] = record
    return inventory


def _write_receipts(
    root: Path,
    *,
    input_hashes: dict[str, Any],
    output_hash: str,
) -> None:
    receipt = {
        "schema_version": COMPOSER_RECEIPT_SCHEMA,
        "receipt_id": stable_id("sreceipt", SEMANTIC_COMPOSER_PROTOCOL, input_hashes, output_hash),
        "step": "composer-build",
        "input_hashes": input_hashes,
        "output_hash": output_hash,
        "status": "built",
        "executed": False,
    }
    _write_jsonl(root / ARTIFACT_PATHS["receipts"], [receipt])


def build_workpack(
    *,
    packages: list[Path],
    candidate_source: Path,
    candidate_spec: Path,
    output: Path,
    created_at: str,
    reviewer_instances: list[str] | None = None,
    candidate_ir_root: Path | None = None,
    pdftoppm: Path | str | None = None,
    candidate_render: Path | None = None,
    candidate_render_page: int | None = None,
    candidate_render_pages: dict[int, Path] | None = None,
) -> dict[str, Any]:
    _ensure_rfc3339(created_at)
    if not packages:
        raise SemanticComposerError("composer_requires_package")
    candidate_source = candidate_source.expanduser().resolve()
    candidate_spec = candidate_spec.expanduser().resolve()
    if not candidate_source.is_file():
        raise SemanticComposerError("candidate_source_missing")
    if not candidate_spec.is_file():
        raise SemanticComposerError("candidate_spec_missing")
    render_paths = {
        int(page): path.expanduser().resolve()
        for page, path in (candidate_render_pages or {}).items()
    }
    if candidate_render is not None:
        if candidate_render_page is None:
            raise SemanticComposerError("render_page_binding_missing")
        render_paths[int(candidate_render_page)] = candidate_render.expanduser().resolve()
    input_paths = [candidate_source, candidate_spec] + [path.expanduser().resolve() for path in packages] + list(render_paths.values())
    if candidate_ir_root is not None:
        input_paths.append(candidate_ir_root.expanduser().resolve())
    output = output.expanduser().resolve()
    _assert_not_inside(output, input_paths, "output_input_overlap")
    output = _ensure_empty_output(output)
    try:
        spec = load_json(candidate_spec)
    except (OSError, json.JSONDecodeError) as error:
        raise SemanticComposerError("candidate_spec_invalid") from error
    if not isinstance(spec, dict) or spec.get("schema_version") != COMPOSER_INPUT_SCHEMA:
        raise SemanticComposerError("candidate_spec_schema_invalid")
    source_record = spec.get("source") if isinstance(spec.get("source"), dict) else {}
    normalized_pdf_ir = source_record.get("normalized_pdf_ir") if isinstance(source_record.get("normalized_pdf_ir"), dict) else None
    if normalized_pdf_ir is not None and candidate_ir_root is None:
        raise SemanticComposerError("candidate_ir_root_required")
    if candidate_ir_root is not None:
        ir_issues: list[Issue] = []
        _validate_candidate_ir_binding(candidate_ir_root, normalized_pdf_ir, source_record.get("sha256"), source_record.get("physical_pages"), candidate_source, pdftoppm, ir_issues, "candidate-input.source.normalized_pdf_ir")
        if ir_issues:
            raise SemanticComposerError(f"candidate_ir_invalid:{ir_issues[0].code}")
    custom_alias_map = spec.get("alias_map") if isinstance(spec.get("alias_map"), dict) else None
    alias_map = _merge_alias_map(custom_alias_map)
    candidate_descriptor, candidate_refs, candidate_bindings = _candidate_concept_refs(
        spec, candidate_source, alias_map, render_paths, sha256_file(candidate_spec)
    )
    package_descriptors: list[dict[str, Any]] = []
    package_refs: list[dict[str, Any]] = []
    package_bindings: list[dict[str, Any]] = []
    seen_package_ids: set[str] = set()
    for package in sorted(packages, key=lambda path: str(path.expanduser().resolve())):
        descriptor, refs, bindings = _package_concept_refs(package, alias_map)
        if descriptor["package_id"] in seen_package_ids:
            raise SemanticComposerError("duplicate_package_id")
        seen_package_ids.add(descriptor["package_id"])
        package_descriptors.append(descriptor)
        package_refs.extend(refs)
        package_bindings.extend(bindings)
    refs = sorted(package_refs + candidate_refs, key=lambda row: row["concept_ref_id"])
    bindings = sorted(package_bindings + candidate_bindings, key=lambda row: row["binding_id"])
    package_ref_rows = sorted(
        (row for row in refs if row["source_role"] == "reference-package"),
        key=lambda row: row["concept_ref_id"],
    )
    candidate_ref_rows = sorted(
        (row for row in refs if row["source_role"] == "candidate-workpack"),
        key=lambda row: row["concept_ref_id"],
    )
    explicit_relations = _explicit_relation_pairs(spec, package_ref_rows, candidate_ref_rows)
    eligible_pairs = [
        (left, right)
        for left in package_ref_rows
        for right in candidate_ref_rows
        if _eligible_pair(left, right, explicit_relations)
    ]
    differences: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    unit_maps: list[dict[str, Any]] = []
    candidate_source_sha = candidate_descriptor["sha256"]
    # Establish the eligible set before computing any semantic differences.
    # This prevents an unrelated cross-product pair from manufacturing a
    # conflict/review item merely because its prose fields differ.
    for left, right in eligible_pairs:
        pair_left, pair_right = left["concept_ref_id"], right["concept_ref_id"]
        pair_differences = _difference_rows(
            left,
            right,
            source_context=(pair_left, pair_right, left["source_sha256"], right["source_sha256"]),
        )
        differences.extend(pair_differences)
        conflicts.extend(
            _conflict_rows(
                pair_differences,
                pair_left,
                pair_right,
                left["source_sha256"],
                right["source_sha256"],
            )
        )
        unit_maps.extend(
            _unit_map_rows(
                left,
                right,
                left_ref=pair_left,
                right_ref=pair_right,
                left_source=left["source_sha256"],
                right_source=right["source_sha256"],
            )
        )
    differences = sorted({row["difference_id"]: row for row in differences}.values(), key=lambda row: row["difference_id"])
    conflicts = sorted({row["conflict_id"]: row for row in conflicts}.values(), key=lambda row: row["conflict_id"])
    unit_maps = sorted({row["unit_map_id"]: row for row in unit_maps}.values(), key=lambda row: row["unit_map_id"])
    alignments = _alignment_rows(
        refs,
        differences=differences,
        conflicts=conflicts,
        unit_maps=unit_maps,
        explicit_relations=explicit_relations,
    )
    review_items = _review_items(alignments, differences, conflicts, unit_maps)
    review_plan = _build_review_plan(
        workpack_id=str(spec["workpack_id"]),
        created_at=created_at,
        source_shas=[candidate_source_sha] + [row["source_sha256"] for row in package_refs],
        items=review_items,
        reviewer_instances=reviewer_instances or [],
    )
    output.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output / ARTIFACT_PATHS["concept_refs"], refs)
    _write_jsonl(output / ARTIFACT_PATHS["evidence_bindings"], bindings)
    _write_jsonl(output / ARTIFACT_PATHS["alignment_candidates"], alignments)
    _write_jsonl(output / ARTIFACT_PATHS["differences"], differences)
    _write_jsonl(output / ARTIFACT_PATHS["conflicts"], conflicts)
    _write_jsonl(output / ARTIFACT_PATHS["unit_maps"], unit_maps)
    write_json(output / ARTIFACT_PATHS["review_plan"], review_plan)
    _write_jsonl(output / ARTIFACT_PATHS["review_attestations"], [])
    write_json(output / ARTIFACT_PATHS["alias_map"], {"schema_version": "tkc.semantic-alias-map/v0.1", "groups": alias_map})
    workpack_state = "pending-independent-review" if review_items else "pending-no-candidates"
    workpack_next_action = (
        "independent-cross-source-review-required"
        if review_items
        else "no-eligible-candidates-review-required"
    )
    state = {
        "state": workpack_state,
        "next_action": workpack_next_action,
        "pause_only": True,
        "completed_steps": ["source-audit", "native-text-candidate-intake", "normalization", "alignment-candidate-build", "review-plan-build"],
        "review_attestation_authoring_allowed": False,
        "promotion_eligible": False,
        "resolution_policy": RESOLUTION_POLICY,
        "reviewer_identity": ATTESTATION_LEVEL,
    }
    write_json(output / ARTIFACT_PATHS["state"], state)
    output_hash = _expected_output_hash(
        refs, bindings, alignments, differences, conflicts, unit_maps, review_plan
    )
    receipt_inputs = {
        "candidate_source_sha256": candidate_source_sha,
        "candidate_spec_sha256": sha256_file(candidate_spec),
        "package_manifest_sha256": {
            descriptor["package_id"]: descriptor["manifest_sha256"]
            for descriptor in package_descriptors
        },
        "candidate_render_pages": [
            {"physical_page": page, "sha256": sha256_file(path)}
            for page, path in sorted(render_paths.items())
        ],
        "protocol": SEMANTIC_COMPOSER_PROTOCOL,
    }
    if isinstance(normalized_pdf_ir, dict):
        receipt_inputs["candidate_ir"] = dict(normalized_pdf_ir)
        receipt_inputs["candidate_ir_verifier"] = "verify_pdf_ir:docling_allow_network=false"
    _write_receipts(
        output,
        input_hashes=receipt_inputs,
        output_hash=output_hash,
    )
    artifacts = _artifact_inventory(output)
    legacy_inputs = any(row.get("input_level") == "legacy-source-support" for row in package_descriptors)
    atomic_required = any(
        row.get("input_level") != "atomic-supported"
        for row in package_descriptors + [candidate_descriptor]
    )
    manifest = {
        "schema_version": COMPOSER_WORKPACK_SCHEMA,
        "protocol": SEMANTIC_COMPOSER_PROTOCOL,
        "compiler_version": SEMANTIC_COMPOSER_COMPILER_VERSION,
        "workpack_id": spec["workpack_id"],
        "created_at": created_at,
        "policy": {
            "cross_source_claims": RESOLUTION_POLICY,
            "lexical_match_is_not_semantic_equivalence": True,
            "automatic_consensus_allowed": False,
            "automatic_merge_allowed": False,
            "capability_escalation_allowed": False,
            "review_attestation_authoring_allowed": False,
            "network_allowed": False,
            "source_text_in_compiler_release_allowed": False,
        },
        "inputs": {
            "packages": package_descriptors,
            "candidate_source": candidate_descriptor,
            "candidate_spec_sha256": sha256_file(candidate_spec),
            "alias_map_sha256": sha256_file(output / ARTIFACT_PATHS["alias_map"]),
            "candidate_render_pages": [
                {"physical_page": page, "sha256": sha256_file(path)}
                for page, path in sorted(render_paths.items())
            ],
        },
        "counts": {
            "concept_refs": len(refs),
            "evidence_bindings": len(bindings),
            "alignment_candidates": len(alignments),
            "differences": len(differences),
            "conflicts": len(conflicts),
            "unit_maps": len(unit_maps),
            "review_items": len(review_items),
            "pending_review_items": len(review_items),
        },
        "artifacts": artifacts,
        "state": {
            "state": workpack_state,
            "next_action": workpack_next_action,
            "pause_only": True,
        },
        "promotion": {
            "promotion_eligible": False,
            "atomic_support_required": atomic_required,
            "legacy_source_support_present": legacy_inputs,
            "reason_codes": sorted(
                {
                    "legacy_source_support" if legacy_inputs else "",
                    "atomic_support_required",
                    "independent_review_required",
                    "preserve_separate_no_consensus",
                }
                - {""}
            ),
            "whole_book_completeness_claimed": False,
        },
        "output_hash": output_hash,
    }
    if isinstance(candidate_descriptor.get("normalized_pdf_ir"), dict):
        manifest["inputs"]["normalized_pdf_ir"] = dict(candidate_descriptor["normalized_pdf_ir"])
    write_json(output / "workpack.json", manifest)
    return manifest


def _issue(issues: list[Issue], code: str, path: str, message: str, severity: str = "error") -> None:
    issues.append(Issue(severity=severity, code=code, path=path, message=message))


def _load_rows(root: Path, relative: str, issues: list[Issue]) -> list[dict[str, Any]]:
    path = root / relative
    if not path.is_file():
        _issue(issues, "artifact_missing", relative, "Composer artifact is missing.")
        return []
    try:
        rows = load_jsonl(path)
    except (OSError, ValueError) as error:
        _issue(issues, "artifact_invalid", relative, str(error))
        return []
    return [row for row in rows if isinstance(row, dict)]


def _recompute_concept_id(row: dict[str, Any]) -> str | None:
    local_ref = row.get("local_ref")
    if not isinstance(local_ref, dict):
        return None
    if row.get("source_role") == "candidate-workpack":
        local_id = local_ref.get("concept_id")
        if not isinstance(local_id, str):
            return None
        return stable_id(
            "scref",
            row.get("source_sha256"),
            row.get("package_id"),
            row.get("workpack_id"),
            local_id,
            local_ref.get("candidate_spec_sha256"),
            sorted(set(row.get("evidence_binding_ids", []))),
        )
    object_id = local_ref.get("object_id")
    if not isinstance(object_id, str):
        return None
    return stable_id(
        "scref",
        row.get("source_sha256"),
        row.get("package_id"),
        row.get("workpack_id"),
        object_id,
        local_ref.get("package_manifest_sha256"),
        local_ref.get("object_sha256"),
        sorted(set(row.get("evidence_binding_ids", []))),
        sorted(set(row.get("atomic_assertion_ids", []))),
        sorted(set(row.get("support_sha256s", []))),
    )


def _recompute_evidence_id(row: dict[str, Any]) -> str | None:
    evidence_ids = row.get("evidence_ids")
    if not isinstance(evidence_ids, list) or len(evidence_ids) != 1 or not isinstance(evidence_ids[0], str):
        return None
    if row.get("workpack_id") is not None:
        workpack_id = row.get("workpack_id")
        local_ref = row.get("local_ref")
        if not isinstance(workpack_id, str) or not isinstance(local_ref, str):
            return None
        prefix = f"{workpack_id}:"
        if not local_ref.startswith(prefix) or not local_ref[len(prefix) :]:
            return None
        return stable_id(
            "sebind",
            row.get("source_sha256"),
            workpack_id,
            local_ref[len(prefix) :],
            evidence_ids[0],
        )
    local_ref = row.get("local_ref")
    if not isinstance(local_ref, str):
        return None
    return stable_id(
        "sebind",
        row.get("source_sha256"),
        row.get("package_id"),
        None,
        local_ref,
        evidence_ids[0],
    )


def _validate_row_hash(row: dict[str, Any], key: str, path: str, issues: list[Issue]) -> None:
    actual = row.get(key)
    if not isinstance(actual, str) or actual != row_hash(row, key):
        _issue(issues, "hash_mismatch", path, f"{key} does not recompute.")


def _validate_source_inputs(
    root: Path,
    manifest: dict[str, Any],
    *,
    packages: list[Path] | None,
    candidate_source: Path | None,
    candidate_spec: Path | None,
    candidate_ir_root: Path | None,
    pdftoppm: Path | str | None,
    candidate_render: Path | None,
    candidate_render_page: int | None,
    candidate_render_pages: dict[int, Path] | None,
    issues: list[Issue],
) -> None:
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict):
        _issue(issues, "input_binding_missing", "workpack.json.inputs", "Input binding is missing.")
        return
    package_descriptors = inputs.get("packages")
    if not isinstance(package_descriptors, list):
        _issue(issues, "input_binding_missing", "workpack.json.inputs.packages", "Package input binding is missing.")
    elif packages is None:
        _issue(issues, "source_binding_unverifiable", "workpack.json.inputs.packages", "Provide --package to verify package identity.")
    else:
        by_id: dict[str, Path] = {}
        for package in packages:
            try:
                package_manifest = load_json(package.expanduser().resolve() / "manifest.json")
                package_id = package_manifest.get("package", {}).get("id")
                if isinstance(package_id, str):
                    by_id[package_id] = package.expanduser().resolve()
            except (OSError, json.JSONDecodeError):
                _issue(issues, "source_binding_unverifiable", str(package), "Package manifest cannot be read.")
        for descriptor in package_descriptors or []:
            if not isinstance(descriptor, dict):
                _issue(issues, "input_binding_invalid", "workpack.json.inputs.packages", "Invalid package descriptor.")
                continue
            package_id = descriptor.get("package_id")
            package = by_id.get(package_id) if isinstance(package_id, str) else None
            if package is None:
                _issue(issues, "source_identity_drift", str(package_id), "Package input is absent or reordered identity does not match.")
                continue
            manifest_path = package / "manifest.json"
            if descriptor.get("manifest_sha256") != sha256_file(manifest_path):
                _issue(issues, "source_identity_drift", str(package_id), "Package manifest hash changed.")
            try:
                actual_manifest = load_json(manifest_path)
                actual_hashes = sorted(
                    source.get("sha256")
                    for source in actual_manifest.get("sources", [])
                    if isinstance(source, dict) and isinstance(source.get("sha256"), str)
                )
                if actual_hashes != descriptor.get("source_sha256"):
                    _issue(issues, "source_identity_drift", str(package_id), "Package source SHA changed.")
            except (OSError, json.JSONDecodeError):
                _issue(issues, "source_binding_unverifiable", str(package_id), "Package manifest cannot be reloaded.")
    candidate_descriptor = inputs.get("candidate_source")
    if not isinstance(candidate_descriptor, dict):
        _issue(issues, "input_binding_missing", "workpack.json.inputs.candidate_source", "Candidate source binding is missing.")
    elif candidate_source is None:
        _issue(issues, "source_binding_unverifiable", "workpack.json.inputs.candidate_source", "Provide --candidate-source to verify source identity.")
    else:
        source = candidate_source.expanduser().resolve()
        if not source.is_file():
            _issue(issues, "source_binding_unverifiable", str(source), "Candidate source is missing.")
        elif candidate_descriptor.get("sha256") != sha256_file(source):
            _issue(issues, "source_identity_drift", str(source), "Candidate source SHA changed.")
    normalized_ir = candidate_descriptor.get("normalized_pdf_ir") if isinstance(candidate_descriptor, dict) else None
    _validate_candidate_ir_binding(
        candidate_ir_root,
        normalized_ir if isinstance(normalized_ir, dict) else None,
        candidate_descriptor.get("sha256") if isinstance(candidate_descriptor, dict) else None,
        candidate_descriptor.get("scope", {}).get("physical_pages") if isinstance(candidate_descriptor, dict) and isinstance(candidate_descriptor.get("scope"), dict) else None,
        candidate_source,
        pdftoppm,
        issues,
        "workpack.json.inputs.candidate_source.normalized_pdf_ir",
    )
    expected_spec_sha = inputs.get("candidate_spec_sha256")
    if isinstance(expected_spec_sha, str) and candidate_spec is None:
        _issue(issues, "source_binding_unverifiable", "workpack.json.inputs.candidate_spec_sha256", "Frozen candidate input requires --candidate-spec for replay validation.")
    elif candidate_spec is not None:
        if not candidate_spec.is_file():
            _issue(issues, "source_binding_unverifiable", str(candidate_spec), "Candidate input spec is missing.")
        elif expected_spec_sha != sha256_file(candidate_spec):
            _issue(issues, "candidate_spec_stale", str(candidate_spec), "Candidate input spec hash changed.")
    render_paths = {
        int(page): path.expanduser().resolve()
        for page, path in (candidate_render_pages or {}).items()
    }
    if candidate_render is not None:
        if candidate_render_page is None:
            _issue(issues, "render_page_binding_missing", str(candidate_render), "A single candidate render requires an explicit physical-page mapping.")
        else:
            render_paths[int(candidate_render_page)] = candidate_render.expanduser().resolve()
    expected_render_pages = {
        int(row.get("physical_page")): row.get("sha256")
        for row in inputs.get("candidate_render_pages", [])
        if isinstance(row, dict) and isinstance(row.get("physical_page"), int) and isinstance(row.get("sha256"), str)
    }
    if expected_render_pages and not render_paths:
        _issue(issues, "source_binding_unverifiable", "workpack.json.inputs.candidate_render_pages", "Visual candidate validation requires every frozen physical-page render mapping.")
    if render_paths and set(render_paths) != set(expected_render_pages):
        _issue(issues, "render_page_binding_mismatch", "workpack.json.inputs.candidate_render_pages", "Render verification pages differ from the frozen page map.")
    for page, render_path in sorted(render_paths.items()):
        if not render_path.is_file():
            _issue(issues, "source_binding_unverifiable", str(render_path), "Candidate render is missing.")
            continue
        render_sha = sha256_file(render_path)
        if expected_render_pages.get(page) != render_sha:
            _issue(issues, "render_hash_mismatch", str(render_path), "Candidate render hash differs from its physical-page binding.")
        bound_pages = {
            row.get("locator", {}).get("physical_page")
            for row in _load_rows(root, ARTIFACT_PATHS["evidence_bindings"], [])
            if isinstance(row.get("locator"), dict) and row.get("render_sha256") == render_sha
        }
        if bound_pages and bound_pages != {page}:
            _issue(issues, "render_page_binding_mismatch", str(render_path), "A render hash is bound to a physical page other than its explicit page map.")


def _validate_evidence_source_bindings(
    root: Path,
    bindings: list[dict[str, Any]],
    refs: dict[str, dict[str, Any]],
    *,
    candidate_source: Path | None,
    require_source_replay: bool = True,
    issues: list[Issue],
) -> None:
    for index, row in enumerate(bindings, start=1):
        path = f"{ARTIFACT_PATHS['evidence_bindings']}:{index}"
        if row.get("schema_version") != EVIDENCE_BINDING_SCHEMA:
            _issue(issues, "evidence_binding_invalid", path, "Unexpected evidence binding schema.")
        _validate_row_hash(row, "evidence_binding_sha256", path, issues)
        if _recompute_evidence_id(row) != row.get("binding_id"):
            _issue(issues, "evidence_binding_id_mismatch", path, "Evidence binding ID is not bound to source/package/local/evidence identity.")
        local_ref = row.get("local_ref")
        expected_local_refs = set()
        for ref in refs.values():
            ref_local = ref.get("local_ref", {})
            if ref.get("source_role") == "candidate-workpack" and isinstance(ref_local, dict):
                concept_id = ref_local.get("concept_id")
                if isinstance(concept_id, str):
                    expected_local_refs.add(f"{ref.get('package_id')}:{concept_id}")
            elif isinstance(ref_local, dict) and isinstance(ref_local.get("object_id"), str):
                expected_local_refs.add(ref_local["object_id"])
        if local_ref not in expected_local_refs:
            _issue(issues, "evidence_binding_orphan", path, "Evidence binding local reference is absent.")
        locator = row.get("locator")
        if not isinstance(locator, dict):
            _issue(issues, "evidence_locator_missing", path, "Evidence locator is missing.")
        if row.get("parser") == "pypdf":
            if not isinstance(locator, dict) or not isinstance(locator.get("physical_page"), int):
                _issue(issues, "physical_page_missing", path, "Candidate evidence requires a 1-based physical page.")
            if not isinstance(locator, dict) or not isinstance(locator.get("printed_label"), str):
                _issue(issues, "printed_label_candidate_missing", path, "Candidate evidence requires a printed-label candidate.")
            if not isinstance(locator, dict) or not isinstance(locator.get("page_text_sha256"), str):
                _issue(issues, "content_locator_missing", path, "Candidate evidence requires a page text hash.")
            if not isinstance(locator, dict) or locator.get("bbox_space") != "pdf-page-top-left-points-v1":
                _issue(issues, "bbox_space_invalid", path, "Candidate bbox coordinates must use the project top-left PDF point space.")
            if locator.get("bbox_policy") == "full-page-candidate":
                if locator.get("exact_bbox") is not False or locator.get("locator_precision") != "full-page":
                    _issue(issues, "bbox_precision_overstated", path, "Full-page candidate locator must be explicitly non-exact.")
                if not row.get("review_required"):
                    _issue(issues, "visual_review_required", path, "Full-page candidate locator requires visual review.")
            if locator.get("exact_bbox") is True and row.get("review_required") is not True:
                _issue(issues, "visual_review_required", path, "Candidate bbox requires review even when explicitly supplied.")
    if candidate_source is None:
        if require_source_replay and any(row.get("parser") == "pypdf" for row in bindings):
            _issue(
                issues,
                "source_binding_unverifiable",
                ARTIFACT_PATHS["evidence_bindings"],
                "Native-text candidate evidence requires the original candidate source for replay.",
            )
        return
    candidate_bindings = [row for row in bindings if row.get("parser") == "pypdf"]
    if not candidate_bindings:
        return
    scope_pages = sorted({row.get("locator", {}).get("physical_page") for row in candidate_bindings if isinstance(row.get("locator"), dict)})
    try:
        _, pypdf_version, page_texts, page_geometries = _load_pypdf_pages(candidate_source, scope_pages)
    except SemanticComposerError as error:
        _issue(issues, str(error), str(candidate_source), "Candidate source cannot be re-extracted.")
        return
    for index, row in enumerate(candidate_bindings, start=1):
        locator = row.get("locator", {})
        page = locator.get("physical_page")
        if page not in page_texts:
            continue
        start, end = locator.get("span_start"), locator.get("span_end")
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(page_texts[page]):
            _issue(issues, "candidate_span_invalid", f"{ARTIFACT_PATHS['evidence_bindings']}:{index}", "Candidate span is not within the current page.")
            continue
        if sha256_text(page_texts[page][start:end]) != row.get("content_hash"):
            _issue(issues, "source_identity_drift", f"{ARTIFACT_PATHS['evidence_bindings']}:{index}", "Candidate content hash changed.")
        if sha256_text(unicodedata.normalize("NFKC", page_texts[page])) != locator.get("page_text_sha256"):
            _issue(issues, "source_identity_drift", f"{ARTIFACT_PATHS['evidence_bindings']}:{index}", "Candidate page text hash changed.")
        if row.get("parser_version") != pypdf_version:
            _issue(issues, "parser_version_mismatch", f"{ARTIFACT_PATHS['evidence_bindings']}:{index}", "Candidate parser version changed.")
        geometry = page_geometries.get(page)
        if locator.get("page_geometry") != geometry:
            _issue(issues, "page_geometry_mismatch", f"{ARTIFACT_PATHS['evidence_bindings']}:{index}", "Candidate page geometry or rotation changed.")
        if locator.get("bbox_policy") == "full-page-candidate" and locator.get("bbox") != geometry.get("full_page_bbox"):
            _issue(issues, "bbox_geometry_mismatch", f"{ARTIFACT_PATHS['evidence_bindings']}:{index}", "Full-page candidate bbox does not match the actual rotated MediaBox/CropBox display geometry.")


def _validate_atomic_support_against_packages(
    packages: list[Path] | None,
    refs: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
    issues: list[Issue],
) -> None:
    """Recompute exact Phase 6A assertion/support closure for atomic inputs.

    Composer rows carry support SHA-256 values rather than assertion IDs.  A
    non-empty list is not sufficient: when the original package is supplied
    for replay, the assertion parent is resolved through the assurance
    manifest entity map and the expected support hash set is recomputed from
    the package's immutable semantic sidecars.
    """
    atomic_refs = [row for row in refs if row.get("input_assurance") == "atomic-supported"]
    if not atomic_refs:
        return
    if packages is None:
        _issue(
            issues,
            "source_binding_unverifiable",
            ARTIFACT_PATHS["concept_refs"],
            "Atomic-supported Composer refs require the original package for exact assertion/support replay.",
        )
        return
    package_by_id: dict[str, Path] = {}
    for package in packages:
        package = package.expanduser().resolve()
        try:
            manifest = load_json(package / "manifest.json")
        except (OSError, json.JSONDecodeError):
            continue
        package_id = manifest.get("package", {}).get("id") if isinstance(manifest.get("package"), dict) else None
        if isinstance(package_id, str):
            package_by_id[package_id] = package
    binding_by_id = {
        row.get("binding_id"): row
        for row in bindings
        if isinstance(row, dict) and isinstance(row.get("binding_id"), str)
    }
    for ref in atomic_refs:
        path = f"{ARTIFACT_PATHS['concept_refs']}:{ref.get('concept_ref_id')}"
        local_ref = ref.get("local_ref") if isinstance(ref.get("local_ref"), dict) else {}
        object_id = local_ref.get("object_id")
        package = package_by_id.get(ref.get("package_id"))
        if not isinstance(object_id, str) or package is None:
            _issue(issues, "atomic_support_unverifiable", path, "Atomic-supported ref has no replayable package/object binding.")
            continue
        try:
            assurance_manifest = load_json(package / "references/semantic/assurance-manifest.json")
            assertion_rows = load_jsonl(package / "references/semantic/semantic-assertions.jsonl")
            support_rows = load_jsonl(package / "references/semantic/support-matrix.jsonl")
        except (OSError, ValueError, json.JSONDecodeError):
            _issue(issues, "atomic_support_unverifiable", path, "Atomic-supported package sidecars cannot be read for exact replay.")
            continue
        entity_map = assurance_manifest.get("entity_map") if isinstance(assurance_manifest, dict) else None
        if not isinstance(entity_map, list):
            _issue(issues, "atomic_support_unverifiable", path, "Atomic-supported package has no assurance entity map.")
            continue
        expected_assertions = _atomic_assertions_for_object(object_id, entity_map, assertion_rows)
        support_by_assertion = {
            row.get("assertion_id"): row
            for row in support_rows
            if isinstance(row, dict) and isinstance(row.get("assertion_id"), str)
        }
        expected_assertion_ids = sorted(
            row["assertion_id"] for row in expected_assertions if isinstance(row.get("assertion_id"), str)
        )
        expected_support_hashes: list[str] = []
        for assertion_id in expected_assertion_ids:
            support = support_by_assertion.get(assertion_id)
            support_sha = support.get("support_sha256") if isinstance(support, dict) else None
            if not isinstance(support_sha, str) or not SHA256_RE.fullmatch(support_sha):
                _issue(issues, "atomic_support_invalid", path, f"Assertion {assertion_id} has no valid support_sha256.")
                continue
            expected_support_hashes.append(support_sha)
        if sorted(set(ref.get("atomic_assertion_ids", []))) != expected_assertion_ids:
            _issue(issues, "atomic_support_binding_mismatch", path, "Atomic assertion IDs differ from the exact entity-map-resolved package set.")
        if sorted(set(ref.get("support_sha256s", []))) != sorted(set(expected_support_hashes)):
            _issue(issues, "atomic_support_binding_mismatch", path, "Support SHA-256 values differ from the exact package support matrix set.")
        for binding_id in ref.get("evidence_binding_ids", []) if isinstance(ref.get("evidence_binding_ids"), list) else []:
            binding = binding_by_id.get(binding_id)
            if not isinstance(binding, dict):
                continue
            if "support_ids" in binding or "support_ids" in ref:
                _issue(issues, "atomic_support_invalid", path, "Atomic Composer bindings must use support_sha256s, never assertion/support IDs as hashes.")
            if sorted(set(binding.get("atomic_assertion_ids", []))) != expected_assertion_ids:
                _issue(issues, "atomic_support_binding_mismatch", str(binding_id), "Evidence binding assertion set is not the exact package object closure.")
            if sorted(set(binding.get("support_sha256s", []))) != sorted(set(expected_support_hashes)):
                _issue(issues, "atomic_support_binding_mismatch", str(binding_id), "Evidence binding support hash set is not the exact package object closure.")


def _pair_key(left_ref: Any, right_ref: Any) -> frozenset[str]:
    return frozenset((str(left_ref), str(right_ref)))


def _expected_output_hash(
    refs: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
    alignments: list[dict[str, Any]],
    differences: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    unit_maps: list[dict[str, Any]],
    review_plan: dict[str, Any],
) -> str:
    return sha256_json(
        {
            "concept_refs": [row["concept_ref_id"] for row in refs],
            "bindings": [row["binding_id"] for row in bindings],
            "alignments": [row["alignment_candidate_id"] for row in alignments],
            "differences": [row["difference_id"] for row in differences],
            "conflicts": [row["conflict_id"] for row in conflicts],
            "unit_maps": [row["unit_map_id"] for row in unit_maps],
            "review_plan": review_plan.get("review_plan_sha256"),
        }
    )


def _validate_composer_rows(
    refs: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
    alignments: list[dict[str, Any]],
    differences: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    unit_maps: list[dict[str, Any]],
    review_plan: dict[str, Any],
    candidate_source: Path | None,
    require_source_replay: bool,
    issues: list[Issue],
) -> None:
    refs_by_id: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(refs, start=1):
        path = f"{ARTIFACT_PATHS['concept_refs']}:{index}"
        if row.get("schema_version") != CONCEPT_REF_SCHEMA:
            _issue(issues, "concept_ref_invalid", path, "Unexpected concept reference schema.")
        identifier = row.get("concept_ref_id")
        if not isinstance(identifier, str) or identifier in refs_by_id:
            _issue(issues, "concept_ref_invalid", path, "Concept reference ID is missing or duplicated.")
            continue
        refs_by_id[identifier] = row
        _validate_row_hash(row, "concept_ref_sha256", path, issues)
        if _recompute_concept_id(row) != identifier:
            _issue(issues, "concept_ref_id_mismatch", path, "Concept reference ID is not source/workpack/object bound.")
        if row.get("input_level") not in SOURCE_LEVELS:
            _issue(issues, "concept_ref_invalid", path, "Unknown source input level.")
        if row.get("input_assurance") != row.get("input_level"):
            _issue(issues, "atomic_support_invalid", path, "Composer input assurance must explicitly mirror the source level.")
        if row.get("promotion_eligible") is not False:
            _issue(issues, "promotion_forbidden", path, "Composer concept references cannot be promoted by this stage.")
        for binding_id in row.get("evidence_binding_ids", []):
            if not isinstance(binding_id, str):
                _issue(issues, "evidence_binding_invalid", path, "Evidence binding IDs must be strings.")

    if not refs_by_id:
        _issue(issues, "concept_ref_missing", ARTIFACT_PATHS["concept_refs"], "No concept references were emitted.")
    binding_by_id: dict[str, dict[str, Any]] = {}
    _validate_evidence_source_bindings(
        Path("."),
        bindings,
        refs_by_id,
        candidate_source=candidate_source,
        require_source_replay=require_source_replay,
        issues=issues,
    )
    for index, row in enumerate(bindings, start=1):
        path = f"{ARTIFACT_PATHS['evidence_bindings']}:{index}"
        identifier = row.get("binding_id")
        if not isinstance(identifier, str) or identifier in binding_by_id:
            _issue(issues, "evidence_binding_invalid", path, "Evidence binding ID is missing or duplicated.")
            continue
        binding_by_id[identifier] = row
        _validate_row_hash(row, "evidence_binding_sha256", path, issues)
        if _recompute_evidence_id(row) != identifier:
            _issue(issues, "evidence_binding_id_mismatch", path, "Evidence binding ID is not source/package/local/evidence bound.")
        if not row.get("evidence_ids"):
            _issue(issues, "evidence_binding_missing", path, "Every evidence binding requires an evidence ID.")
        source_ref = next(
            (
                ref
                for ref in refs_by_id.values()
                if row.get("source_sha256") == ref.get("source_sha256")
                and (
                    row.get("local_ref") == ref.get("local_ref", {}).get("object_id")
                    or row.get("local_ref") == f"{ref.get('package_id')}:{ref.get('local_ref', {}).get('concept_id')}"
                )
            ),
            None,
        )
        if source_ref is None:
            _issue(issues, "evidence_binding_orphan", path, "Evidence binding has no matching source-local concept reference.")
        if source_ref is not None and identifier not in source_ref.get("evidence_binding_ids", []):
            _issue(issues, "evidence_binding_orphan", path, "Concept reference does not retain its evidence binding.")
        if row.get("input_assurance") == "atomic-supported":
            if not row.get("atomic_assertion_ids"):
                _issue(issues, "atomic_support_missing", path, "Atomic-supported binding must retain non-empty assertion IDs.")
            if not row.get("support_sha256s"):
                _issue(issues, "atomic_support_missing", path, "Atomic-supported binding must retain non-empty support hashes.")
            if any(not isinstance(value, str) or not SHA256_RE.fullmatch(value) for value in row.get("support_sha256s", [])):
                _issue(issues, "atomic_support_invalid", path, "Support bindings must be SHA-256 hashes, not assertion IDs.")

    for ref in refs_by_id.values():
        if ref.get("input_assurance") != "atomic-supported":
            continue
        ref_bindings = [binding_by_id.get(identifier) for identifier in ref.get("evidence_binding_ids", [])]
        assertion_ids = {
            value
            for binding in ref_bindings
            if isinstance(binding, dict)
            for value in binding.get("atomic_assertion_ids", [])
            if isinstance(value, str)
        }
        support_hashes = {
            value
            for binding in ref_bindings
            if isinstance(binding, dict)
            for value in binding.get("support_sha256s", [])
            if isinstance(value, str)
        }
        if not assertion_ids or not support_hashes:
            _issue(issues, "atomic_support_missing", str(ref.get("concept_ref_id")), "Atomic-supported concept lacks complete assertion/support binding closure.")

    def _index_rows(collection: list[dict[str, Any]], id_key: str, hash_key: str, schema: str, path_key: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for index, row in enumerate(collection, start=1):
            path = f"{ARTIFACT_PATHS[path_key]}:{index}"
            if row.get("schema_version") != schema:
                _issue(issues, f"{path_key}_invalid", path, "Unexpected Composer row schema.")
            identifier = row.get(id_key)
            if not isinstance(identifier, str) or identifier in result:
                _issue(issues, f"{path_key}_invalid", path, "Row ID is missing or duplicated.")
                continue
            result[identifier] = row
            _validate_row_hash(row, hash_key, path, issues)
            if row.get("resolution") != RESOLUTION_POLICY:
                _issue(issues, "consensus_forbidden", path, "Composer rows must preserve separate sources without consensus.")
        return result

    alignment_by_id = _index_rows(
        alignments,
        "alignment_candidate_id",
        "alignment_candidate_sha256",
        ALIGNMENT_CANDIDATE_SCHEMA,
        "alignment_candidates",
    )
    difference_by_id = _index_rows(differences, "difference_id", "difference_sha256", DIFFERENCE_SCHEMA, "differences")
    conflict_by_id = _index_rows(conflicts, "conflict_id", "conflict_sha256", COMPOSER_CONFLICT_SCHEMA, "conflicts")
    unit_by_id = _index_rows(unit_maps, "unit_map_id", "unit_map_sha256", UNIT_MAP_SCHEMA, "unit_maps")
    alignment_pairs = {_pair_key(row.get("left_ref"), row.get("right_ref")) for row in alignment_by_id.values()}

    for identifier, row in alignment_by_id.items():
        path = f"{ARTIFACT_PATHS['alignment_candidates']}:{identifier}"
        left = refs_by_id.get(row.get("left_ref"))
        right = refs_by_id.get(row.get("right_ref"))
        if left is None or right is None or left.get("source_role") != "reference-package" or right.get("source_role") != "candidate-workpack":
            _issue(issues, "alignment_binding_missing", path, "Alignment must bind reference-package to candidate-workpack refs.")
            continue
        expected_id = stable_id(
            "salign",
            left.get("source_sha256"),
            right.get("source_sha256"),
            left.get("package_id"),
            right.get("package_id"),
            left.get("workpack_id"),
            right.get("workpack_id"),
            _ref_identity(left),
            _ref_identity(right),
            row.get("relation_type"),
        )
        if identifier != expected_id:
            _issue(issues, "alignment_id_mismatch", path, "Alignment ID is not bound to both source identities and relation candidate.")
        if row.get("semantic_status") != "candidate" or row.get("promotion_eligible") is not False:
            _issue(issues, "promotion_forbidden", path, "Alignment candidates cannot be marked reviewed/promotable by the compiler.")
        if row.get("resolution") != RESOLUTION_POLICY:
            _issue(issues, "consensus_forbidden", path, "Alignment resolution policy is not preserve-separate-no-consensus.")
        signals = row.get("signals") if isinstance(row.get("signals"), dict) else {}
        if signals.get("lexical_match_is_not_equivalence") is not True:
            _issue(issues, "lexical_equivalence_forbidden", path, "Lexical matching must be explicitly non-equivalent.")
        if row.get("relation_type") != "related_candidate" and signals.get("explicit_relation_proposal") is not True:
            _issue(issues, "lexical_equivalence_forbidden", path, "Non-related relation candidates require an explicit proposer relation.")
        expected_evidence_ids = sorted(
            set(left.get("evidence_binding_ids", [])) | set(right.get("evidence_binding_ids", []))
        )
        if sorted(set(row.get("evidence_binding_ids", []))) != expected_evidence_ids:
            _issue(issues, "evidence_binding_incomplete", path, "Alignment evidence bindings must be the exact union of both concept refs.")
        linked_ids = set(row.get("difference_ids", [])) | set(row.get("conflict_ids", [])) | set(row.get("unit_map_ids", []))
        if any(identifier not in difference_by_id and identifier not in conflict_by_id and identifier not in unit_by_id for identifier in linked_ids):
            _issue(issues, "alignment_binding_missing", path, "Alignment references an absent difference/conflict/unit-map row.")
        if _pair_key(row.get("left_ref"), row.get("right_ref")) not in alignment_pairs:
            _issue(issues, "alignment_binding_missing", path, "Alignment pair is not internally coherent.")
        relation = row.get("relation_type")
        expected_risk = bool(
            row.get("conflict_ids")
            or row.get("unit_map_ids")
            or signals.get("symbol_overlap")
            or relation in {"equivalent_candidate", "contradicts_candidate"}
        )
        requirement = row.get("review_requirement") if isinstance(row.get("review_requirement"), dict) else {}
        if requirement.get("required_reviewer_count") != (2 if expected_risk else 1):
            _issue(issues, "review_requirement_invalid", path, "Alignment reviewer count does not match risk tier.")

    for identifier, row in difference_by_id.items():
        path = f"{ARTIFACT_PATHS['differences']}:{identifier}"
        left = refs_by_id.get(row.get("left_ref"))
        right = refs_by_id.get(row.get("right_ref"))
        if left is None or right is None or _pair_key(row.get("left_ref"), row.get("right_ref")) not in alignment_pairs:
            _issue(issues, "difference_orphan", path, "Difference must be bound to an eligible alignment pair.")
            continue
        expected_evidence_ids = sorted(
            set(left.get("evidence_binding_ids", [])) | set(right.get("evidence_binding_ids", []))
        )
        if sorted(set(row.get("evidence_binding_ids", []))) != expected_evidence_ids:
            _issue(issues, "evidence_binding_incomplete", path, "Difference evidence bindings must be the exact union of both concept refs.")
        kind = row.get("difference_kind")
        expected_id = stable_id(
            "sdiff",
            left.get("source_sha256"),
            right.get("source_sha256"),
            _ref_identity(left),
            _ref_identity(right),
            kind,
        )
        if kind in {"unit", "dimension"}:
            expected_ids = {
                candidate["difference_id"]
                for candidate in _difference_rows(
                    left,
                    right,
                    source_context=(row.get("left_ref"), row.get("right_ref"), left.get("source_sha256"), right.get("source_sha256")),
                )
                if candidate.get("difference_kind") == kind
            }
            if identifier not in expected_ids:
                _issue(issues, "difference_id_mismatch", path, "Unit/dimension difference ID is not deterministic for its pair.")
        elif identifier != expected_id:
            _issue(issues, "difference_id_mismatch", path, "Difference ID is not deterministic for its pair/kind.")

    for identifier, row in conflict_by_id.items():
        path = f"{ARTIFACT_PATHS['conflicts']}:{identifier}"
        difference = difference_by_id.get(row.get("difference_id"))
        if difference is None or row.get("left_ref") != difference.get("left_ref") or row.get("right_ref") != difference.get("right_ref"):
            _issue(issues, "conflict_orphan", path, "Conflict must bind an existing difference and the same pair.")
            continue
        if sorted(set(row.get("evidence_binding_ids", []))) != sorted(set(difference.get("evidence_binding_ids", []))):
            _issue(issues, "evidence_binding_incomplete", path, "Conflict evidence bindings must match its difference pair.")
        expected_id = stable_id(
            "sconflict",
            refs_by_id[row["left_ref"]].get("source_sha256"),
            refs_by_id[row["right_ref"]].get("source_sha256"),
            row.get("left_ref"),
            row.get("right_ref"),
            row.get("conflict_kind"),
            row.get("difference_id"),
            difference.get("evidence_binding_ids", []),
        )
        if identifier != expected_id:
            _issue(issues, "conflict_id_mismatch", path, "Conflict ID is not deterministic for its bound difference.")

    for identifier, row in unit_by_id.items():
        path = f"{ARTIFACT_PATHS['unit_maps']}:{identifier}"
        left = refs_by_id.get(row.get("left_ref"))
        right = refs_by_id.get(row.get("right_ref"))
        if left is None or right is None or _pair_key(row.get("left_ref"), row.get("right_ref")) not in alignment_pairs:
            _issue(issues, "unit_map_orphan", path, "Unit map must be bound to an eligible alignment pair.")
            continue
        expected_evidence_ids = sorted(
            set(left.get("evidence_binding_ids", [])) | set(right.get("evidence_binding_ids", []))
        )
        if sorted(set(row.get("evidence_binding_ids", []))) != expected_evidence_ids:
            _issue(issues, "evidence_binding_incomplete", path, "Unit-map evidence bindings must be the exact union of both concept refs.")
        compatible = row.get("dimension_compatible") is True
        if compatible and not isinstance(row.get("conversion_factor"), (int, float)):
            _issue(issues, "unit_map_invalid", path, "Compatible unit maps require a deterministic conversion factor.")
        if not compatible and row.get("conversion_factor") is not None:
            _issue(issues, "unit_dimension_incompatible", path, "Dimension-incompatible units cannot receive a conversion factor.")
        if row.get("status") not in {"candidate", "incompatible"}:
            _issue(issues, "unit_map_invalid", path, "Unit maps cannot be marked resolved by the compiler.")
        expected_unit_ids = {
            candidate["unit_map_id"]
            for candidate in _unit_map_rows(
                left,
                right,
                left_ref=row.get("left_ref"),
                right_ref=row.get("right_ref"),
                left_source=left.get("source_sha256"),
                right_source=right.get("source_sha256"),
            )
        }
        if identifier not in expected_unit_ids:
            _issue(issues, "unit_map_id_mismatch", path, "Unit-map ID is not deterministic for its ref/support identity.")

    review_items = review_plan.get("items") if isinstance(review_plan.get("items"), list) else []
    expected_review_ids = set(alignment_by_id) | set(difference_by_id) | set(conflict_by_id) | set(unit_by_id)
    actual_review_ids = {
        row.get("item_id")
        for row in review_items
        if isinstance(row, dict) and isinstance(row.get("item_id"), str)
    }
    if actual_review_ids != expected_review_ids:
        _issue(issues, "review_plan_incomplete", ARTIFACT_PATHS["review_plan"], "Review plan must cover exactly all candidate/difference/conflict/unit-map rows.")
    for item in review_items:
        if not isinstance(item, dict):
            _issue(issues, "review_plan_invalid", ARTIFACT_PATHS["review_plan"], "Review plan item is not an object.")
            continue
        target = alignment_by_id.get(item.get("item_id")) or difference_by_id.get(item.get("item_id")) or conflict_by_id.get(item.get("item_id")) or unit_by_id.get(item.get("item_id"))
        if target is None:
            continue
        hash_key = next((key for key in target if key.endswith("_sha256") and key not in {"schema_version"}), None)
        if hash_key and item.get("candidate_sha256") != target.get(hash_key):
            _issue(issues, "review_item_stale", f"{ARTIFACT_PATHS['review_plan']}:{item.get('item_id')}", "Review item candidate hash is stale.")
        if sorted(set(item.get("evidence_binding_ids", []))) != sorted(set(target.get("evidence_binding_ids", []))):
            _issue(issues, "evidence_binding_incomplete", f"{ARTIFACT_PATHS['review_plan']}:{item.get('item_id')}", "Review item evidence bindings do not match its candidate pair.")
        requirement = target.get("review_requirement") if isinstance(target.get("review_requirement"), dict) else {}
        if item.get("required_reviewer_count") != requirement.get("required_reviewer_count"):
            _issue(issues, "review_requirement_invalid", f"{ARTIFACT_PATHS['review_plan']}:{item.get('item_id')}", "Review plan count differs from candidate risk requirement.")


def _validate_review_plan(review_plan: dict[str, Any], issues: list[Issue]) -> None:
    path = ARTIFACT_PATHS["review_plan"]
    if review_plan.get("schema_version") != COMPOSER_REVIEW_PLAN_SCHEMA:
        _issue(issues, "review_plan_invalid", path, "Unexpected review-plan schema.")
    if review_plan.get("protocol") != COMPOSER_REVIEW_PROTOCOL:
        _issue(issues, "review_plan_invalid", path, "Unexpected independent-review protocol.")
    if review_plan.get("attestation_level") != ATTESTATION_LEVEL:
        _issue(issues, "review_plan_invalid", path, "Reviewer identity boundary is not host-orchestrator-recorded-not-cryptographic.")
    if review_plan.get("attestation_authoring_allowed") is not False:
        _issue(issues, "review_attestation_authoring_forbidden", path, "The compiler cannot author reviewer attestations.")
    if review_plan.get("promotion_allowed") is not False:
        _issue(issues, "promotion_forbidden", path, "Review planning cannot enable promotion.")
    if review_plan.get("resolution_policy") != RESOLUTION_POLICY:
        _issue(issues, "consensus_forbidden", path, "Review planning must preserve separate sources without consensus.")
    _validate_row_hash(review_plan, "review_plan_sha256", path, issues)
    if not isinstance(review_plan.get("items"), list):
        _issue(issues, "review_plan_invalid", path, "Review-plan items are missing.")
    if review_plan.get("state") not in {"pending-independent-review", "pending-no-candidates", "blocked"}:
        _issue(issues, "review_plan_invalid", path, "Unknown review-plan state.")
    if not review_plan.get("items") and review_plan.get("state") == "reviewed":
        _issue(issues, "review_state_fabricated", path, "An empty review plan cannot claim reviewed status.")


def _validate_attestations(
    plan: dict[str, Any],
    attestations: list[dict[str, Any]],
    enforce_missing: bool,
    issues: list[Issue],
) -> dict[str, Any]:
    plan_items = {
        row.get("item_id"): row
        for row in plan.get("items", [])
        if isinstance(row, dict) and isinstance(row.get("item_id"), str)
    }
    registry = set(value for value in plan.get("reviewer_registry", []) if isinstance(value, str))
    proposers = set(value for value in plan.get("proposer_instances", []) if isinstance(value, str))
    seen_ids: set[str] = set()
    seen_reviewer_items: set[tuple[str, str]] = set()
    counts: dict[str, int] = {}
    observations: dict[tuple[str, str], set[str]] = {}
    for index, row in enumerate(attestations, start=1):
        path = f"{ARTIFACT_PATHS['review_attestations']}:{index}"
        if row.get("schema_version") != COMPOSER_REVIEW_ATTESTATION_SCHEMA:
            _issue(issues, "review_attestation_invalid", path, "Unexpected Composer attestation schema.")
        identifier = row.get("attestation_id")
        candidate_id = row.get("candidate_id")
        reviewer = row.get("reviewer_instance")
        proposer = row.get("proposer_instance")
        if not isinstance(identifier, str) or identifier in seen_ids:
            _issue(issues, "review_attestation_replay", path, "Attestation ID is missing, duplicated, or replayed.")
        else:
            seen_ids.add(identifier)
        if not isinstance(candidate_id, str) or candidate_id not in plan_items:
            _issue(issues, "review_attestation_invalid", path, "Attestation references an unknown review item.")
            continue
        item = plan_items[candidate_id]
        if not isinstance(reviewer, str) or reviewer not in registry:
            _issue(issues, "reviewer_unregistered", path, "Reviewer instance is not in the frozen reviewer registry.")
        if reviewer == proposer:
            _issue(issues, "reviewer_self_review", path, "Proposer and reviewer instances must be independent.")
        if not isinstance(proposer, str) or proposer not in proposers:
            _issue(issues, "review_attestation_invalid", path, "Attestation proposer instance is not bound to the plan.")
        if row.get("review_plan_sha256") != plan.get("review_plan_sha256"):
            _issue(issues, "review_attestation_stale", path, "Attestation references a stale review plan.")
        if row.get("candidate_sha256") != item.get("candidate_sha256"):
            _issue(issues, "review_attestation_stale", path, "Attestation candidate hash is stale.")
        if row.get("attestation_level") != ATTESTATION_LEVEL:
            _issue(issues, "review_attestation_invalid", path, "Attestation identity boundary is invalid.")
        if not isinstance(row.get("difference_observation"), str) or not row.get("difference_observation", "").strip():
            _issue(issues, "review_attestation_invalid", path, "Attestation must record a non-empty difference observation.")
        if row.get("evidence_checked") is not True:
            _issue(issues, "review_evidence_missing", path, "Independent review must explicitly record checked evidence.")
        reviewer_key = (str(reviewer), candidate_id)
        if reviewer_key in seen_reviewer_items:
            _issue(issues, "review_attestation_replay", path, "The same reviewer instance has replayed an attestation for an item.")
        seen_reviewer_items.add(reviewer_key)
        counts[candidate_id] = counts.get(candidate_id, 0) + 1
        observation = str(row.get("difference_observation", "")).strip()
        if isinstance(reviewer, str) and observation:
            observations.setdefault((reviewer, observation), set()).add(candidate_id)
        if isinstance(row.get("attestation_sha256"), str) and row.get("attestation_sha256") != row_hash(row, "attestation_sha256"):
            _issue(issues, "review_attestation_hash_mismatch", path, "Attestation content hash does not recompute.")
        elif not isinstance(row.get("attestation_sha256"), str):
            _issue(issues, "review_attestation_hash_mismatch", path, "Attestation content hash is missing.")
    if enforce_missing:
        for item_id, item in plan_items.items():
            required = item.get("required_reviewer_count")
            if isinstance(required, int) and counts.get(item_id, 0) < required:
                _issue(issues, "review_missing", item_id, "Required independent reviewer count has not been met.")
    for (reviewer, _observation), item_ids in observations.items():
        if len(item_ids) > 1:
            _issue(issues, "review_blanket_pattern", reviewer, "Identical observation was reused across multiple review items.")
    return {
        "plan_item_count": len(plan_items),
        "attestation_count": len(attestations),
        "reviewed_item_count": sum(1 for item_id in plan_items if counts.get(item_id, 0) >= int(plan_items[item_id].get("required_reviewer_count", 1))),
        "identity_boundary": ATTESTATION_LEVEL,
        "cryptographic_identity_verified": False,
    }


def _validate_artifacts(
    root: Path,
    manifest: dict[str, Any],
    issues: list[Issue],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        _issue(issues, "artifact_inventory_missing", "workpack.json.artifacts", "Artifact inventory is missing.")
        return [], [], [], [], [], [], {}, []
    loaded: dict[str, Any] = {}
    for key, relative in ARTIFACT_PATHS.items():
        record = artifacts.get(key)
        if not isinstance(record, dict) or record.get("path") != relative:
            _issue(issues, "artifact_inventory_invalid", key, "Artifact path is missing or changed.")
            continue
        target = (root / relative).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            _issue(issues, "artifact_path_escape", key, "Artifact path escapes the workpack.")
            continue
        if not target.is_file():
            _issue(issues, "artifact_missing", relative, "Composer artifact is missing.")
            continue
        if record.get("sha256") != sha256_file(target):
            _issue(issues, "artifact_hash_mismatch", relative, "Artifact hash differs from the frozen inventory.")
        if target.suffix == ".jsonl":
            rows = _load_rows(root, relative, issues)
            loaded[key] = rows
            if record.get("record_count") != len(rows):
                _issue(issues, "artifact_record_count_mismatch", relative, "Artifact record count differs from inventory.")
        else:
            try:
                loaded[key] = load_json(target)
            except (OSError, json.JSONDecodeError) as error:
                _issue(issues, "artifact_invalid", relative, str(error))
    return (
        loaded.get("concept_refs", []),
        loaded.get("evidence_bindings", []),
        loaded.get("alignment_candidates", []),
        loaded.get("differences", []),
        loaded.get("conflicts", []),
        loaded.get("unit_maps", []),
        loaded.get("review_plan", {}),
        loaded.get("review_attestations", []),
    )


def validate_workpack(
    root: Path,
    *,
    packages: list[Path] | None = None,
    candidate_source: Path | None = None,
    candidate_spec: Path | None = None,
    candidate_ir_root: Path | None = None,
    pdftoppm: Path | str | None = None,
    candidate_render: Path | None = None,
    candidate_render_page: int | None = None,
    candidate_render_pages: dict[int, Path] | None = None,
    require_source_replay: bool = True,
    allow_pending_review: bool = False,
) -> tuple[list[Issue], dict[str, Any]]:
    """Validate a Composer workpack without authoring review or promotion."""
    root = root.expanduser().resolve()
    issues: list[Issue] = []
    manifest_path = root / "workpack.json"
    if not manifest_path.is_file():
        _issue(issues, "workpack_missing", str(manifest_path), "Composer workpack manifest is missing.")
        return issues, {"valid": False, "state": "blocked", "next_action": "inspect_gate"}
    try:
        manifest = load_json(manifest_path)
    except (OSError, json.JSONDecodeError) as error:
        _issue(issues, "workpack_invalid", str(manifest_path), str(error))
        return issues, {"valid": False, "state": "blocked", "next_action": "inspect_gate"}
    if not isinstance(manifest, dict):
        _issue(issues, "workpack_invalid", str(manifest_path), "Workpack manifest is not an object.")
        return issues, {"valid": False, "state": "blocked", "next_action": "inspect_gate"}
    if manifest.get("schema_version") != COMPOSER_WORKPACK_SCHEMA:
        _issue(issues, "protocol_identity_invalid", "workpack.json.schema_version", "Composer workpack schema identity is invalid.")
    if manifest.get("protocol") != SEMANTIC_COMPOSER_PROTOCOL:
        _issue(issues, "protocol_identity_invalid", "workpack.json.protocol", "Composer protocol identity is invalid.")
    if manifest.get("compiler_version") != SEMANTIC_COMPOSER_COMPILER_VERSION:
        _issue(issues, "protocol_identity_invalid", "workpack.json.compiler_version", "Composer compiler identity is invalid.")
    if manifest.get("promotion", {}).get("promotion_eligible") is not False:
        _issue(issues, "promotion_forbidden", "workpack.json.promotion", "Composer workpacks are never promotion-eligible at build time.")
    policy = manifest.get("policy") if isinstance(manifest.get("policy"), dict) else {}
    for field in ("automatic_consensus_allowed", "automatic_merge_allowed", "capability_escalation_allowed", "review_attestation_authoring_allowed", "network_allowed", "source_text_in_compiler_release_allowed"):
        if policy.get(field) is not False:
            _issue(issues, "policy_violation", f"workpack.json.policy.{field}", "Composer safety policy must remain false.")
    if policy.get("cross_source_claims") != RESOLUTION_POLICY:
        _issue(issues, "consensus_forbidden", "workpack.json.policy.cross_source_claims", "Cross-source outputs must preserve separate sources.")
    manifest_inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), dict) else {}
    candidate_manifest = manifest_inputs.get("candidate_source") if isinstance(manifest_inputs.get("candidate_source"), dict) else {}
    normalized_ir = candidate_manifest.get("normalized_pdf_ir")
    if normalized_ir is not None:
        if not isinstance(normalized_ir, dict):
            _issue(issues, "normalized_pdf_ir_binding_invalid", "workpack.json.inputs.candidate_source.normalized_pdf_ir", "Normalized PDF IR binding must be an object.")
        else:
            for key in ("ir_sha256", "heading_task_sha256"):
                if not isinstance(normalized_ir.get(key), str) or not SHA256_RE.fullmatch(normalized_ir[key]):
                    _issue(issues, "normalized_pdf_ir_binding_invalid", f"workpack.json.inputs.candidate_source.normalized_pdf_ir.{key}", "Normalized PDF IR identity hash is missing or invalid.")
            if normalized_ir.get("status") != "heading_resolution_required" or normalized_ir.get("heading_locator_status") != "heading_resolution_required":
                _issue(issues, "normalized_pdf_ir_status_invalid", "workpack.json.inputs.candidate_source.normalized_pdf_ir.status", "Unresolved structure must remain explicitly heading_resolution_required.")
            if normalized_ir.get("semantic_workpack_built") is not False or normalized_ir.get("independent_structure_review_required") is not True:
                _issue(issues, "normalized_pdf_workpack_claim_invalid", "workpack.json.inputs.candidate_source.normalized_pdf_ir", "The unresolved IR cannot claim a completed semantic workpack or independent structure review.")
            if normalized_ir.get("source_sha256") != candidate_manifest.get("sha256"):
                _issue(issues, "normalized_pdf_ir_source_mismatch", "workpack.json.inputs.candidate_source.normalized_pdf_ir.source_sha256", "Normalized PDF IR must bind to the candidate source SHA.")
            if manifest_inputs.get("normalized_pdf_ir") != normalized_ir:
                _issue(issues, "normalized_pdf_ir_binding_incomplete", "workpack.json.inputs.normalized_pdf_ir", "Workpack input must retain the same unresolved IR binding as the candidate descriptor.")
    if require_source_replay:
        _validate_source_inputs(
            root,
            manifest,
            packages=packages,
            candidate_source=candidate_source,
            candidate_spec=candidate_spec,
            candidate_ir_root=candidate_ir_root,
            pdftoppm=pdftoppm,
            candidate_render=candidate_render,
            candidate_render_page=candidate_render_page,
            candidate_render_pages=candidate_render_pages,
            issues=issues,
        )
    refs, bindings, alignments, differences, conflicts, unit_maps, review_plan, attestations = _validate_artifacts(root, manifest, issues)
    _validate_review_plan(review_plan, issues)
    _validate_composer_rows(
        refs,
        bindings,
        alignments,
        differences,
        conflicts,
        unit_maps,
        review_plan,
        candidate_source,
        require_source_replay,
        issues,
    )
    _validate_atomic_support_against_packages(packages, refs, bindings, issues)
    attestation_summary = _validate_attestations(review_plan, attestations, not allow_pending_review, issues)
    expected_hash = _expected_output_hash(refs, bindings, alignments, differences, conflicts, unit_maps, review_plan)
    if manifest.get("output_hash") != expected_hash:
        _issue(issues, "output_hash_mismatch", "workpack.json.output_hash", "Workpack output hash does not recompute.")
    receipts = _load_rows(root, ARTIFACT_PATHS["receipts"], issues)
    if len(receipts) != 1:
        _issue(issues, "receipt_invalid", ARTIFACT_PATHS["receipts"], "Composer workpack requires exactly one build receipt.")
    for row in receipts:
        if row.get("schema_version") != COMPOSER_RECEIPT_SCHEMA or row.get("executed") is not False:
            _issue(issues, "receipt_invalid", ARTIFACT_PATHS["receipts"], "Receipt schema or execution boundary is invalid.")
        expected_receipt_id = stable_id("sreceipt", SEMANTIC_COMPOSER_PROTOCOL, row.get("input_hashes"), row.get("output_hash"))
        if row.get("receipt_id") != expected_receipt_id:
            _issue(issues, "receipt_invalid", ARTIFACT_PATHS["receipts"], "Receipt ID is not deterministic.")
        if row.get("output_hash") != expected_hash:
            _issue(issues, "receipt_stale", ARTIFACT_PATHS["receipts"], "Receipt output hash is stale.")
        receipt_inputs = row.get("input_hashes") if isinstance(row.get("input_hashes"), dict) else {}
        if normalized_ir is not None:
            if receipt_inputs.get("candidate_ir") != normalized_ir:
                _issue(issues, "receipt_invalid", ARTIFACT_PATHS["receipts"], "Receipt does not retain the frozen PDF IR component map.")
            if receipt_inputs.get("candidate_ir_verifier") != "verify_pdf_ir:docling_allow_network=false":
                _issue(issues, "receipt_invalid", ARTIFACT_PATHS["receipts"], "Receipt does not record the offline PDF IR verifier boundary.")
    promotion = manifest.get("promotion") if isinstance(manifest.get("promotion"), dict) else {}
    descriptors = manifest.get("inputs", {}).get("packages", []) if isinstance(manifest.get("inputs"), dict) else []
    expected_atomic_required = any(
        isinstance(row, dict) and row.get("input_level") != "atomic-supported"
        for row in list(descriptors) + [manifest.get("inputs", {}).get("candidate_source", {})]
    )
    if promotion.get("atomic_support_required") is not expected_atomic_required:
        _issue(issues, "atomic_support_state_invalid", "workpack.json.promotion.atomic_support_required", "Atomic-support requirement does not reflect actual input levels.")
    manifest_state = manifest.get("state", {}) if isinstance(manifest.get("state"), dict) else {}
    if manifest_state.get("pause_only") is not True or manifest_state.get("state") not in {"pending-independent-review", "pending-no-candidates"}:
        _issue(issues, "pause_boundary_invalid", "workpack.json.state", "Composer state must remain pause-only and explicitly pending review or no-candidate closure.")
    expected_next_action = (
        "independent-cross-source-review-required"
        if review_plan.get("items")
        else "no-eligible-candidates-review-required"
    )
    if manifest_state.get("next_action") != expected_next_action:
        _issue(issues, "pause_boundary_invalid", "workpack.json.state.next_action", "Composer next action is inconsistent with the review-plan candidate count.")
    unique = {(issue.code, issue.path, issue.message): issue for issue in issues}
    result = sorted(unique.values(), key=lambda item: (item.code, item.path, item.message))
    source_replay_codes = {
        "source_binding_unverifiable", "source_identity_drift", "candidate_spec_stale",
        "render_hash_mismatch", "render_page_binding_missing", "render_page_binding_mismatch",
        "parser_version_mismatch", "page_geometry_mismatch", "bbox_geometry_mismatch",
        "candidate_ir_binding_missing", "candidate_ir_unverifiable", "candidate_ir_identity_algorithm_invalid",
        "candidate_ir_component_mismatch", "candidate_ir_hash_mismatch", "candidate_ir_source_mismatch",
        "candidate_ir_heading_task_mismatch", "candidate_ir_heading_task_hash_mismatch", "candidate_ir_status_invalid",
        "candidate_ir_scope_invalid", "candidate_ir_scope_mismatch", "candidate_scope_invalid", "candidate_scope_outside_ir",
        "pdf_ir_verify_failed",
    }
    review_gate_codes = {
        "review_missing", "review_attestation_invalid", "review_attestation_replay",
        "review_attestation_hash_mismatch", "review_attestation_stale", "reviewer_unregistered",
        "reviewer_self_review", "review_blanket_pattern", "review_evidence_missing",
    }
    source_replay_issues = [issue for issue in result if issue.code in source_replay_codes]
    review_gate_issues = [issue for issue in result if issue.code in review_gate_codes]
    contract_issues = [issue for issue in result if issue.code not in source_replay_codes and issue.code not in review_gate_codes]
    counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {}
    pending_review_items = max(
        0,
        int(attestation_summary.get("plan_item_count", 0)) - int(attestation_summary.get("reviewed_item_count", 0)),
    )
    structure_heading_review_pending = bool(normalized_ir) and normalized_ir.get("status") == "heading_resolution_required"
    review_gate_pending = bool(review_gate_issues) or pending_review_items > 0 or structure_heading_review_pending
    only_review_gate_pending = review_gate_pending and not contract_issues and not source_replay_issues
    atomic_upgrade_required = bool(promotion.get("atomic_support_required"))
    required_actions: list[str] = []
    if structure_heading_review_pending:
        required_actions.append("independent-structure-review-required")
    if bool(review_gate_issues) or pending_review_items > 0:
        required_actions.append("independent-cross-source-review-required")
    if atomic_upgrade_required:
        required_actions.append("atomic-support-upgrade-required")
    if not required_actions and review_plan.get("items"):
        required_actions.append("independent-cross-source-review-required")
    pdf_ir_issue_codes = {
        "candidate_ir_binding_missing", "candidate_ir_unverifiable", "candidate_ir_identity_algorithm_invalid",
        "candidate_ir_component_mismatch", "candidate_ir_hash_mismatch", "candidate_ir_source_mismatch",
        "candidate_ir_heading_task_mismatch", "candidate_ir_heading_task_hash_mismatch", "candidate_ir_status_invalid",
        "candidate_ir_scope_invalid", "candidate_ir_scope_mismatch", "candidate_scope_invalid", "candidate_scope_outside_ir",
        "pdf_ir_verify_failed",
    }
    pdf_ir_failed = any(issue.code in pdf_ir_issue_codes for issue in result) or any(
        issue.code == "source_binding_unverifiable" and "normalized_pdf_ir" in issue.path
        for issue in result
    )
    if only_review_gate_pending:
        reported_state = "pending-independent-review"
        reported_next_action = "independent-cross-source-review-required"
    elif result:
        reported_state = "blocked"
        reported_next_action = "inspect_gate"
    else:
        reported_state = manifest_state.get("state", "blocked")
        reported_next_action = manifest_state.get("next_action", "inspect_gate")
    summary = {
        "valid": not result and not review_gate_pending,
        "ready": not result and not review_gate_pending,
        "state": reported_state,
        "next_action": required_actions[0] if only_review_gate_pending and required_actions else reported_next_action,
        "required_actions": required_actions,
        "workpack_id": manifest.get("workpack_id"),
        "protocol": manifest.get("protocol"),
        "compiler_version": manifest.get("compiler_version"),
        "counts": counts,
        "attestations": attestation_summary,
        "promotion_eligible": False,
        "atomic_support_required": promotion.get("atomic_support_required"),
        "legacy_source_support_present": promotion.get("legacy_source_support_present"),
        "identity_boundary": ATTESTATION_LEVEL,
        "cryptographic_identity_verified": False,
        "validation_mode": "source-replay" if require_source_replay else "integrity-only",
        "source_replay_required": bool(
            manifest.get("inputs", {}).get("candidate_spec_sha256")
            or manifest.get("inputs", {}).get("candidate_render_pages")
            or normalized_ir
        ),
        "gates": {
            "structural_contract": not contract_issues,
            "source_replay": not source_replay_issues if require_source_replay else "not-run",
            "independent_review": not review_gate_pending,
            "pdf_ir_integrity": (
                "not-applicable"
                if not normalized_ir
                else ("not-run" if not require_source_replay else not pdf_ir_failed)
            ),
            "structure_heading_review": not structure_heading_review_pending,
            "promotion": False,
        },
        "issue_buckets": {
            "structural_contract": len(contract_issues),
            "source_replay": len(source_replay_issues),
            "review_promotion_gate": len(review_gate_issues),
            "review_integrity_issue_count": len(review_gate_issues),
            "pending_semantic_review_items": pending_review_items,
            "structure_heading_review_pending": 1 if structure_heading_review_pending else 0,
        },
        "issue_count": len(result),
    }
    return result, summary


def composer_plan(root: Path) -> dict[str, Any]:
    manifest = load_json(root.expanduser().resolve() / "workpack.json")
    plan = load_json(root.expanduser().resolve() / ARTIFACT_PATHS["review_plan"])
    return {
        "workpack_id": manifest.get("workpack_id"),
        "state": manifest.get("state", {}).get("state", "blocked"),
        "next_action": manifest.get("state", {}).get("next_action", "inspect_gate"),
        "pause_only": True,
        "review_plan_id": plan.get("review_plan_id"),
        "review_item_count": len(plan.get("items", [])) if isinstance(plan.get("items"), list) else 0,
        "promotion_eligible": False,
        "resolution_policy": RESOLUTION_POLICY,
    }


def composer_status(
    root: Path,
    *,
    packages: list[Path] | None = None,
    candidate_source: Path | None = None,
    candidate_spec: Path | None = None,
    candidate_ir_root: Path | None = None,
    pdftoppm: Path | str | None = None,
    candidate_render: Path | None = None,
    candidate_render_page: int | None = None,
    candidate_render_pages: dict[int, Path] | None = None,
) -> dict[str, Any]:
    replay = bool(packages) or any(
        value is not None
        for value in (candidate_source, candidate_spec, candidate_ir_root, pdftoppm, candidate_render, candidate_render_page)
    ) or bool(candidate_render_pages)
    issues, summary = validate_workpack(
        root,
        packages=packages,
        candidate_source=candidate_source,
        candidate_spec=candidate_spec,
        candidate_ir_root=candidate_ir_root,
        pdftoppm=pdftoppm,
        candidate_render=candidate_render,
        candidate_render_page=candidate_render_page,
        candidate_render_pages=candidate_render_pages,
        require_source_replay=replay,
        allow_pending_review=True,
    )
    summary["issue_count"] = len(issues)
    summary["issue_codes"] = sorted({issue.code for issue in issues})
    return summary


def composer_resume(
    root: Path,
    *,
    packages: list[Path] | None = None,
    candidate_source: Path | None = None,
    candidate_spec: Path | None = None,
    candidate_ir_root: Path | None = None,
    pdftoppm: Path | str | None = None,
    candidate_render: Path | None = None,
    candidate_render_page: int | None = None,
    candidate_render_pages: dict[int, Path] | None = None,
) -> dict[str, Any]:
    """Prepare, but do not execute, the next Composer action."""
    status = composer_status(
        root,
        packages=packages,
        candidate_source=candidate_source,
        candidate_spec=candidate_spec,
        candidate_ir_root=candidate_ir_root,
        pdftoppm=pdftoppm,
        candidate_render=candidate_render,
        candidate_render_page=candidate_render_page,
        candidate_render_pages=candidate_render_pages,
    )
    gates = status.get("gates") if isinstance(status.get("gates"), dict) else {}
    replay_or_integrity_ok = gates.get("structural_contract") is True and gates.get("source_replay") in {True, "not-run"}
    if status.get("valid") or (replay_or_integrity_ok and status.get("state") in {"pending-independent-review", "pending-no-candidates"}):
        required_actions = status.get("required_actions") if isinstance(status.get("required_actions"), list) else []
        next_action = (
            required_actions[0]
            if required_actions
            else "no-eligible-candidates-review-required"
            if status.get("state") == "pending-no-candidates"
            else "independent-review-contradiction-resolution-or-promotion-gate"
        )
        return {
            **status,
            "state": "paused",
            "next_action": next_action,
            "executed": False,
            "resume_intent_only": True,
        }
    return {
        **status,
        "state": "blocked",
        "next_action": "inspect_gate",
        "executed": False,
        "resume_intent_only": True,
    }


def _print_payload(payload: Any, *, json_output: bool = False) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if isinstance(payload, dict):
        print(f"state={payload.get('state')} next_action={payload.get('next_action')} valid={payload.get('valid', True)}")
    else:
        print(payload)


def _parse_render_page_map(values: list[str] | None) -> dict[int, Path]:
    result: dict[int, Path] = {}
    for raw in values or []:
        if "=" not in raw:
            raise SemanticComposerError("render_page_mapping_invalid")
        page_text, path_text = raw.split("=", 1)
        try:
            page = int(page_text)
        except ValueError as error:
            raise SemanticComposerError("render_page_mapping_invalid") from error
        if page < 1 or not path_text:
            raise SemanticComposerError("render_page_mapping_invalid")
        if page in result:
            raise SemanticComposerError("render_page_mapping_duplicate")
        result[page] = Path(path_text)
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--package", action="append", required=True, type=Path)
    build.add_argument("--candidate-source", required=True, type=Path)
    build.add_argument("--candidate-spec", required=True, type=Path)
    build.add_argument("--candidate-ir-root", "--candidate-ir", dest="candidate_ir_root", type=Path)
    build.add_argument("--pdftoppm", type=Path)
    build.add_argument("--candidate-render", type=Path)
    build.add_argument("--candidate-render-physical-page", type=int)
    build.add_argument("--candidate-render-page", action="append", default=[], metavar="PAGE=PATH")
    build.add_argument("--output", required=True, type=Path)
    build.add_argument("--created-at", required=True)
    build.add_argument("--reviewer-instance", action="append", default=[])
    validate = sub.add_parser("validate")
    validate.add_argument("--workpack", required=True, type=Path)
    validate.add_argument("--package", action="append", type=Path)
    validate.add_argument("--candidate-source", type=Path)
    validate.add_argument("--candidate-spec", type=Path)
    validate.add_argument("--candidate-ir-root", "--candidate-ir", dest="candidate_ir_root", type=Path)
    validate.add_argument("--pdftoppm", type=Path)
    validate.add_argument("--candidate-render", type=Path)
    validate.add_argument("--candidate-render-physical-page", type=int)
    validate.add_argument("--candidate-render-page", action="append", default=[], metavar="PAGE=PATH")
    validate.add_argument("--json", action="store_true", dest="json_output")
    for name in ("plan", "status", "resume"):
        command = sub.add_parser(name)
        command.add_argument("--workpack", required=True, type=Path)
        command.add_argument("--package", action="append", type=Path)
        command.add_argument("--candidate-source", type=Path)
        command.add_argument("--candidate-spec", type=Path)
        command.add_argument("--candidate-ir-root", "--candidate-ir", dest="candidate_ir_root", type=Path)
        command.add_argument("--pdftoppm", type=Path)
        command.add_argument("--candidate-render", type=Path)
        command.add_argument("--candidate-render-physical-page", type=int)
        command.add_argument("--candidate-render-page", action="append", default=[], metavar="PAGE=PATH")
        command.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        if args.command == "build":
            render_pages = _parse_render_page_map(args.candidate_render_page)
            result = build_workpack(
                packages=args.package,
                candidate_source=args.candidate_source,
                candidate_spec=args.candidate_spec,
                candidate_ir_root=args.candidate_ir_root,
                pdftoppm=args.pdftoppm,
                candidate_render=args.candidate_render,
                output=args.output,
                created_at=args.created_at,
                reviewer_instances=args.reviewer_instance,
                candidate_render_page=args.candidate_render_physical_page,
                candidate_render_pages=render_pages,
            )
            _print_payload(result, json_output=True)
            return 0
        if args.command == "validate":
            render_pages = _parse_render_page_map(args.candidate_render_page)
            issues, summary = validate_workpack(
                args.workpack,
                packages=args.package,
                candidate_source=args.candidate_source,
                candidate_spec=args.candidate_spec,
                candidate_ir_root=args.candidate_ir_root,
                pdftoppm=args.pdftoppm,
                candidate_render=args.candidate_render,
                candidate_render_page=args.candidate_render_physical_page,
                candidate_render_pages=render_pages,
            )
            payload = {
                "valid": bool(summary.get("valid", not issues)) and not issues,
                "summary": summary,
                "issues": [issue.__dict__ for issue in issues],
            }
            _print_payload(payload, json_output=args.json_output or True)
            return 0 if not issues else 1
        if args.command == "plan":
            payload = composer_plan(args.workpack)
        elif args.command == "status":
            render_pages = _parse_render_page_map(args.candidate_render_page)
            payload = composer_status(
                args.workpack,
                packages=args.package,
                candidate_source=args.candidate_source,
                candidate_spec=args.candidate_spec,
                candidate_ir_root=args.candidate_ir_root,
                pdftoppm=args.pdftoppm,
                candidate_render=args.candidate_render,
                candidate_render_page=args.candidate_render_physical_page,
                candidate_render_pages=render_pages,
            )
        else:
            render_pages = _parse_render_page_map(args.candidate_render_page)
            payload = composer_resume(
                args.workpack,
                packages=args.package,
                candidate_source=args.candidate_source,
                candidate_spec=args.candidate_spec,
                candidate_ir_root=args.candidate_ir_root,
                pdftoppm=args.pdftoppm,
                candidate_render=args.candidate_render,
                candidate_render_page=args.candidate_render_physical_page,
                candidate_render_pages=render_pages,
            )
        _print_payload(payload, json_output=args.json_output or True)
        return 0
    except (SemanticComposerError, OSError, json.JSONDecodeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
