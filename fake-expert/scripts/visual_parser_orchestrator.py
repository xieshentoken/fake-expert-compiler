#!/usr/bin/env python3
"""Unified, local-only orchestration for visual parser candidates.

The orchestrator owns a frozen job, invokes the existing native/PDF-render and
scan-worker boundaries, and emits candidate-only visual workpacks.  It does not
implement a second OCR protocol, does not select a winning parser, and cannot
author review attestations or promotion records.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from compiler_version import (
    LEGACY_TECHNICAL_CHART_PROFILE_ID,
    LEGACY_TECHNICAL_CHART_PROFILE_PROTOCOL,
    TECHNICAL_CHART_PROFILE_ID,
    TECHNICAL_CHART_PROFILE_PROTOCOL,
    VISUAL_PARSER_ORCHESTRATOR_COMPILER_VERSION,
    VISUAL_PARSER_ORCHESTRATOR_PROTOCOL,
    VISUAL_PARSER_JOB_SCHEMA,
    VISUAL_PARSER_MANIFEST_SCHEMA,
    VISUAL_EVALUATION_CONTRACT_SCHEMA,
)
from paddle_runtime_contract import (
    PADDLE_OCR_BACKEND,
    PADDLE_STRUCTURE_BACKEND,
    PADDLE_STRUCTURE_V3_BACKEND,
    PADDLE_CHART_BACKEND,
    PaddleRuntimeContractError,
    freeze_external_runtime_contract,
    validate_runtime_contract,
)
from scanned_pdf_ocr import (
    BACKEND_SPECS,
    ScanOcrError,
    _effective_config,
    _runtime_engine_configuration,
    build_scanned_pdf_ir,
    _run_chart_region_backend,
    compile_pdf_intermediate_representation,
    scan_input_fingerprint,
    sha256_file,
    _page_geometry_receipts,
    validate_backend_selection,
    verify_scanned_pdf_ir,
    write_scanned_pdf_ir,
)
from paddle_runtime_qualification import (
    PaddleRuntimeQualificationError,
    qualification_configuration,
    validate_runtime_qualification,
)
from visual_adapters import VisualAdapterError, adapter_receipt, map_candidate_locators
from visual_evaluation import build_evaluation_contract
from visual_semantics import (
    COORDINATE_SPACE,
    VisualSemanticsError,
    build_visual_conflict,
    build_visual_object,
    extract_visual_candidates,
    normalize_bbox,
    render_receipt,
    rekey_table_grid,
    sha256_json,
    sha256_bytes,
    stable_id,
    validate_visual_bundle,
    validate_visual_review_overlay_manifest,
    visual_review_overlay_manifest,
    write_visual_bundle,
)
from selective_visual_pipeline import (
    SELECTIVE_VISUAL_PROTOCOL,
    VISUAL_CENSUS_SCHEMA as SELECTIVE_VISUAL_CENSUS_SCHEMA,
    VISUAL_COVERAGE_SCHEMA as SELECTIVE_VISUAL_COVERAGE_SCHEMA,
    VISUAL_INDEX_SCHEMA as SELECTIVE_VISUAL_INDEX_SCHEMA,
    VISUAL_ROUTING_SCHEMA as SELECTIVE_VISUAL_ROUTING_SCHEMA,
    VisualPolicy,
    VisualPolicyError,
    build_visual_budget_receipt,
    build_chart_candidate,
    build_visual_coverage,
    build_visual_index,
    normalize_visual_policy,
    policy_only_census,
    route_visual_census,
    run_visual_census,
    sha256_json as selective_sha256_json,
    validate_visual_index,
    validate_visual_census,
    validate_visual_routing,
    validate_visual_budget_receipt,
    validate_visual_coverage,
    visual_policy_from_mapping,
)


class VisualParserOrchestratorError(RuntimeError):
    """Stable fail-closed orchestration error."""


# The selective wrapper supplies a bounded execution page set to the legacy
# artifact writer without changing the frozen job hash or source scope.  It is
# process-local and cleared immediately after a build; it is never serialized
# into a job or semantic identity.
_SELECTIVE_EXECUTION_PAGES: dict[str, list[int]] = {}
_SELECTIVE_EXECUTION_REGIONS: dict[str, list[dict[str, Any]]] = {}


def _synthetic_path_forbidden(path: Path) -> bool:
    parts = {part.casefold() for part in path.expanduser().resolve().parts}
    joined = path.expanduser().resolve().as_posix().casefold()
    forbidden_names = {"private-releases", ".backups", "benchmarks", "benchmark", "ebooks for test", "release", "releases"}
    return bool(parts & forbidden_names) or "/private-releases/" in joined or "/.backups/" in joined


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _read_json_value(path: Path) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VisualParserOrchestratorError(f"job_json_invalid:{path}") from error
    return value


def _read_json(path: Path) -> dict[str, Any]:
    value = _read_json_value(path)
    if not isinstance(value, dict):
        raise VisualParserOrchestratorError("job_json_object_required")
    return value


def _jsonl_rows(path: Path) -> list[dict[str, Any]]:
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as error:
        raise VisualParserOrchestratorError(f"jsonl_invalid:{path.name}") from error
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _hash_path(path: Path) -> str:
    return sha256_file(path)


def _tree_identity(root: Path | None, *, required: bool) -> dict[str, Any]:
    if root is None:
        if required:
            return {"available": False, "code": "local_model_artifacts_required", "identity_sha256": None}
        return {"available": True, "code": "not-required", "identity_sha256": sha256_json({"model": "none"})}
    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        return {"available": False, "code": "local_model_artifacts_missing", "path": str(resolved), "identity_sha256": None}
    files: list[dict[str, Any]] = []
    for path in sorted(resolved.rglob("*")):
        if path.is_symlink():
            return {"available": False, "code": "model_symlink_forbidden", "path": str(path), "identity_sha256": None}
        if path.is_file():
            files.append({"relative_path": path.relative_to(resolved).as_posix(), "bytes": path.stat().st_size, "sha256": _hash_path(path)})
    if required and not files:
        return {"available": False, "code": "local_model_artifacts_empty", "path": str(resolved), "identity_sha256": None}
    identity = {"path": str(resolved), "files": files, "required": required}
    return {"available": True, "path": str(resolved), "file_count": len(files), "identity_sha256": sha256_json(identity), "files_sha256": sha256_json(files)}


def _path_overlap(first: Path, second: Path) -> bool:
    a = first.expanduser().resolve()
    b = second.expanduser().resolve()
    try:
        a.relative_to(b)
        return True
    except ValueError:
        pass
    try:
        b.relative_to(a)
        return True
    except ValueError:
        return False


def _pages_for_job(source: Path, pages: Sequence[int] | None) -> tuple[list[int], int]:
    try:
        from pypdf import PdfReader, __version__ as pypdf_version
    except ModuleNotFoundError as error:
        raise VisualParserOrchestratorError("pypdf_unavailable") from error
    if str(pypdf_version) != "6.10.0":
        raise VisualParserOrchestratorError(f"pypdf_version_mismatch:{pypdf_version}")
    reader = PdfReader(str(source))
    if reader.is_encrypted:
        raise VisualParserOrchestratorError("unsupported_encrypted_pdf")
    count = len(reader.pages)
    selected = sorted(set(int(page) for page in pages)) if pages else list(range(1, count + 1))
    if not selected or any(page < 1 or page > count for page in selected):
        raise VisualParserOrchestratorError("physical_page_out_of_range")
    return selected, count


def _is_contiguous_pages(pages: Sequence[int]) -> bool:
    normalized = sorted(set(int(page) for page in pages))
    return bool(normalized) and normalized == list(range(normalized[0], normalized[-1] + 1))


def _validate_raster_region_contract(
    regions: Sequence[Mapping[str, Any]] | None,
    *,
    source_sha256: str,
    selected_pages: Sequence[int],
    page_sizes: Mapping[int, Sequence[float]],
    page_geometries: Mapping[int, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Validate the explicit raster-region handoff before refusing it.

    The crop is declared in PDF top-left points.  The render/crop worker owns
    the pixel hash and reversible rotation mapping, while this preflight locks
    the page geometry and CropBox boundary before execution.
    """

    normalized = [dict(region) for region in (regions or [])]
    for index, region in enumerate(normalized):
        row = _strict_object(
            region,
            required=("physical_page", "bbox_pdf_points", "coordinate_space", "source_anchor", "render_sha256", "crop_sha256"),
            optional=("region_id",),
            path=f"job.scope.raster_regions[{index}]",
        )
        page = row.get("physical_page")
        if not isinstance(page, int) or page not in set(int(value) for value in selected_pages):
            raise VisualParserOrchestratorError(f"raster_region_page_not_selected:{index}")
        bbox = row.get("bbox_pdf_points")
        if not isinstance(bbox, list) or len(bbox) != 4 or any(not isinstance(value, (int, float)) for value in bbox):
            raise VisualParserOrchestratorError(f"raster_region_bbox_invalid:{index}")
        geometry = (page_geometries or {}).get(int(page), {})
        width, height = geometry.get("width"), geometry.get("height")
        if width is None or height is None:
            width, height = page_sizes[int(page)]
        crop_box = geometry.get("crop_box_top_left")
        boundary = crop_box if isinstance(crop_box, (list, tuple)) and len(crop_box) == 4 else [0.0, 0.0, float(width), float(height)]
        if not (boundary[0] <= float(bbox[0]) < float(bbox[2]) <= boundary[2] and boundary[1] <= float(bbox[1]) < float(bbox[3]) <= boundary[3]):
            raise VisualParserOrchestratorError(f"raster_region_bbox_out_of_page:{index}")
        rotation = geometry.get("rotation", 0)
        if rotation not in {0, 90, 180, 270}:
            raise VisualParserOrchestratorError(f"raster_region_rotation_invalid:{index}")
        if row.get("coordinate_space") != COORDINATE_SPACE:
            raise VisualParserOrchestratorError(f"raster_region_coordinate_space_invalid:{index}")
        for field in ("render_sha256", "crop_sha256"):
            value = row.get(field)
            if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise VisualParserOrchestratorError(f"raster_region_{field}_invalid:{index}")
        anchor = _strict_object(row["source_anchor"], required=("anchor_id", "anchor_kind", "source_sha256"), optional=(), path=f"job.scope.raster_regions[{index}].source_anchor")
        if anchor.get("source_sha256") != source_sha256:
            raise VisualParserOrchestratorError(f"raster_region_source_anchor_mismatch:{index}")
        if not isinstance(anchor.get("anchor_id"), str) or not anchor.get("anchor_id") or not isinstance(anchor.get("anchor_kind"), str) or not anchor.get("anchor_kind"):
            raise VisualParserOrchestratorError(f"raster_region_source_anchor_invalid:{index}")
    return normalized


def _page_sizes(source: Path, pages: Iterable[int]) -> dict[int, list[float]]:
    from pypdf import PdfReader

    reader = PdfReader(str(source))
    result: dict[int, list[float]] = {}
    for page in pages:
        page_obj = reader.pages[int(page) - 1]
        box = page_obj.mediabox
        result[int(page)] = [float(box.width), float(box.height)]
    return result


def _page_geometries(source: Path, pages: Iterable[int]) -> dict[int, dict[str, Any]]:
    from pypdf import PdfReader

    reader = PdfReader(str(source))
    result: dict[int, dict[str, Any]] = {}
    for page_number in sorted(set(int(page) for page in pages)):
        page = reader.pages[page_number - 1]
        media = page.mediabox
        crop = getattr(page, "cropbox", media)
        media_left, media_bottom, media_right, media_top = (float(media.left), float(media.bottom), float(media.right), float(media.top))
        result[page_number] = {
            "physical_page": page_number,
            "media_box": [media_left, media_bottom, media_right, media_top],
            "crop_box": [float(crop.left), float(crop.bottom), float(crop.right), float(crop.top)],
            "crop_box_top_left": [float(crop.left) - media_left, media_top - float(crop.top), float(crop.right) - media_left, media_top - float(crop.bottom)],
            "width": media_right - media_left,
            "height": media_top - media_bottom,
            "rotation": int(page.get("/Rotate", 0) or 0) % 360,
        }
    return result


def _backend_adapter(backend: str) -> str:
    return {PADDLE_OCR_BACKEND: PADDLE_OCR_BACKEND, PADDLE_STRUCTURE_BACKEND: "paddleocr-ppstructure", PADDLE_STRUCTURE_V3_BACKEND: "paddleocr-ppstructure", "docling": "docling", "pymupdf-tesseract": "pymupdf", "fake": "fake"}.get(backend, backend)


def _backend_roots(job: Mapping[str, Any]) -> dict[str, Path | None]:
    roots: dict[str, Path | None] = {}
    for backend, value in (job.get("model_roots") or {}).items():
        roots[str(backend)] = Path(value).expanduser().resolve() if value else None
    return roots


def _canonical_worker_request_sha256(
    *,
    source_sha256: str,
    physical_page: int,
    backend_id: str,
    render_sha256: str,
    crop_sha256: str | None,
    region_id: str | None,
    coordinate_transform_id: str | None,
    receipt: Mapping[str, Any],
) -> str:
    """Hash the crop request commitment without ephemeral filesystem paths."""

    model = receipt.get("model") if isinstance(receipt.get("model"), Mapping) else {}
    return sha256_json({
        "input_kind": "raster-crop",
        "source_sha256": source_sha256,
        "physical_page": int(physical_page),
        "backend_id": backend_id,
        "render_sha256": render_sha256,
        "crop_sha256": crop_sha256,
        "region_id": region_id,
        "coordinate_transform_id": coordinate_transform_id,
        "runtime_contract_sha256": receipt.get("runtime_contract_sha256"),
        "configuration_sha256": receipt.get("configuration_sha256"),
        "model_identity_sha256": model.get("identity_sha256"),
        "model_files_sha256": model.get("files_sha256") or model.get("manifest_sha256"),
    })


def _backend_list(job: Mapping[str, Any]) -> list[str]:
    selection = job.get("backend_selection", {})
    return [str(value) for value in (selection.get("primary"), selection.get("challenger"), selection.get("baseline")) if value]


def _job_hash(job: Mapping[str, Any]) -> str:
    return sha256_json({key: value for key, value in job.items() if key != "job_sha256"})


def _strict_object(value: Any, *, required: Sequence[str], optional: Sequence[str], path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise VisualParserOrchestratorError(f"schema_object_required:{path}")
    allowed = set(required) | set(optional)
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise VisualParserOrchestratorError(f"schema_unknown_property:{path}:{unknown[0]}")
    missing = sorted(set(required) - set(value))
    if missing:
        raise VisualParserOrchestratorError(f"schema_required_property_missing:{path}:{missing[0]}")
    return dict(value)


def validate_visual_parser_job_schema(job: Mapping[str, Any]) -> None:
    """Validate the current job instance against the bundled strict schema.

    The optional ``jsonschema`` package is not part of the offline compiler
    runtime.  This exact structural validator mirrors the shipped JSON Schema,
    including closed nested objects and explicitly open configuration/fixture
    maps, so runtime validation remains available without dependency download.
    """

    root = _strict_object(job, required=("schema_version", "protocol", "compiler_version", "source", "scope", "output_root", "render", "backend_selection", "backend_receipts", "model_roots", "backend_config", "backend_config_sha256", "external_runtime", "policy", "test_mode", "test_only_authorized", "synthetic_only", "state", "paused_gates", "created_at", "job_sha256"), optional=("fake_fixture", "visual_policy", "visual_mode", "visual_kinds", "visual_budget", "visual_context", "policy_sha256"), path="job")
    legacy_job = root.get("schema_version") == "tkc.visual-parser-job/v0.3" and root.get("protocol") == "visual-parser-orchestrator-v0.3"
    current_job = root.get("schema_version") == VISUAL_PARSER_JOB_SCHEMA and root.get("protocol") == VISUAL_PARSER_ORCHESTRATOR_PROTOCOL
    if not legacy_job and not current_job:
        raise VisualParserOrchestratorError("schema_constant_mismatch:job")
    if current_job:
        # Phase 7D.3 job policy is closed and hash-bound.  The legacy v0.3
        # shape remains readable but is never rewritten as a v0.4 job.
        if not isinstance(root.get("visual_policy"), Mapping) or not isinstance(root.get("visual_mode"), str) or not isinstance(root.get("visual_kinds"), list) or not isinstance(root.get("visual_budget"), Mapping) or not isinstance(root.get("visual_context"), str) or not isinstance(root.get("policy_sha256"), str):
            raise VisualParserOrchestratorError("schema_visual_policy_missing")
        try:
            visual_policy_from_mapping(root["visual_policy"])
        except VisualPolicyError as error:
            raise VisualParserOrchestratorError(str(error)) from error
        if root.get("visual_mode") != root["visual_policy"].get("mode") or root.get("visual_context") != root["visual_policy"].get("visual_context"):
            raise VisualParserOrchestratorError("schema_visual_policy_projection_mismatch")
    source = _strict_object(root["source"], required=("path", "sha256", "format", "pypdf_version", "page_count", "provenance"), optional=(), path="job.source")
    if source.get("format") != "pdf" or source.get("pypdf_version") != "6.10.0" or source.get("provenance") not in {"user-supplied-local-pdf", "synthetic-fixture"}:
        raise VisualParserOrchestratorError("schema_source_contract_invalid")
    scope = _strict_object(root["scope"], required=("pages", "page_selection", "bounded", "raster_regions"), optional=(), path="job.scope")
    if scope.get("bounded") is not True or not isinstance(scope.get("pages"), list) or any(not isinstance(page, int) or page < 1 for page in scope["pages"]):
        raise VisualParserOrchestratorError("schema_scope_contract_invalid")
    if not isinstance(scope.get("raster_regions"), list):
        raise VisualParserOrchestratorError("schema_raster_regions_invalid")
    for index, region in enumerate(scope["raster_regions"]):
        _strict_object(region, required=("physical_page", "bbox_pdf_points", "coordinate_space", "source_anchor", "render_sha256", "crop_sha256"), optional=("region_id",), path=f"job.scope.raster_regions[{index}]")
    render = _strict_object(root["render"], required=("pdftoppm", "dpi"), optional=(), path="job.render")
    if not isinstance(render.get("pdftoppm"), str) or not isinstance(render.get("dpi"), int) or render["dpi"] < 36:
        raise VisualParserOrchestratorError("schema_render_contract_invalid")
    selection = _strict_object(root["backend_selection"], required=("primary", "challenger", "baseline"), optional=(), path="job.backend_selection")
    if any(value is not None and not isinstance(value, str) for value in selection.values()):
        raise VisualParserOrchestratorError("schema_backend_selection_invalid")
    for index, receipt in enumerate(root["backend_receipts"]):
        receipt_row = _strict_object(receipt, required=("backend_id", "adapter_id", "synthetic_only", "available", "candidate_only", "model_required", "model_download", "network_enabled", "model"), optional=("unavailable_code",), path=f"job.backend_receipts[{index}]")
        _strict_object(receipt_row["model"], required=("available", "identity_sha256"), optional=("code", "path", "file_count", "files_sha256"), path=f"job.backend_receipts[{index}].model")
    if not isinstance(root["model_roots"], Mapping) or any(value is not None and not isinstance(value, str) for value in root["model_roots"].values()):
        raise VisualParserOrchestratorError("schema_model_roots_invalid")
    if not isinstance(root["backend_config"], Mapping) or not isinstance(root.get("backend_config_sha256"), str):
        raise VisualParserOrchestratorError("schema_backend_config_invalid")
    if root.get("external_runtime") is not None and not isinstance(root.get("external_runtime"), Mapping):
        raise VisualParserOrchestratorError("schema_external_runtime_invalid")
    policy = _strict_object(root["policy"], required=("native_text_first", "canonical_native_extractor", "network_enabled", "model_download", "silent_fallback", "candidate_only", "parser_disagreement_is_conflict", "review_authoring", "promotion", "pause_only_resume", "synthetic_only"), optional=(), path="job.policy")
    if policy.get("native_text_first") is not True or policy.get("network_enabled") is not False or policy.get("model_download") is not False or policy.get("silent_fallback") is not False or policy.get("candidate_only") is not True or policy.get("review_authoring") is not False or policy.get("promotion") is not False:
        raise VisualParserOrchestratorError("schema_policy_contract_invalid")
    if not isinstance(root.get("test_mode"), bool) or not isinstance(root.get("test_only_authorized"), bool) or not isinstance(root.get("synthetic_only"), bool) or root.get("state") not in {"planned", "paused", "candidate-only", "blocked"} or not isinstance(root.get("paused_gates"), list) or any(not isinstance(value, str) for value in root["paused_gates"]):
        raise VisualParserOrchestratorError("schema_state_contract_invalid")
    if "fake_fixture" in root and not isinstance(root["fake_fixture"], Mapping):
        raise VisualParserOrchestratorError("schema_fake_fixture_object_required")


def validate_visual_parser_manifest_schema(manifest: Mapping[str, Any]) -> None:
    root = _strict_object(manifest, required=("schema_version", "protocol", "compiler_version", "job_sha256", "source_sha256", "pages", "candidate_only", "synthetic_only", "whole_book_completeness_claimed", "review_authoring", "promotion", "components", "object_count", "relation_count", "table_grid_count", "conflict_count", "downstream_routing", "manifest_sha256"), optional=("visual_policy", "visual_mode", "visual_kinds", "visual_budget", "visual_context", "policy_sha256", "visual_census", "visual_routing", "visual_budget_receipt", "visual_index", "visual_coverage", "chart_candidates", "text_ocr"), path="manifest")
    legacy_manifest = root.get("schema_version") == "tkc.visual-parser-manifest/v0.3" and root.get("protocol") == "visual-parser-orchestrator-v0.3"
    current_manifest = root.get("schema_version") == VISUAL_PARSER_MANIFEST_SCHEMA and root.get("protocol") == VISUAL_PARSER_ORCHESTRATOR_PROTOCOL
    if not legacy_manifest and not current_manifest or root.get("candidate_only") is not True or root.get("whole_book_completeness_claimed") is not False or root.get("review_authoring") is not False or root.get("promotion") is not False:
        raise VisualParserOrchestratorError("schema_manifest_contract_invalid")
    if current_manifest and (not isinstance(root.get("visual_policy"), Mapping) or not isinstance(root.get("visual_mode"), str) or not isinstance(root.get("visual_kinds"), list) or not isinstance(root.get("visual_budget"), Mapping) or not isinstance(root.get("visual_context"), str) or not isinstance(root.get("policy_sha256"), str)):
        raise VisualParserOrchestratorError("schema_manifest_visual_policy_missing")
    for field in ("visual_census", "visual_routing", "visual_budget_receipt", "visual_index", "visual_coverage", "chart_candidates", "text_ocr"):
        if field in root and not isinstance(root[field], str):
            raise VisualParserOrchestratorError(f"schema_manifest_reference_invalid:{field}")
    if not isinstance(root.get("components"), Mapping) or any(not isinstance(value, str) for value in root["components"].values()):
        raise VisualParserOrchestratorError("schema_manifest_components_invalid")
    routing_required = ("schema_version", "source_sha256", "coverage_ledger", "incremental_dag", "semantic_composer", "routing_sha256") + (("protocol",) if current_manifest else ())
    routing = _strict_object(root["downstream_routing"], required=routing_required, optional=(), path="manifest.downstream_routing")
    _strict_object(routing["incremental_dag"], required=("status", "path"), optional=(), path="manifest.downstream_routing.incremental_dag")
    _strict_object(routing["semantic_composer"], required=("status", "reason", "promotion", "auto_consensus", "input_object_count", "synthetic_only"), optional=(), path="manifest.downstream_routing.semantic_composer")


def validate_visual_evaluation_contract_schema(contract: Mapping[str, Any]) -> None:
    root = _strict_object(contract, required=("schema_version", "contract_kind", "source_kind", "status", "verified_gold", "synthetic_contract_truth", "independent_real_gold", "reason", "metrics", "recommended_targets", "candidate_only"), optional=("evaluation_sha256",), path="evaluation")
    if root.get("schema_version") != VISUAL_EVALUATION_CONTRACT_SCHEMA or root.get("contract_kind") not in {"synthetic-gold", "candidate-benchmark"} or root.get("status") not in {"measured", "not-run"} or root.get("verified_gold") is not False or root.get("independent_real_gold") is not False or root.get("candidate_only") is not True:
        raise VisualParserOrchestratorError("schema_evaluation_contract_invalid")
    if root.get("contract_kind") == "synthetic-gold" and root.get("synthetic_contract_truth") is not True:
        raise VisualParserOrchestratorError("schema_synthetic_gold_semantics_invalid")
    if root.get("contract_kind") == "candidate-benchmark" and root.get("synthetic_contract_truth") is not False:
        raise VisualParserOrchestratorError("schema_candidate_benchmark_semantics_invalid")


def _validate_job_shape(job: Mapping[str, Any]) -> None:
    validate_visual_parser_job_schema(job)
    legacy_job = job.get("schema_version") == "tkc.visual-parser-job/v0.3" and job.get("protocol") == "visual-parser-orchestrator-v0.3"
    if not legacy_job and (job.get("schema_version") != VISUAL_PARSER_JOB_SCHEMA or job.get("protocol") != VISUAL_PARSER_ORCHESTRATOR_PROTOCOL):
        raise VisualParserOrchestratorError("visual_parser_job_schema_invalid")
    if job.get("job_sha256") != _job_hash(job):
        raise VisualParserOrchestratorError("visual_parser_job_tampered")
    policy = job.get("policy")
    if not isinstance(policy, Mapping) or policy.get("network_enabled") is not False or policy.get("model_download") is not False or policy.get("silent_fallback") is not False or policy.get("candidate_only") is not True:
        raise VisualParserOrchestratorError("visual_parser_policy_invalid")
    if policy.get("promotion") is not False or policy.get("review_authoring") is not False:
        raise VisualParserOrchestratorError("visual_parser_capability_escalation")
    if not legacy_job:
        try:
            visual_policy = visual_policy_from_mapping(job.get("visual_policy"))
        except VisualPolicyError as error:
            raise VisualParserOrchestratorError(str(error)) from error
        if job.get("policy_sha256") != visual_policy.policy_sha256:
            raise VisualParserOrchestratorError("visual_policy_hash_mismatch")
        if job.get("visual_mode") != visual_policy.mode or tuple(job.get("visual_kinds", [])) != visual_policy.kinds or job.get("visual_context") != visual_policy.context:
            raise VisualParserOrchestratorError("visual_policy_projection_mismatch")
    synthetic = bool(job.get("synthetic_only"))
    selection = job.get("backend_selection", {})
    uses_fake = "fake" in {str(value) for value in selection.values() if value}
    if uses_fake != synthetic:
        raise VisualParserOrchestratorError("synthetic_backend_policy_mismatch")
    if policy.get("synthetic_only") is not synthetic:
        raise VisualParserOrchestratorError("synthetic_policy_marker_mismatch")
    if synthetic and (job.get("test_mode") is not True or job.get("test_only_authorized") is not True or job.get("source", {}).get("provenance") != "synthetic-fixture"):
        raise VisualParserOrchestratorError("fake_backend_test_only_authorization_required")
    if synthetic and job.get("source", {}).get("path") and _synthetic_path_forbidden(Path(str(job["source"]["path"]))):
        raise VisualParserOrchestratorError("synthetic_fixture_real_source_or_release_forbidden")
    if synthetic and job.get("output_root") and _synthetic_path_forbidden(Path(str(job["output_root"]))):
        raise VisualParserOrchestratorError("synthetic_fixture_release_output_forbidden")


def _config_from_path(config_path: Path | None) -> tuple[dict[str, Any], str]:
    if config_path is None:
        config: dict[str, Any] = {}
    else:
        config = _read_json(config_path)
    if config.get("allow_network") is True or config.get("network_enabled") is True or config.get("allow_model_download") is True or config.get("model_download") is True:
        raise VisualParserOrchestratorError("network_or_model_download_forbidden")
    return config, sha256_json(config)


def _external_runtime_spec(config: Mapping[str, Any], backend: str) -> dict[str, Any] | None:
    supplied = config.get("external_runtime")
    if isinstance(supplied, Mapping) and isinstance(supplied.get(backend), Mapping):
        return dict(supplied[backend])
    if isinstance(supplied, Mapping):
        return dict(supplied)
    return None


def _runtime_qualification(config: Mapping[str, Any], backend: str) -> dict[str, Any] | None:
    supplied = config.get("runtime_qualifications")
    if isinstance(supplied, Mapping) and supplied.get("schema_version") == "tkc.paddle-runtime-qualification/v0.1":
        return dict(supplied) if supplied.get("backend_id") == backend else None
    if isinstance(supplied, Mapping) and isinstance(supplied.get(backend), Mapping):
        return dict(supplied[backend])
    return None


def _freeze_job_runtime(
    *,
    backend: str,
    config: Mapping[str, Any],
    model_root: Path | None,
) -> dict[str, Any] | None:
    if backend not in {PADDLE_OCR_BACKEND, PADDLE_STRUCTURE_BACKEND, PADDLE_STRUCTURE_V3_BACKEND}:
        return None
    spec = _external_runtime_spec(config, backend)
    if spec is None:
        return None
    if model_root is not None:
        spec.setdefault("model_root", str(model_root))
    try:
        return freeze_external_runtime_contract(
            spec,
            backend_id=backend,
            configuration=_runtime_engine_configuration(_effective_config(backend, config)),
            worker_path=Path(__file__).with_name("ocr_backend_worker.py").resolve(),
            probe=True,
        )
    except PaddleRuntimeContractError as error:
        raise VisualParserOrchestratorError(f"external_runtime_contract_invalid:{backend}:{error}") from error


def create_visual_parser_job(*, source: Path, output: Path, pages: Sequence[int] | None = None, primary_backend: str = "none", challenger_backend: str | None = None, baseline_backend: str | None = None, model_roots: Mapping[str, Path | None] | None = None, backend_config: Mapping[str, Any] | None = None, external_runtime: Mapping[str, Any] | None = None, fake_fixture: Mapping[str, Any] | None = None, test_mode: bool = False, test_only_authorized: bool = False, synthetic_source: bool = False, raster_regions: Sequence[Mapping[str, Any]] | None = None, pdftoppm: Path | str = "pdftoppm", render_dpi: int = 150, created_at: str = "1970-01-01T00:00:00Z", visual_mode: str = "auto", visual_kinds: Sequence[str] | None = None, max_visual_pages: int | None = None, max_visual_regions: int | None = None, max_visual_seconds: float | None = None, visual_context: str = "index") -> dict[str, Any]:
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    if not source.is_file() or source.suffix.casefold() != ".pdf":
        raise VisualParserOrchestratorError("source_pdf_required")
    if _path_overlap(source, output):
        raise VisualParserOrchestratorError("input_output_path_overlap")
    try:
        selected, page_count = _pages_for_job(source, pages)
        import pypdf
    except VisualParserOrchestratorError:
        raise
    except Exception as error:
        raise VisualParserOrchestratorError("pypdf_preflight_failed") from error
    if pages is not None and not _is_contiguous_pages(selected):
        raise VisualParserOrchestratorError("non_contiguous_page_selection_unsupported_for_ocr_scope")
    if not isinstance(created_at, str) or not created_at:
        raise VisualParserOrchestratorError("created_at_invalid")
    config = dict(backend_config or {})
    try:
        visual_policy = normalize_visual_policy(mode=visual_mode, visual_kinds=visual_kinds, max_visual_pages=max_visual_pages, max_visual_regions=max_visual_regions, max_visual_seconds=max_visual_seconds, context=visual_context)
    except VisualPolicyError as error:
        raise VisualParserOrchestratorError(str(error)) from error
    if external_runtime is not None:
        config["external_runtime"] = json.loads(json.dumps(dict(external_runtime), ensure_ascii=False, sort_keys=True))
    if config.get("allow_network") is True or config.get("network_enabled") is True or config.get("allow_model_download") is True or config.get("model_download") is True:
        raise VisualParserOrchestratorError("network_or_model_download_forbidden")
    uses_fake = "fake" in {str(value) for value in (primary_backend, challenger_backend, baseline_backend) if value}
    if uses_fake and not (test_mode and test_only_authorized and synthetic_source):
        raise VisualParserOrchestratorError("fake_backend_test_only_authorization_required")
    if fake_fixture is not None and (not uses_fake or primary_backend != "fake" or challenger_backend is not None or baseline_backend is not None):
        raise VisualParserOrchestratorError("fake_fixture_requires_exclusive_primary_test_backend")
    if uses_fake and (_synthetic_path_forbidden(source) or _synthetic_path_forbidden(output)):
        raise VisualParserOrchestratorError("synthetic_fixture_real_source_or_release_forbidden")
    if primary_backend == "none":
        if challenger_backend is not None or baseline_backend is not None:
            raise VisualParserOrchestratorError("challenger_or_baseline_requires_primary_backend")
        selection = {"primary": None, "challenger": None, "baseline": None}
    else:
        try:
            selection = validate_backend_selection(primary_backend, challenger_backend, baseline_backend)
        except ScanOcrError as error:
            raise VisualParserOrchestratorError(str(error)) from error
    # Stamp the closed profile into the job itself.  The runtime contract also
    # carries it, but a job must be independently inspectable: no constructor
    # or predict switch may be supplied only by a library default.
    for selected_backend in [value for value in selection.values() if value in {PADDLE_OCR_BACKEND, PADDLE_STRUCTURE_BACKEND, PADDLE_STRUCTURE_V3_BACKEND}]:
        effective_backend = _effective_config(str(selected_backend), config)
        config[str(selected_backend)] = effective_backend["backend_options"]
    roots = {str(key): (str(value.expanduser().resolve()) if value is not None else None) for key, value in (model_roots or {}).items()}
    page_sizes = _page_sizes(source, selected)
    page_geometries = _page_geometries(source, selected)
    region_rows = _validate_raster_region_contract(raster_regions, source_sha256=_hash_path(source), selected_pages=selected, page_sizes=page_sizes, page_geometries=page_geometries)
    backend_receipts: list[dict[str, Any]] = []
    paused_gates: list[str] = []
    runtime_contracts: dict[str, Any] = {}
    for backend in [value for value in selection.values() if value]:
        adapter_id = _backend_adapter(str(backend))
        root = Path(roots[backend]) if roots.get(backend) else None
        runtime_contract = _freeze_job_runtime(backend=str(backend), config=config, model_root=root)
        if backend in {PADDLE_OCR_BACKEND, PADDLE_STRUCTURE_BACKEND, PADDLE_STRUCTURE_V3_BACKEND}:
            runtime_contracts[str(backend)] = runtime_contract
            if runtime_contract is None:
                paused_gates.append(f"runtime:{backend}:external_runtime_contract_required")
            elif runtime_contract.get("available") is not True:
                missing = ",".join(runtime_contract.get("model_status", {}).get("missing_model_dirs", []))
                paused_gates.append(f"runtime:{backend}:missing_local_models:{missing or 'unknown'}")
            else:
                roots[str(backend)] = str(runtime_contract["model_root"])
                root = Path(runtime_contract["model_root"])
                if backend == PADDLE_STRUCTURE_V3_BACKEND:
                    qualification = _runtime_qualification(config, str(backend))
                    try:
                        if qualification is None:
                            raise PaddleRuntimeQualificationError("runtime_qualification_missing")
                        validate_runtime_qualification(
                            qualification,
                            contract=runtime_contract,
                            backend_id=PADDLE_STRUCTURE_V3_BACKEND,
                            profile_id="technical-table-v1",
                            require_qualified=True,
                        )
                    except PaddleRuntimeQualificationError as error:
                        paused_gates.append(f"runtime:{backend}:paused-runtime-unqualified:{error}")
        try:
            receipt = adapter_receipt(adapter_id, model_root=str(root) if root else None, external_runtime=runtime_contract is not None)
        except VisualAdapterError as error:
            raise VisualParserOrchestratorError(str(error)) from error
        backend_receipts.append({"backend_id": backend, "adapter_id": adapter_id, "synthetic_only": uses_fake, **receipt.to_dict()})
        if not receipt.available:
            paused_gates.append(f"backend:{backend}:{receipt.unavailable_code or 'unavailable'}")
        if runtime_contract is not None:
            model = runtime_contract.get("model", {})
            identity = {"available": bool(runtime_contract.get("available")), "path": runtime_contract.get("model_root"), "file_count": model.get("file_count"), "identity_sha256": model.get("identity_sha256"), "files_sha256": model.get("manifest_sha256"), **({"code": "not-run-missing-models"} if not runtime_contract.get("available") else {})}
        else:
            identity = _tree_identity(root, required=bool(BACKEND_SPECS.get(backend, {}).get("model_required", False)))
        backend_receipts[-1]["model"] = identity
        if not identity.get("available"):
            paused_gates.append(f"model:{backend}:{identity.get('code', 'unavailable')}")
    if region_rows and not any(str(value) in {PADDLE_OCR_BACKEND, PADDLE_STRUCTURE_BACKEND, PADDLE_STRUCTURE_V3_BACKEND} for value in selection.values() if value):
        paused_gates.append("raster_region_requires_external_paddle_backend")
    policy_sha256 = visual_policy.policy_sha256
    policy_record = visual_policy.to_dict()
    policy_record["policy_sha256"] = policy_sha256
    job: dict[str, Any] = {
        "schema_version": VISUAL_PARSER_JOB_SCHEMA,
        "protocol": VISUAL_PARSER_ORCHESTRATOR_PROTOCOL,
        "compiler_version": VISUAL_PARSER_ORCHESTRATOR_COMPILER_VERSION,
        "source": {"path": str(source), "sha256": _hash_path(source), "format": "pdf", "pypdf_version": str(pypdf.__version__), "page_count": page_count, "provenance": "synthetic-fixture" if uses_fake else "user-supplied-local-pdf"},
        "scope": {"pages": selected, "page_selection": "explicit" if pages else "whole-input-default", "bounded": True, "raster_regions": region_rows},
        "output_root": str(output),
        "render": {"pdftoppm": str(Path(pdftoppm).expanduser().resolve()) if str(pdftoppm).startswith("/") else str(pdftoppm), "dpi": int(render_dpi)},
        "backend_selection": selection,
        "backend_receipts": sorted(backend_receipts, key=lambda row: str(row.get("backend_id"))),
        "model_roots": roots,
        "backend_config": config,
        "backend_config_sha256": sha256_json(config),
        "external_runtime": runtime_contracts or None,
        "policy": {"native_text_first": True, "canonical_native_extractor": "pypdf==6.10.0", "network_enabled": False, "model_download": False, "silent_fallback": False, "candidate_only": True, "parser_disagreement_is_conflict": True, "review_authoring": False, "promotion": False, "pause_only_resume": True, "synthetic_only": uses_fake},
        "test_mode": bool(test_mode),
        "test_only_authorized": bool(test_only_authorized),
        "synthetic_only": uses_fake,
        "state": "paused" if paused_gates else "planned",
        "paused_gates": sorted(set(paused_gates)),
        "created_at": created_at,
        "visual_policy": policy_record,
        "visual_mode": visual_policy.mode,
        "visual_kinds": list(visual_policy.kinds),
        "visual_budget": visual_policy.budget.to_dict(),
        "visual_context": visual_policy.context,
        "policy_sha256": policy_sha256,
    }
    if fake_fixture is not None:
        job["fake_fixture"] = json.loads(json.dumps(dict(fake_fixture), ensure_ascii=False, sort_keys=True))
    job["job_sha256"] = _job_hash(job)
    _validate_job_shape(job)
    return job


def write_visual_parser_job(path: Path, job: Mapping[str, Any]) -> None:
    _validate_job_shape(job)
    _write_json(path.expanduser().resolve(), job)


def _load_job(path: Path) -> dict[str, Any]:
    job = _read_json(path.expanduser().resolve())
    _validate_job_shape(job)
    source = Path(str(job.get("source", {}).get("path", ""))).expanduser().resolve()
    if not source.is_file() or _hash_path(source) != job.get("source", {}).get("sha256"):
        raise VisualParserOrchestratorError("source_tampered_or_missing")
    output = Path(str(job.get("output_root", ""))).expanduser().resolve()
    if _path_overlap(source, output):
        raise VisualParserOrchestratorError("input_output_path_overlap")
    if str(job.get("source", {}).get("pypdf_version")) != "6.10.0":
        raise VisualParserOrchestratorError("pypdf_version_mismatch")
    scope_pages = [int(value) for value in job.get("scope", {}).get("pages", [])]
    _validate_raster_region_contract(
        job.get("scope", {}).get("raster_regions", []),
        source_sha256=str(job["source"]["sha256"]),
        selected_pages=scope_pages,
        page_sizes=_page_sizes(source, scope_pages),
        page_geometries=_page_geometries(source, scope_pages),
    )
    runtime_contracts = job.get("external_runtime") or {}
    if not isinstance(runtime_contracts, Mapping):
        raise VisualParserOrchestratorError("schema_external_runtime_invalid")
    for backend, contract in runtime_contracts.items():
        if contract is None:
            continue
        if not isinstance(contract, Mapping):
            raise VisualParserOrchestratorError(f"external_runtime_contract_invalid:{backend}")
        if contract.get("available") is True:
            try:
                validate_runtime_contract(contract)
            except PaddleRuntimeContractError as error:
                raise VisualParserOrchestratorError(f"external_runtime_contract_drift:{backend}:{error}") from error
    if not _is_contiguous_pages(job.get("scope", {}).get("pages", [])):
        raise VisualParserOrchestratorError("non_contiguous_page_selection_unsupported_for_ocr_scope")
    return job


def _page_render_map(preflight: Mapping[str, Any], pages: Sequence[int]) -> dict[int, dict[str, Any]]:
    return {int(row["physical_page"]): dict(row) for row in preflight.get("render_receipts", []) if isinstance(row, Mapping) and int(row.get("physical_page", 0)) in set(pages)}


def _bbox_iou(left: Sequence[float], right: Sequence[float]) -> float:
    x0, y0 = max(float(left[0]), float(right[0])), max(float(left[1]), float(right[1]))
    x1, y1 = min(float(left[2]), float(right[2])), min(float(left[3]), float(right[3]))
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    area_left = max(0.0, float(left[2]) - float(left[0])) * max(0.0, float(left[3]) - float(left[1]))
    area_right = max(0.0, float(right[2]) - float(right[0])) * max(0.0, float(right[3]) - float(right[1]))
    union = area_left + area_right - intersection
    return intersection / union if union else 0.0


def _ocr_objects(
    job: Mapping[str, Any],
    scan_result: Mapping[str, Any],
    page_sizes: Mapping[int, Sequence[float]],
    render_by_page: Mapping[int, Mapping[str, Any]],
    page_geometries: Mapping[int, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    observations = [row for row in scan_result.get("ocr_observations", []) if isinstance(row, Mapping) and int(row.get("physical_page", 0)) in page_sizes]
    receipts = {str(row.get("id")): row for row in scan_result.get("ocr_backend_receipts", []) if isinstance(row, Mapping)}
    roots = _backend_roots(job)
    objects: list[dict[str, Any]] = []
    table_grids: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    adapter_object_aliases: dict[str, str] = {}
    table_grid_bindings = {
        str(row.get("table_grid_id")): row
        for row in scan_result.get("table_grid_bindings", [])
        if isinstance(row, Mapping)
    }
    scan_grids = [row for row in scan_result.get("table_grids", []) if isinstance(row, Mapping)]
    scan_visual_candidates = [row for row in scan_result.get("visual_candidates", []) if isinstance(row, Mapping)]
    scan_visual_gaps = [row for row in scan_result.get("visual_gaps", []) if isinstance(row, Mapping)]
    scan_visual_conflicts = [row for row in scan_result.get("visual_conflicts", []) if isinstance(row, Mapping)]
    grid_visual_ids = {str(row.get("table_visual_object_id")) for row in scan_grids if row.get("table_visual_object_id")}
    for candidate in scan_visual_candidates:
        candidate_id = str(candidate.get("candidate_id") or candidate.get("adapter_local_candidate_id") or "")
        if not candidate_id or candidate.get("kind") == "table" and candidate_id in grid_visual_ids:
            continue
        backend = str(candidate.get("backend_id", ""))
        receipt = receipts.get(str(candidate.get("backend_receipt_id")))
        page = int(candidate.get("physical_page", 0))
        render = render_by_page.get(page)
        if not isinstance(receipt, Mapping) or not isinstance(render, Mapping) or page not in page_sizes:
            raise VisualParserOrchestratorError("paddle_visual_candidate_receipt_missing")
        width, height = float(page_sizes[page][0]), float(page_sizes[page][1])
        page_geometry = dict((page_geometries or {}).get(page) or {})
        render_rotation = int(page_geometry.get("rotation", 0)) % 360
        adapter_id = _backend_adapter(backend)
        result_adapter_id = str(candidate.get("result_adapter_id") or "paddle-result-adapter")
        try:
            locators = map_candidate_locators(
                adapter_id,
                [dict(candidate)],
                source_sha256=str(job["source"]["sha256"]),
                physical_page=page,
                page_width=width,
                page_height=height,
                render_sha256=str(render["render_sha256"]),
                render_dpi=int(job["render"]["dpi"]),
                render_rotation=render_rotation,
                coordinate_space=COORDINATE_SPACE,
                parser_receipt={
                    "id": result_adapter_id,
                    "candidate_only": True,
                    "version": VISUAL_PARSER_ORCHESTRATOR_COMPILER_VERSION,
                    "raw_result_sha256": next((row.get("raw_result_sha256") for row in scan_result.get("result_adapters", []) if isinstance(row, Mapping) and row.get("id") == result_adapter_id), None),
                    "normalized_result_sha256": next((row.get("normalized_result_sha256") for row in scan_result.get("result_adapters", []) if isinstance(row, Mapping) and row.get("id") == result_adapter_id), None),
                    "synthetic_only": bool(job.get("synthetic_only")),
                },
                backend_receipt={
                    "id": str(receipt.get("id")),
                    "backend_id": backend,
                    "candidate_only": True,
                    "status": "candidate-only",
                    "synthetic_only": bool(job.get("synthetic_only")),
                    "runtime_contract_sha256": receipt.get("runtime_contract_sha256"),
                    "worker_sha256": (receipt.get("worker") or {}).get("worker_sha256"),
                    "worker_request_sha256": (receipt.get("worker") or {}).get("request_sha256"),
                    "canonical_request_sha256": _canonical_worker_request_sha256(
                        source_sha256=str(job["source"]["sha256"]),
                        physical_page=page,
                        backend_id=backend,
                        render_sha256=str(render["render_sha256"]),
                        crop_sha256=str(candidate.get("crop_sha256")) if candidate.get("crop_sha256") else None,
                        region_id=str(candidate.get("region_id")) if candidate.get("region_id") else None,
                        coordinate_transform_id=str(candidate.get("coordinate_transform_id")) if candidate.get("coordinate_transform_id") else None,
                        receipt=receipt,
                    ),
                    "worker_response_sha256": (receipt.get("worker") or {}).get("response_sha256"),
                    "worker_response_content_sha256": sha256_json({"raw_result_sha256": next((row.get("raw_result_sha256") for row in scan_result.get("result_adapters", []) if isinstance(row, Mapping) and row.get("id") == result_adapter_id), None), "normalized_result_sha256": next((row.get("normalized_result_sha256") for row in scan_result.get("result_adapters", []) if isinstance(row, Mapping) and row.get("id") == result_adapter_id), None), "result_adapter_id": result_adapter_id}),
                },
                model_receipt={**dict(receipt.get("model") or {}), "id": str((receipt.get("model") or {}).get("model_id", "none")), "available": True, "download": False, "network_enabled": False, "candidate_only": True, "promotion": False, "synthetic_only": bool(job.get("synthetic_only"))},
                config_receipt={"id": str(receipt.get("configuration_sha256", "scan-config")), "candidate_only": True, "synthetic_only": bool(job.get("synthetic_only"))},
                model_root=str(roots.get(backend)) if roots.get(backend) else None,
            )
        except VisualAdapterError as error:
            raise VisualParserOrchestratorError(f"paddle_visual_candidate_mapping_failed:{error}") from error
        for locator in locators:
            object_type = str(locator["type"])
            object_attributes = {
                "candidate_only": True,
                "promotion": False,
                "verified_gold": False,
                "review_status": "unreviewed",
                "review_required": candidate.get("review_required", "visual-localization-one-reviewer"),
                "geometry_precision": candidate.get("geometry_precision", "declared-runtime-result"),
                "source_kind": "paddle-ppstructure-v3-layout",
                "raster_region": {"region_id": candidate.get("region_id"), "crop_sha256": candidate.get("crop_sha256")},
                "adapter_local_candidate_id": candidate_id,
            }
            uncertainty = {"level": "candidate", "geometry_confirmed": False, "numeric_values_verified": False, "series_values_verified": False, "axis_or_series_visual_confirmation": False}
            visual_object = build_visual_object(
                source_sha256=str(job["source"]["sha256"]),
                physical_page=page,
                object_type=object_type,
                subtype=str(locator.get("subtype", "")),
                page_width=width,
                page_height=height,
                bbox=locator["bbox"],
                polygon=locator.get("polygon"),
                coordinate_space=COORDINATE_SPACE,
                rotation=render_rotation,
                render_sha256=str(render["render_sha256"]),
                render_dpi=int(job["render"]["dpi"]),
                crop_sha256=str(candidate.get("crop_sha256")) if candidate.get("crop_sha256") else None,
                label=str(locator.get("label") or candidate.get("label") or object_type),
                content_sha256=str(candidate.get("content_sha256")) if candidate.get("content_sha256") else None,
                parser_receipt=locator["parser_receipt"],
                backend_receipt=locator["backend_receipt"],
                model_receipt=locator["model_receipt"],
                config_receipt=locator["config_receipt"],
                attributes=object_attributes,
                uncertainty=uncertainty,
            )
            objects.append(visual_object)
            adapter_object_aliases[candidate_id] = str(visual_object["visual_object_id"])
    for observation in observations:
        # Plain OCR text is a transcript candidate, not a visual object.  It
        # remains in scan-ir and the coverage route; mapping it as a figure or
        # table would create a false visual assertion.  Other kinds are still
        # strict: an unknown kind must fail closed so adapter/schema drift is
        # visible rather than silently discarded.
        observation_kind = str(observation.get("kind", "text")).strip().casefold().replace("_", "-")
        if observation_kind in {"text", "ocr-text", "text-line", "paragraph"} or (observation_kind == "table" and scan_grids):
            continue
        backend = str((observation.get("backend") or {}).get("id", ""))
        adapter_id = _backend_adapter(backend)
        page = int(observation["physical_page"])
        render = render_by_page.get(page)
        if not isinstance(render, Mapping):
            raise VisualParserOrchestratorError(f"ocr_render_receipt_missing:{page}")
        receipt = receipts.get(str(observation.get("backend_receipt_id")), {})
        if not isinstance(receipt, Mapping):
            raise VisualParserOrchestratorError("ocr_backend_receipt_missing")
        width, height = float(page_sizes[page][0]), float(page_sizes[page][1])
        page_geometry = dict((page_geometries or {}).get(page) or {})
        render_rotation = int(page_geometry.get("rotation", 0)) % 360
        row = {
            "kind": observation.get("kind", "text"),
            "text": observation.get("normalized_transcript", ""),
            "bbox": observation.get("bbox_pdf_points"),
            "polygon": observation.get("polygon_pdf_points"),
            "physical_page": page,
            "source_sha256": job["source"]["sha256"],
            "render_sha256": render.get("render_sha256"),
            "content_sha256": sha256_json({"text": observation.get("normalized_transcript"), "kind": observation.get("kind")}),
        }
        try:
            locators = map_candidate_locators(
                adapter_id,
                [row],
                source_sha256=str(job["source"]["sha256"]),
                physical_page=page,
                page_width=width,
                page_height=height,
                render_sha256=str(render["render_sha256"]),
                render_dpi=int(job["render"]["dpi"]),
                render_rotation=render_rotation,
                coordinate_space=COORDINATE_SPACE,
                parser_receipt={"id": f"visual-parser-orchestrator:{backend}", "candidate_only": True, "version": VISUAL_PARSER_ORCHESTRATOR_COMPILER_VERSION, "synthetic_only": bool(job.get("synthetic_only"))},
                backend_receipt={"id": str(receipt.get("id")), "backend_id": backend, "candidate_only": True, "status": "candidate-only", "synthetic_only": bool(job.get("synthetic_only"))},
                model_receipt={
                    **dict(receipt.get("model") or {}),
                    "id": str((receipt.get("model") or {}).get("model_id", "none")),
                    "available": True,
                    "download": False,
                    "network_enabled": False,
                    "candidate_only": True,
                    "synthetic_only": bool(job.get("synthetic_only")),
                    "promotion": False,
                },
                config_receipt={"id": str(receipt.get("configuration_sha256", "scan-config")), "candidate_only": True, "synthetic_only": bool(job.get("synthetic_only"))},
                model_root=str(roots.get(backend)) if roots.get(backend) else None,
            )
        except VisualAdapterError as error:
            raise VisualParserOrchestratorError(f"ocr_candidate_mapping_failed:{error}") from error
        for locator in locators:
            bbox = locator["bbox"]
            object_type = str(locator["type"])
            label = locator.get("label") or observation.get("normalized_transcript") or object_type
            visual_object = build_visual_object(
                source_sha256=str(job["source"]["sha256"]),
                physical_page=page,
                object_type=object_type,
                subtype=str(locator.get("subtype", "")),
                page_width=width,
                page_height=height,
                bbox=bbox,
                polygon=locator.get("polygon"),
                coordinate_space=COORDINATE_SPACE,
                rotation=render_rotation,
                render_sha256=str(render["render_sha256"]),
                render_dpi=int(job["render"]["dpi"]),
                crop_sha256=sha256_json({"render_sha256": render["render_sha256"], "bbox": bbox}),
                label=str(label),
                content_sha256=locator.get("content_sha256"),
                parser_receipt=locator["parser_receipt"],
                backend_receipt=locator["backend_receipt"],
                model_receipt=locator["model_receipt"],
                config_receipt=locator["config_receipt"],
            )
            objects.append(visual_object)
    canonical_grid_bindings: list[dict[str, Any]] = []
    for grid in scan_grids:
        binding = table_grid_bindings.get(str(grid.get("table_grid_id")))
        if not isinstance(binding, Mapping):
            raise VisualParserOrchestratorError("paddle_table_grid_binding_missing")
        backend = str(binding.get("backend_id", ""))
        receipt = receipts.get(str(binding.get("backend_receipt_id")))
        page = int(binding.get("physical_page", 0))
        render = render_by_page.get(page)
        if not isinstance(receipt, Mapping) or not isinstance(render, Mapping) or page not in page_sizes:
            raise VisualParserOrchestratorError("paddle_table_grid_receipt_missing")
        evidence = grid.get("topology_evidence") if isinstance(grid.get("topology_evidence"), list) else []
        table_bbox = next((item.get("bbox") for item in evidence if isinstance(item, Mapping) and isinstance(item.get("bbox"), (list, tuple))), None)
        if not isinstance(table_bbox, (list, tuple)) or len(table_bbox) != 4:
            raise VisualParserOrchestratorError("paddle_table_grid_bbox_missing")
        width, height = float(page_sizes[page][0]), float(page_sizes[page][1])
        model = dict(receipt.get("model") or {})
        model.setdefault("id", model.get("model_id", "none"))
        model.setdefault("available", True)
        model.update({"download": False, "network_enabled": False, "candidate_only": True, "promotion": False, "synthetic_only": bool(job.get("synthetic_only"))})
        page_geometry = dict((page_geometries or {}).get(page) or {})
        render_rotation = int(page_geometry.get("rotation", 0)) % 360
        result_adapter_id = str(receipt.get("result_adapter_id") or "paddle-ppstructure-v3-table-normalizer")
        adapter_row = next((row for row in scan_result.get("result_adapters", []) if isinstance(row, Mapping) and row.get("id") == result_adapter_id), {})
        table_identity = next((item.get("table_id") for item in evidence if isinstance(item, Mapping) and item.get("table_id") is not None), str(grid.get("table_grid_id")))
        content_sha256 = sha256_json({"result_adapter_id": result_adapter_id, "table_identity": table_identity, "physical_page": page})
        table_object = build_visual_object(
            source_sha256=str(job["source"]["sha256"]),
            physical_page=page,
            object_type="table",
            subtype="paddle-structured-table",
            page_width=width,
            page_height=height,
            bbox=list(table_bbox),
            polygon=None,
            coordinate_space=COORDINATE_SPACE,
            rotation=render_rotation,
            render_sha256=str(render["render_sha256"]),
            render_dpi=int(job["render"]["dpi"]),
            crop_sha256=str(binding.get("crop_sha256")),
            label="Paddle structured table candidate",
            content_sha256=content_sha256,
            page_geometry=page_geometry,
            parser_receipt={"id": result_adapter_id, "candidate_only": True, "version": VISUAL_PARSER_ORCHESTRATOR_COMPILER_VERSION, "synthetic_only": bool(job.get("synthetic_only")), "raw_result_sha256": adapter_row.get("raw_result_sha256"), "normalized_result_sha256": adapter_row.get("normalized_result_sha256")},
            backend_receipt={
                "id": str(receipt.get("id")),
                "backend_id": backend,
                "candidate_only": True,
                "status": "candidate-only",
                "synthetic_only": bool(job.get("synthetic_only")),
                "runtime_contract_sha256": receipt.get("runtime_contract_sha256"),
                "worker_sha256": (receipt.get("worker") or {}).get("worker_sha256"),
                "worker_request_sha256": (receipt.get("worker") or {}).get("request_sha256"),
                "canonical_request_sha256": _canonical_worker_request_sha256(
                    source_sha256=str(job["source"]["sha256"]),
                    physical_page=page,
                    backend_id=backend,
                    render_sha256=str(render["render_sha256"]),
                    crop_sha256=str(binding.get("crop_sha256")),
                    region_id=str(binding.get("region_id")) if binding.get("region_id") else None,
                    coordinate_transform_id=str(binding.get("coordinate_transform_id")) if binding.get("coordinate_transform_id") else None,
                    receipt=receipt,
                ),
                "worker_response_sha256": (receipt.get("worker") or {}).get("response_sha256"),
                "worker_response_content_sha256": sha256_json({"raw_result_sha256": adapter_row.get("raw_result_sha256"), "normalized_result_sha256": adapter_row.get("normalized_result_sha256"), "result_adapter_id": result_adapter_id}),
            },
            model_receipt=model,
            config_receipt={"id": str(receipt.get("configuration_sha256", "scan-config")), "candidate_only": True, "synthetic_only": bool(job.get("synthetic_only"))},
            attributes={"candidate_only": True, "promotion": False, "verified_gold": False, "review_status": "unreviewed", "review_required": "table-location-one-reviewer; table-values-two-reviewers", "geometry_precision": "exact-from-runtime-result", "source_kind": "paddle-ppstructure-v3-table-grid"},
            uncertainty={"level": "candidate", "geometry_confirmed": False, "numeric_values_verified": False},
        )
        adapter_local_table_id = str(grid.get("table_visual_object_id") or "")
        canonical_grid = rekey_table_grid(
            grid,
            table_visual_object_id=str(table_object["visual_object_id"]),
            adapter_local_table_visual_object_id=adapter_local_table_id,
        )
        objects.append(table_object)
        if adapter_local_table_id:
            adapter_object_aliases[adapter_local_table_id] = str(table_object["visual_object_id"])
        table_grids.append(canonical_grid)
        if isinstance(binding, Mapping):
            canonical_binding = dict(binding)
            canonical_binding["table_grid_id"] = canonical_grid["table_grid_id"]
            canonical_binding["table_visual_object_id"] = canonical_grid["table_visual_object_id"]
            canonical_binding["adapter_local_table_visual_object_id"] = grid.get("table_visual_object_id")
            canonical_grid_bindings.append(canonical_binding)
    if isinstance(scan_result, dict) and scan_grids:
        scan_result["table_grids"] = [dict(row) for row in table_grids]
        scan_result["table_grid_bindings"] = canonical_grid_bindings
        scan_ir = scan_result.get("scan_ir")
        if isinstance(scan_ir, dict):
            scan_ir["table_grids"] = [dict(row) for row in table_grids]
            scan_ir["table_grid_bindings"] = [dict(row) for row in canonical_grid_bindings]
            # The scan IR fingerprint commits to table/grid identity.  Rekeying
            # crop-local PP-Structure IDs is a semantic projection, so refresh
            # that commitment before the scan package is written; otherwise the
            # persisted canonical grid would be detached from its fingerprint.
            scan_ir["input_fingerprint"] = scan_input_fingerprint(
                source_sha256=str(scan_ir.get("source_sha256", job["source"]["sha256"])),
                render_receipts=[dict(row) for row in scan_result.get("render_records", []) if isinstance(row, Mapping)],
                backend_receipts=[dict(row) for row in scan_result.get("ocr_backend_receipts", []) if isinstance(row, Mapping)],
                coordinate_transforms=[dict(row) for row in scan_result.get("coordinate_transforms", []) if isinstance(row, Mapping)],
                raster_regions=[dict(row) for row in scan_ir.get("raster_regions", []) if isinstance(row, Mapping)],
                runtime_contracts=[dict(row) for row in scan_ir.get("runtime_contracts", []) if isinstance(row, Mapping)],
                table_grids=[dict(row) for row in table_grids],
                table_grid_bindings=[dict(row) for row in canonical_grid_bindings],
                visual_candidates=[dict(row) for row in scan_result.get("visual_candidates", []) if isinstance(row, Mapping)],
                visual_gaps=[dict(row) for row in scan_result.get("visual_gaps", []) if isinstance(row, Mapping)],
                visual_conflicts=[dict(row) for row in scan_result.get("visual_conflicts", []) if isinstance(row, Mapping)],
                result_adapters=[dict(row) for row in scan_result.get("result_adapters", []) if isinstance(row, Mapping)],
            )
    for raw_conflict in scan_visual_conflicts:
        page = int(raw_conflict.get("physical_page", 0))
        if page not in page_sizes:
            continue
        raw_type = str(raw_conflict.get("conflict_type", "locator"))
        conflict_type = "overlap" if "overlap" in raw_type or "bbox" in raw_type else "type" if "type" in raw_type else "missing" if "unmatched" in raw_type else "locator"
        conflicts.append(build_visual_conflict(source_sha256=str(job["source"]["sha256"]), physical_page=page, conflict_type=conflict_type, object_ids=[adapter_object_aliases.get(str(value), str(value)) for value in raw_conflict.get("object_ids", []) if isinstance(value, str)], details={"adapter_conflict_id": raw_conflict.get("conflict_id"), "adapter_conflict_type": raw_type, "details": raw_conflict.get("details"), "review_required": True}))
    for raw_gap in scan_visual_gaps:
        page = int(raw_gap.get("physical_page", 0))
        if page not in page_sizes:
            continue
        conflicts.append(build_visual_conflict(source_sha256=str(job["source"]["sha256"]), physical_page=page, conflict_type="missing", object_ids=[str(raw_gap.get("visual_object_id"))] if raw_gap.get("visual_object_id") else [], details={"adapter_gap_id": raw_gap.get("gap_id"), "gap_type": raw_gap.get("gap_type"), "reason": raw_gap.get("reason"), "bbox": raw_gap.get("bbox"), "review_required": True}))
    # Every overlapping candidate from different parser/backend identities is
    # retained and receives an explicit conflict; no primary/challenger winner
    # is silently selected.
    for index, left in enumerate(objects):
        for right in objects[index + 1 :]:
            if left.get("physical_page") != right.get("physical_page") or left.get("type") != right.get("type"):
                continue
            left_backend = (left.get("backend_receipt") or {}).get("backend_id") or (left.get("parser_receipt") or {}).get("id")
            right_backend = (right.get("backend_receipt") or {}).get("backend_id") or (right.get("parser_receipt") or {}).get("id")
            if left_backend == right_backend or _bbox_iou(left["bbox"], right["bbox"]) < 0.20:
                continue
            conflicts.append(build_visual_conflict(source_sha256=str(job["source"]["sha256"]), physical_page=int(left["physical_page"]), conflict_type="parser", object_ids=[str(left["visual_object_id"]), str(right["visual_object_id"])], details={"parser_a": left_backend, "parser_b": right_backend, "bbox_iou": round(_bbox_iou(left["bbox"], right["bbox"]), 6), "resolution": "external-review-required"}))
    return objects, table_grids, conflicts


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
    os.chmod(path, 0o600)


def _collect_receipt_hashes(value: Any) -> list[str]:
    """Collect only explicit SHA-256 receipt fields for the review overlay."""

    hashes: set[str] = set()
    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, item in node.items():
                if (str(key).endswith("sha256") or str(key) == "sha256") and isinstance(item, str) and len(item) == 64 and all(character in "0123456789abcdef" for character in item):
                    hashes.add(item)
                walk(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)
    walk(value)
    return sorted(hashes)


def _scan_receipt_material(scan_result: Mapping[str, Any]) -> dict[str, Any]:
    scan_ir = scan_result.get("scan_ir") if isinstance(scan_result.get("scan_ir"), Mapping) else {}
    return {
        "raster_regions": scan_result.get("raster_regions", scan_ir.get("raster_regions", [])),
        "runtime_contracts": scan_result.get("runtime_contracts", scan_ir.get("runtime_contracts", [])),
        "table_grids": scan_result.get("table_grids", scan_ir.get("table_grids", [])),
        "table_grid_bindings": scan_result.get("table_grid_bindings", scan_ir.get("table_grid_bindings", [])),
        "visual_candidates": scan_result.get("visual_candidates", scan_ir.get("visual_candidates", [])),
        "visual_gaps": scan_result.get("visual_gaps", scan_ir.get("visual_gaps", [])),
        "visual_conflicts": scan_result.get("visual_conflicts", scan_ir.get("visual_conflicts", [])),
        "result_adapters": scan_result.get("result_adapters", []),
        "backend_receipts": scan_result.get("ocr_backend_receipts", []),
        "coordinate_transforms": scan_result.get("coordinate_transforms", []),
        "worker_responses": scan_result.get("ocr_worker_responses", []),
    }


def _build_visual_parser_job_unfiltered(job_path: Path) -> dict[str, Any]:
    job = _load_job(job_path)
    output = Path(str(job["output_root"])).expanduser().resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise VisualParserOrchestratorError("output_not_empty")
    output.mkdir(parents=True, exist_ok=True)
    pages = [int(value) for value in job["scope"]["pages"]]
    execution_pages = list(_SELECTIVE_EXECUTION_PAGES.get(str(job_path.expanduser().resolve()), pages))
    execution_regions = [dict(row) for row in _SELECTIVE_EXECUTION_REGIONS.get(str(job_path.expanduser().resolve()), [])]
    if not execution_pages:
        raise VisualParserOrchestratorError("visual_execution_scope_empty")
    page_sizes = _page_sizes(Path(job["source"]["path"]), pages)
    backends = _backend_list(job)
    if job.get("paused_gates"):
        raise VisualParserOrchestratorError("paused_backend_or_model_gate:" + ",".join(job["paused_gates"]))
    source = Path(job["source"]["path"])
    if not _is_contiguous_pages(execution_pages):
        raise VisualParserOrchestratorError("non_contiguous_page_selection_unsupported_for_ocr_scope")
    start_page, end_page = min(execution_pages), max(execution_pages)
    pdftoppm = job["render"].get("pdftoppm", "pdftoppm")
    preflight = compile_pdf_intermediate_representation(source, start_page, end_page, 40, "all", "pypdf", pdftoppm, int(job["render"]["dpi"]), False, False, None)
    native_pages = sorted(set(execution_pages))
    page_geometries = _page_geometries(source, execution_pages)
    render_receipts = _page_render_map(preflight, native_pages)
    visual_render_receipts = {
        page: render_receipt(source_sha256=str(row["source_sha256"]), physical_page=page, render_sha256=str(row["render_sha256"]), dpi=int((row.get("renderer") or {}).get("dpi", job["render"]["dpi"])), rotation=int(page_geometries.get(page, {}).get("rotation", 0)), renderer_version=str((row.get("renderer") or {}).get("version", "unknown")))
        for page, row in render_receipts.items()
    }
    native_bundle = extract_visual_candidates(source, pages=native_pages, pdftoppm=Path(pdftoppm) if str(pdftoppm).startswith("/") else Path(pdftoppm), render_receipts=visual_render_receipts, parser_receipt={"id": "pypdf-native-baseline", "version": "pypdf-6.10.0", "candidate_only": True, "synthetic_only": bool(job.get("synthetic_only"))}, backend_receipt={"id": "native-render-boundary", "candidate_only": True, "synthetic_only": bool(job.get("synthetic_only"))}, model_receipt={"id": "none", "available": False, "download": False, "candidate_only": True, "synthetic_only": bool(job.get("synthetic_only"))}, config_receipt={"id": str(job["backend_config_sha256"]), "candidate_only": True, "synthetic_only": bool(job.get("synthetic_only"))}, render_dpi=int(job["render"]["dpi"]))
    scan_result: dict[str, Any] | None = None
    if backends:
        selection = job["backend_selection"]
        roots = _backend_roots(job)
        selected_runtime_backends = {str(value) for value in selection.values() if value in {PADDLE_OCR_BACKEND, PADDLE_STRUCTURE_BACKEND, PADDLE_STRUCTURE_V3_BACKEND}}
        raster_for_scan = execution_regions if selected_runtime_backends else job.get("scope", {}).get("raster_regions")
        scan_result = build_scanned_pdf_ir(source, start_page, end_page, primary_backend=str(selection["primary"]), challenger_backend=selection.get("challenger"), baseline_backend=selection.get("baseline"), pdftoppm=pdftoppm, render_dpi=int(job["render"]["dpi"]), primary_model_root=roots.get(str(selection["primary"])) if selection.get("primary") else None, challenger_model_root=roots.get(str(selection["challenger"])) if selection.get("challenger") else None, baseline_model_root=roots.get(str(selection["baseline"])) if selection.get("baseline") else None, backend_config=job.get("backend_config"), fake_fixture=job.get("fake_fixture"), selected_pages=execution_pages, raster_regions=raster_for_scan)
        selected_set = set(execution_pages)
        for field in ("ocr_observations", "ocr_transcripts", "ocr_backend_receipts", "coordinate_transforms"):
            if any(int(row.get("physical_page", 0)) not in selected_set for row in scan_result.get(field, []) if isinstance(row, Mapping)):
                raise VisualParserOrchestratorError(f"scan_output_contains_unselected_page:{field}")
        if any(int(page) not in selected_set for page in scan_result.get("scan_ir", {}).get("ocr_pages", [])):
            raise VisualParserOrchestratorError("scan_output_contains_unselected_page:ocr_pages")
    objects = list(native_bundle.get("objects", []))
    relations = list(native_bundle.get("relations", []))
    grids = list(native_bundle.get("table_grids", []))
    conflicts = list(native_bundle.get("conflicts", []))
    if scan_result is not None:
        scan_objects, scan_grids, scan_conflicts = _ocr_objects(job, scan_result, page_sizes, {int(row["physical_page"]): row for row in scan_result.get("render_records", scan_result.get("render_receipts", [])) if isinstance(row, Mapping) and int(row.get("physical_page", 0)) in set(pages)}, page_geometries=page_geometries)
        objects.extend(scan_objects)
        grids.extend(scan_grids)
        conflicts.extend(scan_conflicts)
    bundle = {"schema_version": native_bundle["schema_version"], "source_sha256": job["source"]["sha256"], "pages": pages, "objects": objects, "relations": relations, "table_grids": grids, "table_continuations": native_bundle.get("table_continuations", []), "conflicts": conflicts, "render_receipts": [visual_render_receipts[page] for page in pages if page in visual_render_receipts], "candidate_only": True, "synthetic_only": bool(job.get("synthetic_only")), "whole_book_completeness_claimed": False, "orchestrator_protocol": VISUAL_PARSER_ORCHESTRATOR_PROTOCOL}
    issues = validate_visual_bundle(bundle, source_sha256=str(job["source"]["sha256"]), page_sizes=page_sizes, require_candidate_only=True)
    if issues:
        raise VisualParserOrchestratorError("visual_bundle_invalid:" + issues[0].code)
    receipt_hashes = _collect_receipt_hashes(_scan_receipt_material(scan_result)) if scan_result else []
    overlay = visual_review_overlay_manifest(source_sha256=str(job["source"]["sha256"]), objects=objects, grids=grids, relations=relations, synthetic_only=bool(job.get("synthetic_only")), receipt_hashes=receipt_hashes)
    overlay_issues = validate_visual_review_overlay_manifest(overlay, source_sha256=str(job["source"]["sha256"]), objects=objects, grids=grids, relations=relations, synthetic_only=bool(job.get("synthetic_only")), receipt_hashes=receipt_hashes)
    if overlay_issues:
        raise VisualParserOrchestratorError("visual_review_overlay_invalid:" + overlay_issues[0].code)
    visual_root = output / "visual"
    write_visual_bundle(visual_root, bundle)
    _write_json(output / "job.json", job)
    _write_json(output / "native-preflight.json", preflight)
    if scan_result is not None:
        scan_root = output / "scan-ir"
        write_scanned_pdf_ir(scan_root, scan_result)
        _write_json(output / "scan-verification.json", verify_scanned_pdf_ir(source, scan_root, pdftoppm=pdftoppm, model_roots=_backend_roots(job), backend_config=job.get("backend_config")))
    _write_json(output / "visual-bundle.json", bundle)
    _write_json(output / "visual-review-overlay.json", overlay)
    evaluation = build_evaluation_contract(predicted=bundle, gold=None, source_kind="candidate-benchmark", reason="independent_gold_not_supplied_for_runtime_pages")
    validate_visual_evaluation_contract_schema(evaluation)
    _write_json(output / "evaluation-contract.json", evaluation)
    ocr_observations = [row for row in (scan_result or {}).get("ocr_observations", []) if isinstance(row, Mapping)]
    ocr_conflicts = [row for row in (scan_result or {}).get("ocr_conflicts", []) if isinstance(row, Mapping)]
    coverage_ledger = {
        "schema_version": "tkc.visual-coverage-ledger/v0.1",
        "source_sha256": job["source"]["sha256"],
        "pages": [{"physical_page": page, "object_count": sum(1 for row in objects if int(row.get("physical_page", 0)) == page), "table_grid_count": sum(1 for row in grids if int(row.get("physical_page", 0)) == page), "relation_count": sum(1 for row in relations if int(row.get("physical_page", 0)) == page), "ocr_transcript_count": sum(1 for row in ocr_observations if int(row.get("physical_page", 0)) == page), "ocr_conflict_count": sum(1 for row in ocr_conflicts if int(row.get("physical_page", 0)) == page), "status": "candidate-only"} for page in pages],
        "bounded": True,
        "whole_book_completeness_claimed": False,
        "synthetic_only": bool(job.get("synthetic_only")),
        "ocr_transcript_count": len(ocr_observations),
        "ocr_conflict_count": len(ocr_conflicts),
        "independent_review_required": True,
        "ledger_sha256": sha256_json({"source_sha256": job["source"]["sha256"], "pages": pages, "objects": len(objects), "grids": len(grids), "relations": len(relations), "ocr_transcripts": len(ocr_observations), "ocr_conflicts": len(ocr_conflicts), "synthetic_only": bool(job.get("synthetic_only"))}),
    }
    _write_json(output / "coverage-ledger.json", coverage_ledger)
    downstream_routing = {
        "schema_version": "tkc.visual-downstream-routing/v0.1",
        "source_sha256": job["source"]["sha256"],
        "coverage_ledger": "coverage-ledger.json",
        "incremental_dag": {"status": "excluded-synthetic" if job.get("synthetic_only") else "prepared-after-manifest", "path": "dag" if not job.get("synthetic_only") else None},
        "semantic_composer": {"status": "paused", "reason": "candidate visual evidence requires independent visual review before Phase 7C composition", "promotion": False, "auto_consensus": False, "input_object_count": len(objects), "synthetic_only": bool(job.get("synthetic_only"))},
        "routing_sha256": sha256_json({"source_sha256": job["source"]["sha256"], "object_count": len(objects), "synthetic_only": bool(job.get("synthetic_only"))}),
    }
    if job.get("schema_version") == VISUAL_PARSER_JOB_SCHEMA:
        downstream_routing["protocol"] = SELECTIVE_VISUAL_PROTOCOL
        downstream_routing["coverage_ledger"] = "visual-coverage.json"
    _write_json(output / "downstream-routing.json", downstream_routing)
    component_paths = [path for path in output.rglob("*") if path.is_file() and path.name not in {"manifest.json", "state.json"}]
    manifest = {"schema_version": VISUAL_PARSER_MANIFEST_SCHEMA, "protocol": VISUAL_PARSER_ORCHESTRATOR_PROTOCOL, "compiler_version": VISUAL_PARSER_ORCHESTRATOR_COMPILER_VERSION, "job_sha256": job["job_sha256"], "source_sha256": job["source"]["sha256"], "pages": pages, "candidate_only": True, "synthetic_only": bool(job.get("synthetic_only")), "whole_book_completeness_claimed": False, "review_authoring": False, "promotion": False, "components": {path.relative_to(output).as_posix(): _hash_path(path) for path in sorted(component_paths)}, "object_count": len(objects), "relation_count": len(relations), "table_grid_count": len(grids), "conflict_count": len(conflicts), "downstream_routing": downstream_routing}
    if job.get("schema_version") == VISUAL_PARSER_JOB_SCHEMA:
        manifest.update({
            "visual_policy": job.get("visual_policy"),
            "visual_mode": job.get("visual_mode"),
            "visual_kinds": job.get("visual_kinds"),
            "visual_budget": job.get("visual_budget"),
            "visual_context": job.get("visual_context"),
            "policy_sha256": job.get("policy_sha256"),
        })
    manifest["manifest_sha256"] = sha256_json(manifest)
    validate_visual_parser_manifest_schema(manifest)
    _write_json(output / "manifest.json", manifest)
    dag_status = "excluded-synthetic"
    if not job.get("synthetic_only") and job.get("schema_version") != VISUAL_PARSER_JOB_SCHEMA:
        try:
            from incremental_build_dag import build_dag
            build_dag(output, output / "dag", generated_at=str(job.get("created_at", "1970-01-01T00:00:00Z")))
            dag_status = "built-candidate-input"
        except Exception as error:
            raise VisualParserOrchestratorError(f"incremental_dag_build_failed:{error}") from error
    _write_json(output / "state.json", {"status": "candidate-only", "paused_gates": [], "resume_intent_only": True, "review_execution": "not-run", "promotion": "forbidden", "synthetic_only": bool(job.get("synthetic_only")), "downstream": {"incremental_dag": dag_status, "semantic_composer": "paused"}, "manifest_sha256": manifest["manifest_sha256"]})
    return {"status": "candidate-only", "output_root": str(output), "manifest": manifest, "bundle": bundle, "evaluation": evaluation}


def _selective_policy_for_job(job: Mapping[str, Any]) -> VisualPolicy:
    if job.get("schema_version") != VISUAL_PARSER_JOB_SCHEMA:
        raise VisualParserOrchestratorError("legacy_visual_job_no_selective_policy")
    try:
        policy = visual_policy_from_mapping(job.get("visual_policy"))
    except VisualPolicyError as error:
        raise VisualParserOrchestratorError(str(error)) from error
    if job.get("policy_sha256") != policy.policy_sha256:
        raise VisualParserOrchestratorError("visual_policy_hash_mismatch")
    return policy


def _write_selective_json(path: Path, value: Any) -> None:
    _write_json(path, value)


def _empty_selective_bundle(job: Mapping[str, Any], pages: Sequence[int]) -> dict[str, Any]:
    return {
        "schema_version": "visual-semantics-v0.1",
        "source_sha256": job["source"]["sha256"],
        "pages": list(pages),
        "objects": [],
        "relations": [],
        "table_grids": [],
        "table_continuations": [],
        "conflicts": [],
        "render_receipts": [],
        "candidate_only": True,
        "synthetic_only": bool(job.get("synthetic_only")),
        "whole_book_completeness_claimed": False,
        "orchestrator_protocol": VISUAL_PARSER_ORCHESTRATOR_PROTOCOL,
    }


def _run_off_body_ocr(job: Mapping[str, Any], output: Path) -> dict[str, Any] | None:
    """Keep the independent PP-OCR text route alive when visual inspection is off.

    ``visual-mode=off`` suppresses visual census/render/structure/chart work.  It
    must not turn off the already-selected pages_needing_ocr text route, however.
    The scan adapter owns its own native-text-first routing and crop-only worker
    receipts, so this narrow call does not create a visual object or visual index
    entry.
    """

    selection = job.get("backend_selection") if isinstance(job.get("backend_selection"), Mapping) else {}
    if selection.get("primary") != PADDLE_OCR_BACKEND:
        return None
    if job.get("paused_gates"):
        return None
    pages = [int(value) for value in job.get("scope", {}).get("pages", [])]
    if not pages:
        return None
    source = Path(str(job["source"]["path"])).expanduser().resolve()
    roots = _backend_roots(job)
    start_page, end_page = min(pages), max(pages)
    pdftoppm = job["render"].get("pdftoppm", "pdftoppm")
    scan_result = build_scanned_pdf_ir(
        source,
        start_page,
        end_page,
        primary_backend=PADDLE_OCR_BACKEND,
        challenger_backend=selection.get("challenger"),
        baseline_backend=selection.get("baseline"),
        pdftoppm=pdftoppm,
        render_dpi=int(job["render"]["dpi"]),
        primary_model_root=roots.get(PADDLE_OCR_BACKEND),
        challenger_model_root=roots.get(str(selection.get("challenger"))) if selection.get("challenger") else None,
        baseline_model_root=roots.get(str(selection.get("baseline"))) if selection.get("baseline") else None,
        backend_config=job.get("backend_config"),
        fake_fixture=job.get("fake_fixture"),
        selected_pages=pages,
        raster_regions=None,
    )
    scan_root = output / "scan-ir"
    write_scanned_pdf_ir(scan_root, scan_result)
    verification = verify_scanned_pdf_ir(
        source,
        scan_root,
        pdftoppm=pdftoppm,
        model_roots=roots,
        backend_config=job.get("backend_config"),
    )
    _write_selective_json(output / "scan-verification.json", verification)
    return scan_result


def _chart_candidates_for_routes_legacy(
    *,
    job: Mapping[str, Any],
    routing: Mapping[str, Any],
    bundle: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Run the optional local chart adapter only on chart-candidate routes."""

    policy = visual_policy_from_mapping(job.get("visual_policy"))
    if policy.mode not in {"auto", "full"} or "chart" not in policy.kinds:
        return []
    config = job.get("backend_config") if isinstance(job.get("backend_config"), Mapping) else {}
    model_root_value = config.get("chart_model_root") or config.get("technical_chart_model_root")
    model_dirs = config.get("chart_model_dirs") if isinstance(config.get("chart_model_dirs"), Mapping) else None
    constructor_options = config.get("chart_constructor_options") if isinstance(config.get("chart_constructor_options"), Mapping) else None
    predict_options = config.get("chart_predict_options") if isinstance(config.get("chart_predict_options"), Mapping) else None
    render_by_page = {
        int(row.get("physical_page")): row
        for row in bundle.get("render_receipts", [])
        if isinstance(row, Mapping) and row.get("physical_page") is not None
    }
    objects_by_page: dict[int, list[Mapping[str, Any]]] = {}
    for row in bundle.get("objects", []) if isinstance(bundle.get("objects"), list) else []:
        if isinstance(row, Mapping) and row.get("physical_page") is not None:
            objects_by_page.setdefault(int(row["physical_page"]), []).append(row)
    candidates: list[dict[str, Any]] = []
    for route in routing.get("routes", []) if isinstance(routing.get("routes"), list) else []:
        if not isinstance(route, Mapping) or "chart" not in [str(kind) for kind in route.get("candidate_kinds", [])] or route.get("status") not in {"chart-candidate", "manual-review"}:
            continue
        page = int(route.get("physical_page", 0))
        render = render_by_page.get(page, {})
        render_sha = str(render.get("render_sha256") or sha256_json({"source_sha256": job["source"]["sha256"], "physical_page": page, "dpi": job["render"]["dpi"], "renderer": "pdftoppm"}))
        page_objects = objects_by_page.get(page, [])
        chart_object = next((row for row in page_objects if str(row.get("type")) in {"chart", "plot", "figure"}), None)
        region = next((row for row in route.get("regions", []) if isinstance(row, Mapping)), {})
        bbox = chart_object.get("bbox") if chart_object is not None else region.get("bbox_pdf_points")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            bbox = [0.0, 0.0, 1.0, 1.0]
        crop_sha = str(chart_object.get("crop_sha256")) if chart_object is not None and chart_object.get("crop_sha256") else sha256_json({"render_sha256": render_sha, "bbox_pdf_points": list(bbox), "physical_page": page, "kind": "chart"})
        candidate = build_chart_candidate(
            source_sha256=str(job["source"]["sha256"]),
            physical_page=page,
            bbox_pdf_points=[float(value) for value in bbox],
            render_sha256=render_sha,
            crop_sha256=crop_sha,
            model_root=model_root_value,
            model_dirs={str(key): str(value) for key, value in model_dirs.items()} if model_dirs else None,
            constructor_options=constructor_options,
            predict_options=predict_options,
        )
        candidates.append(candidate)
    return sorted(candidates, key=lambda row: (int(row.get("physical_page", 0)), str(row.get("candidate_sha256", row.get("status", "")))))


def _chart_candidates_for_routes(
    *,
    job: Mapping[str, Any],
    routing: Mapping[str, Any],
    bundle: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Execute ChartParsing only for chart route crops, fail closed otherwise."""

    policy = visual_policy_from_mapping(job.get("visual_policy"))
    if policy.mode not in {"auto", "full"} or "chart" not in policy.kinds:
        return []
    config = job.get("backend_config") if isinstance(job.get("backend_config"), Mapping) else {}
    legacy_profile = str(job.get("compiler_version", "")).startswith("0.13.0-")
    chart_profile_id = LEGACY_TECHNICAL_CHART_PROFILE_ID if legacy_profile else TECHNICAL_CHART_PROFILE_ID
    chart_profile_protocol = LEGACY_TECHNICAL_CHART_PROFILE_PROTOCOL if legacy_profile else TECHNICAL_CHART_PROFILE_PROTOCOL
    model_root_value = config.get("chart_model_root") or config.get("technical_chart_model_root")
    model_root = Path(str(model_root_value)).expanduser().resolve() if model_root_value else None
    model_dirs = {"chart_model_dir": "PP-Chart2Table"}
    constructor_options = config.get("chart_constructor_options") if isinstance(config.get("chart_constructor_options"), Mapping) else ({"device": "cpu", "enable_hpi": False} if legacy_profile else {"device": "cpu", "enable_hpi": False, "engine": "paddle_dynamic"})
    predict_options = config.get("chart_predict_options") if isinstance(config.get("chart_predict_options"), Mapping) else ({} if legacy_profile else {"batch_size": 1})
    chart_runtime = config.get("chart_external_runtime")
    if chart_runtime is None and isinstance(config.get("external_runtime"), Mapping):
        chart_runtime = config["external_runtime"].get(PADDLE_CHART_BACKEND)
    render_by_page = {
        int(row.get("physical_page")): row
        for row in bundle.get("render_receipts", [])
        if isinstance(row, Mapping) and row.get("physical_page") is not None
    }
    route_rows: list[dict[str, Any]] = []
    for route in routing.get("routes", []) if isinstance(routing.get("routes"), list) else []:
        if not isinstance(route, Mapping) or "chart" not in [str(kind) for kind in route.get("candidate_kinds", [])] or route.get("status") not in {"chart-candidate", "manual-review"}:
            continue
        page = int(route.get("physical_page", 0))
        for index, raw_region in enumerate(route.get("regions", []) if isinstance(route.get("regions"), list) else []):
            if not isinstance(raw_region, Mapping):
                continue
            region = dict(raw_region)
            region.setdefault("region_id", f"chart-route-{page}-{index}")
            region["physical_page"] = page
            region.setdefault("coordinate_space", COORDINATE_SPACE)
            region.setdefault("source_anchor", {"source_sha256": str(job["source"]["sha256"]), "physical_page": page, "region_id": region["region_id"]})
            route_rows.append(region)
    if not route_rows:
        return []
    def candidate_without_runtime(status: str, region: Mapping[str, Any], *, render_sha: str | None = None, crop_sha: str | None = None, runtime_contract: Mapping[str, Any] | None = None, runtime_qualification: Mapping[str, Any] | None = None, worker_receipt: Mapping[str, Any] | None = None) -> dict[str, Any]:
        raw_bbox = region.get("bbox_pdf_points")
        if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
            raise VisualParserOrchestratorError("chart_route_bbox_required")
        try:
            bbox = [float(value) for value in raw_bbox]
            page = int(region.get("physical_page", 0))
        except (TypeError, ValueError):
            raise VisualParserOrchestratorError("chart_route_anchor_invalid") from None
        if page < 1 or any(bbox[index] >= bbox[index + 2] for index in (0, 1)):
            raise VisualParserOrchestratorError("chart_route_anchor_invalid")
        anchor = region.get("source_anchor") if isinstance(region.get("source_anchor"), Mapping) else {
            "source_sha256": str(job["source"]["sha256"]),
            "physical_page": page,
            "region_id": str(region.get("region_id") or "chart-route"),
        }
        return build_chart_candidate(
            source_sha256=str(job["source"]["sha256"]),
            physical_page=page,
            bbox_pdf_points=bbox,
            render_sha256=render_sha,
            crop_sha256=crop_sha,
            model_root=model_root,
            model_dirs=model_dirs,
            constructor_options=constructor_options,
            predict_options=predict_options,
            runtime_contract=runtime_contract,
            runtime_qualification=runtime_qualification,
            worker_receipt=worker_receipt,
            runtime_status=status,
            source_anchor=anchor,
            evidence_stage="planned-route-no-render",
            profile_id=chart_profile_id,
            profile_protocol=chart_profile_protocol,
        )
    if model_root is None or not (model_root / model_dirs["chart_model_dir"]).is_dir() or not any(path.is_file() for path in (model_root / model_dirs["chart_model_dir"]).rglob("*")):
        return sorted([candidate_without_runtime("not-run-missing-models", region) for region in route_rows], key=lambda row: (int(row.get("physical_page", 0)), str(row.get("candidate_sha256"))))
    if not isinstance(chart_runtime, Mapping):
        return sorted([candidate_without_runtime("not-run-runtime-unavailable", region) for region in route_rows], key=lambda row: (int(row.get("physical_page", 0)), str(row.get("candidate_sha256"))))
    if legacy_profile:
        chart_configuration = {
            "backend_id": PADDLE_CHART_BACKEND,
            "network_policy": "offline-enforced",
            "preprocessing": {},
            "recognition": {},
            "user_config": {},
            "backend_options": {
                "api": "paddleocr.chart_parsing",
                "device": "cpu",
                "profile_id": chart_profile_id,
                "model_dirs": model_dirs,
                "model_names": {"model_name": "PP-Chart2Table"},
                "constructor_options": dict(constructor_options),
                "predict_options": dict(predict_options),
                "allow_model_download": False,
            },
            "network_enabled": False,
            "model_download": False,
        }
    else:
        chart_configuration = qualification_configuration(PADDLE_CHART_BACKEND)
    runtime_spec = dict(chart_runtime)
    runtime_spec.setdefault("model_root", str(model_root))
    try:
        chart_contract = freeze_external_runtime_contract(
            runtime_spec,
            backend_id=PADDLE_CHART_BACKEND,
            configuration=chart_configuration,
            worker_path=Path(__file__).with_name("ocr_backend_worker.py").resolve(),
            probe=True,
        )
    except PaddleRuntimeContractError:
        return sorted([candidate_without_runtime("not-run-runtime-unavailable", region) for region in route_rows], key=lambda row: (int(row.get("physical_page", 0)), str(row.get("candidate_sha256"))))
    chart_qualification = _runtime_qualification(config, PADDLE_CHART_BACKEND)
    if not legacy_profile:
        try:
            if chart_qualification is None:
                raise PaddleRuntimeQualificationError("runtime_qualification_missing")
            validate_runtime_qualification(
                chart_qualification,
                contract=chart_contract,
                backend_id=PADDLE_CHART_BACKEND,
                profile_id=TECHNICAL_CHART_PROFILE_ID,
                require_qualified=True,
            )
        except PaddleRuntimeQualificationError:
            return sorted([candidate_without_runtime("paused-runtime-unqualified", region, runtime_contract=chart_contract, runtime_qualification=chart_qualification) for region in route_rows], key=lambda row: (int(row.get("physical_page", 0)), str(row.get("candidate_sha256"))))
    pages = sorted({int(region["physical_page"]) for region in route_rows})
    try:
        run = _run_chart_region_backend(
            source_path=Path(str(job["source"]["path"])).expanduser().resolve(),
            source_sha256=str(job["source"]["sha256"]),
            regions=route_rows,
            model_root=model_root,
            configuration=chart_configuration,
            pdftoppm=job["render"].get("pdftoppm", "pdftoppm"),
            render_dpi=int(job["render"]["dpi"]),
            page_geometries=_page_geometry_receipts(Path(str(job["source"]["path"])).expanduser().resolve(), pages),
            expected_render_receipts=render_by_page,
            workspace_root=Path(str(job["output_root"])).expanduser().resolve() / ".chart-workspace",
            runtime_contract=chart_contract,
            total_timeout_seconds=float(config.get("chart_total_timeout_seconds", 900.0)),
            per_request_timeout_seconds=float(config.get("chart_per_request_timeout_seconds", 180.0)),
        )
    except ScanOcrError:
        return sorted([candidate_without_runtime("not-run-runtime-unavailable", region) for region in route_rows], key=lambda row: (int(row.get("physical_page", 0)), str(row.get("candidate_sha256"))))
    candidates: list[dict[str, Any]] = []
    for chart_run in run.get("chart_runs", []) if isinstance(run.get("chart_runs"), list) else []:
        binding = chart_run.get("binding") if isinstance(chart_run.get("binding"), Mapping) else {}
        response = chart_run.get("response") if isinstance(chart_run.get("response"), Mapping) else {}
        adapter = chart_run.get("adapter") if isinstance(chart_run.get("adapter"), Mapping) else None
        error_code = str(chart_run.get("error_code") or "") or None
        if chart_run.get("status") == "candidate" and adapter is not None:
            status = None
        elif adapter is not None:
            chart_payload = adapter.get("chart") if isinstance(adapter.get("chart"), Mapping) else {}
            adapter_rows = adapter.get("chart_rows", chart_payload.get("value_candidates", []))
            status = "paused-chart-result-empty" if not isinstance(adapter_rows, list) or not adapter_rows else "paused-chart-result-shape"
        elif "shape" in (error_code or "") or "adapter" in (error_code or ""):
            status = "paused-chart-result-shape"
        else:
            status = "paused-chart-runtime-error"
        binding_bbox = chart_run.get("bbox_pdf_points", binding.get("bbox_pdf_points"))
        if not isinstance(binding_bbox, (list, tuple)) or len(binding_bbox) != 4:
            raise VisualParserOrchestratorError("chart_result_bbox_missing")
        page = int(chart_run.get("physical_page", binding.get("physical_page", 0)))
        render_sha = chart_run.get("render_sha256", binding.get("render_sha256"))
        crop_sha = chart_run.get("crop_sha256", binding.get("crop_sha256"))
        if page < 1 or not isinstance(render_sha, str) or not isinstance(crop_sha, str):
            raise VisualParserOrchestratorError("chart_result_binding_missing")
        candidates.append(build_chart_candidate(
            source_sha256=str(job["source"]["sha256"]),
            physical_page=page,
            bbox_pdf_points=[float(value) for value in binding_bbox],
            render_sha256=render_sha,
            crop_sha256=crop_sha,
            model_root=model_root,
            model_dirs=model_dirs,
            constructor_options=constructor_options,
            predict_options=predict_options,
            chart_result=adapter,
            runtime_contract=run.get("runtime_contract"),
            runtime_qualification=chart_qualification,
            worker_receipt=response.get("worker_receipt"),
            runtime_status=status,
            source_anchor=binding.get("source_anchor") if isinstance(binding.get("source_anchor"), Mapping) else None,
            evidence_stage="rendered-crop",
            error_code=error_code,
            profile_id=chart_profile_id,
            profile_protocol=chart_profile_protocol,
        ))
    return sorted(candidates, key=lambda row: (int(row.get("physical_page", 0)), str(row.get("candidate_sha256", row.get("status", "")))))


def _finalize_selective_artifacts(
    *,
    job: Mapping[str, Any],
    output: Path,
    census: Mapping[str, Any],
    routing: Mapping[str, Any],
    budget_receipt: Mapping[str, Any],
    bundle: Mapping[str, Any],
    evaluation: Mapping[str, Any] | None = None,
    chart_candidates: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Write source-free selective artifacts and close the v0.4 manifest."""

    policy = _selective_policy_for_job(job)
    objects = [dict(row) for row in bundle.get("objects", []) if isinstance(row, Mapping)]
    charts = [dict(row) for row in chart_candidates if isinstance(row, Mapping)]
    index = build_visual_index(source_sha256=str(job["source"]["sha256"]), policy=policy, census=census, routing=routing, objects=objects, chart_candidates=charts)
    coverage = build_visual_coverage(source_sha256=str(job["source"]["sha256"]), policy=policy, routing=routing, budget_receipt=budget_receipt)
    index_issues = validate_visual_index(index, source_sha256=str(job["source"]["sha256"]))
    if index_issues:
        raise VisualParserOrchestratorError("visual_index_invalid:" + index_issues[0])
    _write_selective_json(output / "visual-census.json", census)
    _write_selective_json(output / "visual-routing.json", routing)
    _write_selective_json(output / "visual-budget-receipt.json", budget_receipt)
    _write_selective_json(output / "visual-index.json", index)
    _write_selective_json(output / "visual-coverage.json", coverage)
    if charts:
        _write_selective_json(output / "chart-candidates.json", charts)
    if evaluation is None:
        evaluation = build_evaluation_contract(predicted=bundle, gold=None, source_kind="candidate-benchmark", reason="selective_visual_candidate_or_policy_pause")
    _write_selective_json(output / "evaluation-contract.json", evaluation)
    policy_record = dict(job["visual_policy"])
    # Recompute the closed source manifest after adding the selective records.
    manifest_path = output / "manifest.json"
    if manifest_path.is_file():
        manifest = _read_json(manifest_path)
    else:
        manifest = {
            "schema_version": VISUAL_PARSER_MANIFEST_SCHEMA,
            "protocol": VISUAL_PARSER_ORCHESTRATOR_PROTOCOL,
            "compiler_version": VISUAL_PARSER_ORCHESTRATOR_COMPILER_VERSION,
            "job_sha256": job["job_sha256"],
            "source_sha256": job["source"]["sha256"],
            "pages": list(job["scope"]["pages"]),
            "candidate_only": True,
            "synthetic_only": bool(job.get("synthetic_only")),
            "whole_book_completeness_claimed": False,
            "review_authoring": False,
            "promotion": False,
            "object_count": len(objects),
            "relation_count": len(bundle.get("relations", [])),
            "table_grid_count": len(bundle.get("table_grids", [])),
            "conflict_count": len(bundle.get("conflicts", [])),
            "downstream_routing": {"schema_version": "tkc.visual-downstream-routing/v0.1", "source_sha256": job["source"]["sha256"], "coverage_ledger": "visual-coverage.json", "incremental_dag": {"status": "not-built", "path": None}, "semantic_composer": {"status": "paused", "reason": "selective-visual-review-required", "promotion": False, "auto_consensus": False, "input_object_count": len(objects), "synthetic_only": bool(job.get("synthetic_only"))}, "routing_sha256": routing.get("routing_sha256")},
        }
    manifest.update({
        "schema_version": VISUAL_PARSER_MANIFEST_SCHEMA,
        "protocol": VISUAL_PARSER_ORCHESTRATOR_PROTOCOL,
        "compiler_version": VISUAL_PARSER_ORCHESTRATOR_COMPILER_VERSION,
        "job_sha256": job["job_sha256"],
        "source_sha256": job["source"]["sha256"],
        "pages": list(job["scope"]["pages"]),
        "visual_policy": policy_record,
        "visual_mode": policy.mode,
        "visual_kinds": list(policy.kinds),
        "visual_budget": policy.budget.to_dict(),
        "visual_context": policy.context,
        "policy_sha256": policy.policy_sha256,
        "visual_census": "visual-census.json",
        "visual_routing": "visual-routing.json",
        "visual_budget_receipt": "visual-budget-receipt.json",
        "visual_index": "visual-index.json",
        "visual_coverage": "visual-coverage.json",
        "candidate_only": True,
        "promotion": False,
        "review_authoring": False,
        "whole_book_completeness_claimed": False,
        "object_count": len(objects),
        "relation_count": len(bundle.get("relations", [])),
        "table_grid_count": len(bundle.get("table_grids", [])),
        "conflict_count": len(bundle.get("conflicts", [])),
    })
    downstream = dict(manifest.get("downstream_routing") or {})
    downstream["protocol"] = SELECTIVE_VISUAL_PROTOCOL
    downstream["coverage_ledger"] = "visual-coverage.json"
    downstream["routing_sha256"] = routing.get("routing_sha256")
    manifest["downstream_routing"] = downstream
    if charts:
        manifest["chart_candidates"] = "chart-candidates.json"
    if (output / "scan-ir" / "scan-ir.json").is_file():
        manifest["text_ocr"] = "scan-ir/scan-ir.json"
    component_paths = [path for path in output.rglob("*") if path.is_file() and path.name not in {"manifest.json", "state.json"}]
    manifest["components"] = {path.relative_to(output).as_posix(): _hash_path(path) for path in sorted(component_paths)}
    manifest.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = sha256_json(manifest)
    validate_visual_parser_manifest_schema(manifest)
    _write_selective_json(manifest_path, manifest)
    dag_status = "excluded-synthetic"
    if not job.get("synthetic_only"):
        try:
            from incremental_build_dag import build_dag
            build_dag(output, output / "dag", generated_at=str(job.get("created_at", "1970-01-01T00:00:00Z")))
            dag_status = "built-selective-candidate-input"
        except Exception as error:
            raise VisualParserOrchestratorError(f"incremental_dag_build_failed:{error}") from error
    state = {
        "status": "paused-budget-exhausted" if budget_receipt.get("status") == "paused-budget-exhausted" else "candidate-only",
        "paused_gates": ["budget-exhausted"] if budget_receipt.get("status") == "paused-budget-exhausted" else [],
        "resume_intent_only": True,
        "review_execution": "not-run",
        "promotion": "forbidden",
        "executable": False,
        "synthetic_only": bool(job.get("synthetic_only")),
        "manifest_sha256": manifest["manifest_sha256"],
        "policy_sha256": policy.policy_sha256,
        "downstream": {"incremental_dag": dag_status, "semantic_composer": "paused"},
    }
    _write_selective_json(output / "state.json", state)
    return {"status": state["status"], "output_root": str(output), "manifest": manifest, "bundle": dict(bundle), "evaluation": dict(evaluation), "census": dict(census), "routing": dict(routing), "budget_receipt": dict(budget_receipt), "index": index, "coverage": coverage}


def _build_selective_empty(job_path: Path, *, census: Mapping[str, Any], routing: Mapping[str, Any], budget_receipt: Mapping[str, Any]) -> dict[str, Any]:
    job = _load_job(job_path)
    output = Path(str(job["output_root"])).expanduser().resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise VisualParserOrchestratorError("output_not_empty")
    output.mkdir(parents=True, exist_ok=True)
    bundle = _empty_selective_bundle(job, job["scope"]["pages"])
    issues = validate_visual_bundle(bundle, source_sha256=str(job["source"]["sha256"]), page_sizes=_page_sizes(Path(job["source"]["path"]), job["scope"]["pages"]), require_candidate_only=True)
    if issues:
        raise VisualParserOrchestratorError("visual_bundle_invalid:" + issues[0].code)
    _write_selective_json(output / "job.json", job)
    _write_selective_json(output / "visual-bundle.json", bundle)
    _write_selective_json(output / "visual-review-overlay.json", visual_review_overlay_manifest(source_sha256=str(job["source"]["sha256"]), objects=[], grids=[], relations=[], synthetic_only=bool(job.get("synthetic_only")), receipt_hashes=[]))
    if job.get("visual_mode") == "off":
        try:
            _run_off_body_ocr(job, output)
        except (ScanOcrError, OSError, ValueError) as error:
            raise VisualParserOrchestratorError(f"off_body_ocr_failed:{error}") from error
    return _finalize_selective_artifacts(job=job, output=output, census=census, routing=routing, budget_receipt=budget_receipt, bundle=bundle)


def build_visual_parser_job(job_path: Path) -> dict[str, Any]:
    """Build a legacy v0.3 job or the v0.4 selective visual job."""

    job = _load_job(job_path)
    if job.get("schema_version") != VISUAL_PARSER_JOB_SCHEMA:
        return _build_visual_parser_job_unfiltered(job_path)
    policy = _selective_policy_for_job(job)
    source = Path(str(job["source"]["path"])).expanduser().resolve()
    pages = [int(value) for value in job["scope"]["pages"]]
    if policy.mode == "off":
        census = policy_only_census(str(job["source"]["sha256"]), pages, policy_sha256=policy.policy_sha256)
    else:
        census = run_visual_census(source, pages=pages, visual_kinds=policy.kinds, policy_sha256=policy.policy_sha256)
    routing = route_visual_census(census, policy=policy, selected_pages=pages)
    routes = [row for row in routing.get("routes", []) if isinstance(row, Mapping)]
    execution_pages = [int(row["physical_page"]) for row in routes if row.get("status") in {"table-candidate", "chart-candidate", "manual-review"} and row.get("regions")]
    execution_regions: list[dict[str, Any]] = []
    for route in routes:
        if route.get("status") not in {"table-candidate", "chart-candidate", "manual-review"}:
            continue
        page = int(route.get("physical_page", 0))
        for index, raw_region in enumerate(route.get("regions", []) if isinstance(route.get("regions"), list) else []):
            if not isinstance(raw_region, Mapping):
                continue
            region = dict(raw_region)
            region.setdefault("region_id", f"route-region-{page}-{index}")
            region["physical_page"] = page
            region.setdefault("coordinate_space", COORDINATE_SPACE)
            region.setdefault("source_anchor", {"source_sha256": str(job["source"]["sha256"]), "physical_page": page, "region_id": region["region_id"]})
            execution_regions.append(region)
    region_count = len(execution_regions)
    paused_budget = any(row.get("status") == "budget-exhausted" for row in routes)
    budget_receipt = build_visual_budget_receipt(policy=policy, source_sha256=str(job["source"]["sha256"]), routes=routes, processed_pages=len(execution_pages), processed_regions=region_count, calls=0, paused=paused_budget, pause_reason="budget-exhausted" if paused_budget else None)
    if not execution_pages:
        return _build_selective_empty(job_path, census=census, routing=routing, budget_receipt=budget_receipt)
    job_key = str(job_path.expanduser().resolve())
    _SELECTIVE_EXECUTION_PAGES[job_key] = sorted(set(execution_pages))
    _SELECTIVE_EXECUTION_REGIONS[job_key] = execution_regions
    started = time.monotonic()
    try:
        result = _build_visual_parser_job_unfiltered(job_path)
    finally:
        _SELECTIVE_EXECUTION_PAGES.pop(job_key, None)
        _SELECTIVE_EXECUTION_REGIONS.pop(job_key, None)
    output = Path(result["output_root"])
    elapsed = max(0.0, time.monotonic() - started)
    budget_receipt = build_visual_budget_receipt(
        policy=policy,
        source_sha256=str(job["source"]["sha256"]),
        routes=routes,
        processed_pages=len(execution_pages),
        processed_regions=region_count,
        elapsed_seconds=elapsed,
        calls=region_count,
        objects=len(result.get("bundle", {}).get("objects", [])) if isinstance(result.get("bundle"), Mapping) else 0,
        paused=paused_budget,
        pause_reason="budget-exhausted" if paused_budget else None,
    )
    if budget_receipt.get("status") == "paused-budget-exhausted" and not any(row.get("status") == "budget-exhausted" for row in routes if isinstance(row, Mapping)):
        rewritten_routes: list[dict[str, Any]] = []
        for row in routes:
            rewritten = dict(row)
            if rewritten.get("status") in {"table-candidate", "chart-candidate", "manual-review"}:
                rewritten["status"] = "budget-exhausted"
                rewritten["reason"] = "max_visual_seconds"
                rewritten["regions"] = []
                rewritten["routing_sha256"] = selective_sha256_json({key: value for key, value in rewritten.items() if key != "routing_sha256"})
            rewritten_routes.append(rewritten)
        routing = dict(routing)
        routing["routes"] = rewritten_routes
        routing["routing_sha256"] = selective_sha256_json({key: value for key, value in routing.items() if key != "routing_sha256"})
        budget_receipt = build_visual_budget_receipt(
            policy=policy,
            source_sha256=str(job["source"]["sha256"]),
            routes=rewritten_routes,
            processed_pages=len(execution_pages),
            processed_regions=region_count,
            elapsed_seconds=elapsed,
            calls=region_count,
            objects=len(result.get("bundle", {}).get("objects", [])) if isinstance(result.get("bundle"), Mapping) else 0,
            paused=True,
            pause_reason="max_visual_seconds",
        )
    charts = _chart_candidates_for_routes(job=job, routing=routing, bundle=result["bundle"])
    return _finalize_selective_artifacts(job=job, output=output, census=census, routing=routing, budget_receipt=budget_receipt, bundle=result["bundle"], evaluation=result.get("evaluation"), chart_candidates=charts)


def validate_visual_parser_job(job_path: Path) -> dict[str, Any]:
    job = _load_job(job_path)
    output = Path(str(job["output_root"])).expanduser().resolve()
    failures: list[str] = []
    runtime_unqualified = False
    manifest_path = output / "manifest.json"
    if not manifest_path.is_file():
        paused_gates = job.get("paused_gates", [])
        return {
            "passed": False,
            "status": "paused" if paused_gates else "planned",
            "failures": ["manifest_missing"],
            "paused_gates": paused_gates,
        }
    manifest = _read_json(manifest_path)
    try:
        validate_visual_parser_manifest_schema(manifest)
    except VisualParserOrchestratorError as error:
        failures.append(str(error))
    if manifest.get("manifest_sha256") != sha256_json({key: value for key, value in manifest.items() if key != "manifest_sha256"}):
        failures.append("manifest_hash_mismatch")
    for relative, expected in (manifest.get("components") or {}).items():
        path = output / relative
        if Path(relative).name == "manifest.json" or not path.is_file() or _hash_path(path) != expected:
            failures.append(f"component_hash_mismatch:{relative}")
    try:
        bundle = _read_json(output / "visual-bundle.json")
        issues = validate_visual_bundle(bundle, source_sha256=str(job["source"]["sha256"]), page_sizes=_page_sizes(Path(job["source"]["path"]), job["scope"]["pages"]), require_candidate_only=True)
        failures.extend(f"visual:{issue.code}" for issue in issues)
    except (OSError, VisualParserOrchestratorError, VisualSemanticsError) as error:
        failures.append(f"visual_bundle_load:{error}")
    try:
        validate_visual_evaluation_contract_schema(_read_json(output / "evaluation-contract.json"))
    except (OSError, VisualParserOrchestratorError) as error:
        failures.append(f"evaluation_contract:{error}")
    try:
        overlay = _read_json(output / "visual-review-overlay.json")
        bundle = _read_json(output / "visual-bundle.json")
        receipt_material: dict[str, Any] = {"overlay": {"receipt_hashes": []}}
        scan_root = output / "scan-ir"
        if (scan_root / "scan-ir.json").is_file():
            scan_ir = _read_json(scan_root / "scan-ir.json")
            receipt_material["scan_ir"] = {key: scan_ir.get(key) for key in ("raster_regions", "runtime_contracts", "table_grids", "table_grid_bindings", "visual_candidates", "visual_gaps", "visual_conflicts")}
            for name in ("ocr-backend-receipts.jsonl", "coordinate-transforms.jsonl", "ocr-worker-responses.jsonl", "paddle-result-adapters.jsonl"):
                if (scan_root / name).is_file():
                    receipt_material[name] = _jsonl_rows(scan_root / name)
        overlay_issues = validate_visual_review_overlay_manifest(overlay, source_sha256=str(job["source"]["sha256"]), objects=bundle.get("objects", []), grids=bundle.get("table_grids", []), relations=bundle.get("relations", []), synthetic_only=bool(job.get("synthetic_only")), receipt_hashes=_collect_receipt_hashes(receipt_material))
        failures.extend(f"overlay:{issue.code}" for issue in overlay_issues)
    except (OSError, VisualParserOrchestratorError, VisualSemanticsError) as error:
        failures.append(f"overlay_load:{error}")
    if manifest.get("candidate_only") is not True or manifest.get("review_authoring") is not False or manifest.get("promotion") is not False:
        failures.append("capability_escalation")
    if manifest.get("synthetic_only") is not bool(job.get("synthetic_only")):
        failures.append("synthetic_only_manifest_mismatch")
    if job.get("schema_version") == VISUAL_PARSER_JOB_SCHEMA:
        try:
            policy = _selective_policy_for_job(job)
            census = _read_json(output / "visual-census.json")
            routing = _read_json(output / "visual-routing.json")
            budget = _read_json(output / "visual-budget-receipt.json")
            index = _read_json(output / "visual-index.json")
            coverage = _read_json(output / "visual-coverage.json")
            if census.get("schema_version") != SELECTIVE_VISUAL_CENSUS_SCHEMA or routing.get("schema_version") != SELECTIVE_VISUAL_ROUTING_SCHEMA:
                failures.append("selective_visual_contract_schema_invalid")
            failures.extend(f"visual_census:{issue}" for issue in validate_visual_census(census, source_sha256=str(job["source"]["sha256"])))
            failures.extend(f"visual_routing:{issue}" for issue in validate_visual_routing(routing, source_sha256=str(job["source"]["sha256"]), policy_sha256=policy.policy_sha256))
            failures.extend(f"visual_budget:{issue}" for issue in validate_visual_budget_receipt(budget, source_sha256=str(job["source"]["sha256"]), policy_sha256=policy.policy_sha256))
            failures.extend(f"visual_coverage:{issue}" for issue in validate_visual_coverage(coverage, source_sha256=str(job["source"]["sha256"]), policy_sha256=policy.policy_sha256))
            if budget.get("policy_sha256") != policy.policy_sha256 or index.get("policy_sha256") != policy.policy_sha256 or coverage.get("policy_sha256") != policy.policy_sha256:
                failures.append("selective_visual_policy_binding_invalid")
            failures.extend(f"visual_index:{issue}" for issue in validate_visual_index(index, source_sha256=str(job["source"]["sha256"])))
            if budget.get("status") == "paused-budget-exhausted" and not any(row.get("coverage") == "budget-exhausted" for row in coverage.get("pages", []) if isinstance(row, Mapping)):
                failures.append("budget_pause_gap_missing")
            if index.get("contains_raw_images") is not False or index.get("contains_crop_bytes") is not False or index.get("contains_ocr_fulltext") is not False or index.get("contains_absolute_source_path") is not False or index.get("contains_credentials") is not False:
                failures.append("visual_index_privacy_contract_invalid")
            chart_reference = _read_json(output / "manifest.json").get("chart_candidates")
            if chart_reference is not None:
                chart_rows = _read_json_value(output / str(chart_reference))
                if not isinstance(chart_rows, list):
                    failures.append("chart_candidates_shape_invalid")
                else:
                    expected_chart_schema = "tkc.technical-chart-candidate/v0.1" if str(job.get("compiler_version", "")).startswith("0.13.0-") else "tkc.technical-chart-candidate/v0.2"
                    for chart in chart_rows:
                        if isinstance(chart, Mapping) and chart.get("status") == "paused-runtime-unqualified":
                            runtime_unqualified = True
                        if not isinstance(chart, Mapping) or chart.get("schema_version") != expected_chart_schema or chart.get("candidate_only") is not True or chart.get("promotion") is not False or chart.get("executable") is not False:
                            failures.append("chart_candidate_contract_invalid")
                        elif chart.get("candidate_sha256") != selective_sha256_json({key: value for key, value in chart.items() if key != "candidate_sha256"}):
                            failures.append("chart_candidate_hash_mismatch")
                        elif expected_chart_schema.endswith("v0.2") and chart.get("status") in {"candidate", "paused-chart-runtime-error", "paused-chart-result-empty", "paused-chart-result-shape"} and not isinstance(chart.get("runtime_qualification_sha256"), str):
                            failures.append("chart_runtime_qualification_missing")
        except (OSError, VisualParserOrchestratorError, json.JSONDecodeError) as error:
            failures.append(f"selective_visual_artifact_load:{error}")
    try:
        if _read_json(output / "visual-bundle.json").get("synthetic_only") is not bool(job.get("synthetic_only")):
            failures.append("synthetic_only_bundle_mismatch")
    except (OSError, VisualParserOrchestratorError):
        pass
    status = "blocked" if failures else "candidate-only"
    if not failures and job.get("schema_version") == VISUAL_PARSER_JOB_SCHEMA:
        try:
            if _read_json(output / "visual-budget-receipt.json").get("status") == "paused-budget-exhausted":
                status = "paused-budget-exhausted"
        except (OSError, VisualParserOrchestratorError):
            pass
    if not failures and runtime_unqualified:
        status = "paused-runtime-unqualified"
    return {"passed": not failures, "status": status, "failures": sorted(set(failures)), "paused_gates": job.get("paused_gates", []), "manifest_sha256": manifest.get("manifest_sha256")}


def inspect_visual_parser_job(job_path: Path) -> dict[str, Any]:
    job = _load_job(job_path)
    validation = validate_visual_parser_job(job_path)
    record = {"job_sha256": job["job_sha256"], "source_sha256": job["source"]["sha256"], "scope": job["scope"], "backend_selection": job["backend_selection"], "state": job["state"], "paused_gates": job.get("paused_gates", []), "validation": validation}
    if job.get("schema_version") == VISUAL_PARSER_JOB_SCHEMA:
        record.update({"visual_mode": job.get("visual_mode"), "visual_kinds": job.get("visual_kinds"), "visual_budget": job.get("visual_budget"), "visual_context": job.get("visual_context"), "policy_sha256": job.get("policy_sha256")})
    return record


def resume_visual_parser_job(job_path: Path) -> dict[str, Any]:
    job = _load_job(job_path)
    if job.get("paused_gates"):
        return {"status": "paused", "resume_intent_only": True, "paused_gates": job["paused_gates"], "review_execution": "forbidden", "promotion": "forbidden"}
    output = Path(str(job["output_root"])).expanduser().resolve()
    if job.get("schema_version") == VISUAL_PARSER_JOB_SCHEMA and (output / "visual-budget-receipt.json").is_file():
        try:
            budget = _read_json(output / "visual-budget-receipt.json")
            if budget.get("status") == "paused-budget-exhausted":
                return {"status": "paused", "resume_intent_only": True, "paused_gates": ["budget-exhausted"], "policy_sha256": job.get("policy_sha256"), "review_execution": "forbidden", "promotion": "forbidden", "seal": "forbidden", "executable": False}
        except (OSError, VisualParserOrchestratorError):
            pass
    if job.get("schema_version") == VISUAL_PARSER_JOB_SCHEMA and (output / "chart-candidates.json").is_file():
        try:
            chart_rows = _read_json_value(output / "chart-candidates.json")
            if isinstance(chart_rows, list) and any(isinstance(row, Mapping) and row.get("status") == "paused-runtime-unqualified" for row in chart_rows):
                return {"status": "paused", "resume_intent_only": True, "paused_gates": ["runtime-qualification"], "review_execution": "forbidden", "promotion": "forbidden", "seal": "forbidden", "executable": False}
        except (OSError, VisualParserOrchestratorError):
            pass
    if (output / "manifest.json").is_file():
        return inspect_visual_parser_job(job_path)
    result = build_visual_parser_job(job_path)
    return {"status": result["status"], "resume_intent_only": True, "output_root": result["output_root"], "manifest_sha256": result["manifest"]["manifest_sha256"], "review_execution": "forbidden", "promotion": "forbidden"}


def _parse_pages(values: Sequence[str] | None) -> list[int] | None:
    if not values:
        return None
    pages: list[int] = []
    for value in values:
        for token in str(value).split(","):
            pages.append(int(token))
    return sorted(set(pages))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--source", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--job", type=Path)
    plan.add_argument("--page", action="append")
    plan.add_argument("--backend", default="none", choices=["none", *BACKEND_SPECS.keys()])
    plan.add_argument("--challenger", choices=list(BACKEND_SPECS))
    plan.add_argument("--baseline", choices=list(BACKEND_SPECS))
    plan.add_argument("--model-root", type=Path)
    plan.add_argument("--challenger-model-root", type=Path)
    plan.add_argument("--baseline-model-root", type=Path)
    plan.add_argument("--backend-config", type=Path)
    plan.add_argument("--raster-regions", type=Path, help="JSON explicit PDF raster-region contract.")
    plan.add_argument("--external-runtime", type=Path, help="JSON external Paddle interpreter/model/worker contract spec.")
    plan.add_argument("--runtime-qualification", type=Path, help="Host-local Paddle qualification receipt or backend-to-receipt JSON map.")
    plan.add_argument("--pdftoppm", default="pdftoppm")
    plan.add_argument("--render-dpi", type=int, default=150)
    plan.add_argument("--created-at", default="1970-01-01T00:00:00Z")
    plan.add_argument("--visual-mode", choices=("off", "auto", "full"), default="auto", help="Selective visual policy (default: auto).")
    plan.add_argument("--visual-kinds", action="append", help="Comma-separated table,figure,chart,diagram,equation kinds.")
    plan.add_argument("--max-visual-pages", type=int)
    plan.add_argument("--max-visual-regions", type=int)
    plan.add_argument("--max-visual-seconds", type=float)
    plan.add_argument("--visual-context", choices=("none", "index", "on-demand"), default="index")
    plan.add_argument("--test-only-synthetic", action="store_true", help="Explicitly authorize the test-only fake backend; never use for production PDFs.")
    plan.add_argument("--fake-fixture", type=Path)
    for name in ("build", "validate", "status", "resume"):
        command = sub.add_parser(name)
        command.add_argument("--job", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            config = _read_json(args.backend_config) if args.backend_config else {}
            external_runtime = _read_json(args.external_runtime) if args.external_runtime else None
            runtime_qualification = _read_json(args.runtime_qualification) if args.runtime_qualification else None
            if runtime_qualification is not None:
                if runtime_qualification.get("schema_version") == "tkc.paddle-runtime-qualification/v0.1":
                    qualification_backend = runtime_qualification.get("backend_id")
                    if not isinstance(qualification_backend, str):
                        raise VisualParserOrchestratorError("runtime_qualification_backend_missing")
                    config["runtime_qualifications"] = {qualification_backend: runtime_qualification}
                else:
                    config["runtime_qualifications"] = runtime_qualification
            fixture = _read_json(args.fake_fixture) if args.fake_fixture else None
            raster_regions = _read_json_value(args.raster_regions) if args.raster_regions else None
            if raster_regions is not None and not isinstance(raster_regions, list):
                raise VisualParserOrchestratorError("raster_regions_json_array_required")
            roots = {}
            if args.model_root: roots[args.backend] = args.model_root
            if args.challenger_model_root and args.challenger: roots[args.challenger] = args.challenger_model_root
            if args.baseline_model_root and args.baseline: roots[args.baseline] = args.baseline_model_root
            output_path = args.output.expanduser().resolve()
            job_path = (args.job or output_path.with_name(f"{output_path.name}.visual-parser-job.json")).expanduser().resolve()
            if _path_overlap(args.source, job_path) or _path_overlap(output_path, job_path):
                raise VisualParserOrchestratorError("input_output_path_overlap")
            visual_kinds = None
            if args.visual_kinds:
                visual_kinds = [token.strip() for value in args.visual_kinds for token in str(value).split(",") if token.strip()]
            job = create_visual_parser_job(source=args.source, output=output_path, pages=_parse_pages(args.page), primary_backend=args.backend, challenger_backend=args.challenger, baseline_backend=args.baseline, model_roots=roots, backend_config=config, external_runtime=external_runtime, fake_fixture=fixture, test_mode=args.test_only_synthetic, test_only_authorized=args.test_only_synthetic, synthetic_source=args.test_only_synthetic, raster_regions=raster_regions, pdftoppm=args.pdftoppm, render_dpi=args.render_dpi, created_at=args.created_at, visual_mode=args.visual_mode, visual_kinds=visual_kinds, max_visual_pages=args.max_visual_pages, max_visual_regions=args.max_visual_regions, max_visual_seconds=args.max_visual_seconds, visual_context=args.visual_context)
            write_visual_parser_job(job_path, job)
            print(json.dumps({"status": job["state"], "job": str(job_path), "job_sha256": job["job_sha256"], "paused_gates": job["paused_gates"]}, ensure_ascii=False, sort_keys=True))
        elif args.command == "build":
            result = build_visual_parser_job(args.job)
            print(json.dumps({"status": result["status"], "output_root": result["output_root"], "manifest_sha256": result["manifest"]["manifest_sha256"]}, ensure_ascii=False, sort_keys=True))
        elif args.command == "validate":
            result = validate_visual_parser_job(args.job)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0 if result.get("passed") else 1
        elif args.command == "status":
            print(json.dumps(inspect_visual_parser_job(args.job), ensure_ascii=False, sort_keys=True))
        else:
            result = resume_visual_parser_job(args.job)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (VisualParserOrchestratorError, ScanOcrError, VisualSemanticsError, VisualAdapterError, OSError, ValueError) as error:
        print(f"error: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "VisualParserOrchestratorError",
    "create_visual_parser_job",
    "write_visual_parser_job",
    "build_visual_parser_job",
    "validate_visual_parser_job",
    "inspect_visual_parser_job",
    "resume_visual_parser_job",
    "validate_visual_parser_job_schema",
    "validate_visual_parser_manifest_schema",
    "main",
]
