#!/usr/bin/env python3
"""Offline, candidate-only Portable Scan Gold Review workbench.

This is a thin scan-specific companion to ``reviewer_workbench.py``.  It
freezes only local page renders/crops and a deterministic, model-independent
full-region, page-stratified text-sample rule.  A browser submission is human form data; an operator-supplied
external attestation is required before a private revision is accepted for
evaluation.  The module never runs OCR, loads a model, calls MCP, or sends data
to a network service.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
import unicodedata
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

from compiler_version import (
    SCAN_GOLD_REVIEW_ATTESTATION_SCHEMA,
    SCAN_GOLD_REVIEW_COMPILER_VERSION,
    SCAN_GOLD_REVIEW_EVALUATION_SCHEMA,
    SCAN_GOLD_REVIEW_OCR_GOLD_SCHEMA,
    SCAN_GOLD_REVIEW_PLAN_SCHEMA,
    SCAN_GOLD_REVIEW_PREDICTION_SCHEMA,
    SCAN_GOLD_REVIEW_PROFILE_SCHEMA,
    SCAN_GOLD_REVIEW_PROTOCOL,
    SCAN_GOLD_REVIEW_REVISION_SCHEMA,
    SCAN_GOLD_REVIEW_STRUCTURE_GOLD_SCHEMA,
    SCAN_GOLD_REVIEW_SUBMISSION_SCHEMA,
    SCAN_GOLD_REVIEW_WORKBENCH_MANIFEST_SCHEMA,
    SCANNED_PDF_SPLIT_GEOMETRY_COMPILER_VERSION,
    SCANNED_PDF_SPLIT_GEOMETRY_PROTOCOL,
    SCANNED_PDF_SPLIT_GEOMETRY_SCHEMA,
)
from expert_skill_contract import sha256_file
from incremental_build_dag import sha256_json
from paddle_runtime_contract import (
    PaddleRuntimeContractError,
    build_crop_coordinate_transform,
    build_crop_mapping,
    crop_render_png,
    rotate_png_for_ocr,
)
from reviewer_workbench import (
    ReviewerWorkbenchError,
    _assert_no_symlink_components,
    _forbidden_key_present,
    _hash_without,
    _read_json,
    _read_jsonl,
    _relative_instance_file,
    _safe_id,
    _secure_directory,
    _secure_file,
    _write_json,
    _write_jsonl,
)
from scanned_pdf_ocr import (
    ScanOcrError,
    _page_geometry_receipts,
    _png_dimensions,
    _render_pages,
    _rotate_crop_transform,
    build_spread_split_contract,
    validate_spread_split_contract,
)


COORDINATE_SPACE = "pdf-page-top-left-points-v1"
SAMPLE_RULE_ID = "region-full-stratified-pages-v2"
OCR_PAGE_TARGET = 12
DEFAULT_ROTATION = 90
DEFAULT_RENDER_DPI = 200
STRUCTURE_STATUSES = {"complete", "abnormal", "unreadable", "needs_review"}
OCR_STATUSES = {"complete", "unreadable", "not_text", "needs_review"}
LABEL_STATES = {"confirmed", "unreadable", "not_text", "needs_review"}
READABILITY_STATES = {"readable", "unreadable", "not_text", "needs_review"}
ABNORMAL_STATES = {"none", "skew", "crop_loss", "occlusion", "fold", "blur", "other", "needs_review"}
ROTATIONS = {0, 90, 180, 270}
DECLARATION_KEYS = (
    "qualified_reviewer",
    "reviewed_specified_source_evidence",
    "did_not_view_hidden_results",
    "did_not_change_split",
    "reviewed_specified_render_region",
    "not_evaluator",
    "independent_from_proposer",
)
BATCH_CONFIRM_FIELDS = {"rotation", "boundary", "region_order"}
BATCH_PASS_FIELDS = {
    "review_status",
    "rotation",
    "boundary",
    "region_order",
    "page_readability",
    "page_abnormal_state",
    "region_bbox",
    "region_label_state",
    "region_readability",
    "region_abnormal_state",
}
SAFE_HASH = re.compile(r"^[0-9a-f]{64}$")
NUMBER_TOKEN = re.compile(
    r"(?<![\w])(?:[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?%?)(?![\w])"
)


class ScanGoldReviewError(ReviewerWorkbenchError):
    """Stable fail-closed error for the scan Gold workflow."""


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and bool(SAFE_HASH.fullmatch(value))


def _private_bytes(path: Path, data: bytes) -> None:
    _assert_no_symlink_components(path)
    created_parents: list[Path] = []
    cursor = path.parent
    while not cursor.exists():
        created_parents.append(cursor)
        cursor = cursor.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    for parent in created_parents:
        os.chmod(parent, 0o700)
    os.chmod(path.parent, 0o700)
    if path.exists() or path.is_symlink():
        raise ScanGoldReviewError("asset_destination_already_exists")
    path.write_bytes(data)
    os.chmod(path, 0o600)


def _copy_private_file(source: Path, destination: Path) -> None:
    source = _secure_file(source, "private_input")
    _assert_no_symlink_components(destination)
    created_parents: list[Path] = []
    cursor = destination.parent
    while not cursor.exists():
        created_parents.append(cursor)
        cursor = cursor.parent
    destination.parent.mkdir(parents=True, exist_ok=True)
    for parent in created_parents:
        os.chmod(parent, 0o700)
    os.chmod(destination.parent, 0o700)
    if destination.exists() or destination.is_symlink():
        raise ScanGoldReviewError("private_copy_destination_exists")
    shutil.copyfile(source, destination)
    os.chmod(destination, 0o600)
    if sha256_file(destination) != sha256_file(source):
        raise ScanGoldReviewError("private_copy_hash_mismatch")


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _valid_bbox(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(_finite_number(number) for number in value)
        and float(value[2]) > float(value[0])
        and float(value[3]) > float(value[1])
    )


def _bbox_within(value: Any, width: float, height: float) -> bool:
    return _valid_bbox(value) and float(value[0]) >= 0 and float(value[1]) >= 0 and float(value[2]) <= width and float(value[3]) <= height


def _bbox_equal(left: Any, right: Any, tolerance: float = 1e-6) -> bool:
    return _valid_bbox(left) and _valid_bbox(right) and all(abs(float(a) - float(b)) <= tolerance for a, b in zip(left, right))


def _item_hash(item: Mapping[str, Any]) -> str:
    return sha256_json({key: value for key, value in item.items() if key != "item_sha256"})


def _plan_hash(plan: Mapping[str, Any]) -> str:
    return _hash_without(plan, "plan_sha256")


def _manifest_hash(manifest: Mapping[str, Any]) -> str:
    return _hash_without(manifest, "manifest_sha256")


def _row_hash(row: Mapping[str, Any]) -> str:
    return _hash_without(row, "row_sha256")


def _profile() -> dict[str, Any]:
    """Return the frozen v0.2 profile; changing it requires a new identity."""

    return {
        "schema_version": SCAN_GOLD_REVIEW_PROFILE_SCHEMA,
        "protocol": SCAN_GOLD_REVIEW_PROTOCOL,
        "compiler_version": SCAN_GOLD_REVIEW_COMPILER_VERSION,
        "profile_id": "scan-gold-v0.2",
        "profile_version": "0.2",
        "scope": {
            "physical_page_unit": "1-based physical PDF page",
            "one_structure_item_per_physical_page": True,
            "one_ocr_item_per_selected_full_region": True,
            "candidate_independent_sampling": True,
        },
        "structure": {
            "rotation_values_clockwise": [0, 90, 180, 270],
            "selected_rotation_is_explicit": True,
            "split_axis": "x",
            "split_boundary_rule": "floor(rotated_width_px/2)",
            "region_count": 2,
            "region_order_values": ["left-to-right", "right-to-left"],
            "locked_label_state": "candidate",
            "review_label_states": ["confirmed", "unreadable", "not_text", "needs_review"],
            "readability_values": sorted(READABILITY_STATES),
            "abnormal_values": sorted(ABNORMAL_STATES),
            "page_status_values": sorted(STRUCTURE_STATUSES),
            "excluded_items_stay_in_denominator": True,
        },
        "ocr": {
            "sample_rule_id": SAMPLE_RULE_ID,
            "sampling_page_target": OCR_PAGE_TARGET,
            "sampling_page_rule": "evenly-spaced-index-round-half-up-v1",
            "slots_per_selected_region": 1,
            "slots": [
                {"sample_order": 0, "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0},
            ],
            "selection_inputs": ["ordered_physical_page_scope", "sampling_page_target", "region_order", "full_region_dimensions_px"],
            "selection_uses_engine_signal": False,
            "selection_uses_candidate_text": False,
            "canonical_text_required_for": "complete",
            "decision_values": sorted(OCR_STATUSES),
            "excluded_reason_required_for": ["unreadable", "not_text", "needs_review"],
            "coverage_definition": "included_complete_full_regions / all_selected_full_region_items",
            "empty_text_is_not_an_exclusion": True,
        },
        "evaluation": {
            "threshold_status": "acceptance-target-candidate",
            "thresholds": {
                "structure_page_exact_match_min": 1.0,
                "structure_coverage_min": 0.90,
                "ocr_cer_max": 0.05,
                "ocr_wer_max": 0.10,
                "numeric_token_accuracy_min": 0.95,
                "ocr_sample_coverage_min": 0.90,
                "excluded_ratio_max": 0.10,
            },
            "thresholds_are_frozen": True,
            "test_results_cannot_change_thresholds": True,
            "overall_verdict": "not-authorized",
            "accuracy_claim": False,
        },
        "privacy": {
            "network_enabled": False,
            "external_resources": False,
            "source_pdf_included": False,
            "hidden_result_fields_included": False,
            "compiler_authors_gold": False,
            "release_included": False,
        },
    }


def validate_scan_gold_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    expected = _profile()
    if dict(profile) != expected:
        raise ScanGoldReviewError("scan_gold_profile_drift")
    return deepcopy(expected)


def scan_gold_profile_identity(profile: Mapping[str, Any] | None = None) -> dict[str, Any]:
    value = validate_scan_gold_profile(profile or _profile())
    return {
        "schema_version": value["schema_version"],
        "protocol": value["protocol"],
        "compiler_version": value["compiler_version"],
        "profile_id": value["profile_id"],
        "profile_version": value["profile_version"],
        "profile_sha256": sha256_json(value),
    }


def _normalized_sample_bbox(dimensions: Mapping[str, Any], slot: Mapping[str, Any]) -> list[int]:
    width = int(dimensions["width"])
    height = int(dimensions["height"])
    left = max(0, min(width - 1, math.floor(float(slot["x0"]) * width)))
    top = max(0, min(height - 1, math.floor(float(slot["y0"]) * height)))
    right = max(left + 1, min(width, math.ceil(float(slot["x1"]) * width)))
    bottom = max(top + 1, min(height, math.ceil(float(slot["y1"]) * height)))
    return [left, top, right, bottom]


def _stratified_ocr_pages(pages: Sequence[int], target: int = OCR_PAGE_TARGET) -> list[int]:
    """Select deterministic, evenly spaced physical pages without model input."""

    ordered = list(pages)
    if not ordered or target < 1:
        raise ScanGoldReviewError("scan_gold_ocr_page_sampling_invalid")
    if len(ordered) <= target:
        return ordered
    last = len(ordered) - 1
    indexes = [math.floor((index * last / (target - 1)) + 0.5) for index in range(target)]
    selected = [ordered[index] for index in indexes]
    if len(selected) != target or len(set(selected)) != target:
        raise ScanGoldReviewError("scan_gold_ocr_page_sampling_not_unique")
    return selected


def _matrix_point(matrix: Sequence[Any], x: float, y: float) -> list[float]:
    if len(matrix) != 9:
        raise ScanGoldReviewError("sample_coordinate_matrix_invalid")
    denominator = float(matrix[6]) * x + float(matrix[7]) * y + float(matrix[8])
    if abs(denominator) < 1e-12:
        raise ScanGoldReviewError("sample_coordinate_matrix_singular")
    return [round((float(matrix[0]) * x + float(matrix[1]) * y + float(matrix[2])) / denominator, 8), round((float(matrix[3]) * x + float(matrix[4]) * y + float(matrix[5])) / denominator, 8)]


def _mapped_bbox(matrix: Sequence[Any], bbox: Sequence[Any]) -> list[float]:
    points = [_matrix_point(matrix, float(x), float(y)) for x, y in ((bbox[0], bbox[1]), (bbox[2], bbox[1]), (bbox[2], bbox[3]), (bbox[0], bbox[3]))]
    return [round(min(point[0] for point in points), 8), round(min(point[1] for point in points), 8), round(max(point[0] for point in points), 8), round(max(point[1] for point in points), 8)]


def _context_mapping(
    geometry: Mapping[str, Any],
    original_dimensions: Mapping[str, Any],
    context_dimensions: Mapping[str, Any],
    rotation: int,
    *,
    source_sha256: str,
    physical_page: int,
    render_sha256: str,
    context_sha256: str,
    render_dpi: int,
) -> dict[str, Any]:
    """Bind the full Context render to the exact PDF top-left transform.

    The reviewer draws against the rotated full-page raster.  Mapping its
    pixel *edges* through the original full-page crop transform and then the
    explicit content-rotation transform keeps odd raster dimensions
    deterministic and permits a corrected box to extend beyond a candidate
    region.  This is deliberately the same contract family used by split
    regions; no DPI adjustment is used to hide a geometry mismatch.
    """

    try:
        context_pdf_bbox = [0.0, 0.0, float(geometry["width"]), float(geometry["height"])]
        full_mapping = build_crop_mapping(
            bbox_pdf_points=context_pdf_bbox,
            page_geometry=geometry,
            render_dimensions_px=original_dimensions,
            render_dpi=render_dpi,
            allow_mediabox_bbox=True,
        )
        transform = build_crop_coordinate_transform(
            source_sha256=source_sha256,
            physical_page=physical_page,
            render_sha256=render_sha256,
            render_dpi=render_dpi,
            mapping=full_mapping,
        )
        transform = _rotate_crop_transform(
            transform,
            degrees_clockwise=rotation,
            original_dimensions=full_mapping["crop_dimensions_px"],
            rotated_dimensions=context_dimensions,
        )
    except (KeyError, TypeError, ValueError, PaddleRuntimeContractError, ScanOcrError) as error:
        raise ScanGoldReviewError(f"scan_gold_context_mapping_invalid:{physical_page}") from error
    preprocessing = dict(transform.get("preprocessing") or {})
    preprocessing["crop_sha256"] = context_sha256
    preprocessing["pre_rotation_crop_sha256"] = render_sha256
    preprocessing["source_anchor"] = {
        "anchor_id": f"sca-{sha256_json({'source_sha256': source_sha256, 'physical_page': physical_page, 'context_sha256': context_sha256})[:20]}",
        "anchor_kind": "physical-page-context-render",
        "physical_page": physical_page,
        "source_sha256": source_sha256,
    }
    transform["preprocessing"] = preprocessing
    transform["id"] = f"pct-{sha256_json({key: value for key, value in transform.items() if key not in {'schema_version', 'id'}})[:20]}"
    return {
        "status": "verified",
        "coordinate_space": COORDINATE_SPACE,
        "context_pdf_bbox": context_pdf_bbox,
        "page_width_points": round(context_pdf_bbox[2], 8),
        "page_height_points": round(context_pdf_bbox[3], 8),
        "source_dimensions_px": {"width": int(original_dimensions["width"]), "height": int(original_dimensions["height"])},
        "context_dimensions_px": {"width": int(context_dimensions["width"]), "height": int(context_dimensions["height"])},
        "content_rotation_clockwise": rotation,
        "mapping_rule": "full-page-render-pixel-edge-to-pdf-four-corner-v1",
        "transform": transform,
        "transform_sha256": sha256_json(transform),
    }


def _page30_cross_check(actual: Mapping[str, Any], contract_path: Path | None, artifacts_path: Path | None, source_sha256: str) -> dict[str, Any]:
    if contract_path is None and artifacts_path is None:
        return {"status": "not-supplied", "physical_page": 30, "source_sha256": source_sha256}
    if contract_path is None or artifacts_path is None:
        raise ScanGoldReviewError("page30_split_cross_check_requires_contract_and_artifacts")
    expected = _read_json(_secure_file(contract_path, "split_evidence_contract"), "split_evidence_contract")
    try:
        validate_spread_split_contract(expected, require_bound=False)
    except ScanOcrError as error:
        raise ScanGoldReviewError(f"page30_split_evidence_invalid:{error}") from error
    if expected.get("physical_page") != 30 or expected.get("source_sha256") != source_sha256 or expected.get("rotation") != DEFAULT_ROTATION:
        raise ScanGoldReviewError("page30_split_evidence_identity_mismatch")
    if actual.get("render", {}).get("sha256") != expected.get("render", {}).get("sha256") or actual.get("rotation") != expected.get("rotation") or actual.get("split", {}).get("boundary_px") != expected.get("split", {}).get("boundary_px") or actual.get("split", {}).get("region_order") != expected.get("split", {}).get("region_order"):
        raise ScanGoldReviewError("page30_split_evidence_core_or_render_mismatch")
    actual_regions = sorted(actual.get("regions", []), key=lambda row: int(row.get("region_order", -1)))
    expected_regions = sorted(expected.get("regions", []), key=lambda row: int(row.get("region_order", -1)))
    if len(actual_regions) != 2 or len(expected_regions) != 2:
        raise ScanGoldReviewError("page30_split_evidence_region_count_mismatch")
    compare_fields = ("region_id", "region_order", "rotated_region_bbox_px", "bbox_pdf_points", "crop_sha256", "crop_dimensions_px")
    for left, right in zip(actual_regions, expected_regions):
        if any(left.get(field) != right.get(field) for field in compare_fields):
            raise ScanGoldReviewError(f"page30_split_evidence_region_mismatch:{left.get('region_order')}")
        left_transform = left.get("coordinate_transform") if isinstance(left.get("coordinate_transform"), Mapping) else {}
        right_transform = right.get("coordinate_transform") if isinstance(right.get("coordinate_transform"), Mapping) else {}
        for field in ("source_sha256", "physical_page", "render_sha256", "render_dpi", "source_space", "target_space", "image_dimensions_px", "pdf_dimensions_points", "matrix_source"):
            if left_transform.get(field) != right_transform.get(field):
                raise ScanGoldReviewError(f"page30_split_evidence_transform_mismatch:{left.get('region_order')}:{field}")
        for field in ("matrix_3x3", "inverse_matrix_3x3"):
            left_values, right_values = left_transform.get(field), right_transform.get(field)
            if not isinstance(left_values, list) or not isinstance(right_values, list) or len(left_values) != len(right_values) or any(abs(float(a) - float(b)) > 1e-6 for a, b in zip(left_values, right_values)):
                raise ScanGoldReviewError(f"page30_split_evidence_transform_matrix_mismatch:{left.get('region_order')}:{field}")
    artifacts = _read_json(_secure_file(artifacts_path, "split_evidence_artifacts"), "split_evidence_artifacts")
    if artifacts.get("source_sha256") != source_sha256 or artifacts.get("physical_page") != 30:
        raise ScanGoldReviewError("page30_split_artifacts_identity_mismatch")
    if artifacts.get("render_original", {}).get("sha256") != actual.get("render", {}).get("sha256") or artifacts.get("render_rotated", {}).get("sha256") != actual.get("rotated_render", {}).get("sha256"):
        raise ScanGoldReviewError("page30_split_artifacts_render_mismatch")
    artifact_regions = sorted(artifacts.get("regions", []), key=lambda row: int(row.get("region_order", -1)))
    if len(artifact_regions) != 2 or any(row.get("sha256") != region.get("crop_sha256") for row, region in zip(artifact_regions, actual_regions)):
        raise ScanGoldReviewError("page30_split_artifacts_region_mismatch")
    return {
        "status": "passed",
        "physical_page": 30,
        "source_sha256": source_sha256,
        "legacy_contract_sha256": expected["contract_sha256"],
        "legacy_split_core_sha256": expected["split_core_sha256"],
        "geometry_contract_sha256": actual.get("contract_sha256"),
        "geometry_split_core_sha256": actual.get("split_core_sha256"),
        "artifacts_manifest_sha256": artifacts.get("contract_sha256"),
        "artifacts_file_sha256": sha256_file(artifacts_path),
    }


def generate_scan_gold_workbench(
    source: Path,
    output: Path,
    pdftoppm: Path | str,
    *,
    start_page: int = 1,
    end_page: int = 40,
    rotation: int = DEFAULT_ROTATION,
    render_dpi: int = DEFAULT_RENDER_DPI,
    reviewer_id: str | None = None,
    review_session_id: str | None = None,
    proposer_instance: str | None = None,
    created_at: str = "2026-08-28T00:00:00+08:00",
    split_contract: Path | None = None,
    split_artifacts: Path | None = None,
) -> dict[str, Any]:
    source = _secure_file(Path(source).expanduser(), "source_pdf")
    if reviewer_id is None or proposer_instance is None or review_session_id is None:
        raise ScanGoldReviewError("scan_gold_reviewer_session_and_proposer_must_be_explicit")
    reviewer_id = _safe_id(reviewer_id, "reviewer_id")
    proposer_instance = _safe_id(proposer_instance, "proposer_instance")
    if start_page < 1 or end_page < start_page:
        raise ScanGoldReviewError("scan_gold_page_range_invalid")
    if rotation not in ROTATIONS:
        raise ScanGoldReviewError("scan_gold_rotation_invalid")
    if render_dpi < 36 or render_dpi > 1200:
        raise ScanGoldReviewError("scan_gold_render_dpi_invalid")
    try:
        from pypdf import PdfReader

        page_count = len(PdfReader(str(source)).pages)
    except Exception as error:  # pragma: no cover - environment-specific parser failures
        raise ScanGoldReviewError("scan_gold_pdf_unreadable") from error
    if end_page > page_count:
        raise ScanGoldReviewError("scan_gold_page_out_of_range")
    pages = list(range(start_page, end_page + 1))
    if not pages:
        raise ScanGoldReviewError("scan_gold_page_range_empty")
    ocr_pages = _stratified_ocr_pages(pages)
    ocr_page_set = set(ocr_pages)
    profile = _profile()
    profile_identity = scan_gold_profile_identity(profile)
    source_sha256 = sha256_file(source)
    destination = _secure_directory(Path(output).expanduser(), must_be_empty=True)
    geometry_by_page = _page_geometry_receipts(source, pages)
    asset_rows: list[dict[str, Any]] = []
    structure_items: list[dict[str, Any]] = []
    ocr_items: list[dict[str, Any]] = []
    split_contracts: list[dict[str, Any]] = []
    render_receipts: list[dict[str, Any]] = []
    with _render_pages(source, pages, pdftoppm, dpi=render_dpi, pdf_dimensions=geometry_by_page) as (render_records, render_paths):
        for render_record in render_records:
            page = int(render_record["physical_page"])
            original_bytes = render_paths[page].read_bytes()
            original_dimensions = _png_dimensions(original_bytes)
            if original_dimensions is None:
                raise ScanGoldReviewError(f"scan_gold_render_invalid:{page}")
            render_receipt = deepcopy(render_record)
            render_receipt["dimensions_px"] = original_dimensions
            render_receipts.append(render_receipt)
            rotated = rotate_png_for_ocr(original_bytes, rotation)
            try:
                contract = build_spread_split_contract(
                    source_sha256=source_sha256,
                    physical_page=page,
                    page_geometry=geometry_by_page[page],
                    render_receipt=render_record,
                    render_bytes=original_bytes,
                    render_dpi=render_dpi,
                    content_rotation_clockwise=rotation,
                    printed_page_label_candidates=[None, None],
                    contract_schema=SCANNED_PDF_SPLIT_GEOMETRY_SCHEMA,
                    contract_protocol=SCANNED_PDF_SPLIT_GEOMETRY_PROTOCOL,
                    contract_compiler_version=SCANNED_PDF_SPLIT_GEOMETRY_COMPILER_VERSION,
                )
                validate_spread_split_contract(contract, require_bound=False)
            except ScanOcrError as error:
                raise ScanGoldReviewError(f"scan_gold_split_contract_invalid:{page}:{error}") from error
            split_contracts.append(contract)
            if page == 30:
                page30_cross_check = _page30_cross_check(contract, split_contract, split_artifacts, source_sha256)
            context_relative = f"assets/context/page-{page:04d}.png"
            _private_bytes(destination / context_relative, rotated["bytes"])
            asset_rows.append({"path": context_relative, "kind": "context", "physical_page": page, "sha256": rotated["sha256"], "dimensions_px": rotated["dimensions_px"]})
            region_rows: list[dict[str, Any]] = []
            sample_rows_for_page: list[dict[str, Any]] = []
            ordered_regions = sorted(contract["regions"], key=lambda row: int(row["region_order"]))
            for region in ordered_regions:
                order = int(region["region_order"])
                region_name = "left" if order == 0 else "right"
                region_relative = f"assets/regions/page-{page:04d}-{region_name}.png"
                region_crop = crop_render_png(rotated["bytes"], {"render_dimensions_px": rotated["dimensions_px"], "render_pixel_bbox": list(region["rotated_region_bbox_px"])}, expected_crop_sha256=str(region["crop_sha256"]))
                _private_bytes(destination / region_relative, region_crop["bytes"])
                asset_rows.append({"path": region_relative, "kind": "region", "physical_page": page, "region_id": region["region_id"], "sha256": region_crop["sha256"], "dimensions_px": region_crop["dimensions_px"]})
                region_asset = {"asset": region_relative, "sha256": region_crop["sha256"], "dimensions_px": region_crop["dimensions_px"]}
                region_rows.append({
                    "region_id": region["region_id"],
                    "region_order": order,
                    "reading_order": "left-to-right",
                    "printed_page_label_candidate": None,
                    "printed_page_label_confirmed": False,
                    "rotated_bbox_px": list(region["rotated_region_bbox_px"]),
                    "bbox_pdf_points": list(region["bbox_pdf_points"]),
                    "coordinate_space": COORDINATE_SPACE,
                    "coordinate_transform_sha256": region["coordinate_transform_sha256"],
                    "crop": region_asset,
                })
                region_dimensions = region_crop["dimensions_px"]
                matrix = region["coordinate_transform"]["matrix_3x3"]
                for slot in profile["ocr"]["slots"] if page in ocr_page_set else []:
                    sample_order = int(slot["sample_order"])
                    sample_bbox_px = _normalized_sample_bbox(region_dimensions, slot)
                    sample_crop = crop_render_png(region_crop["bytes"], {"render_dimensions_px": region_dimensions, "render_pixel_bbox": sample_bbox_px}, expected_crop_sha256=None)
                    sample_id = f"scan-ocr-p{page:04d}-r{order}-s{sample_order:02d}"
                    sample_relative = f"assets/text-samples/page-{page:04d}-{region_name}-sample-{sample_order:02d}.png"
                    _private_bytes(destination / sample_relative, sample_crop["bytes"])
                    asset_rows.append({"path": sample_relative, "kind": "text-sample", "physical_page": page, "region_id": region["region_id"], "sample_id": sample_id, "sha256": sample_crop["sha256"], "dimensions_px": sample_crop["dimensions_px"]})
                    sample_item = {
                        "item_id": sample_id,
                        "item_type": "ocr_text_sample",
                        "physical_page": page,
                        "region_id": region["region_id"],
                        "region_order": order,
                        "sample_order": sample_order,
                        "locked_facts": {
                            "source_sha256": source_sha256,
                            "sample_rule_id": SAMPLE_RULE_ID,
                            "normalized_bbox": [slot["x0"], slot["y0"], slot["x1"], slot["y1"]],
                            "sample_bbox_px": sample_bbox_px,
                            "sample_bbox_pdf_points": _mapped_bbox(matrix, sample_bbox_px),
                            "sample_bbox_sha256": sha256_json({"sample_bbox_px": sample_bbox_px, "sample_bbox_pdf_points": _mapped_bbox(matrix, sample_bbox_px), "coordinate_transform_sha256": region["coordinate_transform_sha256"]}),
                            "coordinate_space": COORDINATE_SPACE,
                            "split_contract_sha256": contract["contract_sha256"],
                            "coordinate_transform_sha256": region["coordinate_transform_sha256"],
                            "context": {"asset": context_relative, "sha256": rotated["sha256"], "dimensions_px": rotated["dimensions_px"]},
                            "region": {"asset": region_relative, "sha256": region_crop["sha256"], "dimensions_px": region_crop["dimensions_px"]},
                            "sample": {"asset": sample_relative, "sha256": sample_crop["sha256"], "dimensions_px": sample_crop["dimensions_px"]},
                        },
                    }
                    sample_item["item_sha256"] = _item_hash(sample_item)
                    sample_rows_for_page.append(sample_item)
            context_mapping = _context_mapping(
                geometry_by_page[page],
                original_dimensions,
                rotated["dimensions_px"],
                rotation,
                source_sha256=source_sha256,
                physical_page=page,
                render_sha256=render_record["render_sha256"],
                context_sha256=rotated["sha256"],
                render_dpi=render_dpi,
            )
            structure_item: dict[str, Any] = {
                "item_id": f"scan-structure-page-{page:04d}",
                "item_type": "structure_page",
                "physical_page": page,
                "locked_facts": {
                    "source_sha256": source_sha256,
                    "original_render": {"sha256": render_record["render_sha256"], "dimensions_px": original_dimensions, "dpi": render_dpi},
                    "context": {"asset": context_relative, "sha256": rotated["sha256"], "dimensions_px": rotated["dimensions_px"], "rotation": rotation},
                    "context_coordinate_mapping": context_mapping,
                    "rotation_clockwise_candidate": rotation,
                    "split_contract_sha256": contract["contract_sha256"],
                    "split_core_sha256": contract["split_core_sha256"],
                    "split": {"axis": "x", "boundary_px": contract["split"]["boundary_px"], "region_count": 2, "reading_order": "left-to-right", "odd_dimension_policy": contract["split"]["odd_dimension_policy"]},
                    "regions": region_rows,
                },
            }
            structure_item["item_sha256"] = _item_hash(structure_item)
            structure_items.append(structure_item)
            ocr_items.extend(sample_rows_for_page)
    if 30 in pages:
        page30_cross_check = locals().get("page30_cross_check", {"status": "not-supplied", "physical_page": 30, "source_sha256": source_sha256})
    else:
        page30_cross_check = {"status": "out-of-scope", "physical_page": 30, "source_sha256": source_sha256}
    review_session_id = _safe_id(review_session_id, "review_session_id")
    core_plan = {
        "schema_version": SCAN_GOLD_REVIEW_PLAN_SCHEMA,
        "protocol": SCAN_GOLD_REVIEW_PROTOCOL,
        "compiler_version": SCAN_GOLD_REVIEW_COMPILER_VERSION,
        "created_at": created_at,
        "profile": profile_identity,
        "source": {"source_sha256": source_sha256, "physical_page_count": page_count, "scope": pages, "ocr_sample_pages": ocr_pages, "source_path_included": False, "source_pdf_included": False},
        "renderer": {"id": "pdftoppm", "version": render_receipts[0]["renderer"]["version"], "dpi": render_dpi, "format": "png", "content_rotation_clockwise": rotation, "physical_pages": pages},
        "split_policy": {"axis": "x", "boundary_rule": "floor(rotated_width_px/2)", "region_count": 2, "reading_order": "left-to-right", "printed_page_label_policy": "candidate-or-null-human-confirmation-required"},
        "proposer_instance": proposer_instance,
        "reviewer_requirements": {"required_external_reviewers": 1, "external_identity_required": True, "external_attestation_required": True, "evaluator_must_be_separate": True},
        "render_receipts": render_receipts,
        "split_contracts": split_contracts,
        "items": structure_items + ocr_items,
        "asset_inventory": asset_rows,
        "counts": {"physical_pages": len(pages), "structure_items": len(structure_items), "regions": len(pages) * 2, "ocr_sample_pages": len(ocr_pages), "ocr_samples": len(ocr_items), "samples_per_selected_region": len(profile["ocr"]["slots"])},
        "page30_split_cross_check": page30_cross_check,
        "evaluation_policy": {"requires_accepted_revision": True, "metrics_null_until_gate": True, "threshold_source": "frozen-profile", "test_cannot_change_threshold": True, "overall_verdict": "not-authorized"},
        "policy": {"network_enabled": False, "external_resources": False, "hidden_result_fields_included": False, "candidate_only": True, "promotion": False, "release_included": False},
    }
    plan_id = "scan-gold-plan-" + sha256_json(core_plan)[:24]
    plan = {"plan_id": plan_id, **core_plan}
    plan["plan_sha256"] = _plan_hash(plan)
    workbench_id = "scan-gold-workbench-" + sha256_json({"plan_sha256": plan["plan_sha256"], "reviewer_id": reviewer_id, "review_session_id": review_session_id})[:24]
    data = {
        "schema_version": SCAN_GOLD_REVIEW_SUBMISSION_SCHEMA,
        "protocol": SCAN_GOLD_REVIEW_PROTOCOL,
        "compiler_version": SCAN_GOLD_REVIEW_COMPILER_VERSION,
        "workbench_instance_id": workbench_id,
        "reviewer": {"reviewer_instance": reviewer_id, "review_session_id": review_session_id, "proposer_instance": proposer_instance},
        "profile": profile_identity,
        "plan_identity": {"plan_id": plan_id, "plan_sha256": plan["plan_sha256"], "source_sha256": source_sha256, "scope": pages},
        "structure_items": structure_items,
        "ocr_samples": ocr_items,
        "item_counts": {"structure_items": len(structure_items), "ocr_samples": len(ocr_items), "total_items": len(structure_items) + len(ocr_items)},
        "assets": asset_rows,
        "ui_policy": {"network_enabled": False, "external_resources": False, "source_pdf_visible": False, "hidden_result_fields_visible": False, "locked_facts_read_only": True, "browser_authors_attestation": False, "browser_marks_gold": False, "autosave_namespace": f"tkc.scan-gold-review:{plan['plan_sha256']}:{reviewer_id}:{review_session_id}"},
        "release_included": False,
    }
    profile_path = destination / "scan-gold-profile.json"
    plan_path = destination / "scan-gold-plan.json"
    data_path = destination / "data" / "scan-gold-workpack.json"
    _write_json(profile_path, profile)
    _write_json(plan_path, plan)
    _write_json(data_path, data)
    manifest = {
        "schema_version": SCAN_GOLD_REVIEW_WORKBENCH_MANIFEST_SCHEMA,
        "protocol": SCAN_GOLD_REVIEW_PROTOCOL,
        "compiler_version": SCAN_GOLD_REVIEW_COMPILER_VERSION,
        "created_at": created_at,
        "status": "paused-independent-scan-gold",
        "workbench_instance_id": workbench_id,
        "reviewer": data["reviewer"],
        "profile_file": "scan-gold-profile.json",
        "profile_sha256": profile_identity["profile_sha256"],
        "plan_file": "scan-gold-plan.json",
        "plan_sha256": plan["plan_sha256"],
        "data_file": "data/scan-gold-workpack.json",
        "data_file_sha256": sha256_file(data_path),
        "source_identity": {"source_sha256": source_sha256, "scope": pages, "source_pdf_included": False},
        "item_counts": data["item_counts"],
        "asset_count": len(asset_rows),
        "assets": asset_rows,
        "assets_sha256": sha256_json(asset_rows),
        "policy": {"network_enabled": False, "external_resources": False, "source_pdf_included": False, "hidden_result_fields_included": False, "compiler_authored_gold": False, "release_included": False},
        "immutable_inputs": True,
    }
    manifest["manifest_sha256"] = _manifest_hash(manifest)
    _write_json(destination / "scan-gold-manifest.json", manifest)
    html_path = destination / "review.html"
    html_path.write_text(render_scan_gold_html(data), encoding="utf-8")
    os.chmod(html_path, 0o600)
    instructions_path = destination / "扫描 Gold 人工审查使用说明.md"
    instructions_path.write_text(render_scan_gold_instructions(destination.name, data["reviewer"], plan, profile_identity), encoding="utf-8")
    os.chmod(instructions_path, 0o600)
    result = validate_scan_gold_workbench(destination)
    result.update({"status": "generated", "workbench": str(destination.resolve()), "review_html": str(html_path.resolve()), "instructions": str(instructions_path.resolve()), "manifest": str((destination / "scan-gold-manifest.json").resolve()), "plan": str(plan_path.resolve()), "profile": str(profile_path.resolve()), "workbench_instance_id": workbench_id, "plan_sha256": plan["plan_sha256"], "profile_sha256": profile_identity["profile_sha256"], "manifest_sha256": manifest["manifest_sha256"], "source_sha256": source_sha256, "physical_pages": len(pages), "structure_item_count": len(structure_items), "region_count": len(pages) * 2, "ocr_sample_count": len(ocr_items), "asset_count": len(asset_rows), "page30_split_cross_check": page30_cross_check, "release_included": False})
    return result


def _expected_item_ids(plan: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    structure = {str(row["item_id"]) for row in plan.get("items", []) if isinstance(row, Mapping) and row.get("item_type") == "structure_page"}
    ocr = {str(row["item_id"]) for row in plan.get("items", []) if isinstance(row, Mapping) and row.get("item_type") == "ocr_text_sample"}
    if not structure or not ocr or structure & ocr:
        raise ScanGoldReviewError("scan_gold_item_sets_invalid")
    return structure, ocr


def _expected_review_input_hashes(item: Mapping[str, Any]) -> dict[str, Any]:
    locked = item.get("locked_facts") if isinstance(item.get("locked_facts"), Mapping) else {}
    source_sha256 = locked.get("source_sha256")
    context = locked.get("context") if isinstance(locked.get("context"), Mapping) else {}
    if item.get("item_type") == "structure_page":
        regions = locked.get("regions") if isinstance(locked.get("regions"), list) else []
        region_hashes = {
            str(row["region_id"]): str((row.get("crop") or {}).get("sha256"))
            for row in regions
            if isinstance(row, Mapping) and isinstance(row.get("region_id"), str)
        }
        return {"source_sha256": source_sha256, "context_sha256": context.get("sha256"), "split_contract_sha256": locked.get("split_contract_sha256"), "region_sha256s": dict(sorted(region_hashes.items()))}
    region = locked.get("region") if isinstance(locked.get("region"), Mapping) else {}
    sample = locked.get("sample") if isinstance(locked.get("sample"), Mapping) else {}
    return {"source_sha256": source_sha256, "context_sha256": context.get("sha256"), "region_sha256": region.get("sha256"), "sample_sha256": sample.get("sha256"), "sample_bbox_sha256": sha256_json({"sample_bbox_px": locked.get("sample_bbox_px"), "sample_bbox_pdf_points": locked.get("sample_bbox_pdf_points"), "coordinate_transform_sha256": locked.get("coordinate_transform_sha256")})}


def _expected_batch_pass_review(item: Mapping[str, Any]) -> dict[str, Any]:
    locked = item["locked_facts"]
    order = locked["split"]["reading_order"]
    regions = sorted(locked["regions"], key=lambda row: int(row["region_order"]))
    region_ids = [str(row["region_id"]) for row in regions]
    if order == "right-to-left":
        region_ids.reverse()
    return {
        "review_status": "complete",
        "exclusion_reason": "",
        "rotation_verdict": "confirmed",
        "canonical_rotation_clockwise": locked["rotation_clockwise_candidate"],
        "split_verdict": "confirmed",
        "canonical_boundary_px": locked["split"]["boundary_px"],
        "canonical_region_order": order,
        "canonical_region_ids": region_ids,
        "page_readability": "readable",
        "abnormal_state": "none",
        "regions": [
            {
                "region_id": row["region_id"],
                "region_order": row["region_order"],
                "bbox_verdict": "confirmed",
                "canonical_bbox_pdf_points": list(row["bbox_pdf_points"]),
                "label_state": "confirmed",
                "printed_page_label": row["printed_page_label_candidate"],
                "readability": "readable",
                "abnormal_state": "none",
                "notes": "",
            }
            for row in locked["regions"]
        ],
        "notes": "",
    }


def _validate_item_asset(root: Path, asset: Mapping[str, Any], asset_hashes: Mapping[str, str], code: str) -> None:
    relative = asset.get("asset")
    if not isinstance(relative, str) or asset_hashes.get(relative) != asset.get("sha256"):
        raise ScanGoldReviewError(code)
    path = _relative_instance_file(root, relative, code)
    if sha256_file(path) != asset.get("sha256"):
        raise ScanGoldReviewError(code)


def _load_scan_gold_workbench(workbench: Path) -> dict[str, Any]:
    root = _secure_directory(Path(workbench).expanduser())
    for path in root.rglob("*"):
        if path.is_symlink() or path.suffix.casefold() == ".pdf":
            raise ScanGoldReviewError("scan_gold_source_or_symlink_leak")
    manifest = _read_json(root / "scan-gold-manifest.json", "scan_gold_manifest")
    if manifest.get("schema_version") != SCAN_GOLD_REVIEW_WORKBENCH_MANIFEST_SCHEMA or manifest.get("protocol") != SCAN_GOLD_REVIEW_PROTOCOL or manifest.get("compiler_version") != SCAN_GOLD_REVIEW_COMPILER_VERSION:
        raise ScanGoldReviewError("scan_gold_manifest_schema_invalid")
    if manifest.get("manifest_sha256") != _manifest_hash(manifest):
        raise ScanGoldReviewError("scan_gold_manifest_hash_mismatch")
    if manifest.get("status") != "paused-independent-scan-gold" or manifest.get("immutable_inputs") is not True:
        raise ScanGoldReviewError("scan_gold_manifest_status_invalid")
    if manifest.get("policy") != {"network_enabled": False, "external_resources": False, "source_pdf_included": False, "hidden_result_fields_included": False, "compiler_authored_gold": False, "release_included": False}:
        raise ScanGoldReviewError("scan_gold_manifest_policy_invalid")
    profile_path = _relative_instance_file(root, manifest.get("profile_file"), "scan_gold_profile")
    profile = _read_json(profile_path, "scan_gold_profile")
    validate_scan_gold_profile(profile)
    profile_identity = scan_gold_profile_identity(profile)
    if manifest.get("profile_sha256") != profile_identity["profile_sha256"]:
        raise ScanGoldReviewError("scan_gold_profile_hash_mismatch")
    plan_path = _relative_instance_file(root, manifest.get("plan_file"), "scan_gold_plan")
    plan = _read_json(plan_path, "scan_gold_plan")
    if plan.get("schema_version") != SCAN_GOLD_REVIEW_PLAN_SCHEMA or plan.get("protocol") != SCAN_GOLD_REVIEW_PROTOCOL or plan.get("compiler_version") != SCAN_GOLD_REVIEW_COMPILER_VERSION or plan.get("plan_sha256") != _plan_hash(plan):
        raise ScanGoldReviewError("scan_gold_plan_hash_or_schema_invalid")
    if plan.get("profile") != profile_identity or manifest.get("plan_sha256") != plan.get("plan_sha256"):
        raise ScanGoldReviewError("scan_gold_plan_profile_binding_invalid")
    source = plan.get("source") if isinstance(plan.get("source"), Mapping) else {}
    scope = source.get("scope")
    if not isinstance(scope, list) or not scope or any(type(page) is not int or page < 1 for page in scope) or scope != list(range(int(scope[0]), int(scope[-1]) + 1)):
        raise ScanGoldReviewError("scan_gold_scope_invalid")
    expected_ocr_pages = _stratified_ocr_pages(scope)
    if source.get("ocr_sample_pages") != expected_ocr_pages:
        raise ScanGoldReviewError("scan_gold_ocr_sample_pages_invalid")
    if source.get("source_path_included") is not False or source.get("source_pdf_included") is not False:
        raise ScanGoldReviewError("scan_gold_source_path_policy_invalid")
    if plan.get("policy") != {"network_enabled": False, "external_resources": False, "hidden_result_fields_included": False, "candidate_only": True, "promotion": False, "release_included": False}:
        raise ScanGoldReviewError("scan_gold_plan_policy_invalid")
    if plan.get("evaluation_policy") != {"requires_accepted_revision": True, "metrics_null_until_gate": True, "threshold_source": "frozen-profile", "test_cannot_change_threshold": True, "overall_verdict": "not-authorized"}:
        raise ScanGoldReviewError("scan_gold_evaluation_policy_invalid")
    structure_ids, ocr_ids = _expected_item_ids(plan)
    data_path = _relative_instance_file(root, manifest.get("data_file"), "scan_gold_data")
    data = _read_json(data_path, "scan_gold_data")
    expected_data_keys = {"schema_version", "protocol", "compiler_version", "workbench_instance_id", "reviewer", "profile", "plan_identity", "structure_items", "ocr_samples", "item_counts", "assets", "ui_policy", "release_included"}
    if set(data) != expected_data_keys or data.get("schema_version") != SCAN_GOLD_REVIEW_SUBMISSION_SCHEMA or data.get("protocol") != SCAN_GOLD_REVIEW_PROTOCOL or data.get("compiler_version") != SCAN_GOLD_REVIEW_COMPILER_VERSION:
        raise ScanGoldReviewError("scan_gold_data_schema_invalid")
    if sha256_file(data_path) != manifest.get("data_file_sha256") or data.get("workbench_instance_id") != manifest.get("workbench_instance_id") or data.get("profile") != profile_identity:
        raise ScanGoldReviewError("scan_gold_data_binding_invalid")
    if data.get("plan_identity", {}).get("plan_sha256") != plan.get("plan_sha256") or data.get("plan_identity", {}).get("source_sha256") != source.get("source_sha256") or data.get("plan_identity", {}).get("scope") != scope:
        raise ScanGoldReviewError("scan_gold_data_plan_binding_invalid")
    if data.get("release_included") is not False or _forbidden_key_present(data) is not None:
        raise ScanGoldReviewError("scan_gold_data_policy_or_hidden_field_invalid")
    structure_items = data.get("structure_items")
    ocr_items = data.get("ocr_samples")
    reviewer = data.get("reviewer")
    if not isinstance(reviewer, Mapping) or set(reviewer) != {"reviewer_instance", "review_session_id", "proposer_instance"}:
        raise ScanGoldReviewError("scan_gold_reviewer_identity_invalid")
    try:
        for field in ("reviewer_instance", "review_session_id", "proposer_instance"):
            _safe_id(reviewer.get(field), field)
    except ReviewerWorkbenchError as error:
        raise ScanGoldReviewError("scan_gold_reviewer_identity_invalid") from error
    if reviewer.get("proposer_instance") != plan.get("proposer_instance") or manifest.get("reviewer") != dict(reviewer):
        raise ScanGoldReviewError("scan_gold_reviewer_proposer_binding_invalid")
    if manifest.get("source_identity") != {"source_sha256": source.get("source_sha256"), "scope": scope, "source_pdf_included": False}:
        raise ScanGoldReviewError("scan_gold_manifest_source_binding_invalid")
    if not isinstance(structure_items, list) or not isinstance(ocr_items, list) or {str(row.get("item_id")) for row in structure_items if isinstance(row, Mapping)} != structure_ids or {str(row.get("item_id")) for row in ocr_items if isinstance(row, Mapping)} != ocr_ids:
        raise ScanGoldReviewError("scan_gold_data_item_set_invalid")
    if len(ocr_items) != len(expected_ocr_pages) * 2:
        raise ScanGoldReviewError("scan_gold_ocr_sample_count_invalid")
    expected_counts = {"structure_items": len(structure_items), "ocr_samples": len(ocr_items), "total_items": len(structure_items) + len(ocr_items)}
    if data.get("item_counts") != expected_counts or manifest.get("item_counts") != expected_counts:
        raise ScanGoldReviewError("scan_gold_item_counts_invalid")
    if plan.get("counts") != {"physical_pages": len(scope), "structure_items": len(structure_items), "regions": len(scope) * 2, "ocr_sample_pages": len(expected_ocr_pages), "ocr_samples": len(ocr_items), "samples_per_selected_region": len(profile["ocr"]["slots"])}:
        raise ScanGoldReviewError("scan_gold_plan_counts_invalid")
    plan_items = plan.get("items")
    if not isinstance(plan_items, list) or plan_items != structure_items + ocr_items:
        raise ScanGoldReviewError("scan_gold_plan_data_item_binding_invalid")
    assets = manifest.get("assets")
    if not isinstance(assets, list) or assets != data.get("assets") or assets != plan.get("asset_inventory") or manifest.get("assets_sha256") != sha256_json(assets) or manifest.get("asset_count") != len(assets):
        raise ScanGoldReviewError("scan_gold_asset_inventory_binding_invalid")
    asset_hashes: dict[str, str] = {}
    for asset in assets:
        if not isinstance(asset, Mapping) or not isinstance(asset.get("path"), str) or asset["path"] in asset_hashes or asset["path"].casefold().endswith(".pdf"):
            raise ScanGoldReviewError("scan_gold_asset_metadata_invalid")
        path = _relative_instance_file(root, asset["path"], "scan_gold_asset")
        if sha256_file(path) != asset.get("sha256"):
            raise ScanGoldReviewError("scan_gold_asset_hash_mismatch")
        asset_hashes[asset["path"]] = str(asset["sha256"])
    contracts = plan.get("split_contracts")
    contract_by_page: dict[int, Mapping[str, Any]] = {}
    if not isinstance(contracts, list) or len(contracts) != len(scope):
        raise ScanGoldReviewError("scan_gold_split_contract_set_invalid")
    for contract in contracts:
        if not isinstance(contract, Mapping):
            raise ScanGoldReviewError("scan_gold_split_contract_invalid")
        if (contract.get("schema_version"), contract.get("protocol"), contract.get("compiler_version")) != (SCANNED_PDF_SPLIT_GEOMETRY_SCHEMA, SCANNED_PDF_SPLIT_GEOMETRY_PROTOCOL, SCANNED_PDF_SPLIT_GEOMETRY_COMPILER_VERSION):
            raise ScanGoldReviewError("scan_gold_legacy_split_contract_not_allowed")
        try:
            validate_spread_split_contract(contract, require_bound=False)
        except ScanOcrError as error:
            raise ScanGoldReviewError(f"scan_gold_split_contract_invalid:{error}") from error
        page = contract.get("physical_page")
        if page in contract_by_page or page not in scope or contract.get("source_sha256") != source.get("source_sha256") or contract.get("rotation") != plan.get("renderer", {}).get("content_rotation_clockwise"):
            raise ScanGoldReviewError("scan_gold_split_contract_binding_invalid")
        contract_by_page[int(page)] = contract
    if set(contract_by_page) != set(scope):
        raise ScanGoldReviewError("scan_gold_split_contract_pages_invalid")
    item_by_id = {str(row["item_id"]): row for row in plan_items if isinstance(row, Mapping)}
    for item in plan_items:
        if not isinstance(item, Mapping) or item.get("item_sha256") != _item_hash(item):
            raise ScanGoldReviewError("scan_gold_item_hash_mismatch")
        page = int(item.get("physical_page", -1))
        contract = contract_by_page.get(page)
        locked = item.get("locked_facts") if isinstance(item.get("locked_facts"), Mapping) else {}
        if not isinstance(contract, Mapping) or locked.get("source_sha256") != source.get("source_sha256"):
            raise ScanGoldReviewError(f"scan_gold_item_binding_invalid:{item.get('item_id')}")
        if item.get("item_type") == "structure_page":
            if page not in scope or locked.get("split_contract_sha256") != contract.get("contract_sha256") or locked.get("split_core_sha256") != contract.get("split_core_sha256"):
                raise ScanGoldReviewError(f"scan_gold_structure_binding_invalid:{item.get('item_id')}")
            context = locked.get("context") if isinstance(locked.get("context"), Mapping) else {}
            if context.get("rotation") != contract.get("rotation") or context.get("sha256") != contract.get("rotated_render", {}).get("sha256"):
                raise ScanGoldReviewError(f"scan_gold_structure_context_binding_invalid:{item.get('item_id')}")
            original_render = locked.get("original_render") if isinstance(locked.get("original_render"), Mapping) else {}
            context_mapping = locked.get("context_coordinate_mapping") if isinstance(locked.get("context_coordinate_mapping"), Mapping) else {}
            try:
                expected_context_mapping = _context_mapping(
                    contract["page_geometry"],
                    original_render["dimensions_px"],
                    context["dimensions_px"],
                    int(context["rotation"]),
                    source_sha256=str(source["source_sha256"]),
                    physical_page=page,
                    render_sha256=str(original_render["sha256"]),
                    context_sha256=str(context["sha256"]),
                    render_dpi=int(original_render["dpi"]),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ScanGoldReviewError(f"scan_gold_structure_context_mapping_invalid:{item.get('item_id')}") from error
            if context_mapping != expected_context_mapping or context_mapping.get("transform_sha256") != sha256_json(context_mapping.get("transform")):
                raise ScanGoldReviewError(f"scan_gold_structure_context_mapping_invalid:{item.get('item_id')}")
            _validate_item_asset(root, context, asset_hashes, "scan_gold_structure_context_asset_invalid")
            regions = locked.get("regions")
            contract_regions = sorted(contract.get("regions", []), key=lambda row: int(row.get("region_order", -1)))
            if not isinstance(regions, list) or len(regions) != 2 or len(contract_regions) != 2:
                raise ScanGoldReviewError(f"scan_gold_structure_region_count_invalid:{item.get('item_id')}")
            for locked_region, contract_region in zip(regions, contract_regions):
                if not isinstance(locked_region, Mapping) or locked_region.get("region_id") != contract_region.get("region_id") or locked_region.get("region_order") != contract_region.get("region_order") or locked_region.get("bbox_pdf_points") != contract_region.get("bbox_pdf_points") or locked_region.get("rotated_bbox_px") != contract_region.get("rotated_region_bbox_px") or locked_region.get("coordinate_transform_sha256") != contract_region.get("coordinate_transform_sha256"):
                    raise ScanGoldReviewError(f"scan_gold_structure_region_binding_invalid:{item.get('item_id')}")
                _validate_item_asset(root, locked_region.get("crop") if isinstance(locked_region.get("crop"), Mapping) else {}, asset_hashes, "scan_gold_structure_region_asset_invalid")
        elif item.get("item_type") == "ocr_text_sample":
            expected = _expected_review_input_hashes(item)
            if page not in expected_ocr_pages or locked.get("normalized_bbox") != [0.0, 0.0, 1.0, 1.0] or locked.get("sample_bbox_px") != [0, 0, int(locked.get("region", {}).get("dimensions_px", {}).get("width", -1)), int(locked.get("region", {}).get("dimensions_px", {}).get("height", -1))] or locked.get("split_contract_sha256") != contract.get("contract_sha256") or locked.get("sample_rule_id") != SAMPLE_RULE_ID or not _valid_bbox(locked.get("sample_bbox_pdf_points")):
                raise ScanGoldReviewError(f"scan_gold_sample_binding_invalid:{item.get('item_id')}")
            for section in ("context", "region", "sample"):
                _validate_item_asset(root, locked.get(section) if isinstance(locked.get(section), Mapping) else {}, asset_hashes, f"scan_gold_sample_{section}_asset_invalid")
            if expected["sample_sha256"] != locked.get("sample", {}).get("sha256"):
                raise ScanGoldReviewError(f"scan_gold_sample_hash_invalid:{item.get('item_id')}")
        else:
            raise ScanGoldReviewError("scan_gold_item_type_invalid")
    html_path = _secure_file(root / "review.html", "scan_gold_review_html")
    html_text = html_path.read_text(encoding="utf-8")
    required_markers = ("default-src 'none'", "connect-src 'none'", "object-src 'none'", "localStorage", "FileReader", "pointerdown", "pointerup", "getBoundingClientRect", "naturalWidth", "框选 BBox", "平移页面", "批量确认", "批量通过所选结构页", "batch-pass-structure", "data-region-tab", "canonical_text", "excluded_reason")
    if any(marker not in html_text for marker in required_markers):
        raise ScanGoldReviewError("scan_gold_review_html_contract_missing")
    if any(token in html_text for token in ("http://", "https://", "<iframe", "<link", "fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon")):
        raise ScanGoldReviewError("scan_gold_review_html_external_resource_or_network")
    hidden_result_tokens = ("prediction", "model_output", "model_confidence", "confidence", "calibration", "test label", "metrics threshold", "source_path", ".pdf")
    if any(token in html_text.casefold() for token in hidden_result_tokens):
        raise ScanGoldReviewError("scan_gold_review_html_hidden_result_field")
    for relative in asset_hashes:
        if f'"{relative}"' not in html_text:
            raise ScanGoldReviewError("scan_gold_review_html_asset_not_embedded")
    if data.get("ui_policy") != {"network_enabled": False, "external_resources": False, "source_pdf_visible": False, "hidden_result_fields_visible": False, "locked_facts_read_only": True, "browser_authors_attestation": False, "browser_marks_gold": False, "autosave_namespace": f"tkc.scan-gold-review:{plan['plan_sha256']}:{data['reviewer']['reviewer_instance']}:{data['reviewer']['review_session_id']}"}:
        raise ScanGoldReviewError("scan_gold_ui_policy_invalid")
    return {"root": root, "manifest": manifest, "profile": profile, "profile_identity": profile_identity, "plan": plan, "data": data, "asset_hashes": asset_hashes, "item_by_id": item_by_id, "structure_items": structure_items, "ocr_items": ocr_items, "structure_ids": structure_ids, "ocr_ids": ocr_ids, "contract_by_page": contract_by_page, "html": html_text}


def validate_scan_gold_workbench(workbench: Path) -> dict[str, Any]:
    facts = _load_scan_gold_workbench(workbench)
    manifest = facts["manifest"]
    plan = facts["plan"]
    data = facts["data"]
    return {
        "status": "verified",
        "workbench": str(facts["root"].resolve()),
        "manifest": str((facts["root"] / "scan-gold-manifest.json").resolve()),
        "profile_sha256": facts["profile_identity"]["profile_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "manifest_sha256": manifest["manifest_sha256"],
        "source_sha256": plan["source"]["source_sha256"],
        "physical_pages": data["item_counts"]["structure_items"],
        "structure_item_count": data["item_counts"]["structure_items"],
        "region_count": data["item_counts"]["structure_items"] * 2,
        "ocr_sample_count": data["item_counts"]["ocr_samples"],
        "asset_count": len(facts["asset_hashes"]),
        "page30_split_cross_check": plan["page30_split_cross_check"],
        "network_enabled": False,
        "model_invocations": 0,
        "mcp_calls": 0,
        "release_included": False,
    }


def _structure_review_issues(item: Mapping[str, Any], review: Any) -> list[str]:
    item_id = str(item.get("item_id"))
    if not isinstance(review, Mapping):
        return [f"structure_review_missing:{item_id}"]
    allowed = {"review_status", "exclusion_reason", "rotation_verdict", "canonical_rotation_clockwise", "split_verdict", "canonical_boundary_px", "canonical_region_order", "canonical_region_ids", "page_readability", "abnormal_state", "regions", "notes"}
    issues = [f"structure_review_unknown_field:{item_id}"] if set(review) - allowed else []
    status = review.get("review_status")
    if status not in STRUCTURE_STATUSES:
        issues.append(f"structure_status_invalid:{item_id}")
    if not isinstance(review.get("notes", ""), str):
        issues.append(f"structure_notes_invalid:{item_id}")
    if status != "complete" and not _nonempty(review.get("exclusion_reason")):
        issues.append(f"structure_exclusion_reason_missing:{item_id}")
    if status == "complete" and review.get("exclusion_reason") not in (None, ""):
        issues.append(f"structure_complete_has_exclusion_reason:{item_id}")
    if review.get("page_readability") not in READABILITY_STATES:
        issues.append(f"structure_page_readability_invalid:{item_id}")
    if review.get("abnormal_state") not in ABNORMAL_STATES:
        issues.append(f"structure_abnormal_state_invalid:{item_id}")
    locked = item.get("locked_facts") if isinstance(item.get("locked_facts"), Mapping) else {}
    candidate_rotation = locked.get("rotation_clockwise_candidate")
    split = locked.get("split") if isinstance(locked.get("split"), Mapping) else {}
    candidate_boundary = split.get("boundary_px")
    locked_regions = locked.get("regions") if isinstance(locked.get("regions"), list) else []
    locked_region_by_id = {str(row.get("region_id")): row for row in locked_regions if isinstance(row, Mapping)}
    if status == "complete":
        if review.get("rotation_verdict") not in {"confirmed", "corrected"} or review.get("canonical_rotation_clockwise") not in ROTATIONS:
            issues.append(f"structure_rotation_missing:{item_id}")
        elif review.get("rotation_verdict") == "confirmed" and review.get("canonical_rotation_clockwise") != candidate_rotation:
            issues.append(f"structure_rotation_confirmation_mismatch:{item_id}")
        if review.get("split_verdict") not in {"confirmed", "corrected"} or type(review.get("canonical_boundary_px")) is not int:
            issues.append(f"structure_boundary_missing:{item_id}")
        elif not (0 < int(review["canonical_boundary_px"]) < int((locked.get("context") or {}).get("dimensions_px", {}).get("width", 0))):
            issues.append(f"structure_boundary_out_of_bounds:{item_id}")
        elif review.get("split_verdict") == "confirmed" and review.get("canonical_boundary_px") != candidate_boundary:
            issues.append(f"structure_boundary_confirmation_mismatch:{item_id}")
        if review.get("canonical_region_order") not in {"left-to-right", "right-to-left"}:
            issues.append(f"structure_region_order_missing:{item_id}")
        expected_ids = [str(row.get("region_id")) for row in sorted(locked_regions, key=lambda row: int(row.get("region_order", -1)))]
        expected_ids = expected_ids if review.get("canonical_region_order") == "left-to-right" else list(reversed(expected_ids))
        if review.get("canonical_region_ids") != expected_ids:
            issues.append(f"structure_region_ids_mismatch:{item_id}")
        if review.get("page_readability") != "readable" or review.get("abnormal_state") != "none":
            issues.append(f"structure_complete_state_not_readable:{item_id}")
    else:
        if review.get("canonical_rotation_clockwise") is not None or review.get("canonical_boundary_px") is not None or review.get("canonical_region_order") is not None or review.get("canonical_region_ids") is not None:
            issues.append(f"structure_excluded_has_scorable_fields:{item_id}")
    supplied_regions = review.get("regions")
    if not isinstance(supplied_regions, list) or set(str(row.get("region_id")) for row in supplied_regions if isinstance(row, Mapping)) != set(locked_region_by_id) or len(supplied_regions) != len(locked_region_by_id):
        issues.append(f"structure_region_decisions_incomplete:{item_id}")
        return sorted(set(issues))
    seen: set[str] = set()
    page_dimensions = (locked.get("context_coordinate_mapping") or {}) if isinstance(locked.get("context_coordinate_mapping"), Mapping) else {}
    page_width = float(page_dimensions.get("page_width_points", 0) or 0)
    page_height = float(page_dimensions.get("page_height_points", 0) or 0)
    for region in supplied_regions:
        if not isinstance(region, Mapping):
            issues.append(f"structure_region_invalid:{item_id}")
            continue
        region_id = str(region.get("region_id"))
        if region_id in seen:
            issues.append(f"structure_region_duplicate:{item_id}:{region_id}")
        seen.add(region_id)
        allowed_region = {"region_id", "region_order", "bbox_verdict", "canonical_bbox_pdf_points", "label_state", "printed_page_label", "readability", "abnormal_state", "notes"}
        if set(region) - allowed_region:
            issues.append(f"structure_region_unknown_field:{item_id}:{region_id}")
        locked_region = locked_region_by_id.get(region_id)
        if not isinstance(locked_region, Mapping) or region.get("region_order") != locked_region.get("region_order"):
            issues.append(f"structure_region_order_binding_invalid:{item_id}:{region_id}")
            continue
        if region.get("label_state") not in LABEL_STATES:
            issues.append(f"structure_label_state_invalid:{item_id}:{region_id}")
        if region.get("readability") not in READABILITY_STATES:
            issues.append(f"structure_region_readability_invalid:{item_id}:{region_id}")
        if region.get("abnormal_state") not in ABNORMAL_STATES:
            issues.append(f"structure_region_abnormal_state_invalid:{item_id}:{region_id}")
        if not isinstance(region.get("notes", ""), str):
            issues.append(f"structure_region_notes_invalid:{item_id}:{region_id}")
        label_state = region.get("label_state")
        label = region.get("printed_page_label")
        if label_state == "confirmed":
            if label is not None and not isinstance(label, str):
                issues.append(f"structure_label_invalid:{item_id}:{region_id}")
        elif label is not None:
            issues.append(f"structure_unconfirmed_label_value:{item_id}:{region_id}")
        if status == "complete" and label_state != "confirmed":
            issues.append(f"structure_label_not_confirmed:{item_id}:{region_id}")
        if status == "complete" and (region.get("readability") != "readable" or region.get("abnormal_state") != "none"):
            issues.append(f"structure_region_not_readable:{item_id}:{region_id}")
        bbox_verdict = region.get("bbox_verdict")
        bbox = region.get("canonical_bbox_pdf_points")
        candidate_bbox = locked_region.get("bbox_pdf_points")
        if bbox_verdict not in {"confirmed", "corrected", "uncertain"}:
            issues.append(f"structure_bbox_verdict_invalid:{item_id}:{region_id}")
        elif bbox_verdict == "confirmed":
            if not _bbox_equal(bbox, candidate_bbox):
                issues.append(f"structure_bbox_confirmation_mismatch:{item_id}:{region_id}")
        elif bbox_verdict == "corrected":
            if not _bbox_within(bbox, page_width, page_height):
                issues.append(f"structure_bbox_corrected_invalid:{item_id}:{region_id}")
        elif bbox is not None:
            issues.append(f"structure_bbox_uncertain_has_value:{item_id}:{region_id}")
        if status == "complete" and bbox_verdict == "uncertain":
            issues.append(f"structure_bbox_unresolved:{item_id}:{region_id}")
    return sorted(set(issues))


def _ocr_review_issues(item: Mapping[str, Any], review: Any) -> list[str]:
    item_id = str(item.get("item_id"))
    if not isinstance(review, Mapping):
        return [f"ocr_review_missing:{item_id}"]
    allowed = {"review_status", "canonical_text", "excluded_reason", "notes"}
    issues = [f"ocr_review_unknown_field:{item_id}"] if set(review) - allowed else []
    status = review.get("review_status")
    if status not in OCR_STATUSES:
        issues.append(f"ocr_status_invalid:{item_id}")
    if not isinstance(review.get("notes", ""), str):
        issues.append(f"ocr_notes_invalid:{item_id}")
    text = review.get("canonical_text")
    if status == "complete":
        if not _nonempty(text):
            issues.append(f"ocr_canonical_text_missing:{item_id}")
        if review.get("excluded_reason") not in (None, ""):
            issues.append(f"ocr_complete_has_exclusion_reason:{item_id}")
    elif status in {"unreadable", "not_text", "needs_review"}:
        if text not in (None, ""):
            issues.append(f"ocr_excluded_has_canonical_text:{item_id}")
        if not _nonempty(review.get("excluded_reason")):
            issues.append(f"ocr_exclusion_reason_missing:{item_id}")
    return sorted(set(issues))


def _structure_scorable(item: Mapping[str, Any], review: Mapping[str, Any]) -> bool:
    if review.get("review_status") != "complete" or review.get("page_readability") != "readable" or review.get("abnormal_state") != "none":
        return False
    if review.get("rotation_verdict") not in {"confirmed", "corrected"} or review.get("canonical_rotation_clockwise") not in ROTATIONS:
        return False
    if review.get("split_verdict") not in {"confirmed", "corrected"} or type(review.get("canonical_boundary_px")) is not int or review.get("canonical_region_order") not in {"left-to-right", "right-to-left"}:
        return False
    for region in review.get("regions", []):
        if not isinstance(region, Mapping) or region.get("label_state") != "confirmed" or region.get("readability") != "readable" or region.get("abnormal_state") != "none" or region.get("bbox_verdict") == "uncertain":
            return False
    return True


def _review_input_sha256(facts: Mapping[str, Any]) -> str:
    data = facts["data"]
    ids = sorted(str(row["item_id"]) for row in data["structure_items"] + data["ocr_samples"])
    return sha256_json({"workbench_instance_id": data["workbench_instance_id"], "plan_sha256": facts["plan"]["plan_sha256"], "profile_sha256": facts["profile_identity"]["profile_sha256"], "item_ids": ids})


def _load_scan_gold_submission(path: Path, facts: Mapping[str, Any]) -> dict[str, Any]:
    submission_path = _secure_file(Path(path).expanduser(), "scan_gold_submission")
    value = _read_json(submission_path, "scan_gold_submission")
    allowed_top = {"schema_version", "protocol", "compiler_version", "workbench_instance_id", "plan_sha256", "profile_sha256", "reviewer", "declarations", "structure_items", "ocr_samples", "batch_audit", "exported_at"}
    if set(value) - allowed_top or value.get("schema_version") != SCAN_GOLD_REVIEW_SUBMISSION_SCHEMA or value.get("protocol") != SCAN_GOLD_REVIEW_PROTOCOL or value.get("compiler_version") != SCAN_GOLD_REVIEW_COMPILER_VERSION:
        raise ScanGoldReviewError("scan_gold_submission_schema_invalid")
    data = facts["data"]
    expected_reviewer = data["reviewer"]
    if value.get("workbench_instance_id") != data.get("workbench_instance_id") or value.get("plan_sha256") != facts["plan"].get("plan_sha256") or value.get("profile_sha256") != facts["profile_identity"].get("profile_sha256") or value.get("reviewer") != expected_reviewer:
        raise ScanGoldReviewError("scan_gold_submission_identity_mismatch")
    if _forbidden_key_present(value, ignore_declarations=True) is not None:
        raise ScanGoldReviewError("scan_gold_submission_hidden_field")
    declarations = value.get("declarations") if isinstance(value.get("declarations"), Mapping) else {}
    if set(declarations) != set(DECLARATION_KEYS):
        raise ScanGoldReviewError("scan_gold_submission_declarations_invalid")
    if "exported_at" in value and not _nonempty(value.get("exported_at")):
        raise ScanGoldReviewError("scan_gold_submission_exported_at_invalid")
    declaration_issues = [f"declaration_missing_or_false:{key}" for key in DECLARATION_KEYS if declarations.get(key) is not True]
    structure_by_id = {str(row["item_id"]): row for row in facts["structure_items"]}
    ocr_by_id = {str(row["item_id"]): row for row in facts["ocr_items"]}
    raw_structure = value.get("structure_items")
    raw_ocr = value.get("ocr_samples")
    if not isinstance(raw_structure, list) or not isinstance(raw_ocr, list):
        raise ScanGoldReviewError("scan_gold_submission_item_lists_required")
    issues = list(declaration_issues)
    structure: dict[str, dict[str, Any]] = {}
    ocr: dict[str, dict[str, Any]] = {}
    for raw, expected, target, kind in ((raw_structure, structure_by_id, structure, "structure"), (raw_ocr, ocr_by_id, ocr, "ocr")):
        for row in raw:
            if not isinstance(row, Mapping) or not isinstance(row.get("item_id"), str):
                raise ScanGoldReviewError(f"scan_gold_{kind}_submission_item_invalid")
            item_id = str(row["item_id"])
            if item_id in target:
                issues.append(f"{kind}_duplicate_item:{item_id}")
                continue
            if item_id not in expected:
                raise ScanGoldReviewError(f"scan_gold_{kind}_unknown_item:{item_id}")
            if set(row) != {"item_id", "item_sha256", "input_hashes", "review"}:
                raise ScanGoldReviewError(f"scan_gold_{kind}_item_field_invalid:{item_id}")
            if row.get("item_sha256") != expected[item_id].get("item_sha256") or row.get("input_hashes") != _expected_review_input_hashes(expected[item_id]):
                raise ScanGoldReviewError(f"scan_gold_{kind}_item_hash_mismatch:{item_id}")
            review = row.get("review")
            row_issues = _structure_review_issues(expected[item_id], review) if kind == "structure" else _ocr_review_issues(expected[item_id], review)
            issues.extend(row_issues)
            target[item_id] = {"item_id": item_id, "item_sha256": row["item_sha256"], "input_hashes": deepcopy(row["input_hashes"]), "review": deepcopy(dict(review)) if isinstance(review, Mapping) else review}
        missing = sorted(set(expected) - set(target))
        issues.extend(f"{kind}_missing_item:{item_id}" for item_id in missing)
    batch_audit = value.get("batch_audit", [])
    if not isinstance(batch_audit, list):
        raise ScanGoldReviewError("scan_gold_batch_audit_invalid")
    expected_structure_ids = set(structure_by_id)
    for index, entry in enumerate(batch_audit):
        base_fields = {"audit_id", "action", "item_ids", "fields", "per_item_visible", "at"}
        detail_fields = {"source_item_id", "candidate_fields", "candidate_fields_by_item", "region_ids_by_item", "human_confirmation"}
        if not isinstance(entry, Mapping) or not base_fields.issubset(set(entry)) or set(entry) - base_fields - detail_fields:
            raise ScanGoldReviewError(f"scan_gold_batch_audit_entry_invalid:{index}")
        try:
            _safe_id(entry.get("audit_id"), "batch_audit_id")
        except ReviewerWorkbenchError as error:
            raise ScanGoldReviewError(f"scan_gold_batch_audit_id_invalid:{index}") from error
        item_ids = entry.get("item_ids")
        fields = entry.get("fields")
        action = entry.get("action")
        expected_fields = BATCH_CONFIRM_FIELDS if action == "batch-confirm-structure" else BATCH_PASS_FIELDS if action == "batch-pass-structure" else set()
        if not expected_fields or entry.get("per_item_visible") is not True or not isinstance(item_ids, list) or not item_ids or len(set(item_ids)) != len(item_ids) or any(item_id not in expected_structure_ids for item_id in item_ids) or not isinstance(fields, Mapping) or set(fields) != expected_fields or any(value is not True for value in fields.values()) or not _nonempty(entry.get("at")):
            raise ScanGoldReviewError(f"scan_gold_batch_audit_contract_invalid:{index}")
        if action == "batch-confirm-structure":
            confirm_details = {"source_item_id", "candidate_fields", "region_ids_by_item"}
            present_details = set(entry) & detail_fields
            if present_details and present_details != confirm_details:
                raise ScanGoldReviewError(f"scan_gold_batch_audit_detail_incomplete:{index}")
            if not present_details:
                continue
            source_item_id = entry.get("source_item_id")
            candidate = entry.get("candidate_fields")
            region_ids_by_item = entry.get("region_ids_by_item")
            if source_item_id not in item_ids or source_item_id not in expected_structure_ids or not isinstance(candidate, Mapping) or set(candidate) != {"rotation", "boundary", "region_order"} or candidate.get("rotation") not in ROTATIONS or type(candidate.get("boundary")) is not int or candidate.get("boundary") <= 0 or candidate.get("region_order") not in {"left-to-right", "right-to-left"} or not isinstance(region_ids_by_item, Mapping) or set(region_ids_by_item) != set(item_ids):
                raise ScanGoldReviewError(f"scan_gold_batch_audit_detail_invalid:{index}")
            for item_id in item_ids:
                item = structure_by_id[item_id]
                locked = item.get("locked_facts") if isinstance(item.get("locked_facts"), Mapping) else {}
                split = locked.get("split") if isinstance(locked.get("split"), Mapping) else {}
                if candidate.get("rotation") != locked.get("rotation_clockwise_candidate") or candidate.get("boundary") != split.get("boundary_px") or candidate.get("region_order") != split.get("reading_order"):
                    raise ScanGoldReviewError(f"scan_gold_batch_audit_candidate_mismatch:{index}:{item_id}")
                region_ids = [str(row.get("region_id")) for row in sorted(locked.get("regions", []), key=lambda row: int(row.get("region_order", -1)))]
                if candidate.get("region_order") == "right-to-left":
                    region_ids.reverse()
                if region_ids_by_item.get(item_id) != region_ids:
                    raise ScanGoldReviewError(f"scan_gold_batch_audit_region_ids_mismatch:{index}:{item_id}")
        else:
            pass_details = {"candidate_fields_by_item", "region_ids_by_item", "human_confirmation"}
            if set(entry) & detail_fields != pass_details or entry.get("human_confirmation") is not True:
                raise ScanGoldReviewError(f"scan_gold_batch_pass_detail_invalid:{index}")
            candidates = entry.get("candidate_fields_by_item")
            region_ids_by_item = entry.get("region_ids_by_item")
            if not isinstance(candidates, Mapping) or set(candidates) != set(item_ids) or not isinstance(region_ids_by_item, Mapping) or set(region_ids_by_item) != set(item_ids):
                raise ScanGoldReviewError(f"scan_gold_batch_pass_item_binding_invalid:{index}")
            for item_id in item_ids:
                item = structure_by_id[item_id]
                locked = item["locked_facts"]
                expected_candidate = {"rotation": locked["rotation_clockwise_candidate"], "boundary": locked["split"]["boundary_px"], "region_order": locked["split"]["reading_order"]}
                if candidates.get(item_id) != expected_candidate:
                    raise ScanGoldReviewError(f"scan_gold_batch_pass_candidate_mismatch:{index}:{item_id}")
                expected_review = _expected_batch_pass_review(item)
                if region_ids_by_item.get(item_id) != expected_review["canonical_region_ids"]:
                    raise ScanGoldReviewError(f"scan_gold_batch_pass_region_ids_mismatch:{index}:{item_id}")
    return {"path": submission_path, "file_sha256": sha256_file(submission_path), "reviewer": deepcopy(dict(expected_reviewer)), "declarations": dict(declarations), "structure": structure, "ocr": ocr, "batch_audit": deepcopy(batch_audit), "issues": sorted(set(issues))}


def _load_scan_gold_attestation(path: Path, facts: Mapping[str, Any], submission_sha256: str) -> dict[str, Any]:
    attestation_path = _secure_file(Path(path).expanduser(), "scan_gold_attestation")
    value = _read_json(attestation_path, "scan_gold_attestation")
    allowed = {"schema_version", "protocol", "compiler_version", "attestation_id", "external_issuer", "issued_at", "reviewer_instance", "review_session_id", "workbench_instance_id", "plan_sha256", "profile_sha256", "submission_sha256", "reviewed_item_ids_sha256", "review_input_sha256", "independent_from_proposer", "not_evaluator", "provided_outside_workbench", "compiler_authored", "attestation_sha256"}
    if set(value) != allowed or value.get("schema_version") != SCAN_GOLD_REVIEW_ATTESTATION_SCHEMA or value.get("protocol") != SCAN_GOLD_REVIEW_PROTOCOL or value.get("compiler_version") != SCAN_GOLD_REVIEW_COMPILER_VERSION:
        raise ScanGoldReviewError("scan_gold_attestation_schema_invalid")
    try:
        _safe_id(value.get("attestation_id"), "attestation_id")
    except ReviewerWorkbenchError as error:
        raise ScanGoldReviewError("scan_gold_attestation_id_invalid") from error
    data = facts["data"]
    expected_ids = sorted(str(row["item_id"]) for row in facts["structure_items"] + facts["ocr_items"])
    if not _nonempty(value.get("external_issuer")) or value.get("reviewer_instance") != data["reviewer"]["reviewer_instance"] or value.get("review_session_id") != data["reviewer"]["review_session_id"] or value.get("workbench_instance_id") != data["workbench_instance_id"] or value.get("plan_sha256") != facts["plan"]["plan_sha256"] or value.get("profile_sha256") != facts["profile_identity"]["profile_sha256"] or value.get("submission_sha256") != submission_sha256 or value.get("reviewed_item_ids_sha256") != sha256_json(expected_ids) or value.get("review_input_sha256") != _review_input_sha256(facts):
        raise ScanGoldReviewError("scan_gold_attestation_binding_invalid")
    if value.get("independent_from_proposer") is not True or value.get("not_evaluator") is not True or value.get("provided_outside_workbench") is not True or value.get("compiler_authored") is not False:
        raise ScanGoldReviewError("scan_gold_attestation_policy_invalid")
    if value.get("attestation_sha256") != _hash_without(value, "attestation_sha256"):
        raise ScanGoldReviewError("scan_gold_attestation_hash_mismatch")
    return {"path": attestation_path, "file_sha256": sha256_file(attestation_path), "value": value}


def _reviewer_overlap_issues(facts: Mapping[str, Any], submission: Mapping[str, Any]) -> list[str]:
    reviewer = submission.get("reviewer") if isinstance(submission.get("reviewer"), Mapping) else {}
    proposer = facts["plan"].get("proposer_instance")
    if reviewer.get("reviewer_instance") == proposer or reviewer.get("proposer_instance") == reviewer.get("reviewer_instance"):
        return ["reviewer_proposer_overlap"]
    return []


def _structure_gold_row(item: Mapping[str, Any], submitted: Mapping[str, Any], attestation_id: str) -> dict[str, Any]:
    review = submitted["review"]
    excluded = not _structure_scorable(item, review)
    row: dict[str, Any] = {
        "schema_version": SCAN_GOLD_REVIEW_STRUCTURE_GOLD_SCHEMA,
        "protocol": SCAN_GOLD_REVIEW_PROTOCOL,
        "compiler_version": SCAN_GOLD_REVIEW_COMPILER_VERSION,
        "item_id": item["item_id"],
        "item_sha256": item["item_sha256"],
        "input_hashes": deepcopy(submitted["input_hashes"]),
        "physical_page": item["physical_page"],
        "review_status": review.get("review_status"),
        "excluded": excluded,
        "excluded_reason": review.get("exclusion_reason") if excluded else None,
        "rotation_clockwise": None,
        "split_boundary_px": None,
        "region_order": None,
        "region_ids": [],
        "regions": [],
        "reviewer_instance": submitted.get("reviewer_instance"),
        "review_session_id": submitted.get("review_session_id"),
        "attestation_id": attestation_id,
        "compiler_authored": False,
        "verified_gold": False,
        "promotion_allowed": False,
        "release_included": False,
    }
    if not excluded:
        row["rotation_clockwise"] = review["canonical_rotation_clockwise"]
        row["split_boundary_px"] = review["canonical_boundary_px"]
        row["region_order"] = review["canonical_region_order"]
        row["region_ids"] = list(review["canonical_region_ids"])
        by_id = {str(region["region_id"]): region for region in review["regions"]}
        for region_id in row["region_ids"]:
            region = by_id[region_id]
            row["regions"].append({
                "region_id": region_id,
                "printed_page_label": region.get("printed_page_label"),
                "bbox_pdf_points": list(region["canonical_bbox_pdf_points"]),
            })
    row["row_sha256"] = _row_hash(row)
    return row


def _ocr_gold_row(item: Mapping[str, Any], submitted: Mapping[str, Any], attestation_id: str) -> dict[str, Any]:
    review = submitted["review"]
    excluded = review.get("review_status") != "complete"
    row: dict[str, Any] = {
        "schema_version": SCAN_GOLD_REVIEW_OCR_GOLD_SCHEMA,
        "protocol": SCAN_GOLD_REVIEW_PROTOCOL,
        "compiler_version": SCAN_GOLD_REVIEW_COMPILER_VERSION,
        "item_id": item["item_id"],
        "item_sha256": item["item_sha256"],
        "input_hashes": deepcopy(submitted["input_hashes"]),
        "physical_page": item["physical_page"],
        "region_id": item["region_id"],
        "region_order": item["region_order"],
        "sample_order": item["sample_order"],
        "review_status": review.get("review_status"),
        "excluded": excluded,
        "excluded_reason": review.get("excluded_reason") if excluded else None,
        "canonical_text": None if excluded else review.get("canonical_text"),
        "reviewer_instance": submitted.get("reviewer_instance"),
        "review_session_id": submitted.get("review_session_id"),
        "attestation_id": attestation_id,
        "compiler_authored": False,
        "verified_gold": False,
        "promotion_allowed": False,
        "release_included": False,
    }
    row["row_sha256"] = _row_hash(row)
    return row


def _revision_file_hashes(root: Path, names: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in names:
        result[name] = sha256_file(_relative_instance_file(root, name, "scan_gold_revision_file"))
    return result


def _revision_counts(structure_rows: Sequence[Mapping[str, Any]], ocr_rows: Sequence[Mapping[str, Any]], expected_structure: int, expected_ocr: int) -> dict[str, Any]:
    structure_excluded = sum(1 for row in structure_rows if row.get("excluded") is True)
    ocr_excluded = sum(1 for row in ocr_rows if row.get("excluded") is True)
    ocr_status_counts = {status: sum(1 for row in ocr_rows if row.get("review_status") == status) for status in sorted(OCR_STATUSES)}
    return {
        "structure_expected": expected_structure,
        "structure_rows": len(structure_rows),
        "structure_included": len(structure_rows) - structure_excluded,
        "structure_excluded": structure_excluded,
        "ocr_expected": expected_ocr,
        "ocr_rows": len(ocr_rows),
        "ocr_included": len(ocr_rows) - ocr_excluded,
        "ocr_excluded": ocr_excluded,
        "ocr_status_counts": ocr_status_counts,
        "gold_rows": len(structure_rows) + len(ocr_rows),
    }


def _revision_manifest_hash(manifest: Mapping[str, Any]) -> str:
    return _hash_without(manifest, "revision_sha256")


def finalize_scan_gold_review(
    workbench: Path,
    submission: Path,
    output: Path,
    *,
    attestation: Path | None = None,
    finalized_at: str = "2026-08-28T00:00:00+08:00",
    revision_id: str | None = None,
) -> dict[str, Any]:
    """Finalize an external submission into a private, zero-or-Gold revision.

    Missing/invalid human coverage and a missing external attestation pause the
    revision and write zero Gold rows.  The compiler never creates the
    attestation or upgrades a paused revision.
    """

    facts = _load_scan_gold_workbench(Path(workbench).expanduser())
    loaded = _load_scan_gold_submission(Path(submission).expanduser(), facts)
    issues = list(loaded["issues"])
    issues.extend(_reviewer_overlap_issues(facts, loaded))
    attestation_facts: dict[str, Any] | None = None
    if attestation is None:
        issues.append("external_attestation_missing")
    else:
        attestation_facts = _load_scan_gold_attestation(Path(attestation).expanduser(), facts, loaded["file_sha256"])
    issues = sorted(set(issues))
    status = "accepted-for-evaluation" if not issues and attestation_facts is not None else "paused-independent-scan-gold"
    destination = _secure_directory(Path(output).expanduser(), must_be_empty=True)
    _copy_private_file(loaded["path"], destination / "review-submission.json")
    copied_attestation_name: str | None = None
    attestation_id: str | None = None
    if attestation_facts is not None:
        copied_attestation_name = "external-attestation.json"
        _copy_private_file(attestation_facts["path"], destination / copied_attestation_name)
        attestation_id = str(attestation_facts["value"]["attestation_id"])
    structure_rows: list[dict[str, Any]] = []
    ocr_rows: list[dict[str, Any]] = []
    if status == "accepted-for-evaluation":
        for item in facts["structure_items"]:
            structure_rows.append(_structure_gold_row(item, loaded["structure"][str(item["item_id"])] | {"reviewer_instance": facts["data"]["reviewer"]["reviewer_instance"], "review_session_id": facts["data"]["reviewer"]["review_session_id"]}, str(attestation_id)))
        for item in facts["ocr_items"]:
            ocr_rows.append(_ocr_gold_row(item, loaded["ocr"][str(item["item_id"])] | {"reviewer_instance": facts["data"]["reviewer"]["reviewer_instance"], "review_session_id": facts["data"]["reviewer"]["review_session_id"]}, str(attestation_id)))
    _write_jsonl(destination / "scan-gold-structure.jsonl", structure_rows)
    _write_jsonl(destination / "scan-gold-ocr.jsonl", ocr_rows)
    counts = _revision_counts(structure_rows, ocr_rows, len(facts["structure_items"]), len(facts["ocr_items"]))
    revision_id = _safe_id(revision_id or f"scan-gold-revision-{sha256_json({'submission': loaded['file_sha256'], 'attestation': attestation_facts['file_sha256'] if attestation_facts else None})[:24]}", "revision_id")
    file_names = ["review-submission.json", "scan-gold-structure.jsonl", "scan-gold-ocr.jsonl"]
    if copied_attestation_name:
        file_names.insert(1, copied_attestation_name)
    manifest: dict[str, Any] = {
        "schema_version": SCAN_GOLD_REVIEW_REVISION_SCHEMA,
        "protocol": SCAN_GOLD_REVIEW_PROTOCOL,
        "compiler_version": SCAN_GOLD_REVIEW_COMPILER_VERSION,
        "revision_id": revision_id,
        "finalized_at": finalized_at,
        "status": status,
        "workbench_instance_id": facts["data"]["workbench_instance_id"],
        "workbench_manifest_sha256": facts["manifest"]["manifest_sha256"],
        "profile_sha256": facts["profile_identity"]["profile_sha256"],
        "plan_sha256": facts["plan"]["plan_sha256"],
        "source_sha256": facts["plan"]["source"]["source_sha256"],
        "scope": facts["plan"]["source"]["scope"],
        "reviewer": facts["data"]["reviewer"],
        "proposer_instance": facts["plan"]["proposer_instance"],
        "submission_sha256": loaded["file_sha256"],
        "attestation": {"file": copied_attestation_name, "file_sha256": attestation_facts["file_sha256"] if attestation_facts else None, "attestation_id": attestation_id},
        "file_hashes": _revision_file_hashes(destination, file_names),
        "counts": counts,
        "issues": issues,
        "evaluation_allowed": status == "accepted-for-evaluation",
        "thresholds_profile_sha256": facts["profile_identity"]["profile_sha256"],
        "threshold_status": facts["profile"]["evaluation"]["threshold_status"],
        "overall_verdict": "not-authorized",
        "policy": {"private": True, "release_excluded": True, "compiler_authored_gold": False, "verified_gold": False, "promotion_allowed": False, "executable": False, "codex_certified": False, "network_enabled": False, "model_invocations": 0, "mcp_calls": 0},
    }
    manifest["revision_sha256"] = _revision_manifest_hash(manifest)
    _write_json(destination / "scan-gold-revision.json", manifest)
    verified = validate_scan_gold_revision(facts["root"], destination)
    verified.update({"status": status, "revision": str(destination.resolve()), "revision_id": revision_id, "issue_count": len(issues), "issues": issues, "counts": counts, "attestation_id": attestation_id, "evaluation_allowed": status == "accepted-for-evaluation", "release_included": False})
    return verified


def _load_revision_manifest(revision: Path) -> tuple[Path, dict[str, Any]]:
    root = _secure_directory(Path(revision).expanduser())
    for path in root.rglob("*"):
        if path.is_symlink() or path.suffix.casefold() == ".pdf":
            raise ScanGoldReviewError("scan_gold_revision_source_or_symlink_leak")
    manifest = _read_json(root / "scan-gold-revision.json", "scan_gold_revision")
    if manifest.get("schema_version") != SCAN_GOLD_REVIEW_REVISION_SCHEMA or manifest.get("protocol") != SCAN_GOLD_REVIEW_PROTOCOL or manifest.get("compiler_version") != SCAN_GOLD_REVIEW_COMPILER_VERSION:
        raise ScanGoldReviewError("scan_gold_revision_schema_invalid")
    if manifest.get("revision_sha256") != _revision_manifest_hash(manifest):
        raise ScanGoldReviewError("scan_gold_revision_hash_mismatch")
    return root, manifest


def validate_scan_gold_revision(workbench: Path, revision: Path) -> dict[str, Any]:
    facts = _load_scan_gold_workbench(Path(workbench).expanduser())
    root, manifest = _load_revision_manifest(Path(revision).expanduser())
    if manifest.get("workbench_instance_id") != facts["data"]["workbench_instance_id"] or manifest.get("workbench_manifest_sha256") != facts["manifest"]["manifest_sha256"] or manifest.get("profile_sha256") != facts["profile_identity"]["profile_sha256"] or manifest.get("plan_sha256") != facts["plan"]["plan_sha256"] or manifest.get("source_sha256") != facts["plan"]["source"]["source_sha256"] or manifest.get("scope") != facts["plan"]["source"]["scope"]:
        raise ScanGoldReviewError("scan_gold_revision_workbench_binding_invalid")
    if manifest.get("policy") != {"private": True, "release_excluded": True, "compiler_authored_gold": False, "verified_gold": False, "promotion_allowed": False, "executable": False, "codex_certified": False, "network_enabled": False, "model_invocations": 0, "mcp_calls": 0}:
        raise ScanGoldReviewError("scan_gold_revision_policy_invalid")
    file_hashes = manifest.get("file_hashes")
    if not isinstance(file_hashes, Mapping) or not file_hashes:
        raise ScanGoldReviewError("scan_gold_revision_file_inventory_invalid")
    for name, expected_hash in file_hashes.items():
        if not isinstance(name, str) or not _is_hash(expected_hash):
            raise ScanGoldReviewError("scan_gold_revision_file_hash_invalid")
        if sha256_file(_relative_instance_file(root, name, "scan_gold_revision_file")) != expected_hash:
            raise ScanGoldReviewError(f"scan_gold_revision_file_hash_mismatch:{name}")
    if set(file_hashes) - {"review-submission.json", "external-attestation.json", "scan-gold-structure.jsonl", "scan-gold-ocr.jsonl"}:
        raise ScanGoldReviewError("scan_gold_revision_file_unknown")
    submission_path = _relative_instance_file(root, "review-submission.json", "scan_gold_revision_submission")
    loaded = _load_scan_gold_submission(submission_path, facts)
    if manifest.get("submission_sha256") != loaded["file_sha256"]:
        raise ScanGoldReviewError("scan_gold_revision_submission_hash_mismatch")
    issues = list(loaded["issues"])
    issues.extend(_reviewer_overlap_issues(facts, loaded))
    attestation_meta = manifest.get("attestation") if isinstance(manifest.get("attestation"), Mapping) else {}
    attestation_facts: dict[str, Any] | None = None
    attestation_file = attestation_meta.get("file")
    if attestation_file is None:
        issues.append("external_attestation_missing")
    else:
        if attestation_file != "external-attestation.json":
            raise ScanGoldReviewError("scan_gold_revision_attestation_path_invalid")
        attestation_facts = _load_scan_gold_attestation(_relative_instance_file(root, attestation_file, "scan_gold_revision_attestation"), facts, loaded["file_sha256"])
        if attestation_meta.get("file_sha256") != attestation_facts["file_sha256"] or attestation_meta.get("attestation_id") != attestation_facts["value"]["attestation_id"]:
            raise ScanGoldReviewError("scan_gold_revision_attestation_binding_invalid")
    expected_status = "accepted-for-evaluation" if not issues and attestation_facts is not None else "paused-independent-scan-gold"
    if manifest.get("status") != expected_status or manifest.get("evaluation_allowed") is not (expected_status == "accepted-for-evaluation") or sorted(set(manifest.get("issues", []))) != sorted(set(issues)):
        raise ScanGoldReviewError("scan_gold_revision_status_or_issues_invalid")
    structure_rows = _read_jsonl(_relative_instance_file(root, "scan-gold-structure.jsonl", "scan_gold_structure"), "scan_gold_structure")
    ocr_rows = _read_jsonl(_relative_instance_file(root, "scan-gold-ocr.jsonl", "scan_gold_ocr"), "scan_gold_ocr")
    if expected_status != "accepted-for-evaluation":
        if structure_rows or ocr_rows:
            raise ScanGoldReviewError("scan_gold_paused_revision_has_gold_rows")
    else:
        attestation_id = str(attestation_facts["value"]["attestation_id"])
        expected_structure = [_structure_gold_row(item, loaded["structure"][str(item["item_id"])] | {"reviewer_instance": facts["data"]["reviewer"]["reviewer_instance"], "review_session_id": facts["data"]["reviewer"]["review_session_id"]}, attestation_id) for item in facts["structure_items"]]
        expected_ocr = [_ocr_gold_row(item, loaded["ocr"][str(item["item_id"])] | {"reviewer_instance": facts["data"]["reviewer"]["reviewer_instance"], "review_session_id": facts["data"]["reviewer"]["review_session_id"]}, attestation_id) for item in facts["ocr_items"]]
        if structure_rows != expected_structure or ocr_rows != expected_ocr:
            raise ScanGoldReviewError("scan_gold_revision_rows_mismatch")
        for row in structure_rows + ocr_rows:
            if row.get("row_sha256") != _row_hash(row) or row.get("compiler_authored") is not False or row.get("verified_gold") is not False or row.get("promotion_allowed") is not False or row.get("release_included") is not False:
                raise ScanGoldReviewError("scan_gold_revision_row_policy_or_hash_invalid")
    counts = _revision_counts(structure_rows, ocr_rows, len(facts["structure_items"]), len(facts["ocr_items"]))
    if manifest.get("counts") != counts:
        raise ScanGoldReviewError("scan_gold_revision_counts_mismatch")
    return {"status": "verified", "revision_status": expected_status, "revision": str(root.resolve()), "revision_id": manifest["revision_id"], "revision_sha256": manifest["revision_sha256"], "workbench": str(facts["root"].resolve()), "profile_sha256": facts["profile_identity"]["profile_sha256"], "plan_sha256": facts["plan"]["plan_sha256"], "source_sha256": facts["plan"]["source"]["source_sha256"], "counts": counts, "issues": sorted(set(issues)), "evaluation_allowed": expected_status == "accepted-for-evaluation", "release_included": False}


def _edit_distance(left: Sequence[Any], right: Sequence[Any]) -> int:
    previous = list(range(len(right) + 1))
    for left_index, left_value in enumerate(left, 1):
        current = [left_index]
        for right_index, right_value in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[right_index] + 1, previous[right_index - 1] + (left_value != right_value)))
        previous = current
    return previous[-1]


def _metric_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ScanGoldReviewError("scan_gold_evaluation_text_invalid")
    # This is intentionally NFC-only.  The Gold profile forbids hidden case,
    # width, punctuation, numeric, or character normalization.
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n").strip())


def _null_metrics() -> dict[str, Any]:
    return {
        "structure_exact_match": {"value": None, "status": "not-run"},
        "structure_coverage": {"value": None, "status": "not-run"},
        "ocr_cer": {"value": None, "status": "not-run"},
        "ocr_wer": {"value": None, "status": "not-run"},
        "numeric_token_accuracy": {"value": None, "status": "not-run"},
        "ocr_sample_coverage": {"value": None, "status": "not-run"},
        "excluded_counts": {"value": None, "status": "not-run"},
    }


def _prediction_structure_rows(value: Mapping[str, Any], facts: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = value.get("structure_pages")
    if not isinstance(raw, list):
        raise ScanGoldReviewError("scan_gold_prediction_structure_list_required")
    expected = {str(row["item_id"]): row for row in facts["structure_items"]}
    result: dict[str, Mapping[str, Any]] = {}
    for row in raw:
        if not isinstance(row, Mapping) or set(row) != {"item_id", "item_sha256", "input_hashes", "rotation_clockwise", "boundary_px", "region_order", "regions"}:
            raise ScanGoldReviewError("scan_gold_prediction_structure_row_invalid")
        item_id = row.get("item_id")
        if item_id not in expected or item_id in result or row.get("item_sha256") != expected[item_id]["item_sha256"] or row.get("input_hashes") != _expected_review_input_hashes(expected[item_id]):
            raise ScanGoldReviewError(f"scan_gold_prediction_structure_binding_invalid:{item_id}")
        if row.get("rotation_clockwise") not in ROTATIONS or type(row.get("boundary_px")) is not int or row.get("region_order") not in {"left-to-right", "right-to-left"}:
            raise ScanGoldReviewError(f"scan_gold_prediction_structure_value_invalid:{item_id}")
        regions = row.get("regions")
        if not isinstance(regions, list) or len(regions) != 2:
            raise ScanGoldReviewError(f"scan_gold_prediction_structure_regions_invalid:{item_id}")
        seen: set[str] = set()
        for region in regions:
            if not isinstance(region, Mapping) or set(region) != {"region_id", "printed_page_label", "bbox_pdf_points"} or not isinstance(region.get("region_id"), str) or region["region_id"] in seen or not _valid_bbox(region.get("bbox_pdf_points")) or (region.get("printed_page_label") is not None and not isinstance(region.get("printed_page_label"), str)):
                raise ScanGoldReviewError(f"scan_gold_prediction_structure_region_invalid:{item_id}")
            seen.add(region["region_id"])
        result[item_id] = row
    if set(result) != set(expected):
        raise ScanGoldReviewError("scan_gold_prediction_structure_item_set_invalid")
    return result


def _prediction_ocr_rows(value: Mapping[str, Any], facts: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = value.get("ocr_samples")
    if not isinstance(raw, list):
        raise ScanGoldReviewError("scan_gold_prediction_ocr_list_required")
    expected = {str(row["item_id"]): row for row in facts["ocr_items"]}
    result: dict[str, Mapping[str, Any]] = {}
    for row in raw:
        if not isinstance(row, Mapping) or set(row) != {"item_id", "item_sha256", "input_hashes", "text"}:
            raise ScanGoldReviewError("scan_gold_prediction_ocr_row_invalid")
        item_id = row.get("item_id")
        if item_id not in expected or item_id in result or row.get("item_sha256") != expected[item_id]["item_sha256"] or row.get("input_hashes") != _expected_review_input_hashes(expected[item_id]) or not isinstance(row.get("text"), str):
            raise ScanGoldReviewError(f"scan_gold_prediction_ocr_binding_invalid:{item_id}")
        result[item_id] = row
    if set(result) != set(expected):
        raise ScanGoldReviewError("scan_gold_prediction_ocr_item_set_invalid")
    return result


def _load_scan_gold_predictions(path: Path, facts: Mapping[str, Any]) -> tuple[Path, dict[str, Any], dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    prediction_path = _secure_file(Path(path).expanduser(), "scan_gold_predictions")
    value = _read_json(prediction_path, "scan_gold_predictions")
    allowed = {"schema_version", "protocol", "compiler_version", "source_type", "plan_sha256", "profile_sha256", "structure_pages", "ocr_samples"}
    if set(value) != allowed or value.get("schema_version") != SCAN_GOLD_REVIEW_PREDICTION_SCHEMA or value.get("protocol") != SCAN_GOLD_REVIEW_PROTOCOL or value.get("compiler_version") != SCAN_GOLD_REVIEW_COMPILER_VERSION or value.get("source_type") != "private-evaluator-input":
        raise ScanGoldReviewError("scan_gold_prediction_schema_invalid")
    if value.get("plan_sha256") != facts["plan"]["plan_sha256"] or value.get("profile_sha256") != facts["profile_identity"]["profile_sha256"] or _forbidden_key_present(value) is not None:
        raise ScanGoldReviewError("scan_gold_prediction_binding_or_hidden_field_invalid")
    structure = _prediction_structure_rows(value, facts)
    ocr = _prediction_ocr_rows(value, facts)
    return prediction_path, value, structure, ocr


def _structure_match(gold: Mapping[str, Any], predicted: Mapping[str, Any]) -> bool:
    if gold.get("excluded") is True:
        return False
    if gold.get("rotation_clockwise") != predicted.get("rotation_clockwise") or gold.get("split_boundary_px") != predicted.get("boundary_px") or gold.get("region_order") != predicted.get("region_order"):
        return False
    gold_regions = gold.get("regions") if isinstance(gold.get("regions"), list) else []
    predicted_regions = predicted.get("regions") if isinstance(predicted.get("regions"), list) else []
    if len(gold_regions) != len(predicted_regions):
        return False
    return all(left.get("region_id") == right.get("region_id") and left.get("printed_page_label") == right.get("printed_page_label") and _bbox_equal(left.get("bbox_pdf_points"), right.get("bbox_pdf_points")) for left, right in zip(gold_regions, predicted_regions))


def evaluate_scan_gold(
    workbench: Path,
    revision: Path,
    predictions: Path | None,
    output: Path,
    *,
    evaluator_id: str,
    evaluated_at: str = "2026-08-28T00:00:00+08:00",
) -> dict[str, Any]:
    """Evaluate only an accepted, externally attested private revision."""

    evaluator_id = _safe_id(evaluator_id, "evaluator_id")
    destination = _secure_directory(Path(output).expanduser(), must_be_empty=True)
    issues: list[str] = []
    facts: dict[str, Any] | None = None
    revision_facts: dict[str, Any] | None = None
    prediction_path: Path | None = None
    prediction_file_sha256: str | None = None
    metrics = _null_metrics()
    status = "not-run"
    try:
        facts = _load_scan_gold_workbench(Path(workbench).expanduser())
        revision_facts = validate_scan_gold_revision(facts["root"], Path(revision).expanduser())
        revision_manifest = _load_revision_manifest(Path(revision).expanduser())[1]
        reviewer_id = facts["data"]["reviewer"]["reviewer_instance"]
        if evaluator_id in {reviewer_id, facts["plan"]["proposer_instance"]}:
            issues.append("evaluator_reviewer_or_proposer_overlap")
        if not revision_facts["evaluation_allowed"]:
            issues.append("revision_not_accepted_for_evaluation")
        if predictions is None:
            issues.append("evaluator_input_missing")
        if not issues:
            prediction_path, _, predicted_structure, predicted_ocr = _load_scan_gold_predictions(Path(predictions).expanduser(), facts)
            prediction_file_sha256 = sha256_file(prediction_path)
            structure_rows = _read_jsonl(_relative_instance_file(Path(revision).expanduser(), "scan-gold-structure.jsonl", "scan_gold_structure"), "scan_gold_structure")
            ocr_rows = _read_jsonl(_relative_instance_file(Path(revision).expanduser(), "scan-gold-ocr.jsonl", "scan_gold_ocr"), "scan_gold_ocr")
            structure_eligible = [row for row in structure_rows if row.get("excluded") is False]
            structure_correct = sum(1 for row in structure_eligible if _structure_match(row, predicted_structure[str(row["item_id"])]))
            ocr_eligible = [row for row in ocr_rows if row.get("excluded") is False]
            reference_chars = 0
            character_distance = 0
            reference_words = 0
            word_distance = 0
            numeric_total = 0
            numeric_correct = 0
            for row in ocr_eligible:
                gold_text = _metric_text(row.get("canonical_text"))
                predicted_text = _metric_text(predicted_ocr[str(row["item_id"])].get("text"))
                reference_chars += len(gold_text)
                character_distance += _edit_distance(list(gold_text), list(predicted_text))
                gold_words = gold_text.split()
                predicted_words = predicted_text.split()
                reference_words += len(gold_words)
                word_distance += _edit_distance(gold_words, predicted_words)
                gold_numbers = NUMBER_TOKEN.findall(gold_text)
                predicted_numbers = NUMBER_TOKEN.findall(predicted_text)
                numeric_total += max(len(gold_numbers), len(predicted_numbers))
                numeric_correct += sum(left == right for left, right in zip(gold_numbers, predicted_numbers))
            structure_expected = len(structure_rows)
            ocr_expected = len(ocr_rows)
            ocr_excluded = ocr_expected - len(ocr_eligible)
            metrics = {
                "structure_exact_match": {"value": structure_correct / len(structure_eligible) if structure_eligible else None, "status": "computed", "correct": structure_correct, "eligible": len(structure_eligible)},
                "structure_coverage": {"value": len(structure_eligible) / structure_expected if structure_expected else None, "status": "computed", "included": len(structure_eligible), "expected": structure_expected},
                "ocr_cer": {"value": character_distance / reference_chars if reference_chars else None, "status": "computed" if reference_chars else "not-run", "distance": character_distance, "reference_characters": reference_chars},
                "ocr_wer": {"value": word_distance / reference_words if reference_words else None, "status": "computed" if reference_words else "not-run", "distance": word_distance, "reference_words": reference_words},
                "numeric_token_accuracy": {"value": numeric_correct / numeric_total if numeric_total else None, "status": "computed" if numeric_total else "not-run", "correct": numeric_correct, "total": numeric_total},
                "ocr_sample_coverage": {"value": len(ocr_eligible) / ocr_expected if ocr_expected else None, "status": "computed", "included": len(ocr_eligible), "expected": ocr_expected},
                "excluded_counts": {"value": {"structure": structure_expected - len(structure_eligible), "ocr": ocr_excluded, "ocr_status_counts": {status_name: sum(1 for row in ocr_rows if row.get("review_status") == status_name) for status_name in sorted(OCR_STATUSES)}}, "status": "computed"},
            }
            status = "evaluated"
        else:
            status = "paused" if revision_facts and any(issue != "evaluator_input_missing" for issue in issues) else "not-run"
    except (ReviewerWorkbenchError, ScanGoldReviewError, OSError, ValueError) as error:
        issues.append(str(error))
        status = "paused"
    profile_sha256 = facts["profile_identity"]["profile_sha256"] if facts else None
    plan_sha256 = facts["plan"]["plan_sha256"] if facts else None
    revision_sha256 = revision_facts.get("revision_sha256") if revision_facts else None
    evaluation: dict[str, Any] = {
        "schema_version": SCAN_GOLD_REVIEW_EVALUATION_SCHEMA,
        "protocol": SCAN_GOLD_REVIEW_PROTOCOL,
        "compiler_version": SCAN_GOLD_REVIEW_COMPILER_VERSION,
        "evaluated_at": evaluated_at,
        "status": status,
        "evaluator_id": evaluator_id,
        "workbench_instance_id": facts["data"]["workbench_instance_id"] if facts else None,
        "revision_sha256": revision_sha256,
        "plan_sha256": plan_sha256,
        "profile_sha256": profile_sha256,
        "prediction_file_sha256": prediction_file_sha256,
        "metrics": metrics,
        "thresholds": deepcopy(facts["profile"]["evaluation"]["thresholds"]) if facts else None,
        "threshold_status": facts["profile"]["evaluation"]["threshold_status"] if facts else "acceptance-target-candidate",
        "thresholds_source_profile_sha256": profile_sha256,
        "verdict": "not-authorized",
        "accuracy_claim": False,
        "issues": sorted(set(issues)),
        "policy": {"private": True, "release_excluded": True, "test_cannot_change_threshold": True, "promotion_allowed": False, "executable": False, "codex_certified": False, "network_enabled": False, "model_invocations": 0, "mcp_calls": 0},
    }
    evaluation["evaluation_sha256"] = _hash_without(evaluation, "evaluation_sha256")
    _write_json(destination / "scan-gold-evaluation.json", evaluation)
    return {"status": status, "evaluation": str((destination / "scan-gold-evaluation.json").resolve()), "evaluation_sha256": evaluation["evaluation_sha256"], "metrics": metrics, "threshold_status": evaluation["threshold_status"], "verdict": "not-authorized", "issues": sorted(set(issues)), "release_included": False, "prediction_file_sha256": prediction_file_sha256}


def render_scan_gold_html(data: Mapping[str, Any]) -> str:
    """Render the self-contained local review page from frozen workpack data."""

    html_data = deepcopy(dict(data))
    for item in html_data.get("structure_items", []):
        mapping = item.get("locked_facts", {}).get("context_coordinate_mapping", {}) if isinstance(item, Mapping) else {}
        transform = mapping.get("transform") if isinstance(mapping, Mapping) else None
        if isinstance(transform, dict):
            # The UI needs the hash-bound matrix, not the internal transform
            # schema label; omitting that label also keeps the page free of
            # source-format identifiers.
            transform.pop("schema_version", None)
    embedded = json.dumps(html_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).replace("</", "<\\/")
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; object-src 'none'">
<title>Portable Scan Gold Review</title>
<style>
:root{color-scheme:light;--ink:#18212b;--muted:#66727d;--line:#d6dde3;--paper:#f7f9fa;--blue:#155eef;--amber:#fff3cd;--red:#b42318;--green:#087443}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Helvetica Neue",Arial,sans-serif}button,input,select,textarea{font:inherit}button{border:1px solid #b8c3cd;background:#fff;border-radius:6px;padding:7px 10px;cursor:pointer}button:hover{border-color:var(--blue)}button.primary{background:var(--blue);border-color:var(--blue);color:#fff}.shell{max-width:1500px;margin:0 auto;padding:18px}.top{display:flex;gap:16px;align-items:flex-start;justify-content:space-between;border-bottom:1px solid var(--line);padding-bottom:14px}.top h1{font-size:22px;margin:0 0 3px}.top p{color:var(--muted);margin:0}.facts{display:grid;grid-template-columns:repeat(4,minmax(140px,1fr));gap:8px;margin:14px 0}.fact{background:#fff;border:1px solid var(--line);padding:9px;border-radius:6px}.fact small{color:var(--muted);display:block}.fact code{font-size:11px;word-break:break-all}.notice{background:var(--amber);border:1px solid #e8c45c;padding:10px;border-radius:6px;margin:12px 0}.toolbar{display:flex;gap:7px;flex-wrap:wrap;margin:12px 0}.layout{display:grid;grid-template-columns:270px minmax(0,1fr) 360px;gap:12px;align-items:start}.panel{background:#fff;border:1px solid var(--line);border-radius:7px;overflow:hidden}.panel>h2{font-size:14px;margin:0;padding:10px 12px;border-bottom:1px solid var(--line)}.list{max-height:calc(100vh - 260px);overflow:auto}.list-group{padding:8px;border-bottom:1px solid var(--line)}.list-group h3{font-size:12px;color:var(--muted);margin:3px 5px 6px}.item-row{display:flex;align-items:center;gap:5px;width:100%;text-align:left;border:0;border-radius:4px;padding:6px 7px}.item-row.active{background:#e7efff;color:#0c3c99}.item-row.done::after{content:"✓";margin-left:auto;color:var(--green)}.item-row input{margin:0}.viewer{padding:12px}.viewer h2{margin:0 0 8px;font-size:17px}.locked{background:#f4f6f8;border:1px solid var(--line);border-radius:5px;padding:8px;margin:8px 0;color:#45525e;font-size:12px}.locked code{word-break:break-all}.context{max-height:42vh;overflow:auto;text-align:center;background:#eef1f4;border:1px solid var(--line);padding:8px}.context img{max-width:100%;height:auto;display:block;margin:auto}.regions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:8px}.region-card{border:1px solid var(--line);border-radius:5px;padding:7px}.region-card img{width:100%;height:160px;object-fit:contain;background:#eef1f4}.region-card h3{font-size:12px;margin:0 0 5px}.sample-view{text-align:center;background:#eef1f4;padding:8px;border:1px solid var(--line)}.sample-view img{max-width:100%;max-height:360px}.form{padding:12px;border-left:1px solid var(--line)}.form h2{font-size:15px;margin:0 0 8px}.form h3{font-size:13px;border-top:1px solid var(--line);padding-top:10px;margin:13px 0 7px}.field{display:grid;grid-template-columns:135px minmax(0,1fr);gap:7px;align-items:center;margin:7px 0}.field label{color:#485560;font-size:12px}.field input,.field select,.field textarea{width:100%;border:1px solid #bdc7d0;border-radius:4px;padding:6px;background:#fff}.field textarea{min-height:100px;resize:vertical}.region-form{border:1px solid var(--line);border-radius:5px;padding:7px;margin:7px 0}.region-form h4{margin:0 0 5px;font-size:12px}.region-form .field{grid-template-columns:105px minmax(0,1fr)}.small{font-size:11px;color:var(--muted)}.status{min-height:22px;color:var(--green);font-size:12px}.danger{color:var(--red)}.checks{display:grid;gap:5px}.checks label{font-size:12px}.draw-tools{display:flex;gap:5px;flex-wrap:wrap;margin:8px 0}.draw-stage{position:relative;display:inline-block;max-width:100%;overflow:hidden;background:#eef1f4;border:1px solid var(--line);touch-action:none}.draw-stage img{display:block;max-width:100%;max-height:330px}.draw-stage svg{position:absolute;inset:0;width:100%;height:100%;pointer-events:auto}.draw-stage rect{fill:rgba(21,94,239,.16);stroke:var(--blue);stroke-width:2}.coord{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;color:#334e68;background:#f4f6f8;padding:6px;border-radius:4px;word-break:break-all}.footer-note{margin-top:16px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:10px}@media(max-width:1100px){.layout{grid-template-columns:230px minmax(0,1fr)}.form{grid-column:1/-1;border-left:0;border-top:1px solid var(--line)}}@media(max-width:700px){.shell{padding:9px}.facts{grid-template-columns:repeat(2,minmax(0,1fr))}.layout{display:block}.list{max-height:260px}.regions{grid-template-columns:1fr}.top{display:block}}
</style>
<style>
.draw-viewport{max-width:100%;height:330px;overflow:auto;text-align:left;background:#eef1f4;border:1px solid var(--line);padding:8px}
.draw-stage{max-width:none;overflow:visible;display:block;position:relative}
.draw-content{position:relative;transform-origin:top left;touch-action:none}
.draw-content img{display:block;max-width:none;max-height:none;width:100%;height:100%}
.draw-content svg{position:absolute;inset:0;width:100%;height:100%;pointer-events:auto}
.region-card{border-width:2px;text-align:left;min-width:0}
.region-card.active{border-color:var(--blue);box-shadow:0 0 0 2px rgba(21,94,239,.12)}
.region-tabs{display:flex;gap:6px;flex-wrap:wrap}
.region-tab.active{background:var(--blue);border-color:var(--blue);color:#fff}
.sample-view img{max-height:520px}
</style>
</head>
<body>
<div class="shell">
<div class="top"><div><h1>Portable Scan Gold Review</h1><p>本页面只提供冻结的 Context / region / full-region OCR samples 与锁定事实；浏览器不生成 Gold，不显示隐藏结果。</p></div><div class="small">候选协议：""" + SCAN_GOLD_REVIEW_COMPILER_VERSION + r""" · 状态：独立人工 Gold 等待中</div></div>
<div class="facts" id="facts"></div>
<div class="notice">审查人必须先实际查看所选结构页。批量通过只接受仍为空白、未被人工修改的页面，并逐页使用自己的冻结候选、留下审计；异常页必须单独处理。OCR Gold 使用分层抽取页的完整 region，文本框初始为空，不能从任何候选结果复制或预填。</div>
<div class="toolbar"><button class="primary" id="save">保存草稿</button><button id="import">导入同一 workbench submission</button><input id="import-file" type="file" accept="application/json" hidden><button id="export">导出 JSON</button><button id="check">Finalize 前检查</button><button id="select-all">选择全部结构页</button><button id="clear-all">清除结构选择</button><span class="status" id="status"></span></div>
<div class="layout">
  <section class="panel"><h2>逐项导航</h2><div class="list" id="item-list"></div></section>
  <section class="panel"><div class="viewer" id="viewer"></div></section>
  <section class="panel"><div class="form" id="form"></div></section>
</div>
<div class="footer-note">导出的 submission 仍是私人审查输入，不等于 accepted revision。完成后由项目操作者在本地执行 finalize；外部 attestation 必须在 workbench 之外真实提供。</div>
</div>
<script id="workpack" type="application/json">""" + embedded + r"""</script>
<script>
(function(){
"use strict";
const data=JSON.parse(document.getElementById("workpack").textContent);
const storageKey=data.ui_policy.autosave_namespace;
const structure=data.structure_items, ocr=data.ocr_samples;
const BATCH_CONFIRM_FIELDS=["rotation","boundary","region_order"];
const BATCH_PASS_FIELDS=["review_status","rotation","boundary","region_order","page_readability","page_abnormal_state","region_bbox","region_label_state","region_readability","region_abnormal_state"];
const itemById=Object.fromEntries(structure.concat(ocr).map(x=>[x.item_id,x]));
const state={structure:Object.fromEntries(structure.map(x=>[x.item_id,blankStructure(x)])),ocr:Object.fromEntries(ocr.map(x=>[x.item_id,blankOcr(x)])),declarations:Object.fromEntries(["qualified_reviewer","reviewed_specified_source_evidence","did_not_view_hidden_results","did_not_change_split","reviewed_specified_render_region","not_evaluator","independent_from_proposer"].map(k=>[k,false])),batch_audit:[],selected:new Set(),current:structure[0].item_id,view:{zoom:1,panX:0,panY:0,regionIndex:0,box:null,drawing:false,start:null}};
function blankStructure(item){return {review_status:"needs_review",exclusion_reason:"",rotation_verdict:"",canonical_rotation_clockwise:null,split_verdict:"",canonical_boundary_px:null,canonical_region_order:"",canonical_region_ids:null,page_readability:"needs_review",abnormal_state:"needs_review",regions:item.locked_facts.regions.map(r=>({region_id:r.region_id,region_order:r.region_order,bbox_verdict:"uncertain",canonical_bbox_pdf_points:null,label_state:"needs_review",printed_page_label:null,readability:"needs_review",abnormal_state:"needs_review",notes:""})),notes:""};}
function blankOcr(){return {review_status:"needs_review",canonical_text:"",excluded_reason:"",notes:""};}
function esc(v){return String(v==null?"":v).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));}
function opt(value,label,selected){return "<option value=\""+esc(value)+"\""+(selected===value?" selected":"")+">"+esc(label)+"</option>";}
function selectHtml(field,items,value){return "<select data-field=\""+field+"\"><option value=\"\">请选择</option>"+items.map(x=>opt(x[0],x[1],value)).join("")+"</select>";}
function regionSelectHtml(index,field,items,value){return "<select data-region-field=\""+field+"\" data-region-index=\""+index+"\"><option value=\"\">请选择</option>"+items.map(x=>opt(x[0],x[1],value)).join("")+"</select>";}
function setStatus(text,danger){const el=document.getElementById("status");el.textContent=text;el.className=danger?"status danger":"status";}
function currentItem(){return itemById[state.current];}
function currentReview(){return state.current.indexOf("scan-structure-")===0?state.structure[state.current]:state.ocr[state.current];}
function renderFacts(){document.getElementById("facts").innerHTML=[["workbench",data.workbench_instance_id],["reviewer",data.reviewer.reviewer_instance],["session",data.reviewer.review_session_id],["profile",data.profile.profile_sha256],["plan",data.plan_identity.plan_sha256],["source",data.plan_identity.source_sha256],["scope",data.plan_identity.scope[0]+"–"+data.plan_identity.scope[data.plan_identity.scope.length-1]+" physical pages"],["OCR Gold",data.item_counts.ocr_samples+" full regions"]].map(x=>"<div class=\"fact\"><small>"+esc(x[0])+"</small><code>"+esc(x[1])+"</code></div>").join("");}
function lockedSummary(item){const f=item.locked_facts;const parts=["item="+item.item_id,"item_sha256="+item.item_sha256,"source_sha256="+f.source_sha256];if(item.item_type==="structure_page")parts.push("context_sha256="+f.context.sha256,"rotation_candidate="+f.rotation_clockwise_candidate,"split_core_sha256="+f.split_core_sha256);else parts.push("sample_rule="+f.sample_rule_id,"sample_sha256="+f.sample.sha256,"sample_bbox_pdf="+JSON.stringify(f.sample_bbox_pdf_points));return "<div class=\"locked\"><b>Locked facts</b><br><code>"+esc(parts.join("\n")) +"</code></div>";}
function renderForm(){
  const item=currentItem(),r=currentReview();
  let html="<h2>人工决定</h2>";
  if(item.item_type==="structure_page"){
    html+="<div class=\"field\"><label>页面状态</label>"+selectHtml("review_status",[["complete","complete"],["abnormal","abnormal"],["unreadable","unreadable"],["needs_review","needs_review"]],r.review_status)+"</div><div class=\"field\"><label>排除/异常理由</label><input data-field=\"exclusion_reason\" value=\""+esc(r.exclusion_reason)+"\"></div><div class=\"field\"><label>rotation verdict</label>"+selectHtml("rotation_verdict",[["confirmed","confirmed"],["corrected","corrected"]],r.rotation_verdict)+"</div><div class=\"field\"><label>顺时针 rotation</label>"+selectHtml("canonical_rotation_clockwise",[["0","0°"],["90","90°"],["180","180°"],["270","270°"]],r.canonical_rotation_clockwise==null?"":String(r.canonical_rotation_clockwise))+"</div><div class=\"field\"><label>split verdict</label>"+selectHtml("split_verdict",[["confirmed","confirmed"],["corrected","corrected"]],r.split_verdict)+"</div><div class=\"field\"><label>boundary px</label><input type=\"number\" data-field=\"canonical_boundary_px\" value=\""+esc(r.canonical_boundary_px==null?"":r.canonical_boundary_px)+"\"></div><div class=\"field\"><label>region order</label>"+selectHtml("canonical_region_order",[["left-to-right","left-to-right"],["right-to-left","right-to-left"]],r.canonical_region_order)+"</div><div class=\"field\"><label>页面可读性</label>"+selectHtml("page_readability",[["readable","readable"],["unreadable","unreadable"],["not_text","not_text"],["needs_review","needs_review"]],r.page_readability)+"</div><div class=\"field\"><label>异常状态</label>"+selectHtml("abnormal_state",[["none","none"],["skew","skew"],["crop_loss","crop_loss"],["occlusion","occlusion"],["fold","fold"],["blur","blur"],["other","other"],["needs_review","needs_review"]],r.abnormal_state)+"</div>";
    html+="<h3>逐 region 决定</h3>"+item.locked_facts.regions.map((locked,i)=>{
      const rr=r.regions[i];
      return "<div class=\"region-form\"><h4>region "+i+" · "+esc(locked.region_id)+"</h4><div class=\"field\"><label>BBox verdict</label>"+regionSelectHtml(i,"bbox_verdict",[["confirmed","confirmed"],["corrected","corrected"],["uncertain","uncertain"]],rr.bbox_verdict)+"</div><div class=\"field\"><label>BBox [x0,y0,x1,y1]</label><input data-region-field=\"canonical_bbox_pdf_points\" data-region-index=\""+i+"\" value=\""+esc(rr.canonical_bbox_pdf_points?rr.canonical_bbox_pdf_points.join(","):"")+"\" placeholder=\"仅 corrected 时填写\"></div><div class=\"field\"><label>label 状态</label>"+regionSelectHtml(i,"label_state",[["confirmed","confirmed"],["unreadable","unreadable"],["not_text","not_text"],["needs_review","needs_review"]],rr.label_state)+"</div><div class=\"field\"><label>打印页标签</label><input data-region-field=\"printed_page_label\" data-region-index=\""+i+"\" value=\""+esc(rr.printed_page_label||"")+"\" placeholder=\"confirmed 可留 null\"></div><div class=\"field\"><label>可读性</label>"+regionSelectHtml(i,"readability",[["readable","readable"],["unreadable","unreadable"],["not_text","not_text"],["needs_review","needs_review"]],rr.readability)+"</div><div class=\"field\"><label>异常状态</label>"+regionSelectHtml(i,"abnormal_state",[["none","none"],["skew","skew"],["crop_loss","crop_loss"],["occlusion","occlusion"],["fold","fold"],["blur","blur"],["other","other"],["needs_review","needs_review"]],rr.abnormal_state)+"</div><div class=\"field\"><label>备注</label><input data-region-field=\"notes\" data-region-index=\""+i+"\" value=\""+esc(rr.notes)+"\"></div></div>";
    }).join("")+"<div class=\"field\"><label>页面备注</label><textarea data-field=\"notes\">"+esc(r.notes)+"</textarea></div>";
  }else{
    html+="<div class=\"field\"><label>样本状态</label>"+selectHtml("review_status",[["complete","complete"],["unreadable","unreadable"],["not_text","not_text"],["needs_review","needs_review"]],r.review_status)+"</div><div class=\"field\"><label>canonical_text</label><textarea data-field=\"canonical_text\" placeholder=\"从冻结 sample render 人工抄录；初始为空\">"+esc(r.canonical_text)+"</textarea></div><div class=\"field\"><label>排除理由</label><input data-field=\"excluded_reason\" value=\""+esc(r.excluded_reason)+"\" placeholder=\"unreadable/not_text/needs_review 必填\"></div><div class=\"field\"><label>备注</label><textarea data-field=\"notes\">"+esc(r.notes)+"</textarea></div>";
  }
  document.getElementById("form").innerHTML=html;
  bindFields();
}
function render(){renderFacts();renderList();renderViewer();renderForm();}
function bindFields(){document.querySelectorAll("[data-field]").forEach(el=>{const event=el.tagName==="TEXTAREA"||el.type==="text"?"input":"change";el.addEventListener(event,()=>updateReviewField(el.dataset.field,el.value));});document.querySelectorAll("[data-region-field]").forEach(el=>{const event=el.type==="text"?"input":"change";el.addEventListener(event,()=>updateRegionField(Number(el.dataset.regionIndex),el.dataset.regionField,el.value));});}
function selectedRegion(){return currentItem().locked_facts.regions[state.view.regionIndex];}
function submissionValue(){return {schema_version:data.schema_version,protocol:data.protocol,compiler_version:data.compiler_version,workbench_instance_id:data.workbench_instance_id,plan_sha256:data.plan_identity.plan_sha256,profile_sha256:data.profile.profile_sha256,reviewer:data.reviewer,declarations:state.declarations,structure_items:structure.map(item=>({item_id:item.item_id,item_sha256:item.item_sha256,input_hashes:inputHashes(item),review:state.structure[item.item_id]})),ocr_samples:ocr.map(item=>({item_id:item.item_id,item_sha256:item.item_sha256,input_hashes:inputHashes(item),review:state.ocr[item.item_id]})),batch_audit:state.batch_audit,exported_at:new Date().toISOString()};}
function inputHashes(item){const f=item.locked_facts;if(item.item_type==="structure_page")return {source_sha256:f.source_sha256,context_sha256:f.context.sha256,split_contract_sha256:f.split_contract_sha256,region_sha256s:Object.fromEntries(f.regions.map(x=>[x.region_id,x.crop.sha256]).sort())};return {source_sha256:f.source_sha256,context_sha256:f.context.sha256,region_sha256:f.region.sha256,sample_sha256:f.sample.sha256,sample_bbox_sha256:sampleBboxHash(f)};}
function sampleBboxHash(f){return f.sample_bbox_sha256||"";}
function download(name,value){const blob=new Blob([JSON.stringify(value,null,2)+"\n"],{type:"application/json"}),a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);}
function saveDraft(){try{localStorage.setItem(storageKey,JSON.stringify({structure:state.structure,ocr:state.ocr,declarations:state.declarations,batch_audit:state.batch_audit}));setStatus("草稿已保存到本机浏览器");}catch(e){setStatus("浏览器不允许本地草稿保存",true);}}
function loadDraft(){try{const raw=localStorage.getItem(storageKey);if(!raw)return;const v=JSON.parse(raw);if(v&&v.structure&&v.ocr){Object.assign(state.structure,v.structure);Object.assign(state.ocr,v.ocr);if(v.declarations)Object.assign(state.declarations,v.declarations);if(Array.isArray(v.batch_audit))state.batch_audit=v.batch_audit;setStatus("已载入本机草稿");}}catch(e){setStatus("草稿读取失败，保留空白状态",true);}}
function sortedRegionIds(item,order){const ids=item.locked_facts.regions.slice().sort((a,b)=>a.region_order-b.region_order).map(x=>x.region_id);return order==="right-to-left"?ids.reverse():ids;}
function candidateFields(item){const f=item.locked_facts;return {rotation:f.rotation_clockwise_candidate,boundary:f.split.boundary_px,region_order:f.split.reading_order};}
function validCandidate(c){return c&&[0,90,180,270].includes(c.rotation)&&Number.isInteger(c.boundary)&&c.boundary>0&&["left-to-right","right-to-left"].includes(c.region_order);}
function sameValue(a,b){const normalize=v=>Array.isArray(v)?v.map(normalize):v&&typeof v==="object"?Object.fromEntries(Object.keys(v).sort().map(k=>[k,normalize(v[k])])):v;return JSON.stringify(normalize(a))===JSON.stringify(normalize(b));}
function bboxEqual(a,b){return Array.isArray(a)&&Array.isArray(b)&&a.length===4&&b.length===4&&a.every((x,i)=>Number.isFinite(Number(x))&&Math.abs(Number(x)-Number(b[i]))<=1e-6);}
function bboxValid(b){return Array.isArray(b)&&b.length===4&&b.every(x=>Number.isFinite(Number(x)))&&Number(b[2])>Number(b[0])&&Number(b[3])>Number(b[1]);}
function clearStructureScorable(item,r){const prior=Object.fromEntries((r.regions||[]).map(x=>[x.region_id,x]));r.canonical_rotation_clockwise=null;r.canonical_boundary_px=null;r.canonical_region_order="";r.canonical_region_ids=null;r.rotation_verdict="";r.split_verdict="";r.page_readability="needs_review";r.abnormal_state="needs_review";r.regions=item.locked_facts.regions.map(x=>({region_id:x.region_id,region_order:x.region_order,bbox_verdict:"uncertain",canonical_bbox_pdf_points:null,label_state:"needs_review",printed_page_label:null,readability:"needs_review",abnormal_state:"needs_review",notes:prior[x.region_id]&&typeof prior[x.region_id].notes==="string"?prior[x.region_id].notes:""}));}
function clearOcrScorable(r){r.canonical_text="";}
function ensureBatchButtons(){let confirmButton=document.getElementById("batch-confirm");if(!confirmButton){confirmButton=document.createElement("button");confirmButton.id="batch-confirm";confirmButton.textContent="批量确认结构候选";document.querySelector(".toolbar").appendChild(confirmButton);}confirmButton.onclick=doBatch;let passButton=document.getElementById("batch-pass");if(!passButton){passButton=document.createElement("button");passButton.id="batch-pass";passButton.textContent="批量通过所选结构页";document.querySelector(".toolbar").appendChild(passButton);}passButton.onclick=doBatchPass;}
  function renderList(){const itemDone=(item,r)=>{if(!r||r.review_status==="needs_review")return false;const reason=item.item_type==="structure_page"?r.exclusion_reason:r.excluded_reason;if(r.review_status!=="complete"&&(typeof reason!=="string"||!reason.trim()))return false;return (item.item_type==="structure_page"?validateStructureReview(item,r):validateOcrReview(r)).length===0;};const make=(items,kind)=>"<div class=\"list-group\"><h3>"+(kind==="structure"?"结构 Gold · "+items.length+" 页":"OCR Gold · "+items.length+" full regions")+"</h3>"+items.map(item=>{const r=kind==="structure"?state.structure[item.item_id]:state.ocr[item.item_id];const done=itemDone(item,r);return "<button class=\"item-row "+(state.current===item.item_id?"active ":"")+(done?"done":"")+"\" data-item=\""+item.item_id+"\"><input type=\"checkbox\" data-select=\""+item.item_id+"\" "+(state.selected.has(item.item_id)?"checked":"")+"><span>"+(kind==="structure"?"p"+String(item.physical_page).padStart(2,"0"):"p"+String(item.physical_page).padStart(2,"0")+" r"+item.region_order)+"</span><span class=\"small\">"+esc(r.review_status)+"</span></button>";}).join("")+"</div>";document.getElementById("item-list").innerHTML=make(structure,"structure")+make(ocr,"ocr");ensureBatchButtons();document.querySelectorAll("[data-item]").forEach(el=>el.addEventListener("click",e=>{if(e.target.matches("input"))return;state.current=el.dataset.item;state.view.box=null;state.view.panX=0;state.view.panY=0;setStatus("",false);render();}));document.querySelectorAll("[data-select]").forEach(el=>el.addEventListener("click",e=>{e.stopPropagation();if(el.checked)state.selected.add(el.dataset.select);else state.selected.delete(el.dataset.select);}));}
function contextTransform(item){return item.locked_facts.context_coordinate_mapping.transform;}
function mapContextPoint(item,x,y){const m=contextTransform(item).matrix_3x3;const d=m[6]*x+m[7]*y+m[8];if(Math.abs(d)<1e-12)throw new Error("context mapping singular");return [(m[0]*x+m[1]*y+m[2])/d,(m[3]*x+m[4]*y+m[5])/d];}
function mapContextBBox(item,b){const points=[[b[0],b[1]],[b[2],b[1]],[b[2],b[3]],[b[0],b[3]]].map(p=>mapContextPoint(item,p[0],p[1]));return [Math.min(...points.map(p=>p[0])),Math.min(...points.map(p=>p[1])),Math.max(...points.map(p=>p[0])),Math.max(...points.map(p=>p[1]))].map(x=>Math.round(x*1e6)/1e6);}
function renderViewer(){const item=currentItem();let html="<h2>"+esc(item.item_type==="structure_page"?"结构页 "+item.physical_page:"OCR 完整区域 "+item.physical_page+" / region "+item.region_order)+"</h2>"+lockedSummary(item);if(item.item_type==="structure_page"){const f=item.locked_facts,d=f.context_coordinate_mapping.context_dimensions_px;html+="<div class=\"context\"><img src=\""+esc(f.context.asset)+"\" alt=\"frozen context render\"></div><div class=\"regions\">"+f.regions.map((r,i)=>"<button type=\"button\" data-region-tab=\""+i+"\" class=\"region-card "+(i===state.view.regionIndex?"active":"")+"\"><h3>Region "+i+" · 点击切换</h3><img src=\""+esc(r.crop.asset)+"\" alt=\"frozen region render\"><div class=\"small\">BBox x0="+esc(r.bbox_pdf_points[0])+" · y0="+esc(r.bbox_pdf_points[1])+" · x1="+esc(r.bbox_pdf_points[2])+" · y1="+esc(r.bbox_pdf_points[3])+"</div></button>").join("")+"</div><h3>当前 Region "+state.view.regionIndex+"：BBox 坐标读数 / 框选 BBox</h3><div class=\"draw-tools\"><div class=\"region-tabs\">"+f.regions.map((r,i)=>"<button type=\"button\" data-region-tab=\""+i+"\" class=\"region-tab "+(i===state.view.regionIndex?"active":"")+"\">Region "+i+"</button>").join("")+"</div><button id=\"zoom-out\">−</button><button id=\"zoom-in\">+</button><button id=\"zoom-fit\">适合窗口</button><button id=\"zoom-100\">原始像素 100%</button><button id=\"pan\">平移页面</button><button id=\"draw\">框选 BBox</button><button id=\"apply-box\">应用为 corrected BBox</button></div><div class=\"draw-viewport\" id=\"draw-viewport\"><div class=\"draw-stage\" id=\"draw-stage\"><div class=\"draw-content\" id=\"draw-content\"><img id=\"bbox-image\" src=\""+esc(f.context.asset)+"\" width=\""+d.width+"\" height=\""+d.height+"\" alt=\"full frozen context render\"><svg id=\"bbox-svg\" viewBox=\"0 0 "+d.width+" "+d.height+"\" width=\""+d.width+"\" height=\""+d.height+"\" preserveAspectRatio=\"none\"><rect id=\"bbox-rect\" x=\"0\" y=\"0\" width=\"0\" height=\"0\"></rect></svg></div></div></div><div class=\"coord\" id=\"coord\">尚未框选</div>";}else{html+="<div class=\"sample-view\"><img src=\""+esc(item.locked_facts.sample.asset)+"\" alt=\"frozen full-region text sample render\"></div><div class=\"small\">这是分层选中物理页的完整 region；canonical text 必须按自然阅读顺序逐行人工转写，不可从页面以外的候选内容粘贴。</div>";}document.getElementById("viewer").innerHTML=html;bindViewer();}
function updateReviewField(field,value){const item=currentItem(),r=currentReview();if(field==="canonical_rotation_clockwise"||field==="canonical_boundary_px")value=value===""?null:Number(value);if(field==="review_status"){r.review_status=value;if(item.item_type==="structure_page"){if(value!=="complete")clearStructureScorable(item,r);else r.exclusion_reason="";}else{if(value!=="complete")clearOcrScorable(r);else r.excluded_reason="";}}else{r[field]=value;if(field==="canonical_region_order"){const ids=sortedRegionIds(item,value);r.canonical_region_ids=value==="right-to-left"||value==="left-to-right"?ids:null;}}render();}
function updateRegionField(index,field,value){const item=currentItem(),rr=currentReview().regions[index],locked=item.locked_facts.regions[index];if(field==="canonical_bbox_pdf_points"){const nums=value.split(",").map(x=>Number(x.trim()));rr[field]=nums.length===4&&nums.every(Number.isFinite)?nums:null;}else if(field==="bbox_verdict"){rr[field]=value;if(value==="confirmed")rr.canonical_bbox_pdf_points=locked.bbox_pdf_points.slice();if(value==="uncertain")rr.canonical_bbox_pdf_points=null;}else if(field==="printed_page_label"){rr[field]=value.trim()===""?null:value;}else if(field==="label_state"){rr[field]=value;if(value!=="confirmed")rr.printed_page_label=null;}else rr[field]=value;render();}
function bindViewer(){const item=currentItem();if(item.item_type!=="structure_page")return;document.querySelectorAll("[data-region-tab]").forEach(tab=>tab.addEventListener("click",()=>{state.view.regionIndex=Number(tab.dataset.regionTab);state.view.box=null;renderViewer();}));document.getElementById("zoom-out").addEventListener("click",()=>{state.view.zoom=Math.max(.25,state.view.zoom/1.25);applyTransform();});document.getElementById("zoom-in").addEventListener("click",()=>{state.view.zoom=Math.min(5,state.view.zoom*1.25);applyTransform();});document.getElementById("zoom-fit").addEventListener("click",()=>{const viewport=document.getElementById("draw-viewport"),d=item.locked_facts.context_coordinate_mapping.context_dimensions_px;state.view.zoom=Math.min(1,Math.max(.1,(viewport.clientWidth-16)/d.width,(viewport.clientHeight-16)/d.height));state.view.panX=0;state.view.panY=0;applyTransform();});document.getElementById("zoom-100").addEventListener("click",()=>{state.view.zoom=1;state.view.panX=0;state.view.panY=0;applyTransform();});document.getElementById("pan").addEventListener("click",()=>{state.view.mode="pan";setStatus("当前模式：平移页面");});document.getElementById("draw").addEventListener("click",()=>{state.view.mode="draw";setStatus("当前模式：框选 BBox");});document.getElementById("apply-box").addEventListener("click",()=>{if(!state.view.box||state.view.box[2]<=state.view.box[0]||state.view.box[3]<=state.view.box[1]){setStatus("请先框选有效 BBox",true);return;}const pdf=mapContextBBox(item,state.view.box);currentReview().regions[state.view.regionIndex].canonical_bbox_pdf_points=pdf;currentReview().regions[state.view.regionIndex].bbox_verdict="corrected";renderForm();setStatus("已按完整 Context 的四角映射写入 Region "+state.view.regionIndex+" corrected BBox");});const img=document.getElementById("bbox-image"),svg=document.getElementById("bbox-svg");svg.addEventListener("pointerdown",e=>{if(state.view.mode!=="draw"){state.view.panStart=[e.clientX,e.clientY];svg.setPointerCapture(e.pointerId);return;}state.view.drawing=true;state.view.start=drawPoint(e,img);state.view.box=[state.view.start[0],state.view.start[1],state.view.start[0],state.view.start[1]];svg.setPointerCapture(e.pointerId);drawOverlay();});svg.addEventListener("pointermove",e=>{if(state.view.mode==="draw"&&state.view.drawing){const p=drawPoint(e,img),s=state.view.start;state.view.box=[Math.min(s[0],p[0]),Math.min(s[1],p[1]),Math.max(s[0],p[0]),Math.max(s[1],p[1])];drawOverlay();}else if(state.view.mode==="pan"&&state.view.panStart){state.view.panX+=e.clientX-state.view.panStart[0];state.view.panY+=e.clientY-state.view.panStart[1];state.view.panStart=[e.clientX,e.clientY];applyTransform();}});svg.addEventListener("pointerup",e=>{if(svg.hasPointerCapture(e.pointerId))svg.releasePointerCapture(e.pointerId);if(state.view.mode==="draw"){state.view.drawing=false;drawOverlay();}else state.view.panStart=null;});svg.addEventListener("pointercancel",()=>{state.view.drawing=false;state.view.panStart=null;});applyTransform();}
function applyTransform(){const item=currentItem(),stage=document.getElementById("draw-stage"),content=document.getElementById("draw-content");if(stage&&content){const d=item.locked_facts.context_coordinate_mapping.context_dimensions_px;stage.style.width=(d.width*state.view.zoom)+"px";stage.style.height=(d.height*state.view.zoom)+"px";content.style.width=d.width+"px";content.style.height=d.height+"px";content.style.transform="translate("+state.view.panX+"px,"+state.view.panY+"px) scale("+state.view.zoom+")";}drawOverlay();}
function drawPoint(e,img){const rect=img.getBoundingClientRect(),width=img.naturalWidth||Number(img.getAttribute("width")),height=img.naturalHeight||Number(img.getAttribute("height"));return [Math.max(0,Math.min(width,(e.clientX-rect.left)*width/rect.width)),Math.max(0,Math.min(height,(e.clientY-rect.top)*height/rect.height))];}
function drawOverlay(){const item=currentItem(),img=document.getElementById("bbox-image"),rect=document.getElementById("bbox-rect"),coord=document.getElementById("coord");if(!img||!rect)return;const b=state.view.box;if(!b){rect.setAttribute("width",0);rect.setAttribute("height",0);coord.textContent="尚未框选";return;}rect.setAttribute("x",b[0]);rect.setAttribute("y",b[1]);rect.setAttribute("width",b[2]-b[0]);rect.setAttribute("height",b[3]-b[1]);const pdf=mapContextBBox(item,b);coord.textContent="context pixel-edge="+JSON.stringify(b.map(x=>Math.round(x*100)/100))+" → pdf top-left points="+JSON.stringify(pdf);}
function validateStructureReview(item,r){const errors=[],keys=["review_status","exclusion_reason","rotation_verdict","canonical_rotation_clockwise","split_verdict","canonical_boundary_px","canonical_region_order","canonical_region_ids","page_readability","abnormal_state","regions","notes"];if(!r||typeof r!=="object"||Object.keys(r).sort().join("|")!==keys.slice().sort().join("|"))return ["structure review fields invalid"];if(!["complete","abnormal","unreadable","needs_review"].includes(r.review_status)||typeof r.exclusion_reason!=="string"||typeof r.notes!=="string"||!["readable","unreadable","not_text","needs_review"].includes(r.page_readability)||!["none","skew","crop_loss","occlusion","fold","blur","other","needs_review"].includes(r.abnormal_state))errors.push("structure status fields invalid");const locked=item.locked_facts.regions,byId=Object.fromEntries(locked.map(x=>[x.region_id,x]));if(!Array.isArray(r.regions)||r.regions.length!==locked.length)errors.push("structure regions incomplete");const noncomplete=r.review_status!=="complete";if(noncomplete){if(r.canonical_rotation_clockwise!==null||r.canonical_boundary_px!==null||!(r.canonical_region_order===""||r.canonical_region_order===null)||r.canonical_region_ids!==null)errors.push("excluded structure has scorable fields");}else{const c=candidateFields(item);if(!["confirmed","corrected"].includes(r.rotation_verdict)||![0,90,180,270].includes(r.canonical_rotation_clockwise)||!["confirmed","corrected"].includes(r.split_verdict)||!Number.isInteger(r.canonical_boundary_px)||r.canonical_boundary_px<=0||r.canonical_boundary_px>=item.locked_facts.context_coordinate_mapping.context_dimensions_px.width||!["left-to-right","right-to-left"].includes(r.canonical_region_order)||!sameValue(r.canonical_region_ids,sortedRegionIds(item,r.canonical_region_order))||r.page_readability!=="readable"||r.abnormal_state!=="none"||r.exclusion_reason!=="")errors.push("complete structure values invalid");if(r.rotation_verdict==="confirmed"&&r.canonical_rotation_clockwise!==c.rotation)errors.push("rotation candidate mismatch");if(r.split_verdict==="confirmed"&&r.canonical_boundary_px!==c.boundary)errors.push("boundary candidate mismatch");}const seen=new Set();(r.regions||[]).forEach(rr=>{const lk=byId[rr&&rr.region_id],rk=["region_id","region_order","bbox_verdict","canonical_bbox_pdf_points","label_state","printed_page_label","readability","abnormal_state","notes"];if(!rr||Object.keys(rr).sort().join("|")!==rk.slice().sort().join("|")||!lk||seen.has(rr.region_id)){errors.push("region fields invalid");return;}seen.add(rr.region_id);if(rr.region_order!==lk.region_order||!["confirmed","corrected","uncertain"].includes(rr.bbox_verdict)||!["confirmed","unreadable","not_text","needs_review"].includes(rr.label_state)||!["readable","unreadable","not_text","needs_review"].includes(rr.readability)||!["none","skew","crop_loss","occlusion","fold","blur","other","needs_review"].includes(rr.abnormal_state)||typeof rr.notes!=="string")errors.push("region values invalid");if(rr.label_state!=="confirmed"&&rr.printed_page_label!==null)errors.push("unconfirmed label has value");if(rr.label_state==="confirmed"&&rr.printed_page_label!==null&&typeof rr.printed_page_label!=="string")errors.push("label type invalid");const bbox=rr.canonical_bbox_pdf_points;if(rr.bbox_verdict==="confirmed"&&!bboxEqual(bbox,lk.bbox_pdf_points))errors.push("confirmed bbox mismatch");if(rr.bbox_verdict==="uncertain"&&bbox!==null)errors.push("uncertain bbox has value");if(rr.bbox_verdict==="corrected"&&(!bboxValid(bbox)||bbox[0]<0||bbox[1]<0||bbox[2]>item.locked_facts.context_coordinate_mapping.page_width_points||bbox[3]>item.locked_facts.context_coordinate_mapping.page_height_points))errors.push("corrected bbox invalid");if(noncomplete&&(rr.bbox_verdict!=="uncertain"||rr.canonical_bbox_pdf_points!==null||rr.label_state!=="needs_review"||rr.printed_page_label!==null||rr.readability!=="needs_review"||rr.abnormal_state!=="needs_review"))errors.push("excluded region has scorable fields");if(!noncomplete&&(rr.label_state!=="confirmed"||rr.readability!=="readable"||rr.abnormal_state!=="none"||rr.bbox_verdict==="uncertain"))errors.push("complete region unresolved");});return errors;}
function validateOcrReview(r){const keys=["review_status","canonical_text","excluded_reason","notes"];if(!r||typeof r!=="object"||Object.keys(r).sort().join("|")!==keys.slice().sort().join("|"))return ["ocr review fields invalid"];const errors=[];if(!["complete","unreadable","not_text","needs_review"].includes(r.review_status)||typeof r.canonical_text!=="string"||typeof r.excluded_reason!=="string"||typeof r.notes!=="string")errors.push("ocr fields invalid");if(r.review_status==="complete"&&(!r.canonical_text.trim()||r.excluded_reason!==""))errors.push("complete OCR fields invalid");if(r.review_status!=="complete"&&r.canonical_text!=="")errors.push("excluded OCR has canonical text");return errors;}
function validateBatchAudit(entry){const base=["audit_id","action","item_ids","fields","per_item_visible","at"],confirmDetails=["candidate_fields","region_ids_by_item","source_item_id"],passDetails=["candidate_fields_by_item","region_ids_by_item","human_confirmation"],allowed=base.concat(confirmDetails,passDetails);if(!entry||typeof entry!=="object"||Object.keys(entry).some(k=>!allowed.includes(k))||base.some(k=>!(k in entry))||entry.per_item_visible!==true||typeof entry.audit_id!=="string"||!entry.audit_id||typeof entry.at!=="string"||!Array.isArray(entry.item_ids)||!entry.item_ids.length||new Set(entry.item_ids).size!==entry.item_ids.length||entry.item_ids.some(id=>!structure.some(x=>x.item_id===id))||!entry.fields||Object.values(entry.fields).some(v=>v!==true))return ["batch audit invalid"];if(entry.action==="batch-confirm-structure"){if(Object.keys(entry.fields).sort().join("|")!==BATCH_CONFIRM_FIELDS.slice().sort().join("|"))return ["batch confirm fields invalid"];const present=confirmDetails.filter(k=>k in entry);if(present.length&&present.length!==confirmDetails.length)return ["batch confirm detail incomplete"];if(present.length){const source=itemById[entry.source_item_id];if(!source||!entry.item_ids.includes(entry.source_item_id)||!sameValue(entry.candidate_fields,candidateFields(source))||!entry.region_ids_by_item||Object.keys(entry.region_ids_by_item).sort().join("|")!==entry.item_ids.slice().sort().join("|")||entry.item_ids.some(id=>!sameValue(entry.region_ids_by_item[id],sortedRegionIds(itemById[id],entry.candidate_fields.region_order))))return ["batch confirm detail binding invalid"];}return [];}if(entry.action==="batch-pass-structure"){if(Object.keys(entry.fields).sort().join("|")!==BATCH_PASS_FIELDS.slice().sort().join("|")||passDetails.some(k=>!(k in entry))||confirmDetails.some(k=>k in entry)||entry.human_confirmation!==true||!entry.candidate_fields_by_item||!entry.region_ids_by_item||Object.keys(entry.candidate_fields_by_item).sort().join("|")!==entry.item_ids.slice().sort().join("|")||Object.keys(entry.region_ids_by_item).sort().join("|")!==entry.item_ids.slice().sort().join("|"))return ["batch pass detail invalid"];if(entry.item_ids.some(id=>!sameValue(entry.candidate_fields_by_item[id],candidateFields(itemById[id]))||!sameValue(entry.region_ids_by_item[id],sortedRegionIds(itemById[id],candidateFields(itemById[id]).region_order))))return ["batch pass candidate binding invalid"];return [];}return ["batch action invalid"];}
function validateSubmission(v){const required=["schema_version","protocol","compiler_version","workbench_instance_id","plan_sha256","profile_sha256","reviewer","declarations","structure_items","ocr_samples"],allowed=required.concat(["batch_audit","exported_at"]),errors=[];if(!v||typeof v!=="object"||Object.keys(v).some(k=>!allowed.includes(k)||!required.includes(k)&&!(k in v))||required.some(k=>!(k in v)))return ["submission top-level fields invalid"];if(v.schema_version!==data.schema_version||v.protocol!==data.protocol||v.compiler_version!==data.compiler_version||v.workbench_instance_id!==data.workbench_instance_id||v.plan_sha256!==data.plan_identity.plan_sha256||v.profile_sha256!==data.profile.profile_sha256||!sameValue(v.reviewer,data.reviewer))errors.push("submission identity mismatch");const declarationKeys=["qualified_reviewer","reviewed_specified_source_evidence","did_not_view_hidden_results","did_not_change_split","reviewed_specified_render_region","not_evaluator","independent_from_proposer"];if(!v.declarations||Object.keys(v.declarations).sort().join("|")!==declarationKeys.slice().sort().join("|")||declarationKeys.some(k=>typeof v.declarations[k]!=="boolean"))errors.push("declarations invalid");if("exported_at" in v&&typeof v.exported_at!=="string")errors.push("exported_at invalid");const check=(raw,items,kind,reviewCheck)=>{if(!Array.isArray(raw)||raw.length!==items.length){errors.push(kind+" item count invalid");return;}const expected=Object.fromEntries(items.map(x=>[x.item_id,x])),seen=new Set();raw.forEach(row=>{if(!row||typeof row!=="object"||Object.keys(row).sort().join("|")!==["item_id","item_sha256","input_hashes","review"].sort().join("|")){errors.push(kind+" row fields invalid");return;}const item=expected[row.item_id];if(!item||seen.has(row.item_id)){errors.push(kind+" unknown/duplicate item");return;}seen.add(row.item_id);if(row.item_sha256!==item.item_sha256||!sameValue(row.input_hashes,inputHashes(item)))errors.push(kind+" hash binding invalid");errors.push(...reviewCheck(item,row.review));});if(seen.size!==items.length)errors.push(kind+" item set incomplete");};check(v.structure_items,structure,"structure",validateStructureReview);check(v.ocr_samples,ocr,"ocr",(_,r)=>validateOcrReview(r));if("batch_audit" in v){if(!Array.isArray(v.batch_audit))errors.push("batch audit is not an array");else v.batch_audit.forEach(entry=>errors.push(...validateBatchAudit(entry)));}return errors;}
function basicSubmission(v){return validateSubmission(v).length===0;}
function importSubmission(v){const errors=validateSubmission(v);if(errors.length){setStatus("submission 导入拒绝："+errors.slice(0,4).join("；")+"；原草稿保持不变",true);return;}const nextS=Object.fromEntries(v.structure_items.map(x=>[x.item_id,x.review])),nextO=Object.fromEntries(v.ocr_samples.map(x=>[x.item_id,x.review]));Object.assign(state.structure,nextS);Object.assign(state.ocr,nextO);Object.assign(state.declarations,v.declarations);state.batch_audit=Array.isArray(v.batch_audit)?v.batch_audit.slice():[];setStatus("已导入同一 workbench submission（身份、item/hash、审查字段已通过前端校验）");render();}
function doBatch(){const ids=structure.filter(item=>state.selected.has(item.item_id)).map(item=>item.item_id);if(!ids.length){setStatus("请先选择结构页",true);return;}if(!ids.includes(state.current)){setStatus("批量确认已拒绝：当前页必须作为所选 source 页",true);return;}const source=itemById[state.current],sourceCandidate=candidateFields(source);if(!validCandidate(sourceCandidate)){setStatus("批量确认已拒绝：source 页冻结 rotation / boundary / order 不完整",true);return;}const mismatch=ids.filter(id=>!sameValue(candidateFields(itemById[id]),sourceCandidate));if(mismatch.length){setStatus("批量确认已拒绝：冻结 candidate 不一致页 "+mismatch.join(", "),true);return;}const regionIdsByItem={};ids.forEach(id=>{const item=itemById[id],c=candidateFields(item),r=state.structure[id];r.rotation_verdict="confirmed";r.canonical_rotation_clockwise=c.rotation;r.split_verdict="confirmed";r.canonical_boundary_px=c.boundary;r.canonical_region_order=c.region_order;r.canonical_region_ids=sortedRegionIds(item,c.region_order);regionIdsByItem[id]=r.canonical_region_ids.slice();});state.batch_audit.push({audit_id:"batch-"+Date.now(),action:"batch-confirm-structure",item_ids:ids,fields:{rotation:true,boundary:true,region_order:true},per_item_visible:true,at:new Date().toISOString(),source_item_id:state.current,candidate_fields:sourceCandidate,region_ids_by_item:regionIdsByItem});setStatus("已逐页写入批量确认审计；每页使用自己的 region IDs；仍需逐页处理状态、label、readability、BBox");render();}
function batchPassReview(item){const c=candidateFields(item);return {review_status:"complete",exclusion_reason:"",rotation_verdict:"confirmed",canonical_rotation_clockwise:c.rotation,split_verdict:"confirmed",canonical_boundary_px:c.boundary,canonical_region_order:c.region_order,canonical_region_ids:sortedRegionIds(item,c.region_order),page_readability:"readable",abnormal_state:"none",regions:item.locked_facts.regions.map(r=>({region_id:r.region_id,region_order:r.region_order,bbox_verdict:"confirmed",canonical_bbox_pdf_points:r.bbox_pdf_points.slice(),label_state:"confirmed",printed_page_label:r.printed_page_label_candidate,readability:"readable",abnormal_state:"none",notes:""})),notes:""};}
function doBatchPass(){const ids=structure.filter(item=>state.selected.has(item.item_id)).map(item=>item.item_id);if(!ids.length){setStatus("请先勾选要批量通过的结构页",true);return;}const modified=ids.filter(id=>!sameValue(state.structure[id],blankStructure(itemById[id])));if(modified.length){setStatus("批量通过已拒绝：这些页面已有人工修改，请单页处理："+modified.join(", "),true);return;}const message="你将批量通过 "+ids.length+" 个结构页。请确认已逐页查看 Context 和两个 Region；候选 BBox/方向/切分/顺序将被确认，candidate/null 打印页标签也会被视为人工确认。异常页必须先取消勾选。";if(!window.confirm(message)){setStatus("已取消批量通过");return;}const candidateFieldsByItem={},regionIdsByItem={};ids.forEach(id=>{const item=itemById[id],review=batchPassReview(item);state.structure[id]=review;candidateFieldsByItem[id]=candidateFields(item);regionIdsByItem[id]=review.canonical_region_ids.slice();});state.batch_audit.push({audit_id:"batch-pass-"+Date.now(),action:"batch-pass-structure",item_ids:ids,fields:{review_status:true,rotation:true,boundary:true,region_order:true,page_readability:true,page_abnormal_state:true,region_bbox:true,region_label_state:true,region_readability:true,region_abnormal_state:true},per_item_visible:true,at:new Date().toISOString(),candidate_fields_by_item:candidateFieldsByItem,region_ids_by_item:regionIdsByItem,human_confirmation:true});setStatus("已批量通过 "+ids.length+" 个结构页并写入逐页审计；OCR 完整区域仍需逐项人工转写");render();}
function finalizeCheck(){const missing=[];for(const item of structure){const r=state.structure[item.item_id];if(r.review_status!=="complete"&&!r.exclusion_reason.trim())missing.push(item.item_id+" 缺 exclusion_reason");missing.push(...validateStructureReview(item,r).map(x=>item.item_id+" "+x));}for(const item of ocr){const r=state.ocr[item.item_id];if(r.review_status!=="complete"&&!r.excluded_reason.trim())missing.push(item.item_id+" 缺 excluded_reason");if(r.review_status==="complete"&&!r.canonical_text.trim())missing.push(item.item_id+" 缺 canonical_text");missing.push(...validateOcrReview(r).map(x=>item.item_id+" "+x));}setStatus(missing.length?"Finalize 检查发现 "+missing.length+" 项缺失/不一致；详情见浏览器提示":"表单字段已填，但仍需外部 attestation 和命令行重验");window.alert(missing.length?missing.slice(0,30).join("\n"):"可以导出 submission。Finalize 仍必须在命令行由外部 attestation 驱动。");}
document.getElementById("save").addEventListener("click",saveDraft);document.getElementById("export").addEventListener("click",()=>download("scan-gold-review-submission.json",submissionValue()));document.getElementById("check").addEventListener("click",finalizeCheck);document.getElementById("select-all").addEventListener("click",()=>{structure.forEach(x=>state.selected.add(x.item_id));renderList();});document.getElementById("clear-all").addEventListener("click",()=>{state.selected.clear();renderList();});document.getElementById("import").addEventListener("click",()=>document.getElementById("import-file").click());document.getElementById("import-file").addEventListener("change",e=>{const file=e.target.files[0];if(!file)return;const reader=new FileReader();reader.onload=()=>{try{importSubmission(JSON.parse(reader.result));}catch(err){setStatus("JSON 读取失败；原草稿保持不变",true);}};reader.readAsText(file);});window.addEventListener("beforeunload",saveDraft);loadDraft();render();
})();
</script>
</body>
</html>
"""


def render_scan_gold_instructions(workbench_name: str, reviewer: Mapping[str, Any], plan: Mapping[str, Any], profile_identity: Mapping[str, Any]) -> str:
    scope = plan["source"]["scope"]
    return f"""# 扫描 Gold 人工审查使用说明

这是 `{workbench_name}` 的本地、私有、candidate-only 审查工作包。范围是物理 PDF 页 `{scope[0]}-{scope[-1]}`；每页冻结一个显式内容旋转、双区 split 和左右顺序结构项。OCR Gold 按 `{SAMPLE_RULE_ID}` 从物理页 `{plan['source']['ocr_sample_pages']}` 分层抽样，每个入选页的两个完整 region 各形成一个人工转写项，不再使用窄条样本。

## 已冻结事实

- profile SHA-256：`{profile_identity['profile_sha256']}`
- plan SHA-256：`{plan['plan_sha256']}`
- source SHA-256：`{plan['source']['source_sha256']}`
- reviewer instance：`{reviewer['reviewer_instance']}`
- review session：`{reviewer['review_session_id']}`
- rotation candidate：顺时针 `90°`；split：旋转图像宽度的 `floor(width/2)`；region order：left-to-right。
- printed page label 只是 candidate/null，必须人工逐 region 确认或标记 unreadable/not_text/needs_review。

## 审查规则

1. 逐页打开 Context，并点击 Region 0 / Region 1 卡片或选项卡确认两侧内容。普通“批量确认结构候选”只写 rotation / boundary / order；“批量通过所选结构页”仅接受尚未修改的空白页，并在二次确认后逐页确认候选 BBox、方向、切分、顺序、可读性、无异常和 candidate/null 打印页标签。异常页必须取消勾选后单独修改。
2. 逐 region 处理 BBox、打印页标签、readability 和 abnormal state。`confirmed` BBox 必须保持冻结候选值；需要改动时选择 `corrected` 并用页面内的框选工具写入 PDF top-left points。
3. OCR 项从冻结的完整 region render 按自然阅读顺序逐行填写 `canonical_text`，保留段落/行次序。不得把任何候选识别结果、置信度或隐藏内容带入页面。整块不可读、非文本或仍需复核时选择相应状态并填写 `excluded_reason`；局部公式、图、表或少数字符困难应在 `notes` 中说明，不能只抄几个容易单词。
4. 完成声明后保存草稿并导出 `scan-gold-review-submission.json`。浏览器不会创建外部 attestation，也不会把 submission 标为 Gold。

## 逐字段填写字典

### 结构页字段

| 字段 | 填写规则 |
| --- | --- |
| `review_status` 页面状态 | `complete` 表示本页所有结构与两个 region 都已确认；`abnormal`、`unreadable`、`needs_review` 表示本页排除/暂停，必须同时填写 `exclusion_reason`。排除项仍留在结构 Gold 分母。 |
| `rotation_verdict` / `canonical_rotation_clockwise` | `confirmed` 保持锁定候选；`corrected` 只在人工看到不同方向时使用。角度必须显式填 `0`、`90`、`180` 或 `270`（`0` 是合法值，不是缺失）。 |
| `split_verdict` / `canonical_boundary_px` | `confirmed` 保持候选 boundary；`corrected` 填旋转 Context 的整数像素边界。当前示例是 page 1=`1156`、page 2–40=`1153`，不能跨页静默复制。 |
| `canonical_region_order` / `canonical_region_ids` | 先选 `left-to-right` 或 `right-to-left`，页面会按该顺序写入本页锁定的两个唯一 `region_id`；不要从其他页粘贴 ID。 |
| `page_readability` / region `readability` | 只能填 `readable`、`unreadable`、`not_text`、`needs_review`。页面可读不代表每个 region 自动可读，两个层级都要逐项确认。 |
| `abnormal_state` | 页面和 region 分别记录 `none`、`skew`、`crop_loss`、`occlusion`、`fold`、`blur`、`other` 或 `needs_review`；异常页不能用批量确认代替单页处理。 |
| region `bbox_verdict` / `canonical_bbox_pdf_points` | `confirmed` 使用冻结候选；`corrected` 用 `[x0,y0,x1,y1]` 填 PDF top-left points；`uncertain` 必须保持坐标为空。框选在完整 Context 上，校正框可超出候选 region，但必须在整页 PDF 范围内。 |
| region `label_state` / `printed_page_label` | `confirmed` 时人工填写读到的打印页标签，无法可靠读取时选 `unreadable`；`not_text`/`needs_review` 不填标签。锁定的 candidate/null 不是人工确认。 |
| `notes` | 记录可复核的人工观察或异常原因；不要粘贴页面外的候选内容。 |

### OCR 完整 Region 字段

| 字段 | 填写规则 |
| --- | --- |
| `review_status` | `complete`、`unreadable`、`not_text`、`needs_review` 四选一；后三者都要填写 `excluded_reason`。 |
| `canonical_text` | 只在 `complete` 时从当前冻结的完整 region render 按自然阅读顺序逐行转写；初始为空，不能由任何识别结果预填。不可只选择几个容易识别的单词，空文本也不能偷偷减少分母。 |
| `excluded_reason` | 说明不可读、非文本或仍需复核的具体原因；状态切换为排除状态后必须重新检查此字段。 |
| `notes` | 补充局部字形、裁切或复核说明，不替代 canonical text 或 excluded reason。 |

### 声明与批量确认

- 七项声明必须逐项阅读后勾选：`qualified_reviewer`、`reviewed_specified_source_evidence`、`did_not_view_hidden_results`、`did_not_change_split`、`reviewed_specified_render_region`、`not_evaluator`、`independent_from_proposer`。声明只表示人工实际行为，浏览器不会替人工创建外部 attestation。
- 普通批量确认只适用于冻结 `rotation`、`boundary`、`region order` 三字段完全相同且当前 source 页在选择内。批量通过允许每页使用自己的候选值，因此 page 1=`1156`、page 2=`1153` 可以同时处理，但只接受仍为初始空白的页面；已修改页面会整体拒绝，防止覆盖人工纠错。确认弹窗中的 candidate/null 页标签表示 reviewer 已实际查看并接受“无可靠打印标签”，不能在未看图时点击。批量通过后仍可逐项修正打印页标签、BBox 或其他字段；审计保留原批量动作，导入和 finalize 读取最终人工值。
- Region 0 / Region 1 有两套同步入口：上方 region 图片卡片和 BBox 工具栏选项卡。当前项以蓝色高亮；点击后“当前 Region N”必须同步变化，后续框选只写入该 Region。
- `complete` 切换到任一排除/暂停状态时，页面会原子清除 rotation、boundary、order、region IDs、BBox、label、readability 和 abnormal 等不兼容的可评分字段，但保留备注，之后必须填写原因。OCR 切换到非 `complete` 时会清除 `canonical_text`；切回 `complete` 前需重新人工填写。
- 导入 submission 会先检查 workbench、profile、plan、reviewer/session/proposer、每个 item ID、item hash、input hash、重复/未知项和字段形状；任何失败都保留当前草稿不变。

### CW90 与像素边界例子

CW90 页面使用完整 Context 的像素边缘映射：像素框四个角分别映射到 PDF top-left points，再取四角包围盒，不按 region crop 的局部轴线性插值。以本书 page 1 为例，旋转 Context 的左上像素边缘映射到原始页的对应下侧边缘，右上/左下等角点共同决定 PDF 框；因此框选方向与 PDF 坐标的 y 轴关系不能凭直觉交换。

本书旋转 Context 的宽度是奇数分割示例：page 1 boundary=`1156`，左侧为 `[0,1156)`、右侧为 `[1156,2313)`；page 2–40 boundary=`1153`。这些是各页冻结 candidate，不能把一个页面的 boundary 或 region ID 复制到另一个页面。

## 本地验证与交接命令

在项目根目录执行；路径请使用本工作包的绝对路径。先验证冻结输入：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python skills/fake-expert/scripts/scan_gold_review.py validate-workbench --workbench /absolute/path/to/{workbench_name}
```

真实 reviewer 在 workbench 外提供符合 profile 的 external attestation 后，由项目操作者在新目录 finalize：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python skills/fake-expert/scripts/scan_gold_review.py finalize \\
  --workbench /absolute/path/to/{workbench_name} \\
  --submission /absolute/path/to/scan-gold-review-submission.json \\
  --attestation /absolute/path/to/external-attestation.json \\
  --output /absolute/path/to/fresh-scan-gold-revision
```

没有真实 external attestation 时也可运行 `finalize`，但结果必须是 `paused-independent-scan-gold` 且结构/OCR Gold 行数为零。之后先重读 revision：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python skills/fake-expert/scripts/scan_gold_review.py validate-revision \\
  --workbench /absolute/path/to/{workbench_name} \\
  --revision /absolute/path/to/fresh-scan-gold-revision
```

本说明只覆盖 reviewer 的人工填写与本地交接；后续内部评估不在本页面展示，也不改变 reviewer 的填写结果。整个流程不运行 OCR、不加载模型、不启动 resident 服务、不联网、不调用 MCP；workbench、submission、attestation 和 revision 都是 `release_excluded` 私有材料。Gold、准确率、promotion、executable、release 或 Codex certification 不会由这些命令自动授予。
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline Portable Scan Gold Review workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="render a blank local review workbench")
    generate.add_argument("--source", required=True, type=Path)
    generate.add_argument("--output", required=True, type=Path)
    generate.add_argument("--pdftoppm", required=True)
    generate.add_argument("--start-page", type=int, default=1)
    generate.add_argument("--end-page", type=int, default=40)
    generate.add_argument("--rotation", type=int, default=DEFAULT_ROTATION)
    generate.add_argument("--render-dpi", type=int, default=DEFAULT_RENDER_DPI)
    generate.add_argument("--reviewer-id", required=True)
    generate.add_argument("--review-session-id", required=True)
    generate.add_argument("--proposer-instance", required=True)
    generate.add_argument("--created-at", default="2026-08-28T00:00:00+08:00")
    generate.add_argument("--split-contract", type=Path)
    generate.add_argument("--split-artifacts", type=Path)

    validate_workbench = subparsers.add_parser("validate-workbench", help="re-read and verify a workbench")
    validate_workbench.add_argument("--workbench", required=True, type=Path)

    finalize = subparsers.add_parser("finalize", help="finalize a reviewer submission into a private revision")
    finalize.add_argument("--workbench", required=True, type=Path)
    finalize.add_argument("--submission", required=True, type=Path)
    finalize.add_argument("--attestation", type=Path)
    finalize.add_argument("--output", required=True, type=Path)
    finalize.add_argument("--revision-id")
    finalize.add_argument("--finalized-at", default="2026-08-28T00:00:00+08:00")

    validate_revision = subparsers.add_parser("validate-revision", help="re-read and verify a private revision")
    validate_revision.add_argument("--workbench", required=True, type=Path)
    validate_revision.add_argument("--revision", required=True, type=Path)

    evaluate = subparsers.add_parser("evaluate", help="evaluate only an accepted revision")
    evaluate.add_argument("--workbench", required=True, type=Path)
    evaluate.add_argument("--revision", required=True, type=Path)
    evaluate.add_argument("--predictions", type=Path)
    evaluate.add_argument("--evaluator-id", required=True)
    evaluate.add_argument("--output", required=True, type=Path)
    evaluate.add_argument("--evaluated-at", default="2026-08-28T00:00:00+08:00")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "generate":
            result = generate_scan_gold_workbench(
                args.source,
                args.output,
                args.pdftoppm,
                start_page=args.start_page,
                end_page=args.end_page,
                rotation=args.rotation,
                render_dpi=args.render_dpi,
                reviewer_id=args.reviewer_id,
                review_session_id=args.review_session_id,
                proposer_instance=args.proposer_instance,
                created_at=args.created_at,
                split_contract=args.split_contract,
                split_artifacts=args.split_artifacts,
            )
        elif args.command == "validate-workbench":
            result = validate_scan_gold_workbench(args.workbench)
        elif args.command == "finalize":
            result = finalize_scan_gold_review(args.workbench, args.submission, args.output, attestation=args.attestation, finalized_at=args.finalized_at, revision_id=args.revision_id)
        elif args.command == "validate-revision":
            result = validate_scan_gold_revision(args.workbench, args.revision)
        elif args.command == "evaluate":
            result = evaluate_scan_gold(args.workbench, args.revision, args.predictions, args.output, evaluator_id=args.evaluator_id, evaluated_at=args.evaluated_at)
        else:  # pragma: no cover - argparse enforces the command
            raise ScanGoldReviewError("scan_gold_command_invalid")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (ReviewerWorkbenchError, ScanGoldReviewError, ScanOcrError, OSError, ValueError) as error:
        print(json.dumps({"status": "rejected", "error": str(error)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2


__all__ = [
    "ScanGoldReviewError",
    "evaluate_scan_gold",
    "finalize_scan_gold_review",
    "generate_scan_gold_workbench",
    "main",
    "render_scan_gold_html",
    "render_scan_gold_instructions",
    "scan_gold_profile_identity",
    "validate_scan_gold_profile",
    "validate_scan_gold_revision",
    "validate_scan_gold_workbench",
]


if __name__ == "__main__":
    raise SystemExit(main())
