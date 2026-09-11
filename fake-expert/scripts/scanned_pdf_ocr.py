#!/usr/bin/env python3
"""Build and verify a candidate-only scanned-PDF OCR intermediate representation.

The coordinator in this module owns source/render/model/config receipts and the
native-text-first routing policy.  OCR engines run in ``ocr_backend_worker.py``
through a fixed JSONL subprocess protocol.  No OCR result produced here is a
canonical evidence anchor, knowledge object, formula, review receipt, or
capability.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from pdf_parser_adapters import build_render_receipts
from pdf_structure import (
    PdfPipelineError,
    compile_pdf_intermediate_representation,
    normalize_text,
    page_dimensions,
    sha256_file,
)
from compiler_version import (
    OCR_BACKEND_RECEIPT_SCHEMA,
    OCR_OBSERVATION_SCHEMA,
    OCR_PROTOCOL_SCHEMA,
    OCR_PROTOCOL_VERSION,
    READONLY_COMPATIBLE_SCAN_PDF_COMPILER_VERSIONS,
    SCAN_ANCHOR_PROTOCOL,
    SCAN_SPLIT_CHECKPOINT_COMPILER_VERSION,
    SCANNED_PDF_SPLIT_GEOMETRY_COMPILER_VERSION,
    SCANNED_PDF_SPLIT_GEOMETRY_PROTOCOL,
    SCANNED_PDF_SPLIT_GEOMETRY_SCHEMA,
    SCANNED_PDF_CHECKPOINT_PROTOCOL,
    SCANNED_PDF_CHECKPOINT_SCHEMA,
    SCANNED_PDF_SPLIT_PROTOCOL,
    SCANNED_PDF_SPLIT_SCHEMA,
    SELECTIVE_VISUAL_PROTOCOL,
    SCAN_IR_SCHEMA,
    SCAN_PDF_COMPILER_VERSION,
    SCANNED_PDF_STRUCTURE_REVIEW_PROTOCOL,
    SCANNED_PDF_STRUCTURE_REVIEW_SCHEMA,
    VISUAL_BATCH_RECEIPT_SCHEMA,
)
from heading_locator import apply_locator_overrides
from paddle_runtime_contract import (
    PADDLE_OCR_BACKEND,
    PADDLE_STRUCTURE_BACKEND,
    PADDLE_STRUCTURE_V3_BACKEND,
    PADDLE_CHART_BACKEND,
    TECHNICAL_TABLE_PROFILE_ID,
    TECHNICAL_TEXT_PROFILE_ID,
    PaddleRuntimeContractError,
    build_crop_coordinate_transform,
    build_crop_mapping,
    crop_render_png,
    rotate_png_for_ocr,
    freeze_external_runtime_contract,
    probe_external_runtime,
    run_external_worker,
    sha256_json as paddle_sha256_json,
    validate_runtime_contract,
    profile_definition,
    PROFILE_MODEL_NAMES,
    run_external_worker_batch,
    normalize_technical_chart_v1_result,
)


OCR_TRANSCRIPT_SCHEMA = "tkc.pdf-ocr-transcript/v0.1"
OCR_COORDINATE_TRANSFORM_SCHEMA = "tkc.pdf-ocr-coordinate-transform/v0.1"
OCR_CONFLICT_SCHEMA = "tkc.pdf-ocr-conflict/v0.1"
OCR_QUALITY_SCHEMA = "tkc.pdf-ocr-quality-routing/v0.1"
COORDINATE_SPACE = "pdf-page-top-left-points-v1"
IMAGE_SPACE = "preprocessed-image-pixels-v1"
WORKER_NAME = "ocr_backend_worker.py"
PRIMARY_BACKEND = PADDLE_OCR_BACKEND
CHALLENGER_BACKEND = "docling"
BASELINE_BACKEND = "pymupdf-tesseract"
_SPLIT_COMMITMENT_KEYS = (
    "runtime_contract_sha256",
    "runtime_inventory_sha256",
    "model_identity_sha256",
    "configuration_sha256",
    "worker_sha256",
)
_QUALIFICATION_COMMITMENT_KEYS = ("plan_sha256", "receipt_sha256")


def _visual_batch_receipt(*, status: str = "completed", regions: int = 0) -> dict[str, Any]:
    """Return the closed visual batch receipt for zero/failed sessions."""

    receipt = {
        "schema_version": VISUAL_BATCH_RECEIPT_SCHEMA,
        "protocol": SELECTIVE_VISUAL_PROTOCOL,
        "batch": True,
        "calls": 0,
        "pages": 0,
        "regions": int(regions),
        "elapsed_seconds": 0.0,
        "bytes": 0,
        "objects": 0,
        "status": status,
        "model_load_calls": 0,
        "source_pdf_passed": False,
        "candidate_only": True,
        "promotion": False,
        "items": [],
    }
    receipt["batch_receipt_sha256"] = paddle_sha256_json({key: value for key, value in receipt.items() if key != "batch_receipt_sha256"})
    return receipt


BACKEND_SPECS: dict[str, dict[str, Any]] = {
    PADDLE_OCR_BACKEND: {
        "engine": "PaddleOCR/PP-OCRv6",
        "role": "primary",
        "model_required": True,
        "network_policy": "offline-enforced-external-runtime",
        "test_only": False,
        "external_runtime_required": True,
    },
    PADDLE_STRUCTURE_BACKEND: {
        "engine": "PaddleOCR/PP-StructureV3",
        "role": "primary",
        "model_required": True,
        "network_policy": "offline-enforced-external-runtime",
        "test_only": False,
        "external_runtime_required": True,
    },
    PADDLE_STRUCTURE_V3_BACKEND: {
        "engine": "PaddleOCR/PP-StructureV3",
        "role": "primary",
        "model_required": True,
        "network_policy": "offline-enforced-external-runtime",
        "test_only": False,
        "external_runtime_required": True,
    },
    CHALLENGER_BACKEND: {
        "engine": "Docling",
        "role": "challenger",
        "model_required": True,
        "network_policy": "offline-enforced",
        "test_only": False,
    },
    BASELINE_BACKEND: {
        "engine": "PyMuPDF/Tesseract",
        "role": "optional-baseline",
        "model_required": False,
        "network_policy": "offline-enforced",
        "test_only": False,
    },
    "fake": {
        "engine": "synthetic-fake-backend",
        "role": "test-only",
        "model_required": False,
        "network_policy": "disabled",
        "test_only": True,
    },
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FORMULA_HINT = re.compile(
    r"(?:=|\^|_|\\(?:alpha|beta|gamma|delta|lambda|mu|sigma|omega)|"
    r"\b(?:eq(?:uation)?|formula|式|方程)\b)",
    re.IGNORECASE,
)
_TABLE_HINT = re.compile(r"(?:\|[^|]+\||\btable\b|表\s*\d*)", re.IGNORECASE)
_UNIT_HINT = re.compile(
    r"\b(?:m|mm|cm|km|s|ms|kg|g|pa|kpa|mpa|hz|khz|mhz|v|a|w|j|ev|k|°c|%)\b",
    re.IGNORECASE,
)


class ScanOcrError(RuntimeError):
    """Stable fail-closed scanned-PDF adapter error."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: Any, length: int = 20) -> str:
    return f"{prefix}-{sha256_json(list(parts))[:length]}"


def _finite(value: Any, *, code: str = "scan_number_invalid") -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ScanOcrError(code) from error
    if not math.isfinite(number):
        raise ScanOcrError(code)
    return number


def _point(value: Any) -> float:
    return round(_finite(value), 4)


def _matrix(values: Any) -> list[float]:
    if not isinstance(values, (list, tuple)) or len(values) != 9:
        raise ScanOcrError("coordinate_transform_matrix_invalid")
    return [_point(value) for value in values]


def _determinant(matrix: list[float]) -> float:
    a, b, c, d, e, f, g, h, i = matrix
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def _inverse(matrix: list[float]) -> list[float]:
    a, b, c, d, e, f, g, h, i = matrix
    determinant = _determinant(matrix)
    if abs(determinant) < 1e-12:
        raise ScanOcrError("coordinate_transform_not_invertible")
    cofactors = [
        e * i - f * h,
        c * h - b * i,
        b * f - c * e,
        f * g - d * i,
        a * i - c * g,
        c * d - a * f,
        d * h - e * g,
        b * g - a * h,
        a * e - b * d,
    ]
    return [_point(value / determinant) for value in cofactors]


def _multiply_matrices(left: Sequence[Any], right: Sequence[Any]) -> list[float]:
    left_values = _matrix(left)
    right_values = _matrix(right)
    return [
        _point(sum(left_values[row * 3 + inner] * right_values[inner * 3 + column] for inner in range(3)))
        for row in range(3)
        for column in range(3)
    ]


def _rotate_crop_transform(
    transform: Mapping[str, Any],
    *,
    degrees_clockwise: int,
    original_dimensions: Mapping[str, Any],
    rotated_dimensions: Mapping[str, Any],
) -> dict[str, Any]:
    rotation = int(degrees_clockwise) % 360
    if rotation not in {0, 90, 180, 270}:
        raise ScanOcrError("crop_content_rotation_invalid")
    width = _finite(original_dimensions.get("width"))
    height = _finite(original_dimensions.get("height"))
    rotated_to_original = {
        0: [1, 0, 0, 0, 1, 0, 0, 0, 1],
        90: [0, 1, 0, -1, 0, height, 0, 0, 1],
        180: [-1, 0, width, 0, -1, height, 0, 0, 1],
        270: [0, -1, width, 1, 0, 0, 0, 0, 1],
    }[rotation]
    result = json.loads(json.dumps(dict(transform), ensure_ascii=False, sort_keys=True))
    result["matrix_3x3"] = _multiply_matrices(result["matrix_3x3"], rotated_to_original)
    result["inverse_matrix_3x3"] = _inverse(result["matrix_3x3"])
    result["image_dimensions_px"] = {
        "width": int(rotated_dimensions["width"]),
        "height": int(rotated_dimensions["height"]),
    }
    preprocessing = dict(result.get("preprocessing") or {})
    preprocessing["content_rotation_clockwise"] = rotation
    preprocessing["pre_rotation_dimensions_px"] = {
        "width": int(width),
        "height": int(height),
    }
    result["preprocessing"] = preprocessing
    result["matrix_source"] = "pdf-top-left-render-crop-explicit-content-rotation-v1"
    result["id"] = _stable_id(
        "pct",
        {key: value for key, value in result.items() if key not in {"schema_version", "id"}},
    )
    return result


def _hash_or_none(value: Any, code: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ScanOcrError(code)
    return value


def _dimensions(value: Any, code: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ScanOcrError(code)
    width, height = value.get("width"), value.get("height")
    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or not isinstance(height, int)
        or width <= 0
        or height <= 0
    ):
        raise ScanOcrError(code)
    return {"width": int(width), "height": int(height)}


def _split_rotation(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in {0, 90, 180, 270}:
        raise ScanOcrError("spread_split_rotation_invalid")
    return int(value)


def _rotate_bbox_to_pre_rotation(
    bbox: Sequence[Any],
    *,
    original_dimensions: Mapping[str, Any],
    rotation: int,
) -> list[int]:
    """Map rotated-image pixel boundaries back to the pre-rotation image."""

    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise ScanOcrError("spread_split_region_bbox_invalid")
    left, top, right, bottom = [int(value) for value in bbox]
    width = int(original_dimensions["width"])
    height = int(original_dimensions["height"])
    if rotation == 0:
        return [left, top, right, bottom]
    if rotation == 90:
        return [top, height - right, bottom, height - left]
    if rotation == 180:
        return [width - right, height - bottom, width - left, height - top]
    if rotation == 270:
        return [width - bottom, left, width - top, right]
    raise ScanOcrError("spread_split_rotation_invalid")


def _split_core_payload(contract: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))
        for key, value in contract.items()
        if key not in {"contract_sha256", "split_core_sha256", "runtime_commitments", "qualification_commitments"}
    }


def _split_commitments(
    value: Mapping[str, Any] | None,
    keys: Sequence[str],
    *,
    code: str,
) -> dict[str, str | None]:
    if value is None:
        supplied: dict[str, Any] = {}
    elif isinstance(value, Mapping):
        supplied = dict(value)
    else:
        raise ScanOcrError(code)
    unknown = sorted(set(supplied) - set(keys))
    if unknown:
        raise ScanOcrError(f"{code}_unknown:{unknown[0]}")
    return {key: _hash_or_none(supplied.get(key), code) for key in keys}


def _split_render_record(render: Mapping[str, Any], dimensions: Mapping[str, Any], digest: str, dpi: int) -> dict[str, Any]:
    result = {
        "sha256": digest,
        "dimensions_px": dict(dimensions),
        "dpi": int(dpi),
    }
    if isinstance(render.get("renderer"), Mapping):
        result["renderer"] = json.loads(json.dumps(render["renderer"], ensure_ascii=False, sort_keys=True))
    return result


def _apply_split_matrix(matrix: Sequence[Any], x: float, y: float) -> tuple[float, float]:
    values = [float(value) for value in matrix]
    if len(values) != 9:
        raise ScanOcrError("spread_split_mapping_matrix_invalid")
    denominator = values[6] * x + values[7] * y + values[8]
    if abs(denominator) < 1e-12:
        raise ScanOcrError("spread_split_mapping_matrix_singular")
    return ((values[0] * x + values[1] * y + values[2]) / denominator, (values[3] * x + values[4] * y + values[5]) / denominator)


def _inverse_split_matrix(matrix: Sequence[Any]) -> list[float]:
    values = [float(value) for value in matrix]
    if len(values) != 9:
        raise ScanOcrError("spread_split_mapping_matrix_invalid")
    a, b, c, d, e, f, _, _, _ = values
    determinant = a * e - b * d
    if abs(determinant) < 1e-12:
        raise ScanOcrError("spread_split_mapping_matrix_singular")
    return [
        round(e / determinant, 12),
        round(-b / determinant, 12),
        round((b * f - e * c) / determinant, 12),
        round(-d / determinant, 12),
        round(a / determinant, 12),
        round((d * c - a * f) / determinant, 12),
        0.0,
        0.0,
        1.0,
    ]


def _pixel_first_split_mapping(full_mapping: Mapping[str, Any], pre_bbox: Sequence[int]) -> dict[str, Any]:
    """Map an exact raster partition back to PDF without re-rounding it.

    The v0.1 contract derived a PDF rectangle first and then asked
    ``build_crop_mapping`` to recover integer pixels.  At a non-integral
    PDF-to-raster scale this can move one edge by one pixel, so a legal odd
    raster partition could not be represented.  v0.2 makes the integer pixel
    edges authoritative and stores the corresponding affine transform.
    """

    if not isinstance(pre_bbox, (list, tuple)) or len(pre_bbox) != 4 or any(isinstance(value, bool) or not isinstance(value, int) for value in pre_bbox):
        raise ScanOcrError("spread_split_region_bbox_invalid")
    left, top, right, bottom = [int(value) for value in pre_bbox]
    render_dimensions = _dimensions(full_mapping.get("render_dimensions_px"), "spread_split_render_dimensions_invalid")
    if not (0 <= left < right <= render_dimensions["width"] and 0 <= top < bottom <= render_dimensions["height"]):
        raise ScanOcrError("spread_split_region_bbox_out_of_bounds")
    full_matrix = [float(value) for value in full_mapping.get("matrix_crop_to_pdf", [])]
    if len(full_matrix) != 9:
        raise ScanOcrError("spread_split_full_mapping_matrix_invalid")
    sub_matrix = [
        round(full_matrix[0], 12),
        round(full_matrix[1], 12),
        round(full_matrix[0] * left + full_matrix[1] * top + full_matrix[2], 12),
        round(full_matrix[3], 12),
        round(full_matrix[4], 12),
        round(full_matrix[3] * left + full_matrix[4] * top + full_matrix[5], 12),
        0.0,
        0.0,
        1.0,
    ]
    inverse = _inverse_split_matrix(sub_matrix)
    points = [_apply_split_matrix(full_matrix, x, y) for x, y in ((left, top), (right, top), (right, bottom), (left, bottom))]
    pdf_bbox = [round(min(point[0] for point in points), 8), round(min(point[1] for point in points), 8), round(max(point[0] for point in points), 8), round(max(point[1] for point in points), 8)]
    return {
        "pdf_bbox_points": pdf_bbox,
        "render_pixel_bbox": [left, top, right, bottom],
        "crop_dimensions_px": {"width": right - left, "height": bottom - top},
        "render_dimensions_px": render_dimensions,
        "page_dimensions_points": dict(full_mapping["page_dimensions_points"]),
        "rotation": int(full_mapping["rotation"]),
        "render_dpi": int(full_mapping["render_dpi"]),
        "matrix_crop_to_pdf": sub_matrix,
        "matrix_pdf_to_crop": inverse,
        "round_trip_tolerance_points": float(full_mapping["round_trip_tolerance_points"]),
        "pixel_edge_mapping": "rotated-render-pixel-edges-v1",
        "crop_to_page": lambda x, y: _apply_split_matrix(sub_matrix, float(x), float(y)),
        "page_to_crop": lambda x, y: _apply_split_matrix(inverse, float(x), float(y)),
    }


def build_spread_split_contract(
    *,
    source_sha256: str,
    physical_page: int,
    page_geometry: Mapping[str, Any],
    render_receipt: Mapping[str, Any],
    render_bytes: bytes,
    render_dpi: int,
    content_rotation_clockwise: int,
    boundary_px: int | None = None,
    printed_page_label_candidates: Sequence[str | None] | None = None,
    runtime_commitments: Mapping[str, Any] | None = None,
    qualification_commitments: Mapping[str, Any] | None = None,
    contract_schema: str = SCANNED_PDF_SPLIT_SCHEMA,
    contract_protocol: str = SCANNED_PDF_SPLIT_PROTOCOL,
    contract_compiler_version: str = SCAN_SPLIT_CHECKPOINT_COMPILER_VERSION,
) -> dict[str, Any]:
    """Build an explicit, hash-bound two-region physical-page spread contract.

    The input is one canonical physical PDF page.  Orientation and the split
    boundary are caller-selected; this function never auto-detects either.
    Printed-page labels are deliberately only candidates or null.
    """

    if not _SHA256_RE.fullmatch(str(source_sha256)):
        raise ScanOcrError("spread_split_source_hash_invalid")
    if isinstance(physical_page, bool) or not isinstance(physical_page, int) or physical_page < 1:
        raise ScanOcrError("spread_split_physical_page_invalid")
    rotation = _split_rotation(content_rotation_clockwise)
    geometry_v2 = contract_schema == SCANNED_PDF_SPLIT_GEOMETRY_SCHEMA and contract_protocol == SCANNED_PDF_SPLIT_GEOMETRY_PROTOCOL and contract_compiler_version == SCANNED_PDF_SPLIT_GEOMETRY_COMPILER_VERSION
    if not geometry_v2 and (contract_schema, contract_protocol, contract_compiler_version) != (SCANNED_PDF_SPLIT_SCHEMA, SCANNED_PDF_SPLIT_PROTOCOL, SCAN_SPLIT_CHECKPOINT_COMPILER_VERSION):
        raise ScanOcrError("spread_split_contract_identity_invalid")
    if not isinstance(page_geometry, Mapping) or not isinstance(render_receipt, Mapping):
        raise ScanOcrError("spread_split_geometry_or_render_invalid")
    media = page_geometry.get("media_box")
    crop = page_geometry.get("crop_box")
    if not isinstance(media, (list, tuple)) or len(media) != 4 or not isinstance(crop, (list, tuple)) or len(crop) != 4:
        raise ScanOcrError("spread_split_page_boxes_invalid")
    try:
        media_values = [_finite(value) for value in media]
        [_finite(value) for value in crop]
    except ScanOcrError as error:
        raise ScanOcrError("spread_split_page_boxes_invalid") from error
    media_width = media_values[2] - media_values[0]
    media_height = media_values[3] - media_values[1]
    if media_width <= 0 or media_height <= 0:
        raise ScanOcrError("spread_split_page_boxes_invalid")
    dimensions = _png_dimensions(render_bytes)
    if dimensions is None:
        raise ScanOcrError("spread_split_render_png_invalid")
    render_hash = hashlib.sha256(render_bytes).hexdigest()
    if render_receipt.get("render_sha256") != render_hash:
        raise ScanOcrError("spread_split_render_hash_mismatch")
    dpi = int(render_receipt.get("renderer", {}).get("dpi", render_dpi)) if isinstance(render_receipt.get("renderer"), Mapping) else int(render_dpi)
    if dpi != int(render_dpi) or dpi < 36:
        raise ScanOcrError("spread_split_render_dpi_invalid")
    full_bbox = [0.0, 0.0, media_width, media_height]
    try:
        full_mapping = build_crop_mapping(
            bbox_pdf_points=full_bbox,
            page_geometry=page_geometry,
            render_dimensions_px=dimensions,
            render_dpi=dpi,
            allow_mediabox_bbox=True,
        )
    except PaddleRuntimeContractError as error:
        raise ScanOcrError(f"spread_split_full_mapping_invalid:{error}") from error
    if full_mapping.get("render_pixel_bbox") != [0, 0, dimensions["width"], dimensions["height"]]:
        raise ScanOcrError("spread_split_full_page_render_bbox_invalid")
    rotated = rotate_png_for_ocr(render_bytes, rotation)
    rotated_dimensions = _dimensions(rotated["dimensions_px"], "spread_split_rotated_dimensions_invalid")
    width = rotated_dimensions["width"]
    height = rotated_dimensions["height"]
    boundary = width // 2 if boundary_px is None else boundary_px
    if isinstance(boundary, bool) or not isinstance(boundary, int) or boundary <= 0 or boundary >= width:
        raise ScanOcrError("spread_split_boundary_invalid")
    labels = list(printed_page_label_candidates) if printed_page_label_candidates is not None else [None, None]
    if len(labels) != 2 or any(label is not None and (not isinstance(label, str) or not label.strip()) for label in labels):
        raise ScanOcrError("spread_split_printed_label_candidate_invalid")
    region_bboxes = [[0, 0, boundary, height], [boundary, 0, width, height]]
    region_ids = [
        _stable_id("spr", source_sha256, physical_page, rotation, boundary, order)
        for order in range(2)
    ]
    regions: list[dict[str, Any]] = []
    for order, rotated_bbox in enumerate(region_bboxes):
        pre_bbox = _rotate_bbox_to_pre_rotation(
            rotated_bbox,
            original_dimensions=dimensions,
            rotation=rotation,
        )
        # The full-page mapping is the authoritative reverse path from the
        # rotated raster region into PDF top-left points.
        pre_corners = [
            [pre_bbox[0], pre_bbox[1]],
            [pre_bbox[2], pre_bbox[1]],
            [pre_bbox[2], pre_bbox[3]],
            [pre_bbox[0], pre_bbox[3]],
        ]
        try:
            if geometry_v2:
                mapping = _pixel_first_split_mapping(full_mapping, pre_bbox)
            else:
                page_points = [list(full_mapping["crop_to_page"](x, y)) for x, y in pre_corners]
                pdf_bbox = [
                    round(min(point[0] for point in page_points), 8),
                    round(min(point[1] for point in page_points), 8),
                    round(max(point[0] for point in page_points), 8),
                    round(max(point[1] for point in page_points), 8),
                ]
                mapping = build_crop_mapping(
                    bbox_pdf_points=pdf_bbox,
                    page_geometry=page_geometry,
                    render_dimensions_px=dimensions,
                    render_dpi=dpi,
                )
            pre_crop = crop_render_png(render_bytes, mapping, expected_crop_sha256=None)
            direct_crop = crop_render_png(
                rotated["bytes"],
                {
                    "render_dimensions_px": dict(rotated_dimensions),
                    "render_pixel_bbox": list(rotated_bbox),
                },
                expected_crop_sha256=None,
            )
            transform = build_crop_coordinate_transform(
                source_sha256=source_sha256,
                physical_page=physical_page,
                render_sha256=render_hash,
                render_dpi=dpi,
                mapping=mapping,
            )
            transform = _rotate_crop_transform(
                transform,
                degrees_clockwise=rotation,
                original_dimensions=pre_crop["dimensions_px"],
                rotated_dimensions=direct_crop["dimensions_px"],
            )
        except PaddleRuntimeContractError as error:
            raise ScanOcrError(f"spread_split_region_mapping_invalid:{order}:{error}") from error
        if mapping.get("render_pixel_bbox") != pre_bbox or pre_crop["dimensions_px"] != {"width": pre_bbox[2] - pre_bbox[0], "height": pre_bbox[3] - pre_bbox[1]}:
            raise ScanOcrError("spread_split_pre_rotation_bbox_round_trip_invalid")
        if direct_crop["dimensions_px"] != {"width": rotated_bbox[2] - rotated_bbox[0], "height": rotated_bbox[3] - rotated_bbox[1]}:
            raise ScanOcrError("spread_split_rotated_bbox_dimensions_invalid")
        rotated_again = rotate_png_for_ocr(pre_crop["bytes"], rotation)
        if rotated_again["sha256"] != direct_crop["sha256"]:
            raise ScanOcrError("spread_split_crop_round_trip_hash_mismatch")
        source_anchor = {
            "anchor_id": _stable_id("sca", source_sha256, physical_page, region_ids[order]),
            "anchor_kind": "physical-page-spread-region-candidate",
            "source_sha256": source_sha256,
            "physical_page": physical_page,
        }
        transform["preprocessing"]["crop_sha256"] = direct_crop["sha256"]
        transform["preprocessing"]["pre_rotation_crop_sha256"] = pre_crop["sha256"]
        transform["preprocessing"]["source_anchor"] = source_anchor
        regions.append(
            {
                "region_id": region_ids[order],
                "physical_page": physical_page,
                "region_order": order,
                "reading_order": "left-to-right",
                "printed_page_label_candidate": labels[order],
                "printed_page_label_confirmed": False,
                "rotated_region_bbox_px": list(rotated_bbox),
                "pre_rotation_render_pixel_bbox": list(pre_bbox),
                "render_pixel_bbox": list(pre_bbox),
                "bbox_pdf_points": list(mapping["pdf_bbox_points"]),
                "render_sha256": render_hash,
                "crop_sha256": direct_crop["sha256"],
                "crop_dimensions_px": dict(direct_crop["dimensions_px"]),
                "pre_rotation_crop_sha256": pre_crop["sha256"],
                "content_rotation_clockwise": rotation,
                "coordinate_space": COORDINATE_SPACE,
                "coordinate_transform": transform,
                "coordinate_transform_sha256": sha256_json(transform),
                "source_anchor": source_anchor,
            }
        )
    contract = {
        "schema_version": contract_schema,
        "protocol": contract_protocol,
        "compiler_version": contract_compiler_version,
        "source_sha256": source_sha256,
        "source_page_kind": "physical-pdf-page",
        "physical_page": physical_page,
        "page_geometry": json.loads(json.dumps(dict(page_geometry), ensure_ascii=False, sort_keys=True)),
        "render": _split_render_record(render_receipt, dimensions, render_hash, dpi),
        "rotation": rotation,
        "rotated_render": {
            "sha256": rotated["sha256"],
            "dimensions_px": rotated_dimensions,
            "dpi": dpi,
            "content_rotation_clockwise": rotation,
        },
        "split": {
            "axis": "x",
            "boundary_px": boundary,
            "region_count": 2,
            "region_order": list(region_ids),
            "reading_order": "left-to-right",
            "odd_dimension_policy": "left=floor(width/2),right=remaining",
        },
        **({"pixel_mapping_policy": "rotated-render-pixel-edges-v1"} if geometry_v2 else {}),
        "printed_page_label_policy": "candidate-or-null-human-confirmation-required",
        "runtime_commitments": _split_commitments(runtime_commitments, _SPLIT_COMMITMENT_KEYS, code="spread_split_runtime_commitment_invalid"),
        "qualification_commitments": _split_commitments(qualification_commitments, _QUALIFICATION_COMMITMENT_KEYS, code="spread_split_qualification_commitment_invalid"),
        "regions": regions,
        "candidate_only": True,
        "promotion": False,
    }
    contract["split_core_sha256"] = sha256_json(_split_core_payload(contract))
    contract["contract_sha256"] = sha256_json({key: value for key, value in contract.items() if key != "contract_sha256"})
    validate_spread_split_contract(contract, require_bound=False)
    return contract


def bind_spread_split_contract(
    contract: Mapping[str, Any],
    *,
    runtime_commitments: Mapping[str, Any] | None = None,
    qualification_commitments: Mapping[str, Any] | None = None,
    require_qualified: bool = False,
) -> dict[str, Any]:
    """Return a new split snapshot with exact runtime/qualification bindings."""

    validate_spread_split_contract(contract, require_bound=False)
    result = json.loads(json.dumps(dict(contract), ensure_ascii=False, sort_keys=True))
    result["runtime_commitments"] = _split_commitments(runtime_commitments, _SPLIT_COMMITMENT_KEYS, code="spread_split_runtime_commitment_invalid")
    result["qualification_commitments"] = _split_commitments(qualification_commitments, _QUALIFICATION_COMMITMENT_KEYS, code="spread_split_qualification_commitment_invalid")
    result["split_core_sha256"] = sha256_json(_split_core_payload(result))
    if result["split_core_sha256"] != contract.get("split_core_sha256"):
        raise ScanOcrError("spread_split_core_hash_changed")
    if require_qualified and result["qualification_commitments"].get("receipt_sha256") is None:
        raise ScanOcrError("spread_split_qualification_receipt_required")
    result["contract_sha256"] = sha256_json({key: value for key, value in result.items() if key != "contract_sha256"})
    validate_spread_split_contract(result, require_bound=require_qualified)
    return result


def validate_spread_split_contract(contract: Mapping[str, Any], *, require_bound: bool = False) -> None:
    """Recompute the protocol-level invariants without reading a source PDF."""

    if not isinstance(contract, Mapping):
        raise ScanOcrError("spread_split_contract_invalid")
    identity = (contract.get("schema_version"), contract.get("protocol"), contract.get("compiler_version"))
    legacy_identity = (SCANNED_PDF_SPLIT_SCHEMA, SCANNED_PDF_SPLIT_PROTOCOL, SCAN_SPLIT_CHECKPOINT_COMPILER_VERSION)
    geometry_identity = (SCANNED_PDF_SPLIT_GEOMETRY_SCHEMA, SCANNED_PDF_SPLIT_GEOMETRY_PROTOCOL, SCANNED_PDF_SPLIT_GEOMETRY_COMPILER_VERSION)
    if identity not in {legacy_identity, geometry_identity}:
        raise ScanOcrError("spread_split_contract_schema_invalid")
    geometry_v2 = identity == geometry_identity
    if contract.get("candidate_only") is not True or contract.get("promotion") is not False:
        raise ScanOcrError("spread_split_contract_policy_invalid")
    if not _SHA256_RE.fullmatch(str(contract.get("source_sha256"))) or contract.get("source_page_kind") != "physical-pdf-page":
        raise ScanOcrError("spread_split_source_binding_invalid")
    if isinstance(contract.get("physical_page"), bool) or not isinstance(contract.get("physical_page"), int) or contract["physical_page"] < 1:
        raise ScanOcrError("spread_split_physical_page_invalid")
    rotation = _split_rotation(contract.get("rotation"))
    if contract.get("printed_page_label_policy") != "candidate-or-null-human-confirmation-required":
        raise ScanOcrError("spread_split_printed_label_policy_invalid")
    page_geometry = contract.get("page_geometry")
    if not isinstance(page_geometry, Mapping):
        raise ScanOcrError("spread_split_page_geometry_missing")
    media, crop, crop_top_left = page_geometry.get("media_box"), page_geometry.get("crop_box"), page_geometry.get("crop_box_top_left")
    if not isinstance(media, (list, tuple)) or len(media) != 4 or not isinstance(crop, (list, tuple)) or len(crop) != 4 or not isinstance(crop_top_left, (list, tuple)) or len(crop_top_left) != 4:
        raise ScanOcrError("spread_split_page_geometry_invalid")
    try:
        media_values = [_finite(value) for value in media]
        [_finite(value) for value in crop]
    except ScanOcrError as error:
        raise ScanOcrError("spread_split_page_geometry_invalid") from error
    media_width = media_values[2] - media_values[0]
    media_height = media_values[3] - media_values[1]
    if media_width <= 0 or media_height <= 0:
        raise ScanOcrError("spread_split_page_geometry_invalid")
    render = contract.get("render")
    rotated_render = contract.get("rotated_render")
    if not isinstance(render, Mapping) or not isinstance(rotated_render, Mapping):
        raise ScanOcrError("spread_split_render_receipt_invalid")
    render_dimensions = _dimensions(render.get("dimensions_px"), "spread_split_render_dimensions_invalid")
    rotated_dimensions = _dimensions(rotated_render.get("dimensions_px"), "spread_split_rotated_dimensions_invalid")
    if not _SHA256_RE.fullmatch(str(render.get("sha256"))) or not _SHA256_RE.fullmatch(str(rotated_render.get("sha256"))):
        raise ScanOcrError("spread_split_render_hash_invalid")
    if render.get("dpi") != rotated_render.get("dpi") or isinstance(render.get("dpi"), bool) or not isinstance(render.get("dpi"), int) or render.get("dpi") < 36:
        raise ScanOcrError("spread_split_render_dpi_invalid")
    try:
        full_mapping = build_crop_mapping(
            bbox_pdf_points=[0.0, 0.0, media_width, media_height],
            page_geometry=page_geometry,
            render_dimensions_px=render_dimensions,
            render_dpi=int(render["dpi"]),
            allow_mediabox_bbox=True,
        )
    except (PaddleRuntimeContractError, TypeError, ValueError) as error:
        raise ScanOcrError(f"spread_split_full_mapping_invalid:{error}") from error
    if full_mapping.get("render_pixel_bbox") != [0, 0, render_dimensions["width"], render_dimensions["height"]]:
        raise ScanOcrError("spread_split_full_page_render_bbox_invalid")
    expected_rotated_dimensions = {"width": render_dimensions["height"], "height": render_dimensions["width"]} if rotation in {90, 270} else render_dimensions
    if rotated_dimensions != expected_rotated_dimensions or rotated_render.get("content_rotation_clockwise") != rotation:
        raise ScanOcrError("spread_split_rotation_dimensions_invalid")
    split = contract.get("split")
    if not isinstance(split, Mapping) or split.get("axis") != "x" or split.get("region_count") != 2 or split.get("reading_order") != "left-to-right" or split.get("odd_dimension_policy") != "left=floor(width/2),right=remaining":
        raise ScanOcrError("spread_split_shape_invalid")
    if geometry_v2 and contract.get("pixel_mapping_policy") != "rotated-render-pixel-edges-v1":
        raise ScanOcrError("spread_split_pixel_mapping_policy_invalid")
    if not geometry_v2 and "pixel_mapping_policy" in contract:
        raise ScanOcrError("spread_split_legacy_pixel_mapping_field_forbidden")
    boundary = split.get("boundary_px")
    if isinstance(boundary, bool) or not isinstance(boundary, int) or boundary <= 0 or boundary >= rotated_dimensions["width"]:
        raise ScanOcrError("spread_split_boundary_invalid")
    region_order = split.get("region_order")
    regions = contract.get("regions")
    if not isinstance(region_order, list) or len(region_order) != 2 or not isinstance(regions, list) or len(regions) != 2 or len(set(str(value) for value in region_order)) != 2:
        raise ScanOcrError("spread_split_region_order_invalid")
    if [row.get("region_id") for row in regions if isinstance(row, Mapping)] != region_order:
        raise ScanOcrError("spread_split_region_order_invalid")
    expected_bboxes = [[0, 0, boundary, rotated_dimensions["height"]], [boundary, 0, rotated_dimensions["width"], rotated_dimensions["height"]]]
    expected_pre = [
        _rotate_bbox_to_pre_rotation(bbox, original_dimensions=render_dimensions, rotation=rotation)
        for bbox in expected_bboxes
    ]
    for order, region in enumerate(regions):
        if not isinstance(region, Mapping):
            raise ScanOcrError(f"spread_split_region_invalid:{order}")
        if region.get("region_id") != region_order[order] or region.get("physical_page") != contract.get("physical_page") or region.get("region_order") != order or region.get("reading_order") != "left-to-right":
            raise ScanOcrError(f"spread_split_region_order_invalid:{order}")
        label = region.get("printed_page_label_candidate")
        if label is not None and (not isinstance(label, str) or not label.strip()):
            raise ScanOcrError(f"spread_split_printed_label_candidate_invalid:{order}")
        if region.get("printed_page_label_confirmed") is not False:
            raise ScanOcrError(f"spread_split_printed_label_confirmation_forbidden:{order}")
        if region.get("rotated_region_bbox_px") != expected_bboxes[order] or region.get("pre_rotation_render_pixel_bbox") != expected_pre[order] or region.get("render_pixel_bbox") != expected_pre[order]:
            raise ScanOcrError(f"spread_split_region_partition_invalid:{order}")
        dims = _dimensions(region.get("crop_dimensions_px"), f"spread_split_crop_dimensions_invalid:{order}")
        expected_dims = {"width": expected_bboxes[order][2] - expected_bboxes[order][0], "height": expected_bboxes[order][3] - expected_bboxes[order][1]}
        if dims != expected_dims:
            raise ScanOcrError(f"spread_split_crop_dimensions_invalid:{order}")
        pre_bbox = expected_pre[order]
        pre_corners = [
            [pre_bbox[0], pre_bbox[1]],
            [pre_bbox[2], pre_bbox[1]],
            [pre_bbox[2], pre_bbox[3]],
            [pre_bbox[0], pre_bbox[3]],
        ]
        if geometry_v2:
            expected_mapping = _pixel_first_split_mapping(full_mapping, expected_pre[order])
            expected_pdf_bbox = expected_mapping["pdf_bbox_points"]
        else:
            page_points = [list(full_mapping["crop_to_page"](x, y)) for x, y in pre_corners]
            expected_pdf_bbox = [
                round(min(point[0] for point in page_points), 8),
                round(min(point[1] for point in page_points), 8),
                round(max(point[0] for point in page_points), 8),
                round(max(point[1] for point in page_points), 8),
            ]
        if region.get("bbox_pdf_points") != expected_pdf_bbox:
            raise ScanOcrError(f"spread_split_pdf_bbox_mapping_invalid:{order}")
        try:
            if not geometry_v2:
                expected_mapping = build_crop_mapping(
                    bbox_pdf_points=expected_pdf_bbox,
                    page_geometry=page_geometry,
                    render_dimensions_px=render_dimensions,
                    render_dpi=int(render["dpi"]),
                )
            if expected_mapping.get("render_pixel_bbox") != pre_bbox or expected_mapping.get("crop_dimensions_px") != {
                "width": pre_bbox[2] - pre_bbox[0],
                "height": pre_bbox[3] - pre_bbox[1],
            }:
                raise ScanOcrError(f"spread_split_pre_rotation_mapping_invalid:{order}")
        except (PaddleRuntimeContractError, TypeError, ValueError) as error:
            raise ScanOcrError(f"spread_split_region_mapping_invalid:{order}:{error}") from error
        for key in ("crop_sha256", "pre_rotation_crop_sha256", "render_sha256", "coordinate_transform_sha256"):
            if key != "pre_rotation_crop_sha256" and not _SHA256_RE.fullmatch(str(region.get(key))):
                raise ScanOcrError(f"spread_split_region_hash_invalid:{order}:{key}")
            if key == "pre_rotation_crop_sha256" and not _SHA256_RE.fullmatch(str(region.get(key))):
                raise ScanOcrError(f"spread_split_region_hash_invalid:{order}:{key}")
        if region.get("render_sha256") != render.get("sha256") or region.get("content_rotation_clockwise") != rotation or region.get("coordinate_space") != COORDINATE_SPACE:
            raise ScanOcrError(f"spread_split_region_render_binding_invalid:{order}")
        transform = region.get("coordinate_transform")
        if not isinstance(transform, Mapping) or region.get("coordinate_transform_sha256") != sha256_json(transform):
            raise ScanOcrError(f"spread_split_transform_hash_invalid:{order}")
        preprocessing = transform.get("preprocessing")
        if not isinstance(preprocessing, Mapping) or transform.get("source_sha256") != contract.get("source_sha256") or transform.get("physical_page") != contract.get("physical_page") or transform.get("render_sha256") != render.get("sha256") or transform.get("image_dimensions_px") != dims or preprocessing.get("content_rotation_clockwise") != rotation:
            raise ScanOcrError(f"spread_split_transform_binding_invalid:{order}")
        anchor = region.get("source_anchor")
        if not isinstance(anchor, Mapping) or not isinstance(anchor.get("anchor_id"), str) or not isinstance(anchor.get("anchor_kind"), str) or anchor.get("source_sha256") != contract.get("source_sha256") or anchor.get("physical_page") != contract.get("physical_page"):
            raise ScanOcrError(f"spread_split_source_anchor_invalid:{order}")
        try:
            expected_transform = build_crop_coordinate_transform(
                source_sha256=str(contract["source_sha256"]),
                physical_page=int(contract["physical_page"]),
                render_sha256=str(render["sha256"]),
                render_dpi=int(render["dpi"]),
                mapping=expected_mapping,
            )
            expected_transform = _rotate_crop_transform(
                expected_transform,
                degrees_clockwise=rotation,
                original_dimensions={
                    "width": expected_mapping["crop_dimensions_px"]["width"],
                    "height": expected_mapping["crop_dimensions_px"]["height"],
                },
                rotated_dimensions=dims,
            )
            expected_transform["preprocessing"]["crop_sha256"] = region["crop_sha256"]
            expected_transform["preprocessing"]["pre_rotation_crop_sha256"] = region["pre_rotation_crop_sha256"]
            expected_transform["preprocessing"]["source_anchor"] = dict(anchor)
        except (PaddleRuntimeContractError, TypeError, ValueError) as error:
            raise ScanOcrError(f"spread_split_transform_recompute_invalid:{order}:{error}") from error
        if transform != expected_transform:
            raise ScanOcrError(f"spread_split_transform_geometry_mismatch:{order}")
    runtime = _split_commitments(contract.get("runtime_commitments"), _SPLIT_COMMITMENT_KEYS, code="spread_split_runtime_commitment_invalid")
    qualification = _split_commitments(contract.get("qualification_commitments"), _QUALIFICATION_COMMITMENT_KEYS, code="spread_split_qualification_commitment_invalid")
    if require_bound and (any(value is None for value in runtime.values()) or any(value is None for value in qualification.values())):
        raise ScanOcrError("spread_split_bindings_incomplete")
    if contract.get("split_core_sha256") != sha256_json(_split_core_payload(contract)):
        raise ScanOcrError("spread_split_core_hash_mismatch")
    if contract.get("contract_sha256") != sha256_json({key: value for key, value in contract.items() if key != "contract_sha256"}):
        raise ScanOcrError("spread_split_contract_hash_mismatch")


def build_scan_checkpoint(
    *,
    bindings: Mapping[str, Any],
    regions: Sequence[Mapping[str, Any]],
    status: str | None = None,
    batch_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a private, candidate-only page/region checkpoint snapshot."""

    if not isinstance(bindings, Mapping) or not bindings:
        raise ScanOcrError("scan_checkpoint_bindings_invalid")
    required_bindings = {
        "source_sha256",
        "render_sha256",
        "split_contract_sha256",
        "split_core_sha256",
        "content_rotation_clockwise",
        "tool_sha256",
        "model_identity_sha256",
        "configuration_sha256",
        "runtime_contract_sha256",
        "worker_sha256",
        "qualification_receipt_sha256",
    }
    if set(bindings) != required_bindings:
        raise ScanOcrError("scan_checkpoint_binding_set_invalid")
    normalized_bindings = json.loads(json.dumps(dict(bindings), ensure_ascii=False, sort_keys=True))
    if not _SHA256_RE.fullmatch(str(normalized_bindings.get("source_sha256"))):
        raise ScanOcrError("scan_checkpoint_source_hash_invalid")
    if normalized_bindings.get("content_rotation_clockwise") not in {0, 90, 180, 270}:
        raise ScanOcrError("scan_checkpoint_rotation_invalid")
    for key in required_bindings - {"content_rotation_clockwise"}:
        value = normalized_bindings.get(key)
        if value is not None and not _SHA256_RE.fullmatch(str(value)):
            raise ScanOcrError(f"scan_checkpoint_binding_hash_invalid:{key}")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, supplied in enumerate(regions):
        if not isinstance(supplied, Mapping):
            raise ScanOcrError(f"scan_checkpoint_region_invalid:{index}")
        region_id = supplied.get("region_id")
        if not isinstance(region_id, str) or not region_id or region_id in seen:
            raise ScanOcrError(f"scan_checkpoint_region_duplicate:{index}")
        seen.add(region_id)
        region_status = supplied.get("status")
        if region_status not in {"complete", "paused", "pending"}:
            raise ScanOcrError(f"scan_checkpoint_region_status_invalid:{index}")
        row = {
            "region_id": region_id,
            "region_order": supplied.get("region_order"),
            "status": region_status,
            "binding_sha256": supplied.get("binding_sha256"),
        }
        if isinstance(row["region_order"], bool) or not isinstance(row["region_order"], int) or row["region_order"] < 0:
            raise ScanOcrError(f"scan_checkpoint_region_order_invalid:{index}")
        if not _SHA256_RE.fullmatch(str(row["binding_sha256"])):
            raise ScanOcrError(f"scan_checkpoint_region_binding_invalid:{index}")
        if region_status == "complete":
            artifact = supplied.get("artifact")
            if not isinstance(artifact, Mapping):
                raise ScanOcrError(f"scan_checkpoint_complete_artifact_missing:{index}")
            row["artifact"] = json.loads(json.dumps(dict(artifact), ensure_ascii=False, sort_keys=True))
            row["artifact_sha256"] = sha256_json(row["artifact"])
        else:
            error_code = supplied.get("error_code")
            if error_code is not None and (not isinstance(error_code, str) or not error_code.strip()):
                raise ScanOcrError(f"scan_checkpoint_error_code_invalid:{index}")
            row["error_code"] = error_code
        rows.append(row)
    rows.sort(key=lambda row: (int(row["region_order"]), row["region_id"]))
    if status is None:
        status = "complete" if rows and all(row["status"] == "complete" for row in rows) else "paused"
    if status not in {"complete", "paused"}:
        raise ScanOcrError("scan_checkpoint_status_invalid")
    if status == "complete" and (not rows or any(row["status"] != "complete" for row in rows)):
        raise ScanOcrError("scan_checkpoint_complete_with_pending_regions")
    checkpoint = {
        "schema_version": SCANNED_PDF_CHECKPOINT_SCHEMA,
        "protocol": SCANNED_PDF_CHECKPOINT_PROTOCOL,
        "compiler_version": SCAN_SPLIT_CHECKPOINT_COMPILER_VERSION,
        "status": status,
        "bindings": normalized_bindings,
        "regions": rows,
        "candidate_only": True,
        "promotion": False,
    }
    if batch_receipt is not None:
        if not isinstance(batch_receipt, Mapping):
            raise ScanOcrError("scan_checkpoint_batch_receipt_invalid")
        checkpoint["batch_receipt"] = json.loads(json.dumps(dict(batch_receipt), ensure_ascii=False, sort_keys=True))
    checkpoint["checkpoint_sha256"] = sha256_json(checkpoint)
    validate_scan_checkpoint(checkpoint, expected_bindings=normalized_bindings)
    return checkpoint


def validate_scan_checkpoint(
    checkpoint: Mapping[str, Any],
    *,
    expected_bindings: Mapping[str, Any] | None = None,
    expected_region_order: Sequence[str] | None = None,
) -> None:
    """Validate checkpoint identity and reject stale/duplicate region state."""

    if not isinstance(checkpoint, Mapping) or checkpoint.get("schema_version") != SCANNED_PDF_CHECKPOINT_SCHEMA or checkpoint.get("protocol") != SCANNED_PDF_CHECKPOINT_PROTOCOL or checkpoint.get("compiler_version") != SCAN_SPLIT_CHECKPOINT_COMPILER_VERSION:
        raise ScanOcrError("scan_checkpoint_schema_invalid")
    if checkpoint.get("candidate_only") is not True or checkpoint.get("promotion") is not False:
        raise ScanOcrError("scan_checkpoint_policy_invalid")
    if checkpoint.get("checkpoint_sha256") != sha256_json({key: value for key, value in checkpoint.items() if key != "checkpoint_sha256"}):
        raise ScanOcrError("scan_checkpoint_hash_mismatch")
    bindings = checkpoint.get("bindings")
    if not isinstance(bindings, Mapping):
        raise ScanOcrError("scan_checkpoint_bindings_invalid")
    if expected_bindings is not None:
        if dict(bindings) != dict(expected_bindings):
            raise ScanOcrError("scan_checkpoint_stale_bindings")
    regions = checkpoint.get("regions")
    if not isinstance(regions, list) or not regions:
        raise ScanOcrError("scan_checkpoint_regions_invalid")
    seen_ids: set[str] = set()
    seen_orders: set[int] = set()
    for index, row in enumerate(regions):
        if not isinstance(row, Mapping):
            raise ScanOcrError(f"scan_checkpoint_region_invalid:{index}")
        region_id = row.get("region_id")
        order = row.get("region_order")
        if not isinstance(region_id, str) or not region_id or region_id in seen_ids:
            raise ScanOcrError(f"scan_checkpoint_region_duplicate:{index}")
        if isinstance(order, bool) or not isinstance(order, int) or order < 0 or order in seen_orders:
            raise ScanOcrError(f"scan_checkpoint_region_order_invalid:{index}")
        seen_ids.add(region_id)
        seen_orders.add(order)
        if row.get("status") not in {"complete", "paused", "pending"}:
            raise ScanOcrError(f"scan_checkpoint_region_status_invalid:{index}")
        if not _SHA256_RE.fullmatch(str(row.get("binding_sha256"))):
            raise ScanOcrError(f"scan_checkpoint_region_binding_invalid:{index}")
        if row.get("status") == "complete":
            artifact = row.get("artifact")
            if not isinstance(artifact, Mapping) or row.get("artifact_sha256") != sha256_json(artifact):
                raise ScanOcrError(f"scan_checkpoint_complete_artifact_invalid:{index}")
    if checkpoint.get("status") not in {"complete", "paused"}:
        raise ScanOcrError("scan_checkpoint_status_invalid")
    if checkpoint.get("status") == "complete" and any(row.get("status") != "complete" for row in regions):
        raise ScanOcrError("scan_checkpoint_complete_with_pending_regions")
    if "batch_receipt" in checkpoint and not isinstance(checkpoint.get("batch_receipt"), Mapping):
        raise ScanOcrError("scan_checkpoint_batch_receipt_invalid")
    if expected_region_order is not None:
        expected = list(expected_region_order)
        actual = [row.get("region_id") for row in sorted(regions, key=lambda item: int(item.get("region_order", -1)))]
        if actual != expected:
            raise ScanOcrError("scan_checkpoint_region_order_mismatch")


def resume_scan_checkpoint(
    checkpoint: Mapping[str, Any],
    *,
    expected_bindings: Mapping[str, Any],
    expected_region_order: Sequence[str],
) -> dict[str, Any]:
    """Return only complete reusable regions and the paused worklist."""

    validate_scan_checkpoint(
        checkpoint,
        expected_bindings=expected_bindings,
        expected_region_order=expected_region_order,
    )
    ordered = sorted(checkpoint["regions"], key=lambda row: int(row["region_order"]))
    reusable = [row["region_id"] for row in ordered if row["status"] == "complete"]
    pending = [row["region_id"] for row in ordered if row["status"] != "complete"]
    return {
        "reusable_region_ids": reusable,
        "pending_region_ids": pending,
        "status": "complete" if not pending else "paused",
        "checkpoint_sha256": checkpoint["checkpoint_sha256"],
    }


def _checkpoint_render_sha256(render_by_page: Mapping[int, Mapping[str, Any]]) -> str:
    return sha256_json([
        {"physical_page": int(page), "render_sha256": row.get("render_sha256")}
        for page, row in sorted(render_by_page.items())
    ])


def _checkpoint_region_binding_sha256(
    *,
    binding: Mapping[str, Any],
    transform: Mapping[str, Any],
    split_contract: Mapping[str, Any] | None,
) -> str:
    return sha256_json(
        {
            "region_id": binding.get("region_id"),
            "physical_page": binding.get("physical_page"),
            "region_order": binding.get("region_order"),
            "render_sha256": binding.get("render_sha256"),
            "render_pixel_bbox": binding.get("render_pixel_bbox"),
            "crop_sha256": binding.get("crop_sha256"),
            "crop_dimensions_px": binding.get("crop_dimensions_px"),
            "pre_rotation_crop_sha256": binding.get("pre_rotation_crop_sha256"),
            "content_rotation_clockwise": binding.get("content_rotation_clockwise"),
            "coordinate_transform_id": transform.get("id"),
            "coordinate_transform_sha256": sha256_json(transform),
            "split_contract_sha256": split_contract.get("contract_sha256") if isinstance(split_contract, Mapping) else None,
            "split_core_sha256": split_contract.get("split_core_sha256") if isinstance(split_contract, Mapping) else None,
        }
    )


def _checkpoint_expected_bindings(
    *,
    source_sha256: str,
    render_by_page: Mapping[int, Mapping[str, Any]],
    split_contract: Mapping[str, Any] | None,
    rotation: int,
    configuration: Mapping[str, Any],
    contract: Mapping[str, Any],
    qualification_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "source_sha256": source_sha256,
        "render_sha256": _checkpoint_render_sha256(render_by_page),
        "split_contract_sha256": split_contract.get("contract_sha256") if isinstance(split_contract, Mapping) else None,
        "split_core_sha256": split_contract.get("split_core_sha256") if isinstance(split_contract, Mapping) else None,
        "content_rotation_clockwise": int(rotation),
        "tool_sha256": sha256_file(Path(__file__).resolve()),
        "model_identity_sha256": (contract.get("model_status") or {}).get("stable_identity_sha256"),
        "configuration_sha256": sha256_json(configuration),
        "runtime_contract_sha256": contract.get("contract_sha256"),
        "worker_sha256": contract.get("worker_sha256"),
        "qualification_receipt_sha256": qualification_receipt_sha256,
    }


def _apply_matrix(matrix: list[float], point: Iterable[Any]) -> list[float]:
    values = list(point)
    if len(values) != 2:
        raise ScanOcrError("coordinate_point_invalid")
    x, y = _finite(values[0]), _finite(values[1])
    a, b, c, d, e, f, g, h, i = matrix
    denominator = g * x + h * y + i
    if abs(denominator) < 1e-12:
        raise ScanOcrError("coordinate_transform_projection_invalid")
    return [_point((a * x + b * y + c) / denominator), _point((d * x + e * y + f) / denominator)]


def _bbox_from_polygon(polygon: list[list[float]]) -> list[float]:
    if not polygon:
        raise ScanOcrError("coordinate_polygon_empty")
    xs = [point[0] for point in polygon]
    ys = [point[1] for point in polygon]
    return [_point(min(xs)), _point(min(ys)), _point(max(xs)), _point(max(ys))]


def transform_polygon(
    polygon: Iterable[Iterable[Any]],
    matrix: Iterable[Any],
    *,
    target_dimensions: Mapping[str, Any] | None = None,
) -> list[list[float]]:
    """Map a preprocessed image polygon into canonical PDF top-left points."""

    normalized_matrix = _matrix(list(matrix))
    result = [_apply_matrix(normalized_matrix, point) for point in polygon]
    if len(result) < 3:
        raise ScanOcrError("coordinate_polygon_too_few_points")
    if target_dimensions is not None:
        width = _finite(target_dimensions.get("width"))
        height = _finite(target_dimensions.get("height"))
        if any(
            point[0] < -0.01
            or point[1] < -0.01
            or point[0] > width + 0.01
            or point[1] > height + 0.01
            for point in result
        ):
            raise ScanOcrError("coordinate_polygon_outside_pdf_page")
    return result


def build_default_coordinate_transform(
    *,
    source_sha256: str,
    physical_page: int,
    render_sha256: str,
    render_dpi: int,
    image_dimensions_px: Mapping[str, Any],
    pdf_dimensions_points: Mapping[str, Any],
    preprocessing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the auditable pixel-to-PDF transform used by every observation."""

    image_width = _finite(image_dimensions_px.get("width"))
    image_height = _finite(image_dimensions_px.get("height"))
    pdf_width = _finite(pdf_dimensions_points.get("width"))
    pdf_height = _finite(pdf_dimensions_points.get("height"))
    if min(image_width, image_height, pdf_width, pdf_height) <= 0:
        raise ScanOcrError("coordinate_dimensions_invalid")
    preprocessing_record = dict(preprocessing or {})
    configured_matrix = preprocessing_record.get("matrix_3x3")
    if configured_matrix is None:
        matrix = [pdf_width / image_width, 0.0, 0.0, 0.0, pdf_height / image_height, 0.0, 0.0, 0.0, 1.0]
        matrix_source = "render-pixel-scale-v1"
    else:
        matrix = _matrix(configured_matrix)
        matrix_source = "caller-supplied-preprocess-homography-v1"
    inverse = _inverse(matrix)
    payload = {
        "source_sha256": source_sha256,
        "physical_page": physical_page,
        "render_sha256": render_sha256,
        "render_dpi": render_dpi,
        "source_space": IMAGE_SPACE,
        "target_space": COORDINATE_SPACE,
        "image_dimensions_px": {"width": int(round(image_width)), "height": int(round(image_height))},
        "pdf_dimensions_points": {"width": _point(pdf_width), "height": _point(pdf_height)},
        "preprocessing": preprocessing_record,
        "matrix_3x3": matrix,
        "inverse_matrix_3x3": inverse,
        "matrix_source": matrix_source,
        "round_trip_tolerance_points": 0.02,
    }
    return {
        "schema_version": OCR_COORDINATE_TRANSFORM_SCHEMA,
        "id": _stable_id("pct", payload),
        **payload,
    }


def _png_dimensions(data: bytes) -> dict[str, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    try:
        width, height = struct.unpack(">II", data[16:24])
    except struct.error:
        return None
    if width <= 0 or height <= 0:
        return None
    return {"width": width, "height": height}


def _renderer_path(value: Path | str) -> Path:
    candidate = Path(str(value)).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    found = shutil.which(str(value))
    if found:
        return Path(found).resolve()
    raise ScanOcrError(f"pdftoppm_missing: {value}")


@contextlib.contextmanager
def _render_pages(
    source_path: Path,
    pages: Iterable[int],
    renderer: Path | str,
    *,
    dpi: int,
    pdf_dimensions: Mapping[int, Mapping[str, Any]],
    workspace_root: Path | None = None,
) -> Iterator[tuple[list[dict[str, Any]], dict[int, Path]]]:
    if dpi < 36 or dpi > 1200:
        raise ScanOcrError("render_dpi_out_of_range")
    renderer_path = _renderer_path(renderer)
    version_process = subprocess.run(
        [str(renderer_path), "-v"], check=False, capture_output=True, text=True
    )
    version_lines = (version_process.stderr or version_process.stdout).strip().splitlines()
    if version_process.returncode != 0 or not version_lines:
        raise ScanOcrError("pdftoppm_version_unavailable")
    renderer_version = version_lines[0]
    records: list[dict[str, Any]] = []
    paths: dict[int, Path] = {}
    temporary_parent = None
    if workspace_root is not None:
        temporary_parent = workspace_root.expanduser().resolve()
        temporary_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tkc-scan-ocr-render-", dir=str(temporary_parent) if temporary_parent else None) as temporary:
        root = Path(temporary)
        try:
            for page in sorted(set(pages)):
                prefix = root / f"page-{page:04d}"
                target = prefix.with_suffix(".png")
                completed = subprocess.run(
                    [
                        str(renderer_path),
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
                    raise ScanOcrError(f"pdf_render_failed: page={page}")
                data = target.read_bytes()
                if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
                    raise ScanOcrError(f"pdf_render_invalid_png: page={page}")
                digest = hashlib.sha256(data).hexdigest()
                records.append(
                    {
                        "schema_version": "tkc.pdf-render-receipt/v0.1",
                        "source_sha256": sha256_file(source_path),
                        "physical_page": page,
                        "renderer": {
                            "id": "pdftoppm",
                            "version": renderer_version,
                            "dpi": dpi,
                            "format": "png",
                            "page_selection": "single-physical-page",
                        },
                        "render_sha256": digest,
                    }
                )
                paths[page] = target
            yield records, paths
        finally:
            paths.clear()


def _validate_backend_name(name: str | None, *, role: str) -> None:
    if name is None:
        return
    if name not in BACKEND_SPECS:
        raise ScanOcrError(f"unsupported_ocr_backend: {name}")
    if name == "fake" and role != "test-only":
        raise ScanOcrError("fake_backend_requires_test_only_role")


def validate_backend_selection(
    primary: str,
    challenger: str | None = None,
    baseline: str | None = None,
) -> dict[str, str | None]:
    _validate_backend_name(primary, role="test-only" if primary == "fake" else "primary")
    _validate_backend_name(challenger, role="challenger")
    _validate_backend_name(baseline, role="baseline")
    selected = [name for name in (primary, challenger, baseline) if name is not None]
    if len(set(selected)) != len(selected):
        raise ScanOcrError("ocr_backend_duplicate_selection")
    if challenger == "fake" or baseline == "fake":
        raise ScanOcrError("fake_backend_must_be_primary_test_backend")
    return {"primary": primary, "challenger": challenger, "baseline": baseline}


def _model_identity(backend: str, model_root: Path | None) -> dict[str, Any]:
    spec = BACKEND_SPECS[backend]
    if backend == "fake" and model_root is None:
        return {
            "model_id": "synthetic-fake-backend",
            "source": "synthetic-test-fixture",
            "files": [],
            "files_sha256": sha256_json([]),
            "identity_sha256": sha256_json({"model_id": "synthetic-fake-backend", "files": []}),
        }
    if not spec["model_required"] and model_root is None:
        return {
            "model_id": "runtime-local-baseline",
            "source": "local-runtime",
            "files": [],
            "files_sha256": sha256_json([]),
            "identity_sha256": sha256_json({"model_id": "runtime-local-baseline", "files": []}),
        }
    if model_root is None:
        raise ScanOcrError(f"model_root_required: backend={backend}")
    root = model_root.expanduser().resolve()
    if not root.is_dir():
        raise ScanOcrError(f"model_root_missing: backend={backend}")
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ScanOcrError(f"model_symlink_forbidden: {relative}")
        if not path.is_file():
            continue
        files.append(
            {
                "relative_path": relative,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    if not files:
        raise ScanOcrError(f"model_root_empty: backend={backend}")
    identity = {
        "model_id": f"local-{backend}",
        "source": "local-preinstalled-model-root",
        "files": files,
        "files_sha256": sha256_json(files),
    }
    identity["identity_sha256"] = sha256_json(identity)
    return identity


def _effective_config(backend: str, raw: Mapping[str, Any] | None) -> dict[str, Any]:
    supplied = dict(raw or {})
    if supplied.get("allow_network") is True or supplied.get("network") in {"on", "enabled", True}:
        raise ScanOcrError("network_disabled_for_scan_ocr")
    preprocessing = dict(supplied.get("preprocessing") or {})
    recognition = dict(supplied.get("recognition") or {})
    backend_options = dict(supplied.get(backend) or supplied.get("backend_options") or {})
    if backend_options.get("allow_model_download") is True:
        raise ScanOcrError("model_download_disabled_for_scan_ocr")
    if backend == PADDLE_OCR_BACKEND and not backend_options:
        profile = profile_definition(TECHNICAL_TEXT_PROFILE_ID)
        backend_options = {
            "api": "paddleocr.predict",
            "device": "cpu",
            "allow_model_download": False,
            "profile_id": TECHNICAL_TEXT_PROFILE_ID,
            "model_names": PROFILE_MODEL_NAMES[TECHNICAL_TEXT_PROFILE_ID],
            "constructor_options": profile["constructor"],
            "predict_options": profile["predict"],
            "model_dirs": profile["model_dirs"],
        }
    elif backend in {PADDLE_STRUCTURE_BACKEND, PADDLE_STRUCTURE_V3_BACKEND} and not backend_options:
        profile = profile_definition(TECHNICAL_TABLE_PROFILE_ID)
        backend_options = {
            "api": "ppstructure-v3",
            "device": "cpu",
            "allow_model_download": False,
            "profile_id": TECHNICAL_TABLE_PROFILE_ID,
            "model_names": PROFILE_MODEL_NAMES[TECHNICAL_TABLE_PROFILE_ID],
            "constructor_options": profile["constructor"],
            "predict_options": profile["predict"],
            "model_dirs": profile["model_dirs"],
        }
    elif backend in {PADDLE_OCR_BACKEND, PADDLE_STRUCTURE_BACKEND, PADDLE_STRUCTURE_V3_BACKEND}:
        profile_id = backend_options.get("profile_id")
        expected_profile = TECHNICAL_TEXT_PROFILE_ID if backend == PADDLE_OCR_BACKEND else TECHNICAL_TABLE_PROFILE_ID
        if profile_id != expected_profile:
            raise ScanOcrError("paddle_profile_required_or_mismatched")
        profile = profile_definition(expected_profile)
        backend_options.setdefault("model_names", PROFILE_MODEL_NAMES[expected_profile])
        if backend_options.get("model_names") != PROFILE_MODEL_NAMES[expected_profile]:
            raise ScanOcrError("paddle_profile_model_names_required")
        if backend_options.get("constructor_options") != profile["constructor"]:
            raise ScanOcrError("paddle_profile_constructor_switches_required")
        if backend_options.get("predict_options") != profile["predict"]:
            raise ScanOcrError("paddle_profile_predict_switches_required")
    effective = {
        "backend_id": backend,
        "network_policy": "offline-enforced",
        "network_enabled": False,
        "model_download": False,
        "preprocessing": preprocessing,
        "recognition": recognition,
        "backend_options": backend_options,
        "user_config": supplied,
    }
    return effective


def _runtime_engine_configuration(configuration: Mapping[str, Any]) -> dict[str, Any]:
    """Strip host orchestration material from the hash-bound engine config."""

    backend_name = str(configuration.get("backend_id", ""))
    if backend_name not in {
        PADDLE_OCR_BACKEND,
        PADDLE_STRUCTURE_BACKEND,
        PADDLE_STRUCTURE_V3_BACKEND,
        PADDLE_CHART_BACKEND,
    }:
        # Legacy/fake/challenger receipts hash the complete effective config.
        # The host-only fields below exist only in the external Paddle route;
        # applying its projection globally would rewrite their historical ABI.
        return json.loads(json.dumps(dict(configuration), ensure_ascii=False, sort_keys=True))
    result = {
        str(key): value
        for key, value in dict(configuration).items()
        if key not in {
            "external_runtime",
            "runtime_qualifications",
            "phase7d1_workspace",
            "worker_timeout_seconds",
            "worker_request_timeout_seconds",
            "worker_max_requests",
        }
    }
    user_config = result.get("user_config")
    if isinstance(user_config, Mapping):
        result["user_config"] = {
            str(key): value
            for key, value in dict(user_config).items()
            if key not in {
                "external_runtime",
                "runtime_qualifications",
                "phase7d1_workspace",
                "worker_timeout_seconds",
                "worker_request_timeout_seconds",
                "worker_max_requests",
                "backend_options",
                "preprocessing",
                "recognition",
                "backend_id",
                "network_policy",
                "network_enabled",
                "model_download",
                "user_config",
                backend_name,
            }
            and not str(key).startswith("chart_")
            and not str(key).startswith("technical_chart_")
        }
    return json.loads(json.dumps(result, ensure_ascii=False, sort_keys=True))


def _qualification_receipt_sha256(
    configuration: Mapping[str, Any],
    backend: str,
) -> str | None:
    """Read an operator-supplied qualification commitment without creating one."""

    supplied = configuration.get("runtime_qualifications")
    if supplied is None and isinstance(configuration.get("user_config"), Mapping):
        supplied = configuration["user_config"].get("runtime_qualifications")
    if isinstance(supplied, Mapping) and supplied.get("schema_version") in {
        "tkc.paddle-runtime-qualification/v0.1",
        "tkc.paddle-runtime-split-qualification/v0.1",
    }:
        supplied = {str(supplied.get("backend_id")): supplied}
    if not isinstance(supplied, Mapping):
        return None
    row = supplied.get(backend)
    if not isinstance(row, Mapping):
        return None
    value = row.get("receipt_sha256")
    return value if isinstance(value, str) and _SHA256_RE.fullmatch(value) else None


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ScanOcrError(f"invalid_json: {path}") from error


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _validate_json_schema_value(value: Any, schema: Mapping[str, Any], root_schema: Mapping[str, Any], path: str) -> None:
    alternatives = schema.get("oneOf")
    if isinstance(alternatives, list):
        matches = 0
        for alternative in alternatives:
            try:
                _validate_json_schema_value(value, alternative, root_schema, path)
            except ScanOcrError:
                continue
            matches += 1
        if matches != 1:
            raise ScanOcrError(f"scan_ir_schema_invalid:{path}:oneOf")
        return
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/"):
        target: Any = root_schema
        for part in reference[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        _validate_json_schema_value(value, target, root_schema, path)
        return
    if "const" in schema and value != schema["const"]:
        raise ScanOcrError(f"scan_ir_schema_invalid:{path}:const")
    if "enum" in schema and value not in schema["enum"]:
        raise ScanOcrError(f"scan_ir_schema_invalid:{path}:enum")
    expected_type = schema.get("type")
    if expected_type is not None:
        types = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_json_type_matches(value, str(item)) for item in types):
            raise ScanOcrError(f"scan_ir_schema_invalid:{path}:type")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            raise ScanOcrError(f"scan_ir_schema_invalid:{path}:minLength")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            raise ScanOcrError(f"scan_ir_schema_invalid:{path}:pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if "minimum" in schema and number < float(schema["minimum"]):
            raise ScanOcrError(f"scan_ir_schema_invalid:{path}:minimum")
        if "maximum" in schema and number > float(schema["maximum"]):
            raise ScanOcrError(f"scan_ir_schema_invalid:{path}:maximum")
        if "exclusiveMinimum" in schema and number <= float(schema["exclusiveMinimum"]):
            raise ScanOcrError(f"scan_ir_schema_invalid:{path}:exclusiveMinimum")
        if "exclusiveMaximum" in schema and number >= float(schema["exclusiveMaximum"]):
            raise ScanOcrError(f"scan_ir_schema_invalid:{path}:exclusiveMaximum")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            raise ScanOcrError(f"scan_ir_schema_invalid:{path}:minItems")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise ScanOcrError(f"scan_ir_schema_invalid:{path}:maxItems")
        if schema.get("uniqueItems"):
            fingerprints = [sha256_json(item) for item in value]
            if len(fingerprints) != len(set(fingerprints)):
                raise ScanOcrError(f"scan_ir_schema_invalid:{path}:uniqueItems")
        prefix_items = schema.get("prefixItems")
        if isinstance(prefix_items, list):
            for index, item_schema in enumerate(prefix_items[: len(value)]):
                _validate_json_schema_value(value[index], item_schema, root_schema, f"{path}[{index}]")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _validate_json_schema_value(item, item_schema, root_schema, f"{path}[{index}]")
    if isinstance(value, dict):
        if "minProperties" in schema and len(value) < int(schema["minProperties"]):
            raise ScanOcrError(f"scan_ir_schema_invalid:{path}:minProperties")
        properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        for name in required:
            if name not in value:
                raise ScanOcrError(f"scan_ir_schema_invalid:{path}.{name}:required")
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise ScanOcrError(f"scan_ir_schema_invalid:{path}.{unknown[0]}:additionalProperties")
        additional = schema.get("additionalProperties")
        for name, item in value.items():
            if name in properties:
                _validate_json_schema_value(item, properties[name], root_schema, f"{path}.{name}")
            elif isinstance(additional, Mapping):
                _validate_json_schema_value(item, additional, root_schema, f"{path}.{name}")


def validate_scanned_pdf_ir_schema(scan_ir: Mapping[str, Any], *, require_bound_components: bool = False) -> None:
    """Validate scan-ir.json against the bundled formal schema and file ABI."""

    if not isinstance(scan_ir, Mapping):
        raise ScanOcrError("scan_ir_schema_invalid:root:type")
    schema_path = Path(__file__).resolve().parents[1] / "assets" / "schemas" / "scanned-pdf-ir.schema.json"
    try:
        schema = _load_json(schema_path)
    except ScanOcrError as error:
        raise ScanOcrError("scan_ir_schema_source_missing") from error
    if not isinstance(schema, Mapping):
        raise ScanOcrError("scan_ir_schema_source_invalid")
    _validate_json_schema_value(dict(scan_ir), schema, schema, "$")
    if scan_ir.get("scope", {}).get("end_page", 0) < scan_ir.get("scope", {}).get("start_page", 1):
        raise ScanOcrError("scan_ir_schema_invalid:scope:order")
    if require_bound_components:
        expected = {
            "ocr_observations", "ocr_transcripts", "ocr_backend_receipts", "coordinate_transforms",
            "ocr_conflicts", "ocr_quality_routing", "ocr_worker_responses", "visual_candidates",
            "visual_gaps", "visual_conflicts", "result_adapters",
        }
        file_refs = scan_ir.get("file_refs")
        component_hashes = scan_ir.get("component_sha256")
        if not isinstance(file_refs, Mapping) or set(file_refs) != expected:
            raise ScanOcrError("scan_ir_file_refs_invalid")
        if not isinstance(component_hashes, Mapping) or set(component_hashes) != expected:
            raise ScanOcrError("scan_ir_component_hashes_invalid")
        for key in expected:
            filename = file_refs.get(key)
            if not isinstance(filename, str) or Path(filename).name != filename or not _SHA256_RE.fullmatch(str(component_hashes.get(key))):
                raise ScanOcrError(f"scan_ir_component_binding_invalid:{key}")
    structure = scan_ir.get("reviewed_scan_structure")
    split_contracts = scan_ir.get("spread_split_contracts")
    if split_contracts is not None:
        if not isinstance(split_contracts, list):
            raise ScanOcrError("scan_ir_spread_split_contracts_invalid")
        for index, contract in enumerate(split_contracts, start=1):
            try:
                validate_spread_split_contract(contract, require_bound=False)
            except ScanOcrError as error:
                raise ScanOcrError(f"scan_ir_spread_split_contract_invalid:{index}:{error}") from error
    checkpoint = scan_ir.get("scan_checkpoint")
    if checkpoint is not None:
        if not isinstance(checkpoint, Mapping) or checkpoint.get("schema_version") != SCANNED_PDF_CHECKPOINT_SCHEMA or checkpoint.get("protocol") != SCANNED_PDF_CHECKPOINT_PROTOCOL or checkpoint.get("compiler_version") != SCAN_SPLIT_CHECKPOINT_COMPILER_VERSION or not _SHA256_RE.fullmatch(str(checkpoint.get("checkpoint_sha256"))) or checkpoint.get("status") not in {"complete", "paused"} or not isinstance(checkpoint.get("bindings"), Mapping) or not isinstance(checkpoint.get("regions"), list):
            raise ScanOcrError("scan_ir_checkpoint_manifest_invalid")
    if structure is not None:
        if (
            not isinstance(structure, Mapping)
            or structure.get("source_sha256") != scan_ir.get("source_sha256")
            or structure.get("structure_sha256") != sha256_json({key: value for key, value in structure.items() if key != "structure_sha256"})
        ):
            raise ScanOcrError("scan_ir_structure_review_hash_invalid")


def _load_fixture(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    value = _load_json(path.expanduser().resolve())
    if not isinstance(value, dict):
        raise ScanOcrError("fake_fixture_must_be_object")
    return value


def _fixture_for_page(fixture: Mapping[str, Any], backend: str, page: int) -> list[dict[str, Any]]:
    backend_value = fixture.get(backend, fixture.get("pages", fixture))
    if isinstance(backend_value, dict):
        value = backend_value.get(str(page), backend_value.get(page, []))
    else:
        value = backend_value
    if value is None:
        return []
    if not isinstance(value, list):
        raise ScanOcrError(f"fake_fixture_page_invalid: backend={backend} page={page}")
    return [dict(row) for row in value if isinstance(row, dict)]


def _safe_worker_env() -> dict[str, str]:
    path = os.environ.get("PATH", "")
    return {
        "PATH": path,
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
        "EFIREBLE_OCR_NETWORK": "disabled",
    }


def _run_worker(
    backend: str,
    requests: list[dict[str, Any]],
    *,
    timeout_seconds: int = 120,
) -> list[dict[str, Any]]:
    worker = Path(__file__).with_name(WORKER_NAME).resolve()
    if not worker.is_file():
        raise ScanOcrError("ocr_worker_missing")
    payload = "".join(
        json.dumps({"schema_version": OCR_PROTOCOL_SCHEMA, **request}, sort_keys=True) + "\n"
        for request in requests
    )
    try:
        completed = subprocess.run(
            [sys.executable, str(worker), "--backend", backend],
            input=payload,
            capture_output=True,
            text=True,
            env=_safe_worker_env(),
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ScanOcrError(f"ocr_backend_timeout: {backend}") from error
    except OSError as error:
        raise ScanOcrError(f"ocr_backend_process_failed: {backend}") from error
    if completed.returncode != 0:
        raise ScanOcrError(f"ocr_backend_process_failed: {backend}")
    responses: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ScanOcrError(f"ocr_backend_protocol_invalid: {backend}") from error
        if not isinstance(value, dict):
            raise ScanOcrError(f"ocr_backend_protocol_invalid: {backend}")
        responses.append(value)
    if len(responses) != len(requests):
        raise ScanOcrError(f"ocr_backend_response_count_mismatch: {backend}")
    for request, response in zip(requests, responses):
        if response.get("schema_version") != OCR_PROTOCOL_SCHEMA or response.get("request_id") != request["request_id"]:
            raise ScanOcrError(f"ocr_backend_protocol_binding_invalid: {backend}")
        if response.get("backend_id") != backend:
            raise ScanOcrError(f"ocr_backend_protocol_backend_mismatch: {backend}")
        if response.get("status") != "ok":
            reason = response.get("error_code") or "backend_unavailable"
            raise ScanOcrError(f"{reason}: backend={backend}")
    return responses


def _coerce_bbox(value: Any) -> list[float] | None:
    if isinstance(value, dict):
        if all(key in value for key in ("x", "y", "width", "height")):
            x, y = _finite(value["x"]), _finite(value["y"])
            width, height = _finite(value["width"]), _finite(value["height"])
            return [_point(x), _point(y), _point(x + width), _point(y + height)]
        keys = ("left", "top", "right", "bottom")
        if all(key in value for key in keys):
            return [_point(value[key]) for key in keys]
    if isinstance(value, (list, tuple)) and len(value) == 4:
        return [_point(item) for item in value]
    return None


def _coerce_polygon(raw: Mapping[str, Any]) -> list[list[float]]:
    polygon = raw.get("polygon")
    if polygon is not None:
        if not isinstance(polygon, (list, tuple)):
            raise ScanOcrError("ocr_polygon_invalid")
        points: list[list[float]] = []
        for point in polygon:
            if isinstance(point, dict):
                point = [point.get("x"), point.get("y")]
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ScanOcrError("ocr_polygon_invalid")
            points.append([_point(point[0]), _point(point[1])])
        if len(points) < 3:
            raise ScanOcrError("ocr_polygon_too_few_points")
        return points
    bbox = _coerce_bbox(raw.get("bbox"))
    if bbox is None:
        raise ScanOcrError("ocr_bbox_missing")
    left, top, right, bottom = bbox
    if right <= left or bottom <= top:
        raise ScanOcrError("ocr_bbox_invalid")
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def _normalize_observations(
    *,
    source_sha256: str,
    physical_page: int,
    backend: str,
    role: str,
    response: Mapping[str, Any],
    render_receipt: Mapping[str, Any],
    transform: Mapping[str, Any],
    model: Mapping[str, Any],
    configuration: Mapping[str, Any],
    image_dimensions: Mapping[str, Any],
    raster_binding: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_rows = response.get("observations")
    if not isinstance(raw_rows, list):
        raise ScanOcrError(f"ocr_observations_invalid: backend={backend} page={physical_page}")
    image_width = _finite(image_dimensions["width"])
    image_height = _finite(image_dimensions["height"])
    normalized_rows: list[dict[str, Any]] = []
    seen_sequences: set[int] = set()
    for sequence, raw_value in enumerate(raw_rows):
        if not isinstance(raw_value, dict):
            raise ScanOcrError("ocr_observation_invalid")
        declared_sequence = raw_value.get("sequence", sequence)
        if (
            isinstance(declared_sequence, bool)
            or not isinstance(declared_sequence, int)
            or declared_sequence < 0
            or declared_sequence in seen_sequences
        ):
            raise ScanOcrError("ocr_observation_sequence_invalid")
        sequence = declared_sequence
        seen_sequences.add(sequence)
        text_value = raw_value.get("text", raw_value.get("transcript", raw_value.get("content")))
        if not isinstance(text_value, str) or not normalize_text(text_value):
            raise ScanOcrError("ocr_transcript_missing")
        polygon_image = _coerce_polygon(raw_value)
        if any(
            point[0] < -0.01
            or point[1] < -0.01
            or point[0] > image_width + 0.01
            or point[1] > image_height + 0.01
            for point in polygon_image
        ):
            raise ScanOcrError("ocr_polygon_outside_render")
        confidence = _finite(raw_value.get("confidence", raw_value.get("score")))
        if not 0.0 <= confidence <= 1.0:
            raise ScanOcrError("ocr_confidence_invalid")
        kind = str(raw_value.get("kind", raw_value.get("label", "text")))
        polygon_pdf = transform_polygon(
            polygon_image,
            transform["matrix_3x3"],
            target_dimensions=transform["pdf_dimensions_points"],
        )
        normalized = normalize_text(text_value)
        raw_record = {
            "sequence": sequence,
            "text": text_value,
            "bbox": _bbox_from_polygon(polygon_image),
            "polygon": polygon_image,
            "confidence": _point(confidence),
            "kind": kind,
        }
        if raster_binding is not None:
            raw_record["raster_region"] = dict(raster_binding)
        content_payload = {
            "source_sha256": source_sha256,
            "physical_page": physical_page,
            "backend": backend,
            "render_sha256": render_receipt["render_sha256"],
            "sequence": sequence,
            "normalized_text": normalized,
            "polygon_pdf": polygon_pdf,
            "confidence": _point(confidence),
            "kind": kind,
        }
        if raster_binding is not None:
            content_payload["raster_region"] = dict(raster_binding)
        normalized_rows.append(
            {
                "schema_version": OCR_OBSERVATION_SCHEMA,
                "id": _stable_id("ocr", content_payload),
                "source_sha256": source_sha256,
                "physical_page": physical_page,
                "canonical_render_sha256": render_receipt["render_sha256"],
                "render": {
                    "renderer_id": render_receipt["renderer"]["id"],
                    "renderer_version": render_receipt["renderer"]["version"],
                    "dpi": render_receipt["renderer"]["dpi"],
                    "format": render_receipt["renderer"]["format"],
                },
                "backend": {
                    "id": backend,
                    "role": role,
                    "engine": str(response.get("engine") or BACKEND_SPECS[backend]["engine"]),
                    "version": str(response.get("engine_version") or "unknown"),
                    "protocol_version": OCR_PROTOCOL_VERSION,
                },
                "model": {
                    "identity_sha256": model["identity_sha256"],
                    "files_sha256": model["files_sha256"],
                    "model_id": model["model_id"],
                },
                "configuration_sha256": sha256_json(configuration),
                "coordinate_transform_id": transform["id"],
                "bbox_image_px": _bbox_from_polygon(polygon_image),
                "polygon_image_px": polygon_image,
                "bbox_pdf_points": _bbox_from_polygon(polygon_pdf),
                "polygon_pdf_points": polygon_pdf,
                "confidence": _point(confidence),
                "kind": kind,
                "raw_observation": raw_record,
                "normalized_transcript": normalized,
                "transcript_span_id": None,
                "candidate_only": True,
                "promotion_status": "candidate-only",
                "canonical_anchor_id": None,
                "canonical_object_id": None,
                "review_status": "unreviewed",
            }
        )

    page_transcript = " ".join(row["normalized_transcript"] for row in normalized_rows)
    raster_identity = (
        {
            "region_id": raster_binding.get("region_id"),
            "crop_sha256": raster_binding.get("crop_sha256"),
        }
        if isinstance(raster_binding, Mapping)
        else None
    )
    page_transcript_id = _stable_id(
        "otx", source_sha256, physical_page, backend, render_receipt["render_sha256"], raster_identity, page_transcript
    )
    transcripts: list[dict[str, Any]] = []
    offset = 0
    for row in normalized_rows:
        text_value = row["normalized_transcript"]
        start, end = offset, offset + len(text_value)
        span_id = _stable_id(
            "ots", page_transcript_id, start, end, sha256_text(text_value)
        )
        row["transcript_span_id"] = span_id
        transcripts.append(
            {
                "schema_version": OCR_TRANSCRIPT_SCHEMA,
                "id": span_id,
                "page_transcript_id": page_transcript_id,
                "source_sha256": source_sha256,
                "physical_page": physical_page,
                "backend_id": backend,
                "span": {"start": start, "end": end, "offset_unit": "unicode-code-point"},
                "raw_text": row["raw_observation"]["text"],
                "normalized_text": text_value,
                "raw_text_sha256": sha256_text(row["raw_observation"]["text"]),
                "normalized_text_sha256": sha256_text(text_value),
                "candidate_only": True,
            }
        )
        offset = end + 1
    return normalized_rows, transcripts


def _scan_bbox_union(bboxes: Sequence[Sequence[Any]]) -> list[float]:
    normalized = [
        [_point(value) for value in bbox]
        for bbox in bboxes
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4
    ]
    if not normalized:
        raise ScanOcrError("scan_anchor_bbox_missing")
    return [
        _point(min(row[0] for row in normalized)),
        _point(min(row[1] for row in normalized)),
        _point(max(row[2] for row in normalized)),
        _point(max(row[3] for row in normalized)),
    ]


def _scan_runtime_binding(
    receipt: Mapping[str, Any],
    *,
    backend_id: str,
) -> dict[str, Any]:
    """Return the immutable runtime boundary carried by one backend receipt.

    The synthetic backend has no external interpreter contract.  It still
    gets an explicit, deterministic test-only contract commitment so a scan
    locator can never silently mean “whatever runtime happens to be present”.
    Real Paddle rows must carry the original contract, worker, backend receipt,
    and qualification commitments produced by the external runtime route.
    """

    worker = receipt.get("worker")
    worker_sha256 = worker.get("worker_sha256") if isinstance(worker, Mapping) else None
    if worker_sha256 is None and isinstance(worker, Mapping):
        worker_sha256 = worker.get("script_sha256")
    if not isinstance(worker_sha256, str) or not _SHA256_RE.fullmatch(worker_sha256):
        raise ScanOcrError("scan_runtime_worker_commitment_missing")
    runtime_contract_sha256 = receipt.get("runtime_contract_sha256")
    runtime_kind = "external-local-runtime"
    if not isinstance(runtime_contract_sha256, str) or not _SHA256_RE.fullmatch(runtime_contract_sha256):
        if backend_id != "fake":
            raise ScanOcrError("scan_runtime_contract_commitment_missing")
        runtime_kind = "synthetic-test-only"
        runtime_contract_sha256 = sha256_json(
            {
                "protocol": "synthetic-scan-runtime-v1",
                "backend_id": backend_id,
                "worker_sha256": worker_sha256,
                "configuration_sha256": receipt.get("configuration_sha256"),
                "model_identity_sha256": (receipt.get("model") or {}).get("identity_sha256")
                if isinstance(receipt.get("model"), Mapping)
                else None,
            }
        )
    qualification = receipt.get("qualification_receipt_sha256")
    if qualification is not None and (
        not isinstance(qualification, str) or not _SHA256_RE.fullmatch(qualification)
    ):
        raise ScanOcrError("scan_runtime_qualification_commitment_invalid")
    if backend_id != "fake" and qualification is None:
        raise ScanOcrError("scan_runtime_qualification_commitment_missing")
    return {
        "runtime_contract_sha256": runtime_contract_sha256,
        "worker_sha256": worker_sha256,
        "backend_receipt_sha256": sha256_json(dict(receipt)),
        "qualification_receipt_sha256": qualification,
        "runtime_binding_kind": runtime_kind,
    }


def _scan_observation_commitment(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Project an observation into a source-free, hash-bound locator record."""

    fields = {
        "id": observation.get("id"),
        "source_sha256": observation.get("source_sha256"),
        "physical_page": observation.get("physical_page"),
        "canonical_render_sha256": observation.get("canonical_render_sha256"),
        "backend": observation.get("backend"),
        "model": observation.get("model"),
        "configuration_sha256": observation.get("configuration_sha256"),
        "coordinate_transform_id": observation.get("coordinate_transform_id"),
        "bbox_image_px": observation.get("bbox_image_px"),
        "polygon_image_px": observation.get("polygon_image_px"),
        "bbox_pdf_points": observation.get("bbox_pdf_points"),
        "polygon_pdf_points": observation.get("polygon_pdf_points"),
        "confidence": observation.get("confidence"),
        "kind": observation.get("kind"),
        "transcript_span_id": observation.get("transcript_span_id"),
        "backend_receipt_id": observation.get("backend_receipt_id"),
    }
    fields["observation_binding_sha256"] = sha256_json(fields)
    fields["observation_sha256"] = sha256_json(dict(observation))
    return fields


def _scan_transcript_commitment(
    row: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Project one transcript row without copying OCR/source text to a release."""

    span = row.get("span") if isinstance(row.get("span"), Mapping) else {}
    fields: dict[str, Any] = {
        "id": row.get("id"),
        "page_transcript_id": row.get("page_transcript_id"),
        "source_sha256": row.get("source_sha256"),
        "physical_page": row.get("physical_page"),
        "backend_id": row.get("backend_id"),
        "span": dict(span),
        "raw_text_sha256": row.get("raw_text_sha256"),
        "normalized_text_sha256": row.get("normalized_text_sha256"),
        "observation_ids": [str(observation.get("id"))],
    }
    fields["row_binding_sha256"] = sha256_json(fields)
    fields["row_sha256"] = sha256_json(dict(row))
    return fields


def _scan_exact_rows_for_span(
    page_text: str,
    page_rows: Sequence[Mapping[str, Any]],
    *,
    start: int,
    end: int,
) -> list[dict[str, Any]]:
    """Map a semantic interval to complete OCR rows; never infer partial rows."""

    selected: list[dict[str, Any]] = []
    for raw in page_rows:
        span = raw.get("span") if isinstance(raw.get("span"), Mapping) else {}
        row_start, row_end = span.get("start"), span.get("end")
        if not isinstance(row_start, int) or not isinstance(row_end, int):
            continue
        overlaps = row_start < end and row_end > start
        contained = start <= row_start and row_end <= end
        if overlaps and not contained:
            raise ScanOcrError("scan_anchor_partial_transcript_row")
        if contained:
            selected.append(dict(raw))
    selected.sort(key=lambda row: (int(row["span"]["start"]), str(row.get("id"))))
    if (
        not selected
        or int(selected[0]["span"]["start"]) != start
        or int(selected[-1]["span"]["end"]) != end
        or any(
            int(right["span"]["start"]) != int(left["span"]["end"]) + 1
            for left, right in zip(selected, selected[1:])
        )
    ):
        raise ScanOcrError("scan_anchor_transcript_row_coverage_missing")
    projected = " ".join(str(row.get("normalized_text", "")) for row in selected)
    if projected != page_text[start:end]:
        raise ScanOcrError("scan_anchor_transcript_projection_mismatch")
    return selected


def build_scan_evidence_locator(
    *,
    source_sha256: str,
    evidence_spans: Sequence[Mapping[str, Any]],
    page_texts: Mapping[int, str],
    transcripts: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    backend_receipts: Sequence[Mapping[str, Any]],
    transforms: Sequence[Mapping[str, Any]],
    render_receipts: Sequence[Mapping[str, Any]],
    primary_backend: str,
    excerpt_sha256: str,
    review_binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Build an exact per-object scan locator from its own evidence spans.

    The returned locator carries only hashes and geometry in addition to the
    semantic interval.  It is therefore safe for a source-free Skill, while
    the private workpack/IR retains the raw rows needed to construct and
    validate the commitments before promotion.
    """

    if not isinstance(excerpt_sha256, str) or not _SHA256_RE.fullmatch(excerpt_sha256):
        raise ScanOcrError("scan_anchor_excerpt_hash_invalid")
    rows_by_page: dict[int, list[dict[str, Any]]] = {}
    for row in transcripts:
        if isinstance(row, Mapping) and row.get("backend_id") == primary_backend:
            page = row.get("physical_page")
            if isinstance(page, int):
                rows_by_page.setdefault(page, []).append(dict(row))
    for rows in rows_by_page.values():
        rows.sort(key=lambda row: (int(row.get("span", {}).get("start", 0)), str(row.get("id"))))
    observations_by_transcript = {
        str(row.get("transcript_span_id")): dict(row)
        for row in observations
        if isinstance(row, Mapping)
        and row.get("backend", {}).get("id") == primary_backend
        and isinstance(row.get("transcript_span_id"), str)
    }
    transforms_by_id = {
        str(row.get("id")): dict(row)
        for row in transforms
        if isinstance(row, Mapping) and isinstance(row.get("id"), str)
    }
    renders_by_page = {
        int(row.get("physical_page")): dict(row)
        for row in render_receipts
        if isinstance(row, Mapping) and isinstance(row.get("physical_page"), int)
    }
    receipts_by_id = {
        str(row.get("id")): dict(row)
        for row in backend_receipts
        if isinstance(row, Mapping) and isinstance(row.get("id"), str)
    }
    normalized_spans: list[dict[str, Any]] = []
    selected_by_page: dict[int, dict[str, dict[str, Any]]] = {}
    span_texts: list[str] = []
    for raw_span in evidence_spans:
        if not isinstance(raw_span, Mapping):
            raise ScanOcrError("scan_anchor_evidence_span_invalid")
        page = raw_span.get("page")
        start, end = raw_span.get("start"), raw_span.get("end")
        page_text = page_texts.get(page) if isinstance(page, int) else None
        if (
            not isinstance(page, int)
            or not isinstance(page_text, str)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or not 0 <= start < end <= len(page_text)
            or raw_span.get("page_text_sha256") != sha256_text(page_text)
            or raw_span.get("span_sha256") != sha256_text(page_text[start:end])
        ):
            raise ScanOcrError("scan_anchor_evidence_span_hash_invalid")
        rows = _scan_exact_rows_for_span(page_text, rows_by_page.get(page, []), start=start, end=end)
        span_texts.append(page_text[start:end])
        row_ids = [str(row.get("id")) for row in rows]
        observation_rows: list[dict[str, Any]] = []
        for row in rows:
            observation = observations_by_transcript.get(str(row.get("id")))
            if not isinstance(observation, Mapping):
                raise ScanOcrError("scan_anchor_observation_binding_missing")
            observation_rows.append(dict(observation))
            selected_by_page.setdefault(page, {})[str(row.get("id"))] = dict(row)
        observation_ids = [str(row.get("id")) for row in observation_rows]
        evidence_bbox = _scan_bbox_union([row.get("bbox_pdf_points") for row in observation_rows])
        span_id = f"ses-{sha256_json([source_sha256, page, start, end, raw_span.get('span_sha256')])[:20]}"
        normalized_spans.append(
            {
                "id": span_id,
                "page": page,
                "start": start,
                "end": end,
                "page_text_sha256": raw_span["page_text_sha256"],
                "span_sha256": raw_span["span_sha256"],
                "transcript_span_ids": row_ids,
                "observation_ids": observation_ids,
                "bbox_pdf_points": evidence_bbox,
                "bbox_sha256": sha256_json(evidence_bbox),
            }
        )

    if not normalized_spans:
        raise ScanOcrError("scan_anchor_evidence_spans_missing")
    page_locators: list[dict[str, Any]] = []
    for page in sorted(selected_by_page):
        selected_rows = list(selected_by_page[page].values())
        selected_rows.sort(key=lambda row: (int(row["span"]["start"]), str(row.get("id"))))
        observation_rows = []
        for row in selected_rows:
            observation = observations_by_transcript.get(str(row.get("id")))
            if not isinstance(observation, Mapping):
                raise ScanOcrError("scan_anchor_observation_binding_missing")
            observation_rows.append(dict(observation))
        receipt_ids = {str(row.get("backend_receipt_id")) for row in observation_rows}
        if len(receipt_ids) != 1 or next(iter(receipt_ids)) not in receipts_by_id:
            raise ScanOcrError("scan_anchor_backend_receipt_binding_invalid")
        receipt_id = next(iter(receipt_ids))
        receipt = receipts_by_id[receipt_id]
        runtime = _scan_runtime_binding(receipt, backend_id=primary_backend)
        transform_ids = {str(row.get("coordinate_transform_id")) for row in observation_rows}
        if len(transform_ids) != 1 or next(iter(transform_ids)) not in transforms_by_id:
            raise ScanOcrError("scan_anchor_transform_binding_invalid")
        transform_id = next(iter(transform_ids))
        transform = transforms_by_id[transform_id]
        render = renders_by_page.get(page)
        if not isinstance(render, Mapping):
            raise ScanOcrError("scan_anchor_render_binding_missing")
        if any(
            row.get("source_sha256") != source_sha256
            or row.get("physical_page") != page
            or row.get("canonical_render_sha256") != render.get("render_sha256")
            or row.get("coordinate_transform_id") != transform_id
            for row in observation_rows
        ):
            raise ScanOcrError("scan_anchor_observation_render_binding_invalid")
        bboxes = [row.get("bbox_pdf_points") for row in observation_rows]
        bbox = _scan_bbox_union(bboxes)
        page_span_ids = [
            span["id"] for span in normalized_spans if span["page"] == page
        ]
        row_commitments = [
            _scan_transcript_commitment(
                row,
                observations_by_transcript[str(row.get("id"))],
            )
            for row in selected_rows
        ]
        observation_commitments = [
            _scan_observation_commitment(row) for row in observation_rows
        ]
        row_commitments.sort(key=lambda row: (int(row["span"]["start"]), str(row["id"])))
        observation_commitments.sort(key=lambda row: str(row["id"]))
        transcript_text = " ".join(str(row.get("normalized_text")) for row in selected_rows)
        backend_binding = {
            "id": receipt_id,
            "source_sha256": source_sha256,
            "physical_page": page,
            "backend_id": primary_backend,
            "model_identity_sha256": (receipt.get("model") or {}).get("identity_sha256")
            if isinstance(receipt.get("model"), Mapping)
            else None,
            "configuration_sha256": receipt.get("configuration_sha256"),
            "render_sha256": render.get("render_sha256"),
            "coordinate_transform_id": transform_id,
            "runtime_contract_sha256": runtime["runtime_contract_sha256"],
            "worker_sha256": runtime["worker_sha256"],
            "qualification_receipt_sha256": runtime["qualification_receipt_sha256"],
            "backend_receipt_sha256": runtime["backend_receipt_sha256"],
        }
        page_locator: dict[str, Any] = {
            "source_sha256": source_sha256,
            "physical_page": page,
            "render_sha256": render.get("render_sha256"),
            "render_dpi": render.get("renderer", {}).get("dpi"),
            "render_receipt": dict(render),
            "render_receipt_sha256": sha256_json(dict(render)),
            "bbox_pdf_points": bbox,
            "bbox_sha256": sha256_json(bbox),
            "coordinate_transform_id": transform_id,
            "coordinate_transform": dict(transform),
            "coordinate_transform_sha256": sha256_json(dict(transform)),
            "coordinate_transform_scope": "page-render",
            "ocr_observation_ids": [str(row["id"]) for row in observation_commitments],
            "observation_commitments": observation_commitments,
            "transcript_span_ids": [str(row["id"]) for row in row_commitments],
            "transcript_row_commitments": row_commitments,
            "transcript_sha256": sha256_text(transcript_text),
            "transcript_commitment_sha256": sha256_json(
                {
                    "transcript_sha256": sha256_text(transcript_text),
                    "transcript_row_commitments": row_commitments,
                }
            ),
            "evidence_span_ids": page_span_ids,
            "backend_id": primary_backend,
            "model_identity_sha256": (receipt.get("model") or {}).get("identity_sha256")
            if isinstance(receipt.get("model"), Mapping)
            else None,
            "configuration_sha256": receipt.get("configuration_sha256"),
            "backend_receipt_id": receipt_id,
            "backend_receipt_binding_sha256": sha256_json(backend_binding),
            **runtime,
        }
        page_locators.append(page_locator)
    normalized_spans.sort(key=lambda row: (int(row["page"]), int(row["start"]), int(row["end"]), str(row["id"])))
    ordered_excerpt = "\f".join(
        page_texts[int(span["page"])][int(span["start"]):int(span["end"])]
        for span in normalized_spans
    )
    locator: dict[str, Any] = {
        "locator_protocol": SCAN_ANCHOR_PROTOCOL,
        "source_sha256": source_sha256,
        "pages": sorted(selected_by_page),
        "page_locators": page_locators,
        "evidence_spans": normalized_spans,
        "evidence_spans_sha256": sha256_json(normalized_spans),
        "excerpt_sha256": excerpt_sha256,
        "scan_excerpt_sha256": sha256_text(ordered_excerpt),
    }
    for key in (
        "proposer_instance",
        "reviewer_instance",
        "review_session_id",
        "review_plan_sha256",
        "review_input_sha256",
        "candidate_sha256",
        "external_fragment_sha256",
        "review_attestation_sha256",
        "review_independence_basis",
    ):
        if key in review_binding:
            locator[key] = review_binding[key]
    locator["evidence_commitment_sha256"] = sha256_json(locator)
    return locator


def _area(bbox: list[float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _overlap_ratio(left: list[float], right: list[float]) -> float:
    intersection = [
        max(left[0], right[0]),
        max(left[1], right[1]),
        min(left[2], right[2]),
        min(left[3], right[3]),
    ]
    inter_area = _area(intersection)
    denominator = min(_area(left), _area(right))
    return inter_area / denominator if denominator else 0.0


def build_conflict_ledger(
    observations: list[dict[str, Any]],
    transcripts: list[dict[str, Any]],
    *,
    native_texts: Mapping[int, str],
) -> list[dict[str, Any]]:
    transcript_by_id = {row["id"]: row for row in transcripts}
    conflicts: list[dict[str, Any]] = []
    by_page: dict[int, list[dict[str, Any]]] = {}
    for row in observations:
        by_page.setdefault(row["physical_page"], []).append(row)
        native = normalize_text(native_texts.get(row["physical_page"], ""))
        normalized = row["normalized_transcript"]
        if native and normalized and normalized in native:
            payload = {
                "kind": "native-ocr-duplicate",
                "page": row["physical_page"],
                "ocr_span": row["transcript_span_id"],
                "text": sha256_text(normalized),
            }
            conflicts.append(
                {
                    "schema_version": OCR_CONFLICT_SCHEMA,
                    "id": _stable_id("ocf", payload),
                    "source_sha256": row["source_sha256"],
                    "physical_page": row["physical_page"],
                    "kind": "native-ocr-duplicate",
                    "left_transcript_span_id": row["transcript_span_id"],
                    "right_transcript_span_id": None,
                    "left_text_sha256": sha256_text(normalized),
                    "right_text_sha256": sha256_text(native),
                    "overlap_ratio": None,
                    "status": "dedupe-required",
                    "resolution": "native-text-authoritative",
                    "canonical_promotion_allowed": False,
                }
            )
    for page, rows in by_page.items():
        for index, left in enumerate(rows):
            for right in rows[index + 1 :]:
                if left["backend"]["id"] == right["backend"]["id"]:
                    same_backend = True
                else:
                    same_backend = False
                overlap = _overlap_ratio(left["bbox_pdf_points"], right["bbox_pdf_points"])
                left_text = left["normalized_transcript"]
                right_text = right["normalized_transcript"]
                if overlap < 0.25:
                    continue
                if left_text == right_text:
                    kind = "duplicate-ocr-text"
                    status = "dedupe-required"
                    resolution = "retain-one-candidate-after-visual-review"
                else:
                    kind = "engine-disagreement" if not same_backend else "ocr-overlap-conflict"
                    status = "unresolved"
                    resolution = "visual-review-required"
                payload = {
                    "kind": kind,
                    "page": page,
                    "left": left["id"],
                    "right": right["id"],
                    "overlap": _point(overlap),
                }
                conflicts.append(
                    {
                        "schema_version": OCR_CONFLICT_SCHEMA,
                        "id": _stable_id("ocf", payload),
                        "source_sha256": left["source_sha256"],
                        "physical_page": page,
                        "kind": kind,
                        "left_transcript_span_id": left["transcript_span_id"],
                        "right_transcript_span_id": right["transcript_span_id"],
                        "left_text_sha256": sha256_text(left_text),
                        "right_text_sha256": sha256_text(right_text),
                        "overlap_ratio": _point(overlap),
                        "status": status,
                        "resolution": resolution,
                        "canonical_promotion_allowed": False,
                    }
                )
    return sorted(conflicts, key=lambda row: row["id"])


def build_quality_routing(
    *,
    preflight: Mapping[str, Any],
    observations: list[dict[str, Any]],
    transcripts: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    confidence_threshold: float,
) -> dict[str, Any]:
    if not 0.0 <= confidence_threshold <= 1.0:
        raise ScanOcrError("confidence_threshold_invalid")
    routed = set(preflight.get("page_routing", {}).get("ocr_required_pages", []))
    manual = set(preflight.get("page_routing", {}).get("manual_review_pages", []))
    scope = preflight.get("scope", {})
    start, end = scope.get("start_page"), scope.get("end_page")
    by_page: dict[int, list[dict[str, Any]]] = {}
    for row in observations:
        by_page.setdefault(row["physical_page"], []).append(row)
    conflicts_by_page: dict[int, list[dict[str, Any]]] = {}
    for row in conflicts:
        conflicts_by_page.setdefault(row["physical_page"], []).append(row)
    pages: list[dict[str, Any]] = []
    for page in range(start, end + 1):
        rows = by_page.get(page, [])
        reasons: list[str] = []
        if page in manual:
            route = "manual-review-required"
            reasons.append("blank-or-unusable-native-page")
        elif page in routed:
            route = "ocr-candidate"
            if not rows:
                reasons.append("ocr_no_observations")
            if rows and min(row["confidence"] for row in rows) < confidence_threshold:
                reasons.append("low_confidence")
            if any(_FORMULA_HINT.search(row["normalized_transcript"]) or row["kind"].casefold() == "formula" for row in rows):
                reasons.extend(["formula_visual_review_required", "formula_ast_review_required"])
            if any(_TABLE_HINT.search(row["normalized_transcript"]) or row["kind"].casefold() == "table" for row in rows):
                reasons.append("table_visual_review_required")
            if any(_UNIT_HINT.search(row["normalized_transcript"]) for row in rows):
                reasons.append("unit_visual_review_required")
            if any(row["kind"].casefold() in {"formula", "equation", "table"} for row in rows):
                reasons.append("structured_candidate_requires_visual_review")
            if conflicts_by_page.get(page):
                reasons.append("ocr_conflict_or_duplicate")
        else:
            route = "native-text"
            reasons.append("native-text-first-preserved")
        pages.append(
            {
                "physical_page": page,
                "route": route,
                "reasons": sorted(set(reasons)),
                "observation_ids": sorted(row["id"] for row in rows),
                "transcript_span_ids": sorted(row["transcript_span_id"] for row in rows),
                "confidence_threshold": confidence_threshold,
                "candidate_only": page in routed,
                "canonical_anchor_allowed": False if page in routed else True,
                "executable": False,
                "visual_review_required": page in routed or bool(reasons and route == "manual-review-required"),
                "formula_executable": False,
            }
        )
    candidate_pages = [row for row in pages if row["route"] == "ocr-candidate"]
    no_observation = any("ocr_no_observations" in row["reasons"] for row in candidate_pages)
    return {
        "schema_version": OCR_QUALITY_SCHEMA,
        "native_text_first": True,
        "ocr_only_pages_needing_route": sorted(routed),
        "manual_review_pages": sorted(manual),
        "pages": pages,
        "promotion_policy": {
            "ocr_output": "candidate-only",
            "canonical_anchor_requires": [
                "independent-semantic-review",
                "independent-visual-review",
                "source-binding",
                "deterministic-promotion",
            ],
            "formula_requires": [
                "full-visual-review",
                "formula-ast-review",
                "unit-dimension-check",
                "independent-numeric-tests",
                "execution-tier-gate",
            ],
            "formula_executable_default": False,
            "silent_backend_fallback": False,
        },
        "eligible_for_scan_candidate_ir": bool(candidate_pages) and not no_observation and not manual,
        "promotable": False,
        "executable": False,
        "transcript_count": len(transcripts),
        "conflict_count": len(conflicts),
    }


def scan_input_fingerprint(
    *,
    source_sha256: str,
    render_receipts: list[dict[str, Any]],
    backend_receipts: list[dict[str, Any]],
    coordinate_transforms: list[dict[str, Any]],
    raster_regions: list[dict[str, Any]] | None = None,
    runtime_contracts: list[dict[str, Any]] | None = None,
    table_grids: list[dict[str, Any]] | None = None,
    table_grid_bindings: list[dict[str, Any]] | None = None,
    visual_candidates: list[dict[str, Any]] | None = None,
    visual_gaps: list[dict[str, Any]] | None = None,
    visual_conflicts: list[dict[str, Any]] | None = None,
    result_adapters: list[dict[str, Any]] | None = None,
    spread_split_contracts: list[dict[str, Any]] | None = None,
    reviewed_structure: Mapping[str, Any] | None = None,
) -> str:
    payload = {
        "source_sha256": source_sha256,
        "render_receipts": [
            {
                "physical_page": row.get("physical_page"),
                "render_sha256": row.get("render_sha256"),
                "renderer": row.get("renderer"),
            }
            for row in render_receipts
        ],
        "backend_receipts": [
            {
                "backend_id": row.get("backend_id"),
                "physical_page": row.get("physical_page"),
                "engine": row.get("engine"),
                "model_identity_sha256": row.get("model", {}).get("identity_sha256"),
                "configuration_sha256": row.get("configuration_sha256"),
                "render_sha256": row.get("render_sha256"),
            }
            for row in sorted(backend_receipts, key=lambda item: str(item.get("id", "")))
        ],
        "coordinate_transforms": [
            {
                "id": row.get("id"),
                "matrix_3x3": row.get("matrix_3x3"),
                "preprocessing": row.get("preprocessing"),
            }
            for row in sorted(coordinate_transforms, key=lambda item: str(item.get("id", "")))
        ],
        "raster_regions": [
            {
                "region_id": row.get("region_id"),
                "physical_page": row.get("physical_page"),
                "bbox_pdf_points": row.get("bbox_pdf_points"),
                "render_sha256": row.get("render_sha256"),
                "crop_sha256": row.get("crop_sha256"),
                "coordinate_transform_id": row.get("coordinate_transform_id"),
            }
            for row in (raster_regions or [])
        ],
        "runtime_contracts": [
            {
                "backend_id": row.get("backend_id"),
                "contract_sha256": row.get("contract_sha256"),
                "worker_sha256": row.get("worker_sha256"),
                "model_identity_sha256": (row.get("model") or {}).get("identity_sha256"),
                "runtime_inventory_sha256": (row.get("runtime_inventory") or {}).get("inventory_sha256"),
            }
            for row in (runtime_contracts or [])
        ],
        "table_grids": [
            {
                "table_grid_id": row.get("table_grid_id"),
                "grid_sha256": row.get("grid_sha256"),
                "table_visual_object_id": row.get("table_visual_object_id"),
            }
            for row in (table_grids or [])
        ],
        "table_grid_bindings": [
            {
                "table_grid_id": row.get("table_grid_id"),
                "backend_receipt_id": row.get("backend_receipt_id"),
                "runtime_contract_sha256": row.get("runtime_contract_sha256"),
                "crop_sha256": row.get("crop_sha256"),
            }
            for row in (table_grid_bindings or [])
        ],
        "visual_candidates": [
            {
                "visual_object_id": row.get("visual_object_id"),
                "physical_page": row.get("physical_page"),
                "bbox": row.get("bbox"),
                "content_sha256": row.get("content_sha256"),
            }
            for row in (visual_candidates or [])
        ],
        "visual_gaps": [
            {"gap_id": row.get("gap_id"), "gap_sha256": row.get("gap_sha256")}
            for row in (visual_gaps or [])
        ],
        "visual_conflicts": [
            {"conflict_id": row.get("conflict_id"), "conflict_sha256": row.get("conflict_sha256")}
            for row in (visual_conflicts or [])
        ],
        "result_adapters": [
            {
                "id": row.get("id"),
                "raw_result_sha256": row.get("raw_result_sha256"),
                "normalized_result_sha256": row.get("normalized_result_sha256"),
                "runtime_contract_sha256": row.get("runtime_contract_sha256"),
            }
            for row in (result_adapters or [])
        ],
    }
    # Keep the legacy fingerprint payload byte-compatible when no new
    # split-bound contract is present.  The 7D.6B R4 receipt is immutable and
    # must remain verifiable by the current source verifier.
    if spread_split_contracts:
        payload["spread_split_contracts"] = [
            {
                "contract_sha256": row.get("contract_sha256"),
                "split_core_sha256": row.get("split_core_sha256"),
                "source_sha256": row.get("source_sha256"),
                "physical_page": row.get("physical_page"),
                "rotation": row.get("rotation"),
                "rotated_render": row.get("rotated_render"),
                "split": row.get("split"),
                "runtime_commitments": row.get("runtime_commitments"),
                "qualification_commitments": row.get("qualification_commitments"),
                "regions": [
                    {
                        "region_id": item.get("region_id"),
                        "region_order": item.get("region_order"),
                        "reading_order": item.get("reading_order"),
                        "rotated_region_bbox_px": item.get("rotated_region_bbox_px"),
                        "pre_rotation_render_pixel_bbox": item.get("pre_rotation_render_pixel_bbox"),
                        "bbox_pdf_points": item.get("bbox_pdf_points"),
                        "render_pixel_bbox": item.get("render_pixel_bbox"),
                        "render_sha256": item.get("render_sha256"),
                        "crop_sha256": item.get("crop_sha256"),
                        "crop_dimensions_px": item.get("crop_dimensions_px"),
                        "pre_rotation_crop_sha256": item.get("pre_rotation_crop_sha256"),
                        "coordinate_transform_sha256": item.get("coordinate_transform_sha256"),
                    }
                    for item in row.get("regions", [])
                    if isinstance(item, Mapping)
                ],
            }
            for row in spread_split_contracts
            if isinstance(row, Mapping)
        ]
    if reviewed_structure is not None:
        payload["reviewed_structure"] = reviewed_structure
    return sha256_json(payload)


def _backend_role(backend: str) -> str:
    return str(BACKEND_SPECS[backend]["role"])


def _paths_overlap(first: Path, second: Path) -> bool:
    left = first.expanduser().resolve()
    right = second.expanduser().resolve()
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _page_geometry_receipts(source_path: Path, pages: Sequence[int]) -> dict[int, dict[str, Any]]:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(source_path))
    except Exception as error:
        raise ScanOcrError("raster_page_geometry_unavailable") from error
    result: dict[int, dict[str, Any]] = {}
    for page_number in sorted(set(int(value) for value in pages)):
        if page_number < 1 or page_number > len(reader.pages):
            raise ScanOcrError("raster_region_page_out_of_range")
        page = reader.pages[page_number - 1]
        media = page.mediabox
        crop = getattr(page, "cropbox", media)
        rotation = int(page.get("/Rotate", 0) or 0) % 360
        result[page_number] = {
            "media_box": [float(media.left), float(media.bottom), float(media.right), float(media.top)],
            "crop_box": [float(crop.left), float(crop.bottom), float(crop.right), float(crop.top)],
            "crop_box_top_left": [float(crop.left) - float(media.left), float(media.top) - float(crop.top), float(crop.right) - float(media.left), float(media.top) - float(crop.bottom)],
            "rotation": rotation,
            "width": float(media.width),
            "height": float(media.height),
            "coordinate_origin": "mediabox-top-left",
            "physical_page": page_number,
        }
    return result


def _full_page_ocr_regions(
    *,
    source_sha256: str,
    pages: Sequence[int],
    page_geometries: Mapping[int, Mapping[str, Any]],
    render_receipts: Mapping[int, Mapping[str, Any]],
    content_rotation_clockwise: int = 0,
) -> list[dict[str, Any]]:
    """Create explicit page-image regions only for native-first OCR pages."""

    regions: list[dict[str, Any]] = []
    for region_order, page in enumerate(pages):
        geometry = page_geometries.get(int(page))
        render = render_receipts.get(int(page))
        if not isinstance(geometry, Mapping) or not isinstance(render, Mapping):
            raise ScanOcrError(f"page_image_region_receipt_missing:{page}")
        bbox = geometry.get("crop_box_top_left")
        render_sha256 = render.get("render_sha256")
        if (
            not isinstance(bbox, (list, tuple))
            or len(bbox) != 4
            or not isinstance(render_sha256, str)
            or not _SHA256_RE.fullmatch(render_sha256)
        ):
            raise ScanOcrError(f"page_image_region_receipt_invalid:{page}")
        rotation = int(content_rotation_clockwise) % 360
        if rotation not in {0, 90, 180, 270}:
            raise ScanOcrError("crop_content_rotation_invalid")
        region_id = _stable_id("rgn", source_sha256, int(page), "ocr-required-page-image", rotation)
        regions.append(
            {
                "region_id": region_id,
                "region_order": region_order,
                "physical_page": int(page),
                "bbox_pdf_points": [float(value) for value in bbox],
                "coordinate_space": COORDINATE_SPACE,
                "source_anchor": {
                    "anchor_id": _stable_id("sca", source_sha256, int(page), "ocr-required-page-image", rotation),
                    "anchor_kind": "ocr-required-page-image",
                    "source_sha256": source_sha256,
                },
                "render_sha256": render_sha256,
                "content_rotation_clockwise": rotation,
            }
        )
    return regions


def _raster_runtime_spec(
    backend: str,
    configuration: Mapping[str, Any],
    model_root: Path | None,
) -> dict[str, Any]:
    supplied = configuration.get("external_runtime")
    if supplied is None and isinstance(configuration.get("user_config"), Mapping):
        supplied = configuration["user_config"].get("external_runtime")
    if isinstance(supplied, Mapping) and isinstance(supplied.get(backend), Mapping):
        supplied = supplied[backend]
    if not isinstance(supplied, Mapping):
        raise ScanOcrError("external_runtime_contract_required")
    spec = dict(supplied)
    if spec.get("backend_id") not in {None, backend}:
        raise ScanOcrError("external_runtime_backend_mismatch")
    spec["backend_id"] = backend
    if model_root is not None:
        if spec.get("model_root") not in {None, str(model_root), str(model_root.expanduser().resolve())}:
            raise ScanOcrError("external_runtime_model_root_mismatch")
        spec["model_root"] = str(model_root.expanduser().resolve())
    spec.setdefault("worker_path", str(Path(__file__).with_name(WORKER_NAME).resolve()))
    return spec


def _external_model_receipt(contract: Mapping[str, Any], backend: str) -> dict[str, Any]:
    model = contract.get("model")
    if not isinstance(model, Mapping):
        raise ScanOcrError("external_runtime_model_manifest_invalid")
    return {
        "model_id": f"external-{backend}",
        "source": "external-local-preinstalled-runtime",
        "identity_sha256": model.get("identity_sha256"),
        "files_sha256": model.get("manifest_sha256"),
        "manifest_sha256": model.get("manifest_sha256"),
        "file_count": model.get("file_count"),
        "path": contract.get("model_root"),
        "runtime_contract_sha256": contract.get("contract_sha256"),
    }


def _map_table_grid_to_page(grid: Mapping[str, Any], transform: Mapping[str, Any]) -> dict[str, Any]:
    """Map PP-Structure crop-local cell geometry into PDF page points."""

    mapped = json.loads(json.dumps(dict(grid), ensure_ascii=False, sort_keys=True))

    def map_box(value: Any) -> list[float] | None:
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            return None
        polygon = [[float(value[0]), float(value[1])], [float(value[2]), float(value[1])], [float(value[2]), float(value[3])], [float(value[0]), float(value[3])]]
        return _bbox_from_polygon(transform_polygon(polygon, transform["matrix_3x3"], target_dimensions=transform["pdf_dimensions_points"]))

    evidence = mapped.get("topology_evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, dict) and item.get("bbox") is not None:
                item["bbox"] = map_box(item.get("bbox"))
    for cell in mapped.get("cells", []) if isinstance(mapped.get("cells"), list) else []:
        if not isinstance(cell, dict):
            continue
        if cell.get("bbox") is not None:
            cell["bbox"] = map_box(cell.get("bbox"))
        anchor = cell.get("source_anchor")
        if isinstance(anchor, dict) and anchor.get("bbox") is not None:
            anchor["bbox"] = cell.get("bbox")
    mapped["grid_sha256"] = sha256_json({key: value for key, value in mapped.items() if key not in {"grid_sha256", "status"}})
    return mapped


def _map_adapter_row_to_page(row: Mapping[str, Any], transform: Mapping[str, Any], binding: Mapping[str, Any]) -> dict[str, Any]:
    """Map a crop-local adapter candidate/gap/conflict into page space."""

    mapped = json.loads(json.dumps(dict(row), ensure_ascii=False, sort_keys=True))
    if mapped.get("visual_object_id"):
        mapped["adapter_local_candidate_id"] = mapped.pop("visual_object_id")
    crop_box = mapped.get("bbox")
    if isinstance(crop_box, (list, tuple)) and len(crop_box) == 4:
        polygon = [[float(crop_box[0]), float(crop_box[1])], [float(crop_box[2]), float(crop_box[1])], [float(crop_box[2]), float(crop_box[3])], [float(crop_box[0]), float(crop_box[3])]]
        page_box = _bbox_from_polygon(transform_polygon(polygon, transform["matrix_3x3"], target_dimensions=transform["pdf_dimensions_points"]))
        mapped["bbox_image_px"] = list(crop_box)
        mapped["bbox"] = page_box
        mapped["bbox_pdf_points"] = page_box
    mapped["source_sha256"] = binding.get("source_anchor", {}).get("source_sha256") or mapped.get("source_sha256")
    mapped["physical_page"] = int(binding["physical_page"])
    mapped["render_sha256"] = binding["render_sha256"]
    mapped["crop_sha256"] = binding["crop_sha256"]
    mapped["region_id"] = binding["region_id"]
    mapped["coordinate_transform_id"] = transform["id"]
    return mapped


def _run_raster_region_backend(
    *,
    source_path: Path,
    source_sha256: str,
    regions: Sequence[Mapping[str, Any]],
    backend: str,
    model_root: Path | None,
    configuration: Mapping[str, Any],
    pdftoppm: Path | str,
    render_dpi: int,
    page_geometries: Mapping[int, Mapping[str, Any]],
    expected_render_receipts: Mapping[int, Mapping[str, Any]],
    workspace_root: Path,
    split_contract: Mapping[str, Any] | None = None,
    checkpoint: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Render all selected crops, then run one bounded external worker session."""

    if not regions:
        return {
            "ocr_observations": [],
            "ocr_transcripts": [],
            "ocr_backend_receipts": [],
            "coordinate_transforms": [],
            "ocr_worker_responses": [],
            "raster_regions": [],
            "table_grids": [],
            "table_grid_bindings": [],
            "visual_candidates": [],
            "visual_gaps": [],
            "visual_conflicts": [],
            "result_adapters": [],
            "runtime_contract": None,
            "render_records": [],
            "batch_receipt": _visual_batch_receipt(),
        }
    if backend not in {PADDLE_OCR_BACKEND, PADDLE_STRUCTURE_BACKEND, PADDLE_STRUCTURE_V3_BACKEND}:
        raise ScanOcrError("raster_region_backend_requires_paddle_runtime")
    if _paths_overlap(source_path, workspace_root):
        raise ScanOcrError("raster_region_input_workspace_overlap")
    workspace_root = workspace_root.expanduser().resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    worker_path = Path(__file__).with_name(WORKER_NAME).resolve()
    try:
        spec = _raster_runtime_spec(backend, configuration, model_root)
        engine_configuration = _runtime_engine_configuration(configuration)
        contract = freeze_external_runtime_contract(
            spec,
            backend_id=backend,
            configuration=engine_configuration,
            worker_path=worker_path,
            probe=True,
        )
    except PaddleRuntimeContractError as error:
        raise ScanOcrError(str(error)) from error
    if contract.get("available") is not True:
        if backend in {PADDLE_STRUCTURE_BACKEND, PADDLE_STRUCTURE_V3_BACKEND}:
            raise ScanOcrError("ppstructure_v3_not_run_missing_local_models")
        raise ScanOcrError("paddle_runtime_model_not_available")
    if split_contract is not None:
        try:
            validate_spread_split_contract(split_contract, require_bound=True)
        except ScanOcrError:
            raise
        expected_runtime_commitments = {
            "runtime_contract_sha256": contract.get("contract_sha256"),
            "runtime_inventory_sha256": (contract.get("runtime_inventory") or {}).get("inventory_sha256"),
            "model_identity_sha256": (contract.get("model_status") or {}).get("stable_identity_sha256"),
            "configuration_sha256": sha256_json(engine_configuration),
            "worker_sha256": contract.get("worker_sha256"),
        }
        if split_contract.get("runtime_commitments") != expected_runtime_commitments:
            raise ScanOcrError("spread_split_runtime_commitment_mismatch")
        if split_contract.get("physical_page") not in {int(row.get("physical_page", 0)) for row in regions}:
            raise ScanOcrError("spread_split_page_region_mismatch")
        expected_qualification = _qualification_receipt_sha256(configuration, backend)
        if split_contract.get("qualification_commitments", {}).get("receipt_sha256") != expected_qualification:
            raise ScanOcrError("spread_split_qualification_commitment_mismatch")
    model = _external_model_receipt(contract, backend)
    pages = sorted({int(row.get("physical_page", 0)) for row in regions})
    if not pages or any(page not in page_geometries for page in pages):
        raise ScanOcrError("raster_region_page_geometry_missing")
    observations: list[dict[str, Any]] = []
    transcripts: list[dict[str, Any]] = []
    backend_receipts: list[dict[str, Any]] = []
    transforms: list[dict[str, Any]] = []
    worker_responses: list[dict[str, Any]] = []
    raster_bindings: list[dict[str, Any]] = []
    table_grids: list[dict[str, Any]] = []
    table_grid_bindings: list[dict[str, Any]] = []
    visual_candidates: list[dict[str, Any]] = []
    visual_gaps: list[dict[str, Any]] = []
    visual_conflicts: list[dict[str, Any]] = []
    result_adapters: list[dict[str, Any]] = []
    contexts: list[dict[str, Any]] = []
    with _render_pages(
        source_path,
        pages,
        pdftoppm,
        dpi=render_dpi,
        pdf_dimensions={page: page_geometries[page] for page in pages},
        workspace_root=workspace_root,
    ) as (render_records, render_paths):
        render_by_page = {int(row["physical_page"]): row for row in render_records}
        for page in pages:
            expected = expected_render_receipts.get(page)
            actual = render_by_page.get(page)
            if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
                raise ScanOcrError(f"raster_region_render_receipt_missing:{page}")
            if actual.get("render_sha256") != expected.get("render_sha256"):
                raise ScanOcrError(f"raster_region_render_sha256_mismatch:{page}")
        for index, region_value in enumerate(regions):
            region = dict(region_value)
            page = int(region.get("physical_page", 0))
            render_record = render_by_page.get(page)
            if render_record is None:
                raise ScanOcrError(f"raster_region_render_receipt_missing:{index}")
            image_data = render_paths[page].read_bytes()
            dimensions = _png_dimensions(image_data)
            if dimensions is None:
                raise ScanOcrError(f"raster_region_render_png_invalid:{index}")
            try:
                mapping = build_crop_mapping(
                    bbox_pdf_points=region.get("bbox_pdf_points"),
                    page_geometry=page_geometries[page],
                    render_dimensions_px=dimensions,
                    render_dpi=render_dpi,
                )
                expected_crop = region.get("crop_sha256")
                if not isinstance(expected_crop, str) or len(expected_crop) != 64:
                    expected_crop = None
                crop = crop_render_png(
                    image_data,
                    mapping,
                    expected_crop_sha256=expected_crop if int(region.get("content_rotation_clockwise", 0)) % 360 == 0 else None,
                )
                transform = build_crop_coordinate_transform(
                    source_sha256=source_sha256,
                    physical_page=page,
                    render_sha256=str(render_record["render_sha256"]),
                    render_dpi=render_dpi,
                    mapping=mapping,
                )
                rotation = int(region.get("content_rotation_clockwise", 0)) % 360
                configured_rotation = int(engine_configuration.get("preprocessing", {}).get("content_rotation_clockwise", 0)) % 360
                if rotation != configured_rotation:
                    raise PaddleRuntimeContractError("crop_content_rotation_config_mismatch")
                pre_rotation_crop_sha256 = crop["sha256"]
                if rotation:
                    rotated = rotate_png_for_ocr(crop["bytes"], rotation)
                    transform = _rotate_crop_transform(
                        transform,
                        degrees_clockwise=rotation,
                        original_dimensions=crop["dimensions_px"],
                        rotated_dimensions=rotated["dimensions_px"],
                    )
                    crop = {
                        **crop,
                        "bytes": rotated["bytes"],
                        "sha256": rotated["sha256"],
                        "dimensions_px": rotated["dimensions_px"],
                    }
                    if expected_crop is not None and crop["sha256"] != expected_crop:
                        raise PaddleRuntimeContractError("raster_region_crop_sha256_mismatch")
            except PaddleRuntimeContractError as error:
                raise ScanOcrError(f"raster_region_contract_invalid:{index}:{error}") from error
            transform["preprocessing"]["crop_sha256"] = crop["sha256"]
            transform["preprocessing"]["pre_rotation_crop_sha256"] = pre_rotation_crop_sha256
            transform["preprocessing"]["source_anchor"] = dict(region.get("source_anchor") or {})
            transforms.append(transform)
            binding = {
                "region_id": str(region.get("region_id") or _stable_id("rgn", source_sha256, page, region.get("bbox_pdf_points"), crop["sha256"])),
                "region_order": int(region.get("region_order", index)),
                "physical_page": page,
                "bbox_pdf_points": list(mapping["pdf_bbox_points"]),
                "render_sha256": str(render_record["render_sha256"]),
                "render_pixel_bbox": list(mapping["render_pixel_bbox"]),
                "crop_sha256": crop["sha256"],
                "crop_dimensions_px": dict(crop["dimensions_px"]),
                "pre_rotation_crop_sha256": pre_rotation_crop_sha256,
                "content_rotation_clockwise": rotation,
                "source_anchor": dict(region.get("source_anchor") or {}),
                "coordinate_transform_id": transform["id"],
            }
            if split_contract is not None:
                split_region = next((row for row in split_contract.get("regions", []) if isinstance(row, Mapping) and row.get("region_id") == binding["region_id"]), None)
                if not isinstance(split_region, Mapping):
                    raise ScanOcrError(f"spread_split_region_missing:{index}")
                for key in ("region_order", "reading_order", "printed_page_label_candidate", "rotated_region_bbox_px", "pre_rotation_render_pixel_bbox", "split_contract_sha256", "split_core_sha256"):
                    if key == "split_contract_sha256":
                        binding[key] = split_contract.get("contract_sha256")
                    elif key == "split_core_sha256":
                        binding[key] = split_contract.get("split_core_sha256")
                    else:
                        binding[key] = split_region.get(key)
                if binding.get("crop_sha256") != split_region.get("crop_sha256") or binding.get("crop_dimensions_px") != split_region.get("crop_dimensions_px") or binding.get("render_pixel_bbox") != split_region.get("pre_rotation_render_pixel_bbox"):
                    raise ScanOcrError(f"spread_split_region_crop_binding_mismatch:{index}")
            raster_bindings.append(binding)
            crop_dir = workspace_root / "crops" / f"page-{page:04d}"
            crop_dir.mkdir(parents=True, exist_ok=True)
            crop_path = crop_dir / f"{binding['region_id']}.png"
            if _paths_overlap(crop_path, source_path):
                raise ScanOcrError("raster_region_crop_source_overlap")
            crop_path.write_bytes(crop["bytes"])
            os.chmod(crop_path, 0o600)
            request = {
                "request_id": _stable_id("ocrq", source_sha256, backend, page, binding["region_id"], crop["sha256"]),
                "backend_id": backend,
                "input_kind": "raster-crop",
                "image_path": str(crop_path),
                "input_sha256": crop["sha256"],
                "source_sha256": source_sha256,
                "physical_page": page,
                "render": {
                    "sha256": render_record["render_sha256"],
                    "dpi": render_dpi,
                    "format": "png",
                    "image_dimensions_px": dict(crop["dimensions_px"]),
                },
                "raster_region": binding,
                "coordinate_transform": transform,
                **({"table_visual_object_id": _stable_id("vo", source_sha256, page, backend, binding["region_id"])} if backend in {PADDLE_STRUCTURE_BACKEND, PADDLE_STRUCTURE_V3_BACKEND} else {}),
                "configuration": engine_configuration,
                "model": model,
                "model_root": contract["model_root"],
                "runtime_contract_sha256": contract["contract_sha256"],
            }
            contexts.append({"request": request, "page": page, "binding": binding, "transform": transform, "render": render_record, "crop": crop, "region_order": int(binding.get("region_order", index))})
        contexts.sort(key=lambda row: (int(row.get("region_order", 0)), str(row["binding"]["region_id"])))
        if len({row["binding"]["region_id"] for row in contexts}) != len(contexts):
            raise ScanOcrError("raster_region_duplicate_id")
        expected_bindings = _checkpoint_expected_bindings(
            source_sha256=source_sha256,
            render_by_page=render_by_page,
            split_contract=split_contract,
            rotation=int(engine_configuration.get("preprocessing", {}).get("content_rotation_clockwise", 0)),
            configuration=engine_configuration,
            contract=contract,
            qualification_receipt_sha256=_qualification_receipt_sha256(configuration, backend),
        )
        reusable_ids: set[str] = set()
        checkpoint_rows: dict[str, Mapping[str, Any]] = {}
        if checkpoint is not None:
            checkpoint_rows = {
                str(row.get("region_id")): row
                for row in checkpoint.get("regions", [])
                if isinstance(row, Mapping)
            } if isinstance(checkpoint.get("regions"), list) else {}
            try:
                resume = resume_scan_checkpoint(
                    checkpoint,
                    expected_bindings=expected_bindings,
                    expected_region_order=[row["binding"]["region_id"] for row in contexts],
                )
            except ScanOcrError:
                raise
            reusable_ids = set(resume["reusable_region_ids"])
        pending_contexts: list[dict[str, Any]] = []
        responses: dict[str, Mapping[str, Any]] = {}
        errors_by_id: dict[str, Mapping[str, Any]] = {}
        for context in contexts:
            region_id = context["binding"]["region_id"]
            current_binding_hash = _checkpoint_region_binding_sha256(
                binding=context["binding"],
                transform=context["transform"],
                split_contract=split_contract,
            )
            if region_id in reusable_ids:
                saved = checkpoint_rows.get(region_id)
                artifact = saved.get("artifact") if isinstance(saved, Mapping) else None
                if not isinstance(saved, Mapping) or saved.get("binding_sha256") != current_binding_hash or not isinstance(artifact, Mapping):
                    raise ScanOcrError("scan_checkpoint_reusable_artifact_binding_invalid")
                response = artifact.get("worker_response")
                if not isinstance(response, Mapping) or response.get("request_id") != context["request"]["request_id"] or response.get("input_sha256") != context["request"]["input_sha256"] or response.get("status") != "ok":
                    raise ScanOcrError("scan_checkpoint_reusable_worker_response_invalid")
                responses[str(context["request"]["request_id"])] = response
            else:
                pending_contexts.append(context)
        try:
            if pending_contexts:
                host_options = configuration.get("user_config") if isinstance(configuration.get("user_config"), Mapping) else {}
                batch = run_external_worker_batch(
                    contract,
                    [row["request"] for row in pending_contexts],
                    timeout_seconds=float(configuration.get("worker_timeout_seconds", host_options.get("worker_timeout_seconds", 120))),
                    per_request_timeout_seconds=(
                        float(configuration.get("worker_request_timeout_seconds", host_options.get("worker_request_timeout_seconds")))
                        if configuration.get("worker_request_timeout_seconds", host_options.get("worker_request_timeout_seconds")) is not None
                        else None
                    ),
                    max_requests=(
                        int(configuration.get("worker_max_requests", host_options.get("worker_max_requests")))
                        if configuration.get("worker_max_requests", host_options.get("worker_max_requests")) is not None
                        else None
                    ),
                    workspace_root=workspace_root,
                )
            else:
                batch = {
                    "responses": [],
                    "errors": [],
                    "receipt": checkpoint.get("batch_receipt") if isinstance(checkpoint, Mapping) and isinstance(checkpoint.get("batch_receipt"), Mapping) else _visual_batch_receipt(status="resumed", regions=len(contexts)),
                }
        except PaddleRuntimeContractError as error:
            raise ScanOcrError(str(error)) from error
        responses.update({str(row.get("request_id")): row for row in batch.get("responses", []) if isinstance(row, Mapping)})
        errors_by_id.update({
            str(row.get("request_id")): row
            for row in batch.get("errors", [])
            if isinstance(row, Mapping) and row.get("request_id") is not None
        })
        for index, context in enumerate(contexts):
            request = context["request"]
            request_id = str(request["request_id"])
            response = responses.get(request_id)
            if response is None:
                error_row = errors_by_id.get(request_id, {"error_code": "worker_batch_response_missing"})
                worker_responses.append({
                    "schema_version": OCR_PROTOCOL_SCHEMA,
                    "request_id": request_id,
                    "backend_id": backend,
                    "physical_page": context["page"],
                    "status": "error",
                    "error_code": error_row.get("error_code"),
                    "runtime_contract_sha256": contract["contract_sha256"],
                })
                continue
            page = context["page"]
            binding = context["binding"]
            transform = context["transform"]
            render_record = context["render"]
            crop = context["crop"]
            rows, page_transcripts = _normalize_observations(
                source_sha256=source_sha256,
                physical_page=page,
                backend=backend,
                role=_backend_role(backend),
                response=response,
                render_receipt=render_record,
                transform=transform,
                model=model,
                configuration=engine_configuration,
                image_dimensions=crop["dimensions_px"],
                raster_binding=binding,
            )
            receipt_payload = {
                "source_sha256": source_sha256,
                "physical_page": page,
                "backend_id": backend,
                "render_sha256": render_record["render_sha256"],
                "crop_sha256": crop["sha256"],
                "runtime_contract_sha256": contract["contract_sha256"],
                "worker_response_content_sha256": sha256_json({key: value for key, value in response.items() if key not in {"elapsed_ms", "worker_receipt"}}),
                "observation_ids": [row["id"] for row in rows],
                "raw_result_sha256": response.get("raw_result_sha256"),
                "normalized_result_sha256": response.get("normalized_result_sha256"),
            }
            receipt_id = _stable_id("obr", receipt_payload)
            for row in rows:
                row["backend_receipt_id"] = receipt_id
            response_tables = response.get("table_grids", [])
            if backend in {PADDLE_STRUCTURE_BACKEND, PADDLE_STRUCTURE_V3_BACKEND}:
                if not isinstance(response_tables, list):
                    raise ScanOcrError("ppstructure_table_grid_output_invalid")
                for grid in response_tables:
                    if not isinstance(grid, Mapping):
                        raise ScanOcrError("ppstructure_table_grid_output_invalid")
                    mapped_grid = _map_table_grid_to_page(grid, transform)
                    table_grids.append(mapped_grid)
                    table_grid_bindings.append({
                        "table_grid_id": mapped_grid.get("table_grid_id"),
                        "backend_receipt_id": receipt_id,
                        "backend_id": backend,
                        "physical_page": page,
                        "region_id": binding["region_id"],
                        "crop_sha256": crop["sha256"],
                        "runtime_contract_sha256": contract["contract_sha256"],
                    })
                adapter = response.get("result_adapter")
                if not isinstance(adapter, Mapping) or adapter.get("schema_version") is None:
                    raise ScanOcrError("ppstructure_result_adapter_receipt_missing")
                raw_result = adapter.get("raw_result")
                if adapter.get("raw_result_sha256") != sha256_json(raw_result):
                    raise ScanOcrError("ppstructure_raw_result_hash_mismatch")
                normalized_without_hash = {key: value for key, value in adapter.items() if key not in {"normalized_result_sha256", "raw_result", "raw_result_sha256", "raw_result_keys"}}
                if adapter.get("normalized_result_sha256") != sha256_json(normalized_without_hash):
                    raise ScanOcrError("ppstructure_normalized_result_hash_mismatch")
                adapter_id = _stable_id("pra", source_sha256, page, backend, crop["sha256"], adapter.get("raw_result_sha256"), adapter.get("normalized_result_sha256"))
                adapter_row = {**dict(adapter), "id": adapter_id, "source_sha256": source_sha256, "physical_page": page, "backend_id": backend, "crop_sha256": crop["sha256"], "runtime_contract_sha256": contract["contract_sha256"], "backend_receipt_id": receipt_id, "candidate_only": True, "promotion": False, "verified_gold": False}
                result_adapters.append(adapter_row)
                for field_name, target in (("visual_candidates", visual_candidates), ("gaps", visual_gaps), ("conflicts", visual_conflicts)):
                    for candidate in adapter.get(field_name, []) if isinstance(adapter.get(field_name), list) else []:
                        if not isinstance(candidate, Mapping):
                            raise ScanOcrError("ppstructure_visual_result_invalid")
                        mapped_candidate = _map_adapter_row_to_page(candidate, transform, binding)
                        mapped_candidate.update({"backend_id": backend, "backend_receipt_id": receipt_id, "result_adapter_id": adapter_id})
                        target.append(mapped_candidate)
            backend_receipts.append({
                "schema_version": OCR_BACKEND_RECEIPT_SCHEMA,
                "id": receipt_id,
                "source_sha256": source_sha256,
                "physical_page": page,
                "backend_id": backend,
                "role": _backend_role(backend),
                "engine": {"id": str(response.get("engine") or BACKEND_SPECS[backend]["engine"]), "version": str(response.get("engine_version") or "unknown")},
                "protocol": {"schema_version": OCR_PROTOCOL_SCHEMA, "version": OCR_PROTOCOL_VERSION, "transport": "jsonl-external-interpreter-batch"},
                "render": {"source_sha256": source_sha256, "physical_page": page, "render_sha256": render_record["render_sha256"], "renderer": render_record["renderer"]},
                "model": model,
                "configuration": engine_configuration,
                "configuration_sha256": sha256_json(engine_configuration),
                "coordinate_transform_id": transform["id"],
                "crop_sha256": crop["sha256"],
                "runtime_contract_sha256": contract["contract_sha256"],
                "qualification_receipt_sha256": _qualification_receipt_sha256(configuration, backend),
                "worker": {
                    **dict(response.get("worker_receipt") or {}),
                    "runtime_contract_sha256": contract["contract_sha256"],
                    "worker_sha256": contract.get("worker_sha256"),
                    "input_crop_sha256": crop["sha256"],
                    "source_pdf_passed": False,
                    "batch": True,
                },
                "observation_ids_sha256": sha256_json([row["id"] for row in rows]),
                "observation_count": len(rows),
                "result_adapter_id": next((row["id"] for row in reversed(result_adapters) if row.get("backend_receipt_id") == receipt_id), None),
                "raw_result_sha256": response.get("raw_result_sha256"),
                "normalized_result_sha256": response.get("normalized_result_sha256"),
                "status": "candidate-only",
                "canonical_promotion_allowed": False,
            })
            observations.extend(rows)
            transcripts.extend(page_transcripts)
            worker_responses.append({
                "schema_version": OCR_PROTOCOL_SCHEMA,
                "request_id": request_id,
                "backend_id": backend,
                "physical_page": page,
                "status": response.get("status"),
                "engine": response.get("engine"),
                "engine_version": response.get("engine_version"),
                "runtime_contract_sha256": contract["contract_sha256"],
                "worker_receipt": response.get("worker_receipt"),
                "raw_observations": response.get("observations", []),
                "table_grids": response.get("table_grids", []),
                "visual_candidates": response.get("visual_candidates", []),
                "visual_gaps": response.get("visual_gaps", []),
                "visual_conflicts": response.get("visual_conflicts", []),
                "result_adapter_id": next((row["id"] for row in reversed(result_adapters) if row.get("backend_receipt_id") == receipt_id), None),
                "raw_result_sha256": response.get("raw_result_sha256"),
                "normalized_result_sha256": response.get("normalized_result_sha256"),
                "empty_text_candidate_count": (
                    response.get("result_adapter", {}).get("empty_text_candidate_count", 0)
                    if isinstance(response.get("result_adapter"), Mapping)
                    else 0
                ),
            })
        checkpoint_region_rows: list[dict[str, Any]] = []
        for context in contexts:
            binding = context["binding"]
            region_id = binding["region_id"]
            request_id = str(context["request"]["request_id"])
            binding_hash = _checkpoint_region_binding_sha256(
                binding=binding,
                transform=context["transform"],
                split_contract=split_contract,
            )
            response = responses.get(request_id)
            if not isinstance(response, Mapping) or response.get("status") != "ok":
                error_row = errors_by_id.get(request_id, {})
                checkpoint_region_rows.append(
                    {
                        "region_id": region_id,
                        "region_order": int(context["region_order"]),
                        "status": "paused",
                        "binding_sha256": binding_hash,
                        "error_code": error_row.get("error_code", "worker_batch_response_missing"),
                    }
                )
                continue
            receipt = next(
                (
                    row
                    for row in backend_receipts
                    if row.get("physical_page") == binding.get("physical_page")
                    and row.get("crop_sha256") == binding.get("crop_sha256")
                ),
                None,
            )
            if not isinstance(receipt, Mapping):
                raise ScanOcrError("scan_checkpoint_backend_receipt_missing")
            region_observations = [
                row
                for row in observations
                if isinstance(row.get("raw_observation"), Mapping)
                and isinstance(row["raw_observation"].get("raster_region"), Mapping)
                and row["raw_observation"]["raster_region"].get("region_id") == region_id
            ]
            transcript_ids = {row.get("transcript_span_id") for row in region_observations}
            region_transcripts = [row for row in transcripts if row.get("id") in transcript_ids]
            receipt_id = receipt.get("id")
            region_tables = [row for row in table_grids if row.get("region_id") == region_id]
            region_table_bindings = [row for row in table_grid_bindings if row.get("region_id") == region_id]
            region_adapters = [row for row in result_adapters if row.get("backend_receipt_id") == receipt_id]
            region_candidates = [row for row in visual_candidates if row.get("region_id") == region_id]
            region_gaps = [row for row in visual_gaps if row.get("region_id") == region_id]
            region_conflicts = [row for row in visual_conflicts if row.get("region_id") == region_id]
            checkpoint_region_rows.append(
                {
                    "region_id": region_id,
                    "region_order": int(context["region_order"]),
                    "status": "complete",
                    "binding_sha256": binding_hash,
                    "artifact": {
                        "binding": binding,
                        "coordinate_transform": context["transform"],
                        "worker_response": dict(response),
                        "backend_receipt": dict(receipt),
                        "observations": region_observations,
                        "transcripts": region_transcripts,
                        "table_grids": region_tables,
                        "table_grid_bindings": region_table_bindings,
                        "visual_candidates": region_candidates,
                        "visual_gaps": region_gaps,
                        "visual_conflicts": region_conflicts,
                        "result_adapters": region_adapters,
                    },
                }
            )
        batch_receipt = batch.get("receipt") if isinstance(batch.get("receipt"), Mapping) else None
        if batch_receipt is None and isinstance(checkpoint, Mapping) and isinstance(checkpoint.get("batch_receipt"), Mapping):
            batch_receipt = checkpoint["batch_receipt"]
        checkpoint_value = build_scan_checkpoint(
            bindings=expected_bindings,
            regions=checkpoint_region_rows,
            batch_receipt=batch_receipt,
        )
        return {
            "ocr_observations": observations,
            "ocr_transcripts": transcripts,
            "ocr_backend_receipts": backend_receipts,
            "coordinate_transforms": transforms,
            "ocr_worker_responses": worker_responses,
            "raster_regions": raster_bindings,
            "table_grids": table_grids,
            "table_grid_bindings": table_grid_bindings,
            "visual_candidates": visual_candidates,
            "visual_gaps": visual_gaps,
            "visual_conflicts": visual_conflicts,
            "result_adapters": result_adapters,
            "runtime_contract": contract,
            "render_records": [render_by_page[page] for page in pages],
            "batch_receipt": batch_receipt,
            "batch_errors": batch.get("errors", []),
            "checkpoint": checkpoint_value,
        }


def _run_chart_region_backend(
    *,
    source_path: Path,
    source_sha256: str,
    regions: Sequence[Mapping[str, Any]],
    model_root: Path | None,
    configuration: Mapping[str, Any],
    pdftoppm: Path | str,
    render_dpi: int,
    page_geometries: Mapping[int, Mapping[str, Any]],
    expected_render_receipts: Mapping[int, Mapping[str, Any]],
    workspace_root: Path,
    runtime_contract: Mapping[str, Any] | None = None,
    total_timeout_seconds: float = 600.0,
    per_request_timeout_seconds: float | None = 120.0,
) -> dict[str, Any]:
    """Run ChartParsing once over a bounded list of chart crops."""

    if not regions:
        return {
            "status": "completed",
            "chart_runs": [],
            "errors": [],
            "raster_regions": [],
            "runtime_contract": None,
            "batch_receipt": _visual_batch_receipt(),
        }
    if _paths_overlap(source_path, workspace_root):
        raise ScanOcrError("raster_region_input_workspace_overlap")
    workspace_root = workspace_root.expanduser().resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    worker_path = Path(__file__).with_name(WORKER_NAME).resolve()
    engine_configuration = _runtime_engine_configuration(configuration)
    try:
        if runtime_contract is None:
            spec = _raster_runtime_spec(PADDLE_CHART_BACKEND, configuration, model_root)
            contract = freeze_external_runtime_contract(
                spec,
                backend_id=PADDLE_CHART_BACKEND,
                configuration=engine_configuration,
                worker_path=worker_path,
                probe=True,
            )
        else:
            contract = dict(runtime_contract)
            validate_runtime_contract(contract)
            if contract.get("backend_id") != PADDLE_CHART_BACKEND or contract.get("configuration") != engine_configuration:
                raise PaddleRuntimeContractError("runtime_qualification_contract_mismatch")
    except PaddleRuntimeContractError as error:
        raise ScanOcrError(str(error)) from error
    if contract.get("available") is not True:
        return {
            "status": "not-run-missing-models",
            "chart_runs": [],
            "errors": [{"error_code": "not-run-missing-models"}],
            "raster_regions": [],
            "runtime_contract": contract,
            "batch_receipt": _visual_batch_receipt(status="paused-budget-exhausted"),
        }
    model = _external_model_receipt(contract, PADDLE_CHART_BACKEND)
    pages = sorted({int(row.get("physical_page", 0)) for row in regions})
    if not pages or any(page not in page_geometries for page in pages):
        raise ScanOcrError("raster_region_page_geometry_missing")
    contexts: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    with _render_pages(
        source_path,
        pages,
        pdftoppm,
        dpi=render_dpi,
        pdf_dimensions={page: page_geometries[page] for page in pages},
        workspace_root=workspace_root,
    ) as (render_records, render_paths):
        render_by_page = {int(row["physical_page"]): row for row in render_records}
        for page in pages:
            expected = expected_render_receipts.get(page)
            actual = render_by_page.get(page)
            if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
                raise ScanOcrError(f"raster_region_render_receipt_missing:{page}")
            if actual.get("render_sha256") != expected.get("render_sha256"):
                raise ScanOcrError(f"raster_region_render_sha256_mismatch:{page}")
        for index, region_value in enumerate(regions):
            region = dict(region_value)
            page = int(region.get("physical_page", 0))
            render_record = render_by_page.get(page)
            if render_record is None:
                raise ScanOcrError(f"raster_region_render_receipt_missing:{index}")
            image_data = render_paths[page].read_bytes()
            dimensions = _png_dimensions(image_data)
            if dimensions is None:
                raise ScanOcrError(f"raster_region_render_png_invalid:{index}")
            try:
                mapping = build_crop_mapping(
                    bbox_pdf_points=region.get("bbox_pdf_points"),
                    page_geometry=page_geometries[page],
                    render_dimensions_px=dimensions,
                    render_dpi=render_dpi,
                )
                expected_crop = region.get("crop_sha256")
                if not isinstance(expected_crop, str) or len(expected_crop) != 64:
                    expected_crop = None
                crop = crop_render_png(image_data, mapping, expected_crop_sha256=expected_crop)
                transform = build_crop_coordinate_transform(
                    source_sha256=source_sha256,
                    physical_page=page,
                    render_sha256=str(render_record["render_sha256"]),
                    render_dpi=render_dpi,
                    mapping=mapping,
                )
            except PaddleRuntimeContractError as error:
                raise ScanOcrError(f"raster_region_contract_invalid:{index}:{error}") from error
            binding = {
                "region_id": str(region.get("region_id") or _stable_id("chart-rgn", source_sha256, page, region.get("bbox_pdf_points"))),
                "physical_page": page,
                "bbox_pdf_points": list(mapping["pdf_bbox_points"]),
                "render_sha256": str(render_record["render_sha256"]),
                "render_pixel_bbox": list(mapping["render_pixel_bbox"]),
                "crop_sha256": crop["sha256"],
                "crop_dimensions_px": dict(crop["dimensions_px"]),
                "source_anchor": dict(region.get("source_anchor") or {}),
                "coordinate_transform_id": transform["id"],
            }
            bindings.append(binding)
            crop_dir = workspace_root / "chart-crops" / f"page-{page:04d}"
            crop_dir.mkdir(parents=True, exist_ok=True)
            crop_path = crop_dir / f"{binding['region_id']}.png"
            if _paths_overlap(crop_path, source_path):
                raise ScanOcrError("raster_region_crop_source_overlap")
            crop_path.write_bytes(crop["bytes"])
            os.chmod(crop_path, 0o600)
            request = {
                "request_id": _stable_id("chartq", source_sha256, page, binding["region_id"], crop["sha256"]),
                "backend_id": PADDLE_CHART_BACKEND,
                "input_kind": "raster-crop",
                "image_path": str(crop_path),
                "input_sha256": crop["sha256"],
                "source_sha256": source_sha256,
                "physical_page": page,
                "render": {"sha256": render_record["render_sha256"], "dpi": render_dpi, "format": "png", "image_dimensions_px": dict(crop["dimensions_px"])},
                "raster_region": binding,
                "coordinate_transform": transform,
                "configuration": engine_configuration,
                "model": model,
                "model_root": contract["model_root"],
                "runtime_contract_sha256": contract["contract_sha256"],
            }
            contexts.append({"request": request, "binding": binding, "transform": transform, "render": render_record})
        try:
            batch = run_external_worker_batch(
                contract,
                [row["request"] for row in contexts],
                timeout_seconds=float(total_timeout_seconds),
                per_request_timeout_seconds=float(per_request_timeout_seconds) if per_request_timeout_seconds is not None else None,
                max_requests=int(configuration["worker_max_requests"]) if configuration.get("worker_max_requests") is not None else None,
                workspace_root=workspace_root,
            )
        except PaddleRuntimeContractError as error:
            raise ScanOcrError(str(error)) from error
    responses = {str(row.get("request_id")): row for row in batch.get("responses", []) if isinstance(row, Mapping)}
    errors_by_id = {str(row.get("request_id")): row for row in batch.get("errors", []) if isinstance(row, Mapping) and row.get("request_id") is not None}
    chart_runs: list[dict[str, Any]] = []
    for context in contexts:
        request = context["request"]
        response = responses.get(str(request["request_id"]))
        if response is None:
            error = errors_by_id.get(str(request["request_id"]), {"error_code": "worker_batch_response_missing"})
            chart_runs.append({"status": "paused", "error_code": error.get("error_code"), "request_id": request["request_id"], "physical_page": context["binding"]["physical_page"], "binding": context["binding"], "render": context["render"]})
            continue
        adapter = response.get("result_adapter")
        if not isinstance(adapter, Mapping):
            chart_runs.append({"status": "paused", "error_code": "chart_result_adapter_missing", "request_id": request["request_id"], "physical_page": context["binding"]["physical_page"], "binding": context["binding"], "render": context["render"]})
            continue
        chart_runs.append({
            "status": "candidate" if response.get("chart_candidates") else "paused",
            "request_id": request["request_id"],
            "physical_page": context["binding"]["physical_page"],
            "bbox_pdf_points": context["binding"]["bbox_pdf_points"],
            "render_sha256": context["binding"]["render_sha256"],
            "crop_sha256": context["binding"]["crop_sha256"],
            "binding": context["binding"],
            "render": context["render"],
            "response": response,
            "adapter": adapter,
        })
    return {
        "status": "paused" if batch.get("receipt", {}).get("status") == "paused-budget-exhausted" or any(row.get("status") != "candidate" for row in chart_runs) else "candidate",
        "chart_runs": chart_runs,
        "errors": batch.get("errors", []),
        "raster_regions": bindings,
        "runtime_contract": contract,
        "batch_receipt": batch.get("receipt"),
        "render_records": list(render_by_page.values()),
    }


def _scan_structure_specs(request: Mapping[str, Any], scope: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_segments = request.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ScanOcrError("scan_structure_segments_required")
    start_page = int(scope["start_page"])
    end_page = int(scope["end_page"])
    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_segments):
        if not isinstance(raw, Mapping):
            raise ScanOcrError(f"scan_structure_segment_invalid:{index + 1}")
        segment_id = raw.get("segment_id", raw.get("id"))
        title_value = raw.get("title")
        pages_value = raw.get("physical_pages")
        if pages_value is None:
            page_range = raw.get("page_range")
            if isinstance(page_range, (list, tuple)) and len(page_range) == 2:
                try:
                    pages_value = list(range(int(page_range[0]), int(page_range[1]) + 1))
                except (TypeError, ValueError):
                    pages_value = None
        if (
            not isinstance(segment_id, str)
            or not segment_id.strip()
            or segment_id in seen
            or not isinstance(title_value, str)
            or not normalize_text(title_value)
            or not isinstance(pages_value, (list, tuple))
            or not pages_value
        ):
            raise ScanOcrError(f"scan_structure_segment_invalid:{index + 1}")
        try:
            pages = [int(page) for page in pages_value]
        except (TypeError, ValueError) as error:
            raise ScanOcrError(f"scan_structure_segment_invalid:{index + 1}") from error
        if (
            pages != sorted(set(pages))
            or pages != list(range(pages[0], pages[-1] + 1))
            or pages[0] < start_page
            or pages[-1] > end_page
        ):
            raise ScanOcrError(f"scan_structure_page_range_invalid:{index + 1}")
        order = raw.get("structure_order", index)
        if isinstance(order, bool) or not isinstance(order, int) or order < 0:
            raise ScanOcrError(f"scan_structure_order_invalid:{index + 1}")
        parent_id = raw.get("parent_id")
        if parent_id is not None and (not isinstance(parent_id, str) or not parent_id.strip()):
            raise ScanOcrError(f"scan_structure_parent_invalid:{index + 1}")
        depth = raw.get("depth", 0)
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
            raise ScanOcrError(f"scan_structure_depth_invalid:{index + 1}")
        seen.add(segment_id)
        specs.append(
            {
                "id": segment_id,
                "title": normalize_text(title_value),
                "physical_pages": pages,
                "structure_order": order,
                "parent_id": parent_id,
                "depth": depth,
            }
        )
    if [row["structure_order"] for row in specs] != sorted(row["structure_order"] for row in specs):
        raise ScanOcrError("scan_structure_order_not_monotonic")
    return specs


def _scan_structure_review_payload(request: Mapping[str, Any]) -> Mapping[str, Any] | None:
    review = request.get("review")
    if review is None and any(key in request for key in ("reviewer", "attestation", "accepted_segments", "review_segments")):
        review = request
    if review is None:
        return None
    if not isinstance(review, Mapping):
        raise ScanOcrError("scan_structure_review_invalid")
    return review


def _scan_review_authorization(plan: Mapping[str, Any]) -> str:
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
        "review_protocol": plan.get("review_protocol"),
        "review_session_id": plan.get("review_session_id"),
    }
    return sha256_json(payload)


def _scan_review_fragment(
    review: Mapping[str, Any],
    *,
    source_sha256: str,
    candidates: Sequence[Mapping[str, Any]],
    specs: Sequence[Mapping[str, Any]],
    proposer_instance: str | None,
) -> tuple[Mapping[str, Any], dict[str, Any]]:
    """Load and validate a host-supplied scan review fragment.

    A direct mapping supplied to the compiler is not review evidence.  The
    accepted path is an external fragment bound to the existing orchestrator
    plan/session/input conventions and to the exact candidate hashes emitted
    for this scan.  The compiler only verifies and projects the fragment; it
    never creates its attestation.
    """

    descriptor = review.get("external_fragment")
    if not isinstance(descriptor, Mapping):
        raise ScanOcrError("scan_structure_external_fragment_required")
    fragment_path_value = descriptor.get("path")
    expected_fragment_hash = descriptor.get("sha256")
    if (
        not isinstance(fragment_path_value, str)
        or not fragment_path_value
        or not isinstance(expected_fragment_hash, str)
        or not _SHA256_RE.fullmatch(expected_fragment_hash)
        or descriptor.get("provenance") != "external-review-fragment"
    ):
        raise ScanOcrError("scan_structure_external_fragment_invalid")
    fragment_path = Path(fragment_path_value).expanduser()
    if fragment_path.is_symlink() or not fragment_path.is_file():
        raise ScanOcrError("scan_structure_external_fragment_missing")
    fragment_path = fragment_path.resolve()
    if sha256_file(fragment_path) != expected_fragment_hash:
        raise ScanOcrError("scan_structure_external_fragment_hash_mismatch")
    try:
        fragment = _load_json(fragment_path)
    except ScanOcrError as error:
        raise ScanOcrError("scan_structure_external_fragment_invalid") from error
    if (
        not isinstance(fragment, Mapping)
        or fragment.get("schema_version") != "tkc.scanned-pdf-structure-review-fragment/v0.1"
        or fragment.get("provenance") != "external-review-fragment"
    ):
        raise ScanOcrError("scan_structure_external_fragment_invalid")
    plan = fragment.get("review_plan")
    attestation = fragment.get("attestation")
    if not isinstance(plan, Mapping) or not isinstance(attestation, Mapping):
        raise ScanOcrError("scan_structure_review_plan_or_attestation_missing")
    plan = dict(plan)
    reviewer = attestation.get("reviewer_instance")
    session = attestation.get("review_session_id")
    fragment_proposer = attestation.get("proposer_instance")
    if (
        plan.get("schema_version") != "tkc.review-plan/v0.1"
        or plan.get("attestation_level") != "host-orchestrator-recorded-not-cryptographic"
        or plan.get("issued_by") != "host-orchestrator"
        or not isinstance(plan.get("issued_at"), str)
        or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
            plan["issued_at"],
        )
        or plan.get("source_sha256") != source_sha256
        or plan.get("gap_resolution_policy") != "explicit-package-scope-v1"
        or plan.get("review_protocol") != "separate-source-render-review-v1"
        or not isinstance(plan.get("review_session_id"), str)
        or not re.fullmatch(r"rws-[0-9a-f]{20}", plan["review_session_id"])
        or plan.get("review_session_id") != session
        or plan.get("authorization_sha256") != _scan_review_authorization(plan)
        or not isinstance(reviewer, str)
        or not reviewer.strip()
        or not isinstance(fragment_proposer, str)
        or not fragment_proposer.strip()
        or not isinstance(proposer_instance, str)
        or fragment_proposer != proposer_instance
    ):
        raise ScanOcrError("scan_structure_review_plan_invalid")
    proposers = plan.get("proposer_instances")
    reviewers = plan.get("reviewer_instances")
    if (
        not isinstance(proposers, list)
        or not isinstance(reviewers, list)
        or proposers != [proposer_instance]
        or reviewers != [reviewer]
        or reviewer == proposer_instance
        or len(set(proposers + reviewers)) != len(proposers + reviewers)
    ):
        raise ScanOcrError("scan_structure_reviewer_not_independent")
    registry = fragment.get("reviewer_registry")
    if (
        not isinstance(registry, Mapping)
        or registry.get("reviewer_instances") != reviewers
        or registry.get("registry_sha256")
        != sha256_json({"reviewer_instances": reviewers})
    ):
        raise ScanOcrError("scan_structure_reviewer_registry_invalid")
    candidate_rows = {
        str(row.get("candidate_id")): str(row.get("candidate_sha256"))
        for row in candidates
        if isinstance(row, Mapping)
        and isinstance(row.get("candidate_id"), str)
        and isinstance(row.get("candidate_sha256"), str)
    }
    candidate_hashes = fragment.get("candidate_hashes")
    if candidate_hashes != candidate_rows:
        raise ScanOcrError("scan_structure_candidate_hash_binding_invalid")
    expected_inputs: dict[str, str] = {}
    for spec in specs:
        segment_id = str(spec["id"])
        segment_candidates = sorted(
            row for row in candidates if row.get("segment_id") == segment_id
        )
        expected_inputs[segment_id] = sha256_json(
            {
                "source_sha256": source_sha256,
                "segment_id": segment_id,
                "candidate_ids": [str(row.get("candidate_id")) for row in segment_candidates],
                "candidate_hashes": {
                    str(row.get("candidate_id")): str(row.get("candidate_sha256"))
                    for row in segment_candidates
                },
                "physical_pages": list(spec["physical_pages"]),
                "structure_order": spec["structure_order"],
            }
        )
    input_hashes = fragment.get("review_input_hashes")
    if input_hashes != expected_inputs:
        raise ScanOcrError("scan_structure_review_input_hash_binding_invalid")
    expected_bundle = sha256_json(
        {"candidate_hashes": candidate_rows, "review_input_hashes": expected_inputs}
    )
    if plan.get("bundle_sha256") != expected_bundle:
        raise ScanOcrError("scan_structure_review_bundle_hash_invalid")
    plan_hash = sha256_json(plan)
    if fragment.get("review_plan_sha256") != plan_hash or attestation.get("review_plan_sha256") != plan_hash:
        raise ScanOcrError("scan_structure_review_plan_hash_invalid")
    if (
        attestation.get("proposer_instance") != proposer_instance
        or attestation.get("reviewer_instance") != reviewer
        or attestation.get("review_session_id") != session
        or attestation.get("candidate_hashes") != candidate_rows
        or attestation.get("review_input_hashes") != expected_inputs
    ):
        raise ScanOcrError("scan_structure_review_attestation_binding_invalid")
    supplied_hash = attestation.get("attestation_sha256")
    if (
        not isinstance(supplied_hash, str)
        or not _SHA256_RE.fullmatch(supplied_hash)
        or supplied_hash
        != sha256_json({key: value for key, value in attestation.items() if key != "attestation_sha256"})
    ):
        raise ScanOcrError("scan_structure_review_attestation_invalid")
    raw_segments = fragment.get("segments")
    if not isinstance(raw_segments, list):
        raise ScanOcrError("scan_structure_review_segment_set_invalid")
    return fragment, {
        "reviewer_instance": reviewer,
        "review_session_id": session,
        "attestation_sha256": supplied_hash,
        "proposer_instance": proposer_instance,
        "review_plan_sha256": plan_hash,
        "external_fragment_sha256": expected_fragment_hash,
        "review_input_sha256": expected_inputs,
        "candidate_sha256": candidate_rows,
        "review_independence_basis": "host-plan-proposer-reviewer-separation-v1",
    }


def _scan_review_confirmation(row: Mapping[str, Any], review: Mapping[str, Any], key: str) -> bool:
    aliases = {
        "title_confirmed": ("title_confirmed", "title_checked"),
        "page_range_confirmed": ("page_range_confirmed", "pages_confirmed"),
        "reading_order_confirmed": ("reading_order_confirmed", "order_confirmed"),
        "bbox_confirmed": ("bbox_confirmed", "geometry_confirmed"),
        "evidence_confirmed": ("evidence_confirmed", "binding_confirmed"),
    }
    for source in (row, row.get("confirmations") if isinstance(row.get("confirmations"), Mapping) else {}, review.get("checks") if isinstance(review.get("checks"), Mapping) else {}, review.get("confirmations") if isinstance(review.get("confirmations"), Mapping) else {}):
        for alias in aliases[key]:
            if alias in source:
                return source.get(alias) is True
    return False


def _scan_structure_ocr_indexes(
    *,
    source_sha256: str,
    primary_backend: str,
    observations: Sequence[Mapping[str, Any]],
    transcripts: Sequence[Mapping[str, Any]],
    transforms: Sequence[Mapping[str, Any]],
    backend_receipts: Sequence[Mapping[str, Any]],
    render_receipts: Sequence[Mapping[str, Any]],
    scope: Mapping[str, Any],
) -> tuple[dict[int, str], dict[int, list[dict[str, Any]]], dict[str, dict[str, Any]], dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    transcript_rows: dict[int, list[dict[str, Any]]] = {}
    observation_by_transcript: dict[str, dict[str, Any]] = {}
    for row in transcripts:
        if not isinstance(row, Mapping) or row.get("backend_id") != primary_backend:
            continue
        page = row.get("physical_page")
        span = row.get("span")
        text_value = row.get("normalized_text")
        if not isinstance(page, int) or not isinstance(span, Mapping) or not isinstance(text_value, str) or not text_value:
            continue
        transcript_rows.setdefault(page, []).append(dict(row))
    for row in observations:
        if isinstance(row, Mapping) and row.get("backend", {}).get("id") == primary_backend and isinstance(row.get("transcript_span_id"), str):
            observation_by_transcript[str(row["transcript_span_id"])] = dict(row)
    for page, rows in transcript_rows.items():
        rows.sort(key=lambda row: (int(row.get("span", {}).get("start", 0)), str(row.get("id", ""))))
    page_texts = {
        page: normalize_text(" ".join(str(row["normalized_text"]) for row in rows))
        for page, rows in transcript_rows.items()
    }
    transform_by_page = {
        int(row["physical_page"]): dict(row)
        for row in transforms
        if isinstance(row, Mapping) and row.get("physical_page") in range(int(scope["start_page"]), int(scope["end_page"]) + 1)
    }
    render_by_page = {
        int(row["physical_page"]): dict(row)
        for row in render_receipts
        if isinstance(row, Mapping) and isinstance(row.get("physical_page"), int)
    }
    if not page_texts:
        raise ScanOcrError("scan_structure_ocr_transcript_empty")
    return page_texts, transcript_rows, observation_by_transcript, transform_by_page, render_by_page


def _scan_structure_locator(
    *,
    source_sha256: str,
    page: int,
    transcript_rows: Sequence[Mapping[str, Any]],
    observation_by_transcript: Mapping[str, Mapping[str, Any]],
    transform_by_page: Mapping[int, Mapping[str, Any]],
    render_by_page: Mapping[int, Mapping[str, Any]],
    backend_receipts: Sequence[Mapping[str, Any]],
    primary_backend: str,
) -> dict[str, Any]:
    if not transcript_rows:
        raise ScanOcrError("scan_structure_transcript_binding_missing")
    obs_rows = [observation_by_transcript.get(str(row.get("id"))) for row in transcript_rows]
    if any(not isinstance(row, Mapping) for row in obs_rows):
        raise ScanOcrError("scan_structure_observation_binding_missing")
    observations = [dict(row) for row in obs_rows if isinstance(row, Mapping)]
    render = render_by_page.get(page)
    transform = transform_by_page.get(page)
    if not isinstance(render, Mapping) or not isinstance(transform, Mapping):
        raise ScanOcrError("scan_structure_render_or_transform_missing")
    bboxes = [row.get("bbox_pdf_points") for row in observations]
    if any(not isinstance(bbox, list) or len(bbox) != 4 for bbox in bboxes):
        raise ScanOcrError("scan_structure_bbox_missing")
    bbox = [
        _point(min(float(row[0]) for row in bboxes)),
        _point(min(float(row[1]) for row in bboxes)),
        _point(max(float(row[2]) for row in bboxes)),
        _point(max(float(row[3]) for row in bboxes)),
    ]
    model_hashes = {str(row.get("model", {}).get("identity_sha256")) for row in observations}
    config_hashes = {str(row.get("configuration_sha256")) for row in observations}
    if len(model_hashes) != 1 or len(config_hashes) != 1:
        raise ScanOcrError("scan_structure_runtime_binding_drift")
    receipt_ids = {str(row.get("backend_receipt_id")) for row in observations}
    receipt_by_id = {
        str(row.get("id")): row
        for row in backend_receipts
        if isinstance(row, Mapping) and isinstance(row.get("id"), str)
    }
    if len(receipt_ids) != 1 or next(iter(receipt_ids)) not in receipt_by_id:
        raise ScanOcrError("scan_structure_backend_receipt_binding_missing")
    receipt = receipt_by_id[next(iter(receipt_ids))]
    runtime = _scan_runtime_binding(receipt, backend_id=primary_backend)
    row_commitments = [
        _scan_transcript_commitment(row, observation_by_transcript[str(row.get("id"))])
        for row in transcript_rows
    ]
    observation_commitments = [_scan_observation_commitment(row) for row in observations]
    row_commitments.sort(key=lambda row: (int(row["span"]["start"]), str(row["id"])))
    observation_commitments.sort(key=lambda row: str(row["id"]))
    transform_commitment = sha256_json(dict(transform))
    render_commitment = sha256_json(dict(render))
    transcript_text = " ".join(str(row.get("normalized_text")) for row in transcript_rows)
    backend_binding = {
        "id": next(iter(receipt_ids)),
        "source_sha256": source_sha256,
        "physical_page": page,
        "backend_id": primary_backend,
        "model_identity_sha256": next(iter(model_hashes)),
        "configuration_sha256": next(iter(config_hashes)),
        "render_sha256": render.get("render_sha256"),
        "coordinate_transform_id": transform.get("id"),
        "runtime_contract_sha256": runtime["runtime_contract_sha256"],
        "worker_sha256": runtime["worker_sha256"],
        "qualification_receipt_sha256": runtime["qualification_receipt_sha256"],
        "backend_receipt_sha256": runtime["backend_receipt_sha256"],
    }
    return {
        "source_sha256": source_sha256,
        "physical_page": page,
        "render_sha256": render.get("render_sha256"),
        "render_dpi": render.get("renderer", {}).get("dpi"),
        "bbox_pdf_points": bbox,
        "bbox_sha256": sha256_json(bbox),
        "coordinate_transform_id": transform.get("id"),
        "ocr_observation_ids": [str(row["id"]) for row in observations],
        "transcript_span_ids": [str(row["id"]) for row in transcript_rows],
        "transcript_sha256": sha256_text(transcript_text),
        "backend_id": primary_backend,
        "model_identity_sha256": next(iter(model_hashes)),
        "configuration_sha256": next(iter(config_hashes)),
        "coordinate_transform_scope": "page-render",
        "render_receipt": dict(render),
        "render_receipt_sha256": render_commitment,
        "coordinate_transform": dict(transform),
        "coordinate_transform_sha256": transform_commitment,
        "transcript_row_commitments": row_commitments,
        "transcript_commitment_sha256": sha256_json(
            {
                "transcript_sha256": sha256_text(transcript_text),
                "transcript_row_commitments": row_commitments,
            }
        ),
        "observation_commitments": observation_commitments,
        "backend_receipt_id": next(iter(receipt_ids)),
        "backend_receipt_binding_sha256": sha256_json(backend_binding),
        **runtime,
    }


def _build_scan_anchor_candidates(source_id: str, segments: Sequence[Mapping[str, Any]], page_texts: Mapping[int, str], *, reviewed: bool) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for segment in segments:
        pages = [int(page) for page in segment["physical_pages"]]
        joined = "\f".join(str(page_texts.get(page, "")) for page in pages)
        first_page, last_page = pages[0], pages[-1]
        page_word = "page" if first_page == last_page else "pages"
        page_range = str(first_page) if first_page == last_page else f"{first_page}-{last_page}"
        candidates.append(
            {
                "schema_version": "tkc.pdf-ir/v0.3",
                "id": _stable_id("evc", source_id, segment["id"], joined),
                "candidate_status": "needs-semantic-review",
                "source_id": source_id,
                "pages": pages,
                "printed_page_labels": [str(page) for page in pages],
                "segment_id": segment["id"],
                "locator": {
                    "title": segment["title"],
                    "structure_order": int(segment["structure_order"]),
                    "hash_scope": "reviewed OCR transcript spans" if reviewed else "scan OCR candidate transcript spans",
                    "extractor": "scan-ocr-projection-v0.1",
                },
                "excerpt_sha256": sha256_text(joined),
                "visual_verification_required": True,
                "visual_verification_pages": pages,
                "supports": [],
            }
        )
    return candidates


def build_scan_structure_review(
    *,
    source_id: str,
    source_sha256: str,
    scope: Mapping[str, Any],
    primary_backend: str,
    observations: Sequence[Mapping[str, Any]],
    transcripts: Sequence[Mapping[str, Any]],
    transforms: Sequence[Mapping[str, Any]],
    backend_receipts: Sequence[Mapping[str, Any]],
    render_receipts: Sequence[Mapping[str, Any]],
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """Create candidate scan headings and apply only an explicit review choice."""

    specs = _scan_structure_specs(request, scope)
    page_texts, transcript_rows, observation_by_transcript, transform_by_page, render_index = _scan_structure_ocr_indexes(
        source_sha256=source_sha256,
        primary_backend=primary_backend,
        observations=observations,
        transcripts=transcripts,
        transforms=transforms,
        backend_receipts=backend_receipts,
        render_receipts=render_receipts,
        scope=scope,
    )
    candidates: list[dict[str, Any]] = []
    candidates_by_segment: dict[str, list[dict[str, Any]]] = {}
    resolutions: list[dict[str, Any]] = []
    for index, spec in enumerate(specs):
        local: list[dict[str, Any]] = []
        title = spec["title"]
        for page in spec["physical_pages"]:
            page_text = page_texts.get(page, "")
            cursor = 0
            while page_text:
                start = page_text.find(title, cursor)
                if start < 0:
                    break
                end = start + len(title)
                cursor = end
                overlapping = [
                    row for row in transcript_rows.get(page, [])
                    if int(row.get("span", {}).get("start", -1)) < end and int(row.get("span", {}).get("end", -1)) > start
                ]
                if not overlapping:
                    raise ScanOcrError("scan_structure_transcript_span_ambiguous")
                locator = _scan_structure_locator(
                    source_sha256=source_sha256,
                    page=page,
                    transcript_rows=overlapping,
                    observation_by_transcript=observation_by_transcript,
                    transform_by_page=transform_by_page,
                    render_by_page=render_index,
                    backend_receipts=backend_receipts,
                    primary_backend=primary_backend,
                )
                raw_title = page_text[start:end]
                candidate = {
                    "schema_version": "tkc.heading-candidate/v0.2",
                    "candidate_id": _stable_id("hgc", source_sha256, spec["id"], page, start, end, locator["transcript_sha256"]),
                    "source_sha256": source_sha256,
                    "segment_id": spec["id"],
                    "physical_page": page,
                    "raw_title": raw_title,
                    "requested_title": title,
                    "matching_title": title,
                    "match_normalization": {
                        "profile": "nfkc-casefold-dash-ligature-dehyphenated-v2",
                        "value": title.casefold(),
                        "requested_value": title.casefold(),
                    },
                    "char_span": [start, end],
                    "page_text_sha256": sha256_text(page_text),
                    "span_sha256": sha256_text(raw_title),
                    "line_index": 0,
                    "line_sha256": sha256_text(raw_title),
                    "line_boundary": "exact-line",
                    "role": "body-heading",
                    "candidate_method": "normalized-exact",
                    "parser": {
                        "id": "scan-ocr-projection",
                        "version": "v0.1",
                        "canonical_text_extractor": "ocr-transcript-projection-not-native",
                    },
                    "render_sha256": locator["render_sha256"],
                    "coordinate_space": COORDINATE_SPACE,
                    "bbox": locator["bbox_pdf_points"],
                    "layout_line": None,
                    "layout_candidate_ids": [],
                    "constraints": {
                        "physical_pages": list(spec["physical_pages"]),
                        "structure_order": int(spec["structure_order"]),
                        "page_in_segment_envelope": True,
                        "ocr_projection": True,
                    },
                    "score": 100,
                    "source_kind": "scan-ocr-candidate",
                    "scan_locator": locator,
                    "selection_reason": "requires-external-scan-structure-review",
                }
                candidate["candidate_sha256"] = sha256_json({key: value for key, value in candidate.items() if key != "candidate_sha256"})
                local.append(candidate)
        candidates_by_segment[spec["id"]] = local
        candidate_ids = [row["candidate_id"] for row in local]
        resolution = {
            "schema_version": "tkc.heading-resolution/v0.2",
            "resolution_id": f"hgr-{sha256_json([source_sha256, spec['id']])[:20]}",
            "source_sha256": source_sha256,
            "segment_id": spec["id"],
            "requested_title": title,
            "title_correction": None,
            "candidate_ids": candidate_ids,
            "candidate_hashes": {row["candidate_id"]: row["candidate_sha256"] for row in local},
            "status": "heading_resolution_required",
            "selection_policy": "manual-required",
            "selected_candidate_id": None,
            "selected_candidate_sha256": None,
            "score_margin": None,
            "deterministic_margin": 12,
            "failure_code": "scan_structure_review_required" if local else "scan_structure_candidate_missing",
            "selection_reason": "OCR may propose a title occurrence; an external reviewer must confirm title, pages, order, geometry and evidence.",
            "structure_order": int(spec["structure_order"]),
            "physical_page": local[0]["physical_page"] if local else spec["physical_pages"][0],
            "char_span": local[0]["char_span"] if local else None,
            "review_required": True,
        }
        resolution["resolution_sha256"] = sha256_json({key: value for key, value in resolution.items() if key != "resolution_sha256"})
        resolutions.append(resolution)
        candidates.extend(local)

    review = _scan_structure_review_payload(request)
    reviewer_instance = review_session_id = attestation_sha256 = None
    review_binding: dict[str, Any] | None = None
    overrides: list[dict[str, Any]] = []
    if review is not None:
        proposer_instance = request.get("proposer_instance")
        if not isinstance(proposer_instance, str) or not proposer_instance.strip():
            raise ScanOcrError("scan_structure_proposer_identity_required")
        review, review_binding = _scan_review_fragment(
            review,
            source_sha256=source_sha256,
            candidates=candidates,
            specs=specs,
            proposer_instance=proposer_instance,
        )
        reviewer_instance = review_binding["reviewer_instance"]
        review_session_id = review_binding["review_session_id"]
        attestation_sha256 = review_binding["attestation_sha256"]
        raw_review_segments = review.get("segments", review.get("review_segments", review.get("accepted_segments")))
        if not isinstance(raw_review_segments, list) or len(raw_review_segments) != len(specs):
            raise ScanOcrError("scan_structure_review_segment_set_invalid")
        review_by_segment: dict[str, Mapping[str, Any]] = {}
        for row in raw_review_segments:
            if not isinstance(row, Mapping) or not isinstance(row.get("segment_id"), str) or row["segment_id"] in review_by_segment:
                raise ScanOcrError("scan_structure_review_segment_invalid")
            review_by_segment[str(row["segment_id"])] = row
        if set(review_by_segment) != {str(row["id"]) for row in specs}:
            raise ScanOcrError("scan_structure_review_segment_set_invalid")
        for spec in specs:
            row = review_by_segment[spec["id"]]
            if any(not _scan_review_confirmation(row, review, key) for key in ("title_confirmed", "page_range_confirmed", "reading_order_confirmed", "bbox_confirmed", "evidence_confirmed")):
                raise ScanOcrError(f"scan_structure_review_confirmation_incomplete:{spec['id']}")
            if row.get("decision", "accepted") not in {"accepted", "accept"}:
                raise ScanOcrError(f"scan_structure_review_not_accepted:{spec['id']}")
            if normalize_text(str(row.get("title", spec["title"]))) != spec["title"]:
                raise ScanOcrError(f"scan_structure_review_title_drift:{spec['id']}")
            selected_id = row.get("candidate_id")
            local = candidates_by_segment[spec["id"]]
            selected = next((candidate for candidate in local if candidate.get("candidate_id") == selected_id), None)
            if selected is None:
                raise ScanOcrError(f"scan_structure_review_candidate_invalid:{spec['id']}")
            expected_range = [spec["physical_pages"][0], spec["physical_pages"][-1]]
            if row.get("page_range") != expected_range or row.get("structure_order") != spec["structure_order"]:
                raise ScanOcrError(f"scan_structure_review_structure_drift:{spec['id']}")
            bbox_value = row.get("bbox_pdf_points", row.get("bbox"))
            if not isinstance(bbox_value, list) or len(bbox_value) != 4 or [_point(value) for value in bbox_value] != selected["scan_locator"]["bbox_pdf_points"]:
                raise ScanOcrError(f"scan_structure_review_bbox_drift:{spec['id']}")
            evidence = row.get("evidence")
            locator = selected["scan_locator"]
            required_evidence = {
                "source_sha256": locator["source_sha256"],
                "render_sha256": locator["render_sha256"],
                "render_dpi": locator["render_dpi"],
                "coordinate_transform_id": locator["coordinate_transform_id"],
                "transcript_sha256": locator["transcript_sha256"],
                "backend_id": locator["backend_id"],
                "model_identity_sha256": locator["model_identity_sha256"],
                "configuration_sha256": locator["configuration_sha256"],
                "render_receipt_sha256": locator["render_receipt_sha256"],
                "coordinate_transform_sha256": locator["coordinate_transform_sha256"],
                "backend_receipt_sha256": locator["backend_receipt_sha256"],
                "runtime_contract_sha256": locator["runtime_contract_sha256"],
                "worker_sha256": locator["worker_sha256"],
            }
            if not isinstance(evidence, Mapping) or any(evidence.get(key) != value for key, value in required_evidence.items()):
                raise ScanOcrError(f"scan_structure_review_evidence_drift:{spec['id']}")
            if sorted(str(value) for value in row.get("ocr_observation_ids", row.get("observation_ids", []))) != sorted(locator["ocr_observation_ids"]):
                raise ScanOcrError(f"scan_structure_review_observation_binding_invalid:{spec['id']}")
            if sorted(str(value) for value in row.get("transcript_span_ids", [])) != sorted(locator["transcript_span_ids"]):
                raise ScanOcrError(f"scan_structure_review_transcript_binding_invalid:{spec['id']}")
            if sorted(str(value) for value in row.get("transcript_row_hashes", [])) != sorted(
                str(value.get("row_sha256")) for value in locator["transcript_row_commitments"]
            ):
                raise ScanOcrError(f"scan_structure_review_transcript_hash_binding_invalid:{spec['id']}")
            if sorted(str(value) for value in row.get("observation_hashes", [])) != sorted(
                str(value.get("observation_sha256")) for value in locator["observation_commitments"]
            ):
                raise ScanOcrError(f"scan_structure_review_observation_hash_binding_invalid:{spec['id']}")
            rationale = row.get("rationale", review.get("rationale"))
            if not isinstance(rationale, str) or not rationale.strip():
                raise ScanOcrError(f"scan_structure_review_rationale_missing:{spec['id']}")
            overrides.append(
                {
                    "schema_version": "tkc.heading-locator-override/v0.2",
                    "resolution_id": next(resolution["resolution_id"] for resolution in resolutions if resolution["segment_id"] == spec["id"]),
                    "candidate_id": selected["candidate_id"],
                    "before_candidate_sha256": selected["candidate_sha256"],
                    "before_resolution_sha256": next(resolution["resolution_sha256"] for resolution in resolutions if resolution["segment_id"] == spec["id"]),
                    "reviewer_instance": reviewer_instance,
                    "rationale": rationale.strip(),
                }
            )
        # Requiring selected bboxes not to overlap prevents a reviewer from
        # turning an ambiguous OCR layout into two silently colliding headings.
        selected_candidates = [next(candidate for candidate in candidates if candidate["candidate_id"] == override["candidate_id"]) for override in overrides]
        for index, left in enumerate(selected_candidates):
            for right in selected_candidates[index + 1:]:
                if left["physical_page"] != right["physical_page"]:
                    continue
                if _overlap_ratio(left["bbox"], right["bbox"]) > 0:
                    raise ScanOcrError("scan_structure_review_bbox_overlap")
        apply_locator_overrides(candidates, resolutions, overrides, source_sha256)
        for resolution in resolutions:
            selected = next(candidate for candidate in candidates if candidate["candidate_id"] == resolution["selected_candidate_id"])
            segment_binding = dict(review_binding or {})
            input_hashes = segment_binding.get("review_input_sha256")
            candidate_hashes = segment_binding.get("candidate_sha256")
            segment_binding["review_input_sha256"] = (
                input_hashes.get(resolution["segment_id"])
                if isinstance(input_hashes, Mapping)
                else input_hashes
            )
            segment_binding["candidate_sha256"] = (
                candidate_hashes.get(selected["candidate_id"])
                if isinstance(candidate_hashes, Mapping)
                else candidate_hashes
            )
            resolution["scan_review"] = {
                "source_sha256": source_sha256,
                **segment_binding,
                "title_confirmed": True,
                "page_range_confirmed": True,
                "reading_order_confirmed": True,
                "bbox_confirmed": True,
                "evidence_confirmed": True,
            }
            resolution["resolution_sha256"] = sha256_json({key: value for key, value in resolution.items() if key != "resolution_sha256"})
    accepted = review is not None
    output_segments = []
    for spec in specs:
        resolution = next(row for row in resolutions if row["segment_id"] == spec["id"])
        selected = next((row for row in candidates if row.get("candidate_id") == resolution.get("selected_candidate_id")), None)
        output_segments.append(
            {
                **spec,
                "heading_resolution_id": resolution["resolution_id"],
                "candidate_ids": list(resolution["candidate_ids"]),
                "selected_candidate_id": resolution.get("selected_candidate_id"),
                "page_range": [spec["physical_pages"][0], spec["physical_pages"][-1]],
                "bbox_pdf_points": selected["scan_locator"]["bbox_pdf_points"] if selected else None,
                "scan_locator": selected["scan_locator"] if selected else None,
                "review_status": "accepted" if accepted else "review-required",
            }
        )
    structure = {
        "schema_version": SCANNED_PDF_STRUCTURE_REVIEW_SCHEMA,
        "protocol": SCANNED_PDF_STRUCTURE_REVIEW_PROTOCOL,
        "source_sha256": source_sha256,
        "scope": {"start_page": int(scope["start_page"]), "end_page": int(scope["end_page"])},
        "status": "accepted" if accepted else "review-required",
        "segments": output_segments,
        "reviewer": (
            dict(review_binding or {})
            if accepted
            else None
        ),
        "candidate_only": True,
        "promotable": False,
    }
    structure["structure_sha256"] = sha256_json(structure)
    return {
        "structure": structure,
        "segments": output_segments,
        "candidates": sorted(candidates, key=lambda row: (int(row["physical_page"]), int(row["char_span"][0]), row["candidate_id"])),
        "resolutions": sorted(resolutions, key=lambda row: (int(row["structure_order"]), row["segment_id"])),
        "overrides": overrides,
        "page_texts": page_texts,
    }


def build_scanned_pdf_ir(
    source_path: Path,
    start_page: int = 1,
    end_page: int | None = None,
    *,
    primary_backend: str = PRIMARY_BACKEND,
    challenger_backend: str | None = None,
    baseline_backend: str | None = None,
    pdftoppm: Path | str = "pdftoppm",
    render_dpi: int = 200,
    minimum_native_characters: int = 40,
    confidence_threshold: float = 0.80,
    primary_model_root: Path | None = None,
    challenger_model_root: Path | None = None,
    baseline_model_root: Path | None = None,
    backend_config: Mapping[str, Any] | None = None,
    fake_fixture: Mapping[str, Any] | None = None,
    selected_pages: Sequence[int] | None = None,
    raster_regions: Sequence[Mapping[str, Any]] | None = None,
    split_contract: Mapping[str, Any] | None = None,
    spread_split_contract: Mapping[str, Any] | None = None,
    checkpoint: Mapping[str, Any] | None = None,
    scan_checkpoint: Mapping[str, Any] | None = None,
    scan_structure_review: Mapping[str, Any] | None = None,
    structure_review: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run explicit OCR backend(s) on OCR-required pages and return candidate IR."""

    selection = validate_backend_selection(primary_backend, challenger_backend, baseline_backend)
    if selected_pages is not None:
        requested_pages = sorted(set(int(page) for page in selected_pages))
        if not requested_pages:
            raise ScanOcrError("selected_pages_empty")
        if requested_pages != list(range(requested_pages[0], requested_pages[-1] + 1)):
            raise ScanOcrError("non_contiguous_page_selection_unsupported_for_ocr_scope")
        if start_page != requested_pages[0] or (end_page is not None and end_page != requested_pages[-1]):
            raise ScanOcrError("selected_pages_scope_mismatch")
        end_page = requested_pages[-1]
        start_page = requested_pages[0]
    if split_contract is not None and spread_split_contract is not None:
        raise ScanOcrError("spread_split_contract_duplicate_inputs")
    split_contract = split_contract if split_contract is not None else spread_split_contract
    if checkpoint is not None and scan_checkpoint is not None:
        raise ScanOcrError("scan_checkpoint_duplicate_inputs")
    checkpoint = checkpoint if checkpoint is not None else scan_checkpoint
    source_path = source_path.expanduser().resolve()
    if not source_path.is_file():
        raise ScanOcrError(f"source_missing: {source_path}")
    if primary_backend == "fake":
        # The fake worker is a closed-world fixture boundary, never a parser.
        # Requiring a fixture makes the test-only intent explicit even for
        # callers that use the lower-level scan API without a Phase 7D job.
        if fake_fixture is None:
            raise ScanOcrError("fake_backend_requires_explicit_synthetic_fixture")
        source_text = source_path.as_posix().casefold()
        if any(marker in source_text for marker in ("/private-releases/", "/.backups/", "benchmark", "ebooks for test", "/release/", "/releases/")):
            raise ScanOcrError("fake_fixture_real_source_or_release_forbidden")
    elif fake_fixture is not None:
        raise ScanOcrError("fake_fixture_requires_fake_primary_backend")
    native = compile_pdf_intermediate_representation(
        source_path,
        start_page,
        end_page,
        minimum_native_characters,
        "all",
        "pypdf",
        pdftoppm,
        render_dpi,
        False,
        False,
        None,
    )
    source_hash = native["source"]["sha256"]
    preflight = native["preflight"]
    scope = preflight["scope"]
    ocr_pages = sorted(preflight.get("page_routing", {}).get("ocr_required_pages", []))
    manual_pages = sorted(preflight.get("page_routing", {}).get("manual_review_pages", []))
    if preflight.get("pages_needing_ocr") and not ocr_pages and manual_pages:
        # A blank/unusable page must remain a human route; it is never silently
        # sent to OCR merely because native extraction was short.
        ocr_pages = []
    render_by_page = {
        row["physical_page"]: row
        for row in native["render_receipts"]
        if isinstance(row, dict)
    }
    if ocr_pages and set(render_by_page) != set(range(scope["start_page"], scope["end_page"] + 1)):
        raise ScanOcrError("scan_render_receipt_coverage_invalid")
    fixture = dict(fake_fixture or {})
    model_roots = {
        primary_backend: primary_model_root,
        **({challenger_backend: challenger_model_root} if challenger_backend else {}),
        **({baseline_backend: baseline_model_root} if baseline_backend else {}),
    }
    selected_backends = [name for name in selection.values() if name is not None]
    observations: list[dict[str, Any]] = []
    transcripts: list[dict[str, Any]] = []
    backend_receipts: list[dict[str, Any]] = []
    transforms: list[dict[str, Any]] = []
    worker_responses: list[dict[str, Any]] = []
    visual_candidates: list[dict[str, Any]] = []
    visual_gaps: list[dict[str, Any]] = []
    visual_conflicts: list[dict[str, Any]] = []
    result_adapters: list[dict[str, Any]] = []
    page_dimensions_by_page = {
        int(row["physical_page"]): row["page_dimensions_points"]
        for row in native["parser_receipt"]["pages"]
    }
    raster_result: dict[str, Any] | None = None
    primary_configuration = _effective_config(primary_backend, backend_config)
    split_contracts: list[dict[str, Any]] = []
    if split_contract is not None:
        validate_spread_split_contract(split_contract, require_bound=False)
        if split_contract.get("source_sha256") != source_hash:
            raise ScanOcrError("spread_split_source_sha256_mismatch")
        split_page = split_contract.get("physical_page")
        if split_page not in set(int(page) for page in (selected_pages or range(start_page, int(end_page or start_page) + 1))):
            raise ScanOcrError("spread_split_page_not_selected")
        configured_rotation = int(primary_configuration.get("preprocessing", {}).get("content_rotation_clockwise", 0))
        if configured_rotation != int(split_contract.get("rotation")):
            raise ScanOcrError("spread_split_rotation_config_mismatch")
        if raster_regions is not None:
            raise ScanOcrError("spread_split_raster_region_duplicate_inputs")
        raster_regions = list(split_contract.get("regions", []))
        split_contracts = [dict(split_contract)]
    if checkpoint is not None and not raster_regions:
        raise ScanOcrError("scan_checkpoint_requires_raster_regions")
    if checkpoint is not None and len(selected_backends) != 1:
        raise ScanOcrError("scan_checkpoint_requires_single_backend")
    if not raster_regions and ocr_pages and selected_backends == [PADDLE_OCR_BACKEND]:
        raster_regions = _full_page_ocr_regions(
            source_sha256=source_hash,
            pages=ocr_pages,
            page_geometries=_page_geometry_receipts(source_path, ocr_pages),
            render_receipts=render_by_page,
            content_rotation_clockwise=int(primary_configuration.get("preprocessing", {}).get("content_rotation_clockwise", 0)),
        )
    if raster_regions:
        if not selected_backends:
            raise ScanOcrError("raster_region_backend_required")
        region_rows = [dict(row) for row in raster_regions if isinstance(row, Mapping)]
        if len(region_rows) != len(raster_regions):
            raise ScanOcrError("raster_region_shape_invalid")
        page_geometries = _page_geometry_receipts(source_path, [int(row.get("physical_page", 0)) for row in region_rows])
        selected_set = set(int(page) for page in (selected_pages or range(start_page, int(end_page or start_page) + 1)))
        for index, region in enumerate(region_rows):
            if int(region.get("physical_page", 0)) not in selected_set:
                raise ScanOcrError(f"raster_region_page_not_selected:{index}")
            if region.get("coordinate_space") != COORDINATE_SPACE:
                raise ScanOcrError(f"raster_region_coordinate_space_invalid:{index}")
            anchor = region.get("source_anchor")
            if not isinstance(anchor, Mapping) or anchor.get("source_sha256") != source_hash:
                raise ScanOcrError(f"raster_region_source_anchor_invalid:{index}")
        effective_configuration = primary_configuration
        workspace_value = effective_configuration.get("user_config", {}).get("phase7d1_workspace")
        workspace = Path(workspace_value).expanduser().resolve() if isinstance(workspace_value, str) and workspace_value else Path(__file__).resolve().parents[3] / "tmp" / "workspace" / "output" / "phase7d1" / source_hash[:16]
        if _paths_overlap(source_path, workspace):
            raise ScanOcrError("raster_region_input_workspace_overlap")
        combined: dict[str, Any] = {"ocr_observations": [], "ocr_transcripts": [], "ocr_backend_receipts": [], "coordinate_transforms": [], "ocr_worker_responses": [], "worker_errors": [], "raster_regions": [], "runtime_contracts": [], "batch_receipts": [], "table_grids": [], "table_grid_bindings": [], "visual_candidates": [], "visual_gaps": [], "visual_conflicts": [], "result_adapters": [], "render_records": [], "checkpoint": None}
        for backend in selected_backends:
            configuration = _effective_config(backend, backend_config)
            result = _run_raster_region_backend(
                source_path=source_path,
                source_sha256=source_hash,
                regions=region_rows,
                backend=backend,
                model_root=model_roots.get(backend),
                configuration=configuration,
                pdftoppm=pdftoppm,
                render_dpi=render_dpi,
                page_geometries=page_geometries,
                expected_render_receipts=render_by_page,
                workspace_root=workspace / backend,
                split_contract=split_contract,
                checkpoint=checkpoint if backend == primary_backend else None,
            )
            for key in ("ocr_observations", "ocr_transcripts", "ocr_backend_receipts", "coordinate_transforms", "ocr_worker_responses", "visual_candidates", "visual_gaps", "visual_conflicts", "result_adapters"):
                combined[key].extend(result.get(key, []))
            combined["raster_regions"].extend(result.get("raster_regions", []))
            combined["table_grids"].extend(result.get("table_grids", []))
            combined["table_grid_bindings"].extend(result.get("table_grid_bindings", []))
            if result.get("runtime_contract") is not None:
                combined["runtime_contracts"].append(result["runtime_contract"])
            if result.get("batch_receipt") is not None:
                combined["batch_receipts"].append(result["batch_receipt"])
            combined["worker_errors"].extend(result.get("batch_errors", []))
            combined["render_records"] = result.get("render_records", [])
            if result.get("checkpoint") is not None:
                combined["checkpoint"] = result["checkpoint"]
        raster_result = combined
        observations = list(combined["ocr_observations"])
        transcripts = list(combined["ocr_transcripts"])
        backend_receipts = list(combined["ocr_backend_receipts"])
        transforms = list(combined["coordinate_transforms"])
        worker_responses = list(combined["ocr_worker_responses"])
        visual_candidates = list(combined["visual_candidates"])
        visual_gaps = list(combined["visual_gaps"])
        visual_conflicts = list(combined["visual_conflicts"])
        result_adapters = list(combined["result_adapters"])
        ocr_pages = sorted({int(row["physical_page"]) for row in region_rows})
    if ocr_pages and not raster_regions and any(backend != "fake" for backend in selected_backends):
        if any(model_roots.get(backend) is None for backend in selected_backends if backend != "fake"):
            raise ScanOcrError("model_root_required")
        raise ScanOcrError("paddle_canonical_requires_explicit_raster_regions")
    if ocr_pages and not raster_regions:
        for backend in selected_backends:
            model = _model_identity(backend, model_roots.get(backend))
            configuration = _effective_config(backend, backend_config)
            requests: list[dict[str, Any]] = []
            render_paths_data: list[tuple[dict[str, Any], Path]] = []
            with _render_pages(
                source_path,
                ocr_pages,
                pdftoppm,
                dpi=render_dpi,
                pdf_dimensions=page_dimensions_by_page,
            ) as (render_records, render_paths):
                if [row["physical_page"] for row in render_records] != ocr_pages:
                    raise ScanOcrError(f"scan_render_page_order_invalid: backend={backend}")
                for render_record in render_records:
                    page = render_record["physical_page"]
                    render_path = render_paths[page]
                    image_data = render_path.read_bytes()
                    image_dimensions = _png_dimensions(image_data) or {
                        "width": max(1, int(round(page_dimensions_by_page[page]["width"] * render_dpi / 72))),
                        "height": max(1, int(round(page_dimensions_by_page[page]["height"] * render_dpi / 72))),
                    }
                    preprocessing = configuration.get("preprocessing", {})
                    transform = build_default_coordinate_transform(
                        source_sha256=source_hash,
                        physical_page=page,
                        render_sha256=render_record["render_sha256"],
                        render_dpi=render_dpi,
                        image_dimensions_px=image_dimensions,
                        pdf_dimensions_points=page_dimensions_by_page[page],
                        preprocessing=preprocessing,
                    )
                    if not any(row["id"] == transform["id"] for row in transforms):
                        transforms.append(transform)
                    request = {
                        "request_id": _stable_id("ocrq", source_hash, backend, page, render_record["render_sha256"]),
                        "backend_id": backend,
                        "physical_page": page,
                        "render": {
                            "sha256": render_record["render_sha256"],
                            "dpi": render_dpi,
                            "format": "png",
                            "image_dimensions_px": image_dimensions,
                        },
                        "coordinate_transform": transform,
                        "configuration": configuration,
                        "model": {
                            "identity_sha256": model["identity_sha256"],
                            "files_sha256": model["files_sha256"],
                            "model_id": model["model_id"],
                        },
                        "image_path": str(render_path),
                    }
                    if backend == "fake":
                        request["fixture_observations"] = _fixture_for_page(fixture, backend, page)
                    elif backend in fixture:
                        # A fixture for a real backend is not an execution path;
                        # only the test-only fake backend may consume fixtures.
                        raise ScanOcrError("fixture_requires_fake_backend")
                    if backend != "fake":
                        request["model_root"] = str(model_roots[backend].expanduser().resolve()) if model_roots.get(backend) else None
                    requests.append(request)
                    render_paths_data.append((render_record, render_path))
                responses = _run_worker(backend, requests)
                for request, response in zip(requests, responses):
                    page = request["physical_page"]
                    render_record = next(row for row in render_records if row["physical_page"] == page)
                    transform = next(
                        row
                        for row in transforms
                        if row["physical_page"] == page
                        and row["render_sha256"] == render_record["render_sha256"]
                    )
                    image_dimensions = request["render"]["image_dimensions_px"]
                    rows, page_transcripts = _normalize_observations(
                        source_sha256=source_hash,
                        physical_page=page,
                        backend=backend,
                        role=_backend_role(backend),
                        response=response,
                        render_receipt=render_record,
                        transform=transform,
                        model=model,
                        configuration=configuration,
                        image_dimensions=image_dimensions,
                    )
                    backend_receipt_payload = {
                        "source_sha256": source_hash,
                        "physical_page": page,
                        "backend_id": backend,
                        "role": _backend_role(backend),
                        "render_sha256": render_record["render_sha256"],
                        "model_identity_sha256": model["identity_sha256"],
                        "configuration_sha256": sha256_json(configuration),
                        "coordinate_transform_id": transform["id"],
                        "runtime_contract_sha256": sha256_json(
                            {
                                "protocol": "synthetic-scan-runtime-v1",
                                "backend_id": backend,
                                "worker_sha256": sha256_file(Path(__file__).with_name(WORKER_NAME)),
                                "configuration_sha256": sha256_json(configuration),
                                "model_identity_sha256": model["identity_sha256"],
                            }
                        ) if backend == "fake" else None,
                        "worker_sha256": sha256_file(Path(__file__).with_name(WORKER_NAME)),
                        "qualification_receipt_sha256": _qualification_receipt_sha256(configuration, backend),
                    }
                    receipt_id = _stable_id("obr", backend_receipt_payload)
                    for row in rows:
                        row["backend_receipt_id"] = receipt_id
                    backend_receipt = {
                        "schema_version": OCR_BACKEND_RECEIPT_SCHEMA,
                        "id": receipt_id,
                        "source_sha256": source_hash,
                        "physical_page": page,
                        "backend_id": backend,
                        "role": _backend_role(backend),
                        "engine": {
                            "id": str(response.get("engine") or BACKEND_SPECS[backend]["engine"]),
                            "version": str(response.get("engine_version") or "unknown"),
                        },
                        "protocol": {
                            "schema_version": OCR_PROTOCOL_SCHEMA,
                            "version": OCR_PROTOCOL_VERSION,
                            "transport": "jsonl-subprocess",
                        },
                        "render": {
                            "source_sha256": source_hash,
                            "physical_page": page,
                            "render_sha256": render_record["render_sha256"],
                            "renderer": render_record["renderer"],
                        },
                        "model": model,
                        "configuration": configuration,
                        "configuration_sha256": sha256_json(configuration),
                        "coordinate_transform_id": transform["id"],
                        "runtime_contract_sha256": backend_receipt_payload.get("runtime_contract_sha256"),
                        "qualification_receipt_sha256": backend_receipt_payload.get("qualification_receipt_sha256"),
                        "worker": {
                            "script": WORKER_NAME,
                            "script_sha256": sha256_file(Path(__file__).with_name(WORKER_NAME)),
                            "worker_sha256": sha256_file(Path(__file__).with_name(WORKER_NAME)),
                            "runtime_contract_sha256": backend_receipt_payload.get("runtime_contract_sha256"),
                            "process_isolated": True,
                            "network_policy": "disabled",
                            "ambient_credentials_passed": False,
                        },
                        "observation_ids_sha256": sha256_json([row["id"] for row in rows]),
                        "observation_count": len(rows),
                        "status": "candidate-only",
                        "canonical_promotion_allowed": False,
                    }
                    backend_receipts.append(backend_receipt)
                    observations.extend(rows)
                    transcripts.extend(page_transcripts)
                    worker_responses.append(
                        {
                            "schema_version": OCR_PROTOCOL_SCHEMA,
                            "request_id": request["request_id"],
                            "backend_id": backend,
                            "physical_page": page,
                            "status": response.get("status"),
                            "engine": response.get("engine"),
                            "engine_version": response.get("engine_version"),
                            "raw_observations": response.get("observations", []),
                        }
                    )
    reader_page_texts: dict[int, str] = {}
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(source_path))
        reader_page_texts = {
            page: normalize_text(reader.pages[page - 1].extract_text() or "")
            for page in range(scope["start_page"], scope["end_page"] + 1)
        }
    except Exception as error:
        raise ScanOcrError("native_text_recheck_failed") from error
    conflicts = build_conflict_ledger(observations, transcripts, native_texts=reader_page_texts)
    quality_preflight = preflight
    if raster_regions:
        quality_preflight = json.loads(json.dumps(preflight, ensure_ascii=False))
        quality_preflight.setdefault("page_routing", {})["ocr_required_pages"] = list(ocr_pages)
        quality_preflight["page_routing"]["manual_review_pages"] = list(manual_pages)
    quality = build_quality_routing(
        preflight=quality_preflight,
        observations=observations,
        transcripts=transcripts,
        conflicts=conflicts,
        confidence_threshold=confidence_threshold,
    )
    scan_render_receipts = [render_by_page[page] for page in ocr_pages]
    if scan_structure_review is not None and structure_review is not None:
        raise ScanOcrError("scan_structure_review_duplicate_inputs")
    supplied_structure_review = scan_structure_review if scan_structure_review is not None else structure_review
    structure_result: dict[str, Any] | None = None
    if supplied_structure_review is not None:
        if not isinstance(supplied_structure_review, Mapping):
            raise ScanOcrError("scan_structure_review_invalid")
        structure_result = build_scan_structure_review(
            source_id=str(native["source"]["source_id"]),
            source_sha256=source_hash,
            scope=scope,
            primary_backend=primary_backend,
            observations=observations,
            transcripts=transcripts,
            transforms=transforms,
            backend_receipts=backend_receipts,
            render_receipts=native["render_receipts"],
            request=supplied_structure_review,
        )
        proposed_segments = [
            {
                **segment,
                "has_children": False,
                "printed_page_label_candidates": [str(page) for page in segment["physical_pages"]],
                "heading_locator_resolution_id": next(
                    row["resolution_id"]
                    for row in structure_result["resolutions"]
                    if row["segment_id"] == segment["id"]
                ),
            }
            for segment in structure_result["segments"]
        ]
        native["document_map"] = dict(native["document_map"])
        native["document_map"]["structure_source"] = (
            "bounded-scan-ocr-reviewed"
            if structure_result["structure"]["status"] == "accepted"
            else "bounded-scan-ocr-candidate"
        )
        native["document_map"]["segments"] = proposed_segments
        native["anchor_candidates"] = _build_scan_anchor_candidates(
            native["source"]["source_id"],
            proposed_segments,
            structure_result["page_texts"],
            reviewed=structure_result["structure"]["status"] == "accepted",
        )
        native["heading_candidates"] = structure_result["candidates"]
        native["heading_resolutions"] = structure_result["resolutions"]
        native["heading_locator_overrides"] = structure_result["overrides"]
        native["source"]["pdf_ir_receipts"] = {
            **dict(native["source"].get("pdf_ir_receipts") or {}),
            "document_map_sha256": sha256_json(native["document_map"]),
            "anchor_candidates_sha256": sha256_json(native["anchor_candidates"]),
            "heading_candidates_sha256": sha256_json(native["heading_candidates"]),
            "heading_resolutions_sha256": sha256_json(native["heading_resolutions"]),
            "scan_structure_review_sha256": structure_result["structure"]["structure_sha256"],
            **(
                {"heading_locator_overrides_sha256": sha256_json(native["heading_locator_overrides"])}
                if native["heading_locator_overrides"]
                else {}
            ),
        }
    fingerprint = scan_input_fingerprint(
        source_sha256=source_hash,
        render_receipts=scan_render_receipts,
        backend_receipts=backend_receipts,
        coordinate_transforms=transforms,
        raster_regions=(raster_result or {}).get("raster_regions", []) if raster_result else [],
        runtime_contracts=(raster_result or {}).get("runtime_contracts", []) if raster_result else [],
        table_grids=(raster_result or {}).get("table_grids", []) if raster_result else [],
        table_grid_bindings=(raster_result or {}).get("table_grid_bindings", []) if raster_result else [],
        visual_candidates=visual_candidates,
        visual_gaps=visual_gaps,
        visual_conflicts=visual_conflicts,
        result_adapters=result_adapters,
        reviewed_structure=structure_result["structure"] if structure_result is not None else None,
        spread_split_contracts=split_contracts,
    )
    scan_ir = {
        "schema_version": SCAN_IR_SCHEMA,
        "compiler_version": SCAN_PDF_COMPILER_VERSION,
        "source_id": native["source"]["source_id"],
        "source_sha256": source_hash,
        "scope": scope,
        "native_text_first": True,
        "native_text_extractor": native["parser_receipt"]["canonical_text_extractor"],
        "backend_selection": {
            **selection,
            "fallback_policy": "forbidden",
            "selection_is_explicit": True,
        },
        "backend_roles": {name: BACKEND_SPECS[name]["role"] for name in selected_backends},
        "ocr_pages": ocr_pages,
        "native_pages_preserved": [
            page
            for page in range(scope["start_page"], scope["end_page"] + 1)
            if page not in ocr_pages and page not in manual_pages
        ],
        "manual_review_pages": manual_pages,
        "render": {
            "renderer_id": "pdftoppm",
            "dpi": render_dpi,
            "receipt_pages": ocr_pages,
            "receipt_sha256": sha256_json(scan_render_receipts),
        },
        "raster_regions": (raster_result or {}).get("raster_regions", []) if raster_result else [],
        **({"spread_split_contracts": split_contracts} if split_contracts else {}),
        "runtime_contracts": (raster_result or {}).get("runtime_contracts", []) if raster_result else [],
        "batch_receipts": (raster_result or {}).get("batch_receipts", []) if raster_result else [],
        "table_grids": (raster_result or {}).get("table_grids", []) if raster_result else [],
        "table_grid_bindings": (raster_result or {}).get("table_grid_bindings", []) if raster_result else [],
        "visual_candidates": visual_candidates,
        "visual_gaps": visual_gaps,
        "visual_conflicts": visual_conflicts,
        "result_adapters": result_adapters,
        **({"scan_checkpoint": (raster_result or {}).get("checkpoint")} if (raster_result or {}).get("checkpoint") is not None else {}),
        "input_fingerprint": fingerprint,
        "quality_routing_sha256": sha256_json(quality),
        "candidate_policy": {
            "ocr_observations_are_candidates": True,
            "canonical_anchor_creation": "forbidden",
            "canonical_object_creation": "forbidden",
            "formula_normalization": "visual-and-semantic-review-required",
            "formula_executable": False,
            "review_attestation": "existing-independent-semantic-and-visual-review-gates-required",
        },
        **(
            {"reviewed_scan_structure": structure_result["structure"]}
            if structure_result is not None
            else {}
        ),
        "file_refs": {},
        "component_sha256": {},
    }
    return {
        **native,
        "scan_ir": scan_ir,
        "ocr_observations": observations,
        "ocr_transcripts": transcripts,
        "ocr_backend_receipts": sorted(backend_receipts, key=lambda row: row["id"]),
        "coordinate_transforms": sorted(transforms, key=lambda row: row["id"]),
        "ocr_conflicts": conflicts,
        "ocr_quality_routing": quality,
        "ocr_worker_responses": worker_responses,
        "ocr_worker_errors": list((raster_result or {}).get("worker_errors", [])) if raster_result else [],
        "table_grids": (raster_result or {}).get("table_grids", []) if raster_result else [],
        "table_grid_bindings": (raster_result or {}).get("table_grid_bindings", []) if raster_result else [],
        "visual_candidates": (raster_result or {}).get("visual_candidates", []) if raster_result else [],
        "visual_gaps": (raster_result or {}).get("visual_gaps", []) if raster_result else [],
        "visual_conflicts": (raster_result or {}).get("visual_conflicts", []) if raster_result else [],
        "result_adapters": (raster_result or {}).get("result_adapters", []) if raster_result else [],
        **(
            {
                "reviewed_scan_structure": structure_result["structure"],
                "scan_structure_candidates": structure_result["candidates"],
                "scan_structure_resolutions": structure_result["resolutions"],
            }
            if structure_result is not None
            else {}
        ),
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for value in values),
        encoding="utf-8",
    )


def write_scanned_pdf_ir(output: Path, result: Mapping[str, Any]) -> None:
    """Write native and scan IR files, then hash-lock every component in source.json."""

    output = output.expanduser().resolve()
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise ScanOcrError(f"output_not_empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "preflight.json", result["preflight"])
    _write_json(output / "document-map.json", result["document_map"])
    _write_jsonl(output / "anchor-candidates.jsonl", result["anchor_candidates"])
    _write_json(output / "visual-review.json", result["visual_review"])
    _write_json(output / "parser-receipt.json", result["parser_receipt"])
    _write_jsonl(output / "layout-candidates.jsonl", result["layout_candidates"])
    _write_jsonl(output / "render-receipts.jsonl", result["render_receipts"])
    # Keep the v0.3 PDF-IR heading locator receipts available to proposal-time
    # scan workpacks.  They are still canonical native-text bindings; OCR
    # observations below remain candidate-only and cannot replace them.
    _write_jsonl(output / "heading-candidates.jsonl", result.get("heading_candidates", []))
    _write_jsonl(output / "heading-resolutions.jsonl", result.get("heading_resolutions", []))
    if result.get("heading_locator_overrides"):
        _write_jsonl(
            output / "heading-locator-overrides.jsonl",
            result["heading_locator_overrides"],
        )
    if result.get("heading_overrides"):
        _write_jsonl(output / "heading-overrides.jsonl", result["heading_overrides"])
    scan_files = {
        "ocr_observations": "ocr-observations.jsonl",
        "ocr_transcripts": "ocr-transcripts.jsonl",
        "ocr_backend_receipts": "ocr-backend-receipts.jsonl",
        "coordinate_transforms": "coordinate-transforms.jsonl",
        "ocr_conflicts": "ocr-conflict-ledger.jsonl",
        "ocr_quality_routing": "ocr-quality-routing.json",
        "ocr_worker_responses": "ocr-worker-responses.jsonl",
        "visual_candidates": "visual-candidates.jsonl",
        "visual_gaps": "visual-gaps.jsonl",
        "visual_conflicts": "visual-conflicts.jsonl",
        "result_adapters": "paddle-result-adapters.jsonl",
    }
    _write_jsonl(output / scan_files["ocr_observations"], result["ocr_observations"])
    _write_jsonl(output / scan_files["ocr_transcripts"], result["ocr_transcripts"])
    _write_jsonl(output / scan_files["ocr_backend_receipts"], result["ocr_backend_receipts"])
    _write_jsonl(output / scan_files["coordinate_transforms"], result["coordinate_transforms"])
    _write_jsonl(output / scan_files["ocr_conflicts"], result["ocr_conflicts"])
    _write_json(output / scan_files["ocr_quality_routing"], result["ocr_quality_routing"])
    _write_jsonl(output / scan_files["ocr_worker_responses"], result["ocr_worker_responses"])
    _write_jsonl(output / scan_files["visual_candidates"], result.get("visual_candidates", []))
    _write_jsonl(output / scan_files["visual_gaps"], result.get("visual_gaps", []))
    _write_jsonl(output / scan_files["visual_conflicts"], result.get("visual_conflicts", []))
    _write_jsonl(output / scan_files["result_adapters"], result.get("result_adapters", []))
    scan_ir = dict(result["scan_ir"])
    checkpoint = result.get("scan_checkpoint")
    if not isinstance(checkpoint, Mapping):
        checkpoint = scan_ir.get("scan_checkpoint") if isinstance(scan_ir.get("scan_checkpoint"), Mapping) else None
    if isinstance(checkpoint, Mapping):
        _write_json(output / "scan-checkpoint.json", checkpoint)
        os.chmod(output / "scan-checkpoint.json", 0o600)
        scan_ir["scan_checkpoint"] = {
            key: json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))
            for key, value in checkpoint.items()
            if key not in {"regions", "batch_receipt"}
        }
        scan_ir["scan_checkpoint"]["regions"] = [
            {
                key: value
                for key, value in row.items()
                if key not in {"artifact"}
            }
            for row in checkpoint.get("regions", [])
            if isinstance(row, Mapping)
        ]
        if isinstance(checkpoint.get("batch_receipt"), Mapping):
            scan_ir["scan_checkpoint"]["batch_receipt_sha256"] = sha256_json(checkpoint["batch_receipt"])
    scan_ir["file_refs"] = scan_files
    scan_ir["component_sha256"] = {
        key: sha256_file(output / filename) for key, filename in scan_files.items()
    }
    validate_scanned_pdf_ir_schema(scan_ir, require_bound_components=True)
    _write_json(output / "scan-ir.json", scan_ir)
    source = dict(result["source"])
    receipts = dict(source.get("pdf_ir_receipts") or {})
    receipts["scan_ir"] = "scan-ir.json"
    receipts["scan_ir_sha256"] = sha256_file(output / "scan-ir.json")
    receipts["scan_component_sha256"] = dict(scan_ir["component_sha256"])
    if isinstance(checkpoint, Mapping):
        receipts["scan_checkpoint"] = "scan-checkpoint.json"
        receipts["scan_checkpoint_sha256"] = sha256_file(output / "scan-checkpoint.json")
    receipts["scan_receipt_status"] = "partial-worker-errors" if result.get("ocr_worker_errors") else "complete"
    source["pdf_ir_receipts"] = receipts
    _write_json(output / "source.json", source)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as error:
        raise ScanOcrError(f"invalid_jsonl: {path.name}") from error


def _validate_legacy_scan_ir_readonly(scan_ir: Mapping[str, Any]) -> None:
    """Validate the minimum immutable shape of a legacy scan IR.

    Legacy input is intentionally not upgraded in memory or on disk.  The
    current formal schema remains strict; this small check only permits an
    operator to inspect an older, already hash-bound input explicitly.
    """

    required = (
        "schema_version", "compiler_version", "source_id", "source_sha256", "scope",
        "native_text_first", "backend_selection", "ocr_pages", "native_pages_preserved",
        "manual_review_pages", "render", "input_fingerprint", "quality_routing_sha256",
        "candidate_policy", "file_refs", "component_sha256",
    )
    if any(key not in scan_ir for key in required):
        raise ScanOcrError("scan_ir_legacy_readonly_shape_invalid")
    if (
        scan_ir.get("schema_version") != SCAN_IR_SCHEMA
        or scan_ir.get("compiler_version") not in READONLY_COMPATIBLE_SCAN_PDF_COMPILER_VERSIONS
    ):
        raise ScanOcrError("scan_ir_legacy_readonly_version_invalid")
    if not _SHA256_RE.fullmatch(str(scan_ir.get("source_sha256"))) or not _SHA256_RE.fullmatch(str(scan_ir.get("input_fingerprint"))):
        raise ScanOcrError("scan_ir_legacy_readonly_hash_invalid")
    scope = scan_ir.get("scope")
    if (
        not isinstance(scope, Mapping)
        or not isinstance(scope.get("start_page"), int)
        or not isinstance(scope.get("end_page"), int)
        or scope["start_page"] < 1
        or scope["end_page"] < scope["start_page"]
    ):
        raise ScanOcrError("scan_ir_legacy_readonly_scope_invalid")


def _load_scan_ir(ir_root: Path, *, allow_legacy_readonly: bool = False) -> dict[str, Any]:
    root = ir_root.expanduser().resolve()
    scan_path = root / "scan-ir.json"
    if not scan_path.is_file():
        return {}
    scan_ir = _load_json(scan_path)
    if not isinstance(scan_ir, dict) or scan_ir.get("schema_version") != SCAN_IR_SCHEMA:
        raise ScanOcrError("scan_ir_schema_invalid")
    legacy_readonly = scan_ir.get("compiler_version") in READONLY_COMPATIBLE_SCAN_PDF_COMPILER_VERSIONS
    if legacy_readonly:
        if not allow_legacy_readonly:
            raise ScanOcrError("scan_ir_legacy_readonly_explicit_opt_in_required")
        _validate_legacy_scan_ir_readonly(scan_ir)
    else:
        validate_scanned_pdf_ir_schema(scan_ir, require_bound_components=True)
    files = scan_ir.get("file_refs")
    if not isinstance(files, dict) or set(files) != {
        "ocr_observations",
        "ocr_transcripts",
        "ocr_backend_receipts",
        "coordinate_transforms",
        "ocr_conflicts",
        "ocr_quality_routing",
        "ocr_worker_responses",
        "visual_candidates",
        "visual_gaps",
        "visual_conflicts",
        "result_adapters",
    }:
        raise ScanOcrError("scan_ir_file_refs_invalid")
    for filename in files.values():
        if not isinstance(filename, str) or Path(filename).name != filename or not (root / filename).is_file():
            raise ScanOcrError("scan_ir_component_missing")
    checkpoint = None
    if isinstance(scan_ir.get("scan_checkpoint"), Mapping):
        checkpoint_path = root / "scan-checkpoint.json"
        if not checkpoint_path.is_file():
            raise ScanOcrError("scan_checkpoint_missing")
        checkpoint = _load_json(checkpoint_path)
        try:
            validate_scan_checkpoint(checkpoint)
        except ScanOcrError:
            raise
        refs = _load_json(root / "source.json").get("pdf_ir_receipts", {}) if (root / "source.json").is_file() else {}
        if refs.get("scan_checkpoint_sha256") != sha256_file(checkpoint_path) or scan_ir.get("scan_checkpoint", {}).get("checkpoint_sha256") != checkpoint.get("checkpoint_sha256"):
            raise ScanOcrError("scan_checkpoint_source_binding_mismatch")
    return {
        "root": root,
        "scan_ir": scan_ir,
        "legacy_readonly": legacy_readonly,
        "observations": _load_jsonl(root / files["ocr_observations"]),
        "transcripts": _load_jsonl(root / files["ocr_transcripts"]),
        "backend_receipts": _load_jsonl(root / files["ocr_backend_receipts"]),
        "transforms": _load_jsonl(root / files["coordinate_transforms"]),
        "conflicts": _load_jsonl(root / files["ocr_conflicts"]),
        "quality": _load_json(root / files["ocr_quality_routing"]),
        "worker_responses": _load_jsonl(root / files["ocr_worker_responses"]),
        "visual_candidates": _load_jsonl(root / files["visual_candidates"]),
        "visual_gaps": _load_jsonl(root / files["visual_gaps"]),
        "visual_conflicts": _load_jsonl(root / files["visual_conflicts"]),
        "result_adapters": _load_jsonl(root / files["result_adapters"]),
        "checkpoint": checkpoint,
    }


def load_scanned_pdf_ir_readonly(ir_root: Path) -> dict[str, Any]:
    """Load current or explicitly supported legacy scan IR without writing it."""

    return _load_scan_ir(ir_root, allow_legacy_readonly=True)


def verify_scanned_pdf_ir(
    source_path: Path,
    ir_root: Path,
    *,
    pdftoppm: Path | str | None = "pdftoppm",
    model_roots: Mapping[str, Path | None] | None = None,
    backend_config: Mapping[str, Any] | None = None,
    allow_legacy_readonly: bool = False,
) -> dict[str, Any]:
    """Verify scan locks and report stale render/model/config dependencies."""

    loaded = _load_scan_ir(ir_root, allow_legacy_readonly=allow_legacy_readonly)
    if not loaded:
        return {"passed": True, "present": False, "verification_level": "not-present", "failures": [], "stale_reasons": []}
    root = loaded["root"]
    scan_ir = loaded["scan_ir"]
    checkpoint = loaded.get("checkpoint")
    source_record = _load_json(root / "source.json")
    source_path = source_path.expanduser().resolve()
    actual_source_sha256 = sha256_file(source_path)
    failures: list[str] = []
    stale_reasons: list[str] = []
    verification_level = "legacy-readonly-integrity" if loaded.get("legacy_readonly") else "scan-integrity"
    if scan_ir.get("source_sha256") != actual_source_sha256:
        failures.append("scan_source_sha256_mismatch")
        stale_reasons.append("source")
    if source_record.get("sha256") != actual_source_sha256:
        failures.append("source_sha256_mismatch")
    refs = source_record.get("pdf_ir_receipts", {})
    if refs.get("scan_ir_sha256") != sha256_file(root / "scan-ir.json"):
        failures.append("scan_ir_hash_mismatch")
    component_hashes = scan_ir.get("component_sha256")
    if not isinstance(component_hashes, dict):
        failures.append("scan_component_hashes_missing")
        component_hashes = {}
    for key, expected in component_hashes.items():
        filename = scan_ir.get("file_refs", {}).get(key)
        if not isinstance(filename, str) or not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected):
            failures.append(f"scan_component_receipt_invalid:{key}")
            continue
        actual = sha256_file(root / filename)
        if actual != expected:
            failures.append(f"scan_component_hash_mismatch:{key}")
            stale_reasons.append(key)
    if refs.get("scan_component_sha256") != component_hashes:
        failures.append("scan_component_source_binding_mismatch")
    observations = loaded["observations"]
    transcripts = loaded["transcripts"]
    backend_receipts = loaded["backend_receipts"]
    transforms = loaded["transforms"]
    visual_candidates = loaded["visual_candidates"]
    visual_gaps = loaded["visual_gaps"]
    visual_conflicts = loaded["visual_conflicts"]
    result_adapters = loaded["result_adapters"]
    quality = loaded["quality"]
    if scan_ir.get("native_text_first") is not True:
        failures.append("scan_native_text_first_missing")
    if scan_ir.get("compiler_version") != SCAN_PDF_COMPILER_VERSION:
        failures.append("scan_compiler_version_mismatch")
    if scan_ir.get("backend_selection", {}).get("fallback_policy") != "forbidden":
        failures.append("scan_fallback_policy_invalid")
    preflight = _load_json(root / "preflight.json")
    raster_rows = scan_ir.get("raster_regions", [])
    if not isinstance(raster_rows, list):
        failures.append("scan_raster_region_receipt_invalid")
        raster_rows = []
    split_contracts = scan_ir.get("spread_split_contracts", [])
    if not isinstance(split_contracts, list):
        failures.append("scan_spread_split_contracts_invalid")
        split_contracts = []
    for split_index, split in enumerate(split_contracts, start=1):
        try:
            validate_spread_split_contract(split, require_bound=False)
            if split.get("source_sha256") != actual_source_sha256:
                raise ScanOcrError("spread_split_source_sha256_mismatch")
        except ScanOcrError as error:
            failures.append(f"scan_spread_split_contract_invalid:{split_index}:{error}")
    runtime_contracts = scan_ir.get("runtime_contracts", [])
    if not isinstance(runtime_contracts, list):
        failures.append("scan_runtime_contract_receipt_invalid")
        runtime_contracts = []
    runtime_contract_by_backend = {
        str(row.get("backend_id")): row
        for row in runtime_contracts
        if isinstance(row, Mapping) and row.get("backend_id")
    }
    expected_ocr_pages = sorted(
        {int(row.get("physical_page")) for row in raster_rows if isinstance(row, Mapping) and isinstance(row.get("physical_page"), int)}
    ) if raster_rows else sorted(preflight.get("page_routing", {}).get("ocr_required_pages", []))
    if scan_ir.get("ocr_pages") != expected_ocr_pages:
        failures.append("scan_ocr_page_route_mismatch")
    if set(scan_ir.get("manual_review_pages", [])) & set(scan_ir.get("ocr_pages", [])):
        failures.append("scan_manual_page_ocr_overlap")
    render_receipts = _load_jsonl(root / "render-receipts.jsonl")
    render_by_page = {row.get("physical_page"): row for row in render_receipts}
    for split_index, split in enumerate(split_contracts, start=1):
        if not isinstance(split, Mapping):
            continue
        split_page = split.get("physical_page")
        split_render = split.get("render")
        if split_page not in render_by_page or not isinstance(split_render, Mapping) or split_render.get("sha256") != render_by_page.get(split_page, {}).get("render_sha256") or split.get("source_sha256") != actual_source_sha256:
            failures.append(f"scan_spread_split_render_binding_invalid:{split_index}")
        if not any(
            isinstance(region, Mapping)
            and region.get("split_contract_sha256") == split.get("contract_sha256")
            and region.get("region_id") in {item.get("region_id") for item in split.get("regions", []) if isinstance(item, Mapping)}
            for region in raster_rows
        ):
            failures.append(f"scan_spread_split_region_binding_missing:{split_index}")
    transforms_by_id = {row.get("id"): row for row in transforms}
    transcripts_by_id = {row.get("id"): row for row in transcripts}
    receipt_by_id = {row.get("id"): row for row in backend_receipts}
    if len(transcripts_by_id) != len(transcripts) or len(transforms_by_id) != len(transforms) or len(receipt_by_id) != len(backend_receipts):
        failures.append("scan_duplicate_ids")
    for index, row in enumerate(transforms, start=1):
        if row.get("schema_version") != OCR_COORDINATE_TRANSFORM_SCHEMA:
            failures.append(f"scan_transform_schema_invalid:{index}")
        page = row.get("physical_page")
        if row.get("source_sha256") != actual_source_sha256 or page not in expected_ocr_pages:
            failures.append(f"scan_transform_source_or_page_invalid:{index}")
        render = render_by_page.get(page)
        if not isinstance(render, dict) or row.get("render_sha256") != render.get("render_sha256"):
            failures.append(f"scan_transform_render_binding_invalid:{index}")
        try:
            matrix = _matrix(row.get("matrix_3x3"))
            inverse = _matrix(row.get("inverse_matrix_3x3"))
            if abs(_determinant(matrix)) < 1e-12 or abs(_determinant(inverse)) < 1e-12:
                raise ScanOcrError("coordinate_transform_not_invertible")
            dimensions = row.get("pdf_dimensions_points", {})
            image_dimensions = row.get("image_dimensions_px", {})
            if min(
                _finite(dimensions.get("width")),
                _finite(dimensions.get("height")),
                _finite(image_dimensions.get("width")),
                _finite(image_dimensions.get("height")),
            ) <= 0:
                raise ScanOcrError("coordinate_dimensions_invalid")
        except (ScanOcrError, TypeError, ValueError):
            failures.append(f"scan_transform_matrix_invalid:{index}")
    transform_by_id = {str(row.get("id")): row for row in transforms if isinstance(row, Mapping)}
    for index, region in enumerate(raster_rows, start=1):
        if not isinstance(region, Mapping):
            failures.append(f"scan_raster_region_invalid:{index}")
            continue
        if region.get("render_sha256") != render_by_page.get(region.get("physical_page"), {}).get("render_sha256"):
            failures.append(f"scan_raster_region_render_binding_invalid:{index}")
        if not _SHA256_RE.fullmatch(str(region.get("crop_sha256", ""))):
            failures.append(f"scan_raster_region_crop_hash_invalid:{index}")
        transform = transform_by_id.get(str(region.get("coordinate_transform_id")))
        if not isinstance(transform, Mapping) or transform.get("preprocessing", {}).get("crop_sha256") != region.get("crop_sha256"):
            failures.append(f"scan_raster_region_transform_binding_invalid:{index}")
    observations_by_page: dict[int, list[dict[str, Any]]] = {}
    for index, row in enumerate(observations, start=1):
        path = f"ocr-observations.jsonl:{index}"
        if row.get("schema_version") != OCR_OBSERVATION_SCHEMA:
            failures.append(f"scan_observation_schema_invalid:{index}")
        if row.get("source_sha256") != actual_source_sha256:
            failures.append(f"scan_observation_source_mismatch:{index}")
        if row.get("physical_page") not in expected_ocr_pages:
            failures.append(f"scan_observation_native_page:{index}")
        if row.get("candidate_only") is not True or row.get("promotion_status") != "candidate-only":
            failures.append(f"scan_observation_not_candidate_only:{index}")
        if row.get("canonical_anchor_id") is not None or row.get("canonical_object_id") is not None or row.get("review_status") != "unreviewed":
            failures.append(f"scan_observation_premature_promotion:{index}")
        if not isinstance(row.get("confidence"), (int, float)) or not 0 <= row["confidence"] <= 1:
            failures.append(f"scan_observation_confidence_invalid:{index}")
        if row.get("coordinate_transform_id") not in transforms_by_id:
            failures.append(f"scan_observation_transform_missing:{index}")
        if row.get("transcript_span_id") not in transcripts_by_id:
            failures.append(f"scan_observation_transcript_missing:{index}")
        if row.get("backend_receipt_id") not in receipt_by_id:
            failures.append(f"scan_observation_backend_receipt_missing:{index}")
        page = row.get("physical_page")
        render = row.get("render", {})
        expected_render = render_by_page.get(page, {})
        if (
            row.get("canonical_render_sha256") != expected_render.get("render_sha256")
            or not isinstance(render, dict)
            or render.get("dpi") != expected_render.get("renderer", {}).get("dpi")
            or row.get("coordinate_transform_id")
            not in transforms_by_id
            or transforms_by_id.get(row.get("coordinate_transform_id"), {}).get("render_sha256")
            != row.get("canonical_render_sha256")
        ):
            failures.append(f"scan_observation_render_binding_invalid:{index}")
        raster_binding = row.get("raw_observation", {}).get("raster_region")
        if raster_rows:
            if not isinstance(raster_binding, Mapping):
                failures.append(f"scan_observation_raster_binding_missing:{index}")
            elif not any(
                raster_binding.get("region_id") == region.get("region_id")
                and raster_binding.get("crop_sha256") == region.get("crop_sha256")
                for region in raster_rows
                if isinstance(region, Mapping)
            ):
                failures.append(f"scan_observation_raster_binding_invalid:{index}")
        model = row.get("model", {})
        if not isinstance(model, dict) or not _SHA256_RE.fullmatch(str(model.get("identity_sha256", ""))) or not _SHA256_RE.fullmatch(str(model.get("files_sha256", ""))):
            failures.append(f"scan_observation_model_receipt_invalid:{index}")
        receipt = receipt_by_id.get(row.get("backend_receipt_id"), {})
        receipt_model = receipt.get("model") if isinstance(receipt, dict) else None
        if (
            isinstance(receipt, dict)
            and (
                receipt.get("configuration_sha256") != row.get("configuration_sha256")
                or not isinstance(receipt_model, dict)
                or receipt_model.get("identity_sha256") != model.get("identity_sha256")
            )
        ):
            failures.append(f"scan_observation_backend_binding_invalid:{index}")
        observations_by_page.setdefault(row.get("physical_page"), []).append(row)
    for index, row in enumerate(transcripts, start=1):
        if row.get("schema_version") != OCR_TRANSCRIPT_SCHEMA:
            failures.append(f"scan_transcript_schema_invalid:{index}")
        if row.get("source_sha256") != actual_source_sha256 or row.get("physical_page") not in expected_ocr_pages:
            failures.append(f"scan_transcript_source_or_page_invalid:{index}")
        raw_text = row.get("raw_text")
        normalized_text = row.get("normalized_text")
        span = row.get("span", {})
        if (
            not isinstance(raw_text, str)
            or not isinstance(normalized_text, str)
            or row.get("raw_text_sha256") != sha256_text(raw_text)
            or row.get("normalized_text_sha256") != sha256_text(normalized_text)
            or not isinstance(span, dict)
            or not isinstance(span.get("start"), int)
            or not isinstance(span.get("end"), int)
            or span.get("offset_unit") != "unicode-code-point"
            or span["start"] < 0
            or span["end"] <= span["start"]
        ):
            failures.append(f"scan_transcript_integrity_invalid:{index}")
    for row in backend_receipts:
        if row.get("schema_version") != OCR_BACKEND_RECEIPT_SCHEMA:
            failures.append(f"scan_backend_receipt_schema_invalid:{row.get('id')}")
        if row.get("source_sha256") != actual_source_sha256:
            failures.append(f"scan_backend_source_mismatch:{row.get('id')}")
        page = row.get("physical_page")
        render = row.get("render", {})
        if page not in render_by_page or render.get("render_sha256") != render_by_page.get(page, {}).get("render_sha256"):
            failures.append(f"scan_backend_render_binding_invalid:{row.get('id')}")
        model = row.get("model", {})
        if not isinstance(model, dict) or not _SHA256_RE.fullmatch(str(model.get("identity_sha256", ""))):
            failures.append(f"scan_backend_model_receipt_invalid:{row.get('id')}")
        if row.get("canonical_promotion_allowed") is not False or row.get("status") != "candidate-only":
            failures.append(f"scan_backend_capability_escalation:{row.get('id')}")
        if row.get("configuration_sha256") != sha256_json(row.get("configuration")):
            failures.append(f"scan_backend_config_hash_invalid:{row.get('id')}")
        if row.get("backend_id") in runtime_contract_by_backend:
            contract = runtime_contract_by_backend[row.get("backend_id")]
            if row.get("runtime_contract_sha256") != contract.get("contract_sha256"):
                failures.append(f"scan_backend_runtime_contract_binding_invalid:{row.get('id')}")
            worker = row.get("worker")
            if not isinstance(worker, Mapping) or worker.get("runtime_contract_sha256") != contract.get("contract_sha256"):
                failures.append(f"scan_backend_worker_contract_binding_invalid:{row.get('id')}")
            if row.get("crop_sha256") != row.get("worker", {}).get("input_crop_sha256"):
                failures.append(f"scan_backend_crop_binding_invalid:{row.get('id')}")
        if row.get("observation_ids_sha256") != sha256_json([obs["id"] for obs in observations_by_page.get(page, []) if obs.get("backend_receipt_id") == row.get("id")]):
            failures.append(f"scan_backend_observation_binding_invalid:{row.get('id')}")
    model_roots = dict(model_roots or {})
    for index, contract in enumerate(runtime_contracts, start=1):
        if not isinstance(contract, Mapping):
            failures.append(f"scan_runtime_contract_invalid:{index}")
            continue
        try:
            validate_runtime_contract(contract)
            inventory = contract.get("runtime_inventory")
            expected_distributions = inventory.get("distributions") if isinstance(inventory, Mapping) else None
            if not isinstance(expected_distributions, Mapping):
                raise PaddleRuntimeContractError("runtime_inventory_distributions_invalid")
            current_inventory = probe_external_runtime(
                Path(str(contract.get("interpreter"))),
                expected_distributions,
            )
            if current_inventory.get("inventory_sha256") != inventory.get("inventory_sha256"):
                raise PaddleRuntimeContractError("runtime_inventory_drift")
        except (PaddleRuntimeContractError, OSError, ValueError) as error:
            failures.append(f"scan_runtime_contract_drift:{contract.get('backend_id')}:{error}")
            stale_reasons.extend(["runtime", "worker", "model"])
    for backend in sorted({row.get("backend_id") for row in backend_receipts}):
        backend_rows = [row for row in backend_receipts if row.get("backend_id") == backend]
        needs_recheck = BACKEND_SPECS.get(backend, {}).get("model_required") or model_roots.get(backend) is not None
        if backend in runtime_contract_by_backend:
            # The external Paddle contract owns interpreter, worker, model and
            # distribution drift checks above.  The legacy model-root receipt
            # is intentionally not used as a substitute for that contract.
            pass
        elif needs_recheck and backend != "fake":
            model_root = model_roots.get(backend)
            if model_root is None:
                failures.append(f"model_identity_recheck_required:{backend}")
                stale_reasons.append("model")
            else:
                try:
                    current = _model_identity(backend, model_root)
                except ScanOcrError:
                    failures.append(f"model_identity_unavailable:{backend}")
                    stale_reasons.append("model")
                else:
                    if any(row.get("model", {}).get("identity_sha256") != current["identity_sha256"] for row in backend_rows):
                        failures.append(f"model_identity_mismatch:{backend}")
                        stale_reasons.append("model")
        elif needs_recheck and backend == "fake" and model_roots.get(backend) is not None:
            try:
                current = _model_identity(backend, model_roots[backend])
            except ScanOcrError:
                failures.append(f"model_identity_unavailable:{backend}")
                stale_reasons.append("model")
            else:
                if any(row.get("model", {}).get("identity_sha256") != current["identity_sha256"] for row in backend_rows):
                    failures.append(f"model_identity_mismatch:{backend}")
                    stale_reasons.append("model")
    if backend_config is not None:
        for backend in sorted({row.get("backend_id") for row in backend_receipts}):
            expected_configuration = _runtime_engine_configuration(_effective_config(backend, backend_config))
            expected_hash = sha256_json(expected_configuration)
            if any(row.get("configuration_sha256") != expected_hash for row in backend_receipts if row.get("backend_id") == backend):
                failures.append(f"scan_configuration_mismatch:{backend}")
                stale_reasons.append("config")
    if checkpoint is not None:
        checkpoint_backends = sorted({str(row.get("backend_id")) for row in backend_receipts})
        checkpoint_backend = checkpoint_backends[0] if len(checkpoint_backends) == 1 else None
        checkpoint_contract = runtime_contract_by_backend.get(checkpoint_backend) if checkpoint_backend else None
        checkpoint_split = split_contracts[0] if len(split_contracts) == 1 and isinstance(split_contracts[0], Mapping) else None
        checkpoint_pages = sorted({int(row.get("physical_page")) for row in raster_rows if isinstance(row, Mapping) and isinstance(row.get("physical_page"), int)})
        checkpoint_renders = {page: render_by_page[page] for page in checkpoint_pages if page in render_by_page}
        try:
            if checkpoint_backend is None or not isinstance(checkpoint_contract, Mapping):
                raise ScanOcrError("scan_checkpoint_runtime_contract_missing")
            checkpoint_configuration = _runtime_engine_configuration(_effective_config(checkpoint_backend, backend_config)) if backend_config is not None else checkpoint_contract.get("configuration")
            if not isinstance(checkpoint_configuration, Mapping):
                raise ScanOcrError("scan_checkpoint_configuration_missing")
            checkpoint_q = next(
                (
                    row.get("qualification_receipt_sha256")
                    for row in backend_receipts
                    if isinstance(row, Mapping) and isinstance(row.get("qualification_receipt_sha256"), str)
                ),
                None,
            )
            expected_checkpoint_bindings = _checkpoint_expected_bindings(
                source_sha256=actual_source_sha256,
                render_by_page=checkpoint_renders,
                split_contract=checkpoint_split,
                rotation=int(checkpoint_configuration.get("preprocessing", {}).get("content_rotation_clockwise", 0)),
                configuration=checkpoint_configuration,
                contract=checkpoint_contract,
                qualification_receipt_sha256=checkpoint_q,
            )
            expected_region_order = [
                str(row.get("region_id"))
                for row in sorted(
                    (row for row in raster_rows if isinstance(row, Mapping)),
                    key=lambda row: (int(row.get("region_order", 0)), str(row.get("region_id", ""))),
                )
            ]
            resume_scan_checkpoint(
                checkpoint,
                expected_bindings=expected_checkpoint_bindings,
                expected_region_order=expected_region_order,
            )
            if checkpoint.get("status") != "complete":
                failures.append("scan_checkpoint_paused")
                stale_reasons.append("checkpoint")
        except ScanOcrError as error:
            failures.append(f"scan_checkpoint_invalid:{error}")
            stale_reasons.append("checkpoint")
    if pdftoppm is not None and expected_ocr_pages:
        try:
            dpi = int(scan_ir.get("render", {}).get("dpi"))
            _, current_renders = build_render_receipts(
                source_path,
                actual_source_sha256,
                expected_ocr_pages,
                pdftoppm,
                dpi=dpi,
            )
        except Exception:
            failures.append("scan_render_recompute_failed")
            stale_reasons.append("render")
        else:
            stored = [render_by_page.get(page) for page in expected_ocr_pages]
            if current_renders != stored:
                failures.append("scan_render_receipt_stale")
                stale_reasons.append("render")
    native_texts: dict[int, str] = {}
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(source_path))
        native_texts = {
            page: normalize_text(reader.pages[page - 1].extract_text() or "")
            for page in range(preflight["scope"]["start_page"], preflight["scope"]["end_page"] + 1)
        }
    except Exception:
        failures.append("scan_native_text_recheck_failed")
    expected_conflicts = build_conflict_ledger(observations, transcripts, native_texts=native_texts)
    if expected_conflicts != loaded["conflicts"]:
        failures.append("scan_conflict_ledger_mismatch")
    expected_quality = build_quality_routing(
        preflight=(
            dict(preflight, page_routing={
                **dict(preflight.get("page_routing") or {}),
                "ocr_required_pages": expected_ocr_pages,
            })
            if raster_rows
            else preflight
        ),
        observations=observations,
        transcripts=transcripts,
        conflicts=loaded["conflicts"],
        confidence_threshold=float(
            next(
                (
                    row.get("confidence_threshold")
                    for row in quality.get("pages", [])
                    if isinstance(row, dict) and isinstance(row.get("confidence_threshold"), (int, float))
                ),
                0.80,
            )
        ),
    )
    if expected_quality != quality:
        failures.append("scan_quality_routing_mismatch")
    transforms_for_fingerprint = transforms
    expected_fingerprint = scan_input_fingerprint(
        source_sha256=actual_source_sha256,
        render_receipts=[render_by_page[page] for page in expected_ocr_pages if page in render_by_page],
        backend_receipts=backend_receipts,
        coordinate_transforms=transforms_for_fingerprint,
        raster_regions=raster_rows,
        runtime_contracts=runtime_contracts,
        table_grids=[row for row in scan_ir.get("table_grids", []) if isinstance(row, Mapping)],
        table_grid_bindings=[row for row in scan_ir.get("table_grid_bindings", []) if isinstance(row, Mapping)],
        visual_candidates=visual_candidates,
        visual_gaps=visual_gaps,
        visual_conflicts=visual_conflicts,
        result_adapters=result_adapters,
        spread_split_contracts=[dict(row) for row in split_contracts if isinstance(row, Mapping)],
        reviewed_structure=scan_ir.get("reviewed_scan_structure"),
    )
    if scan_ir.get("input_fingerprint") != expected_fingerprint:
        failures.append("scan_input_fingerprint_mismatch")
        stale_reasons.append("fingerprint")
    return {
        "passed": not failures,
        "present": True,
        "source_sha256": actual_source_sha256,
        "ocr_pages": expected_ocr_pages,
        "observation_count": len(observations),
        "backend_count": len(backend_receipts),
        "conflict_count": len(loaded["conflicts"]),
        "promotable": False,
        "executable": False,
        "verification_level": verification_level,
        "failures": sorted(set(failures)),
        "stale_reasons": sorted(set(stale_reasons)),
    }


def _ensure_private_empty_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.exists():
        if not resolved.is_dir() or any(resolved.iterdir()):
            raise ScanOcrError(f"output_not_empty: {resolved}")
    else:
        resolved.mkdir(parents=True)
    os.chmod(resolved, 0o700)
    return resolved


def _write_private_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_bytes(value)
    os.chmod(path, 0o600)


def write_spread_split_artifacts(
    output: Path,
    *,
    contract: Mapping[str, Any],
    render_bytes: bytes,
) -> dict[str, Any]:
    """Persist private split previews/crops without persisting the source PDF."""

    output = _ensure_private_empty_output(output)
    validate_spread_split_contract(contract, require_bound=False)
    render = contract["render"]
    if hashlib.sha256(render_bytes).hexdigest() != render.get("sha256"):
        raise ScanOcrError("spread_split_render_hash_mismatch")
    rotated = rotate_png_for_ocr(render_bytes, int(contract["rotation"]))
    if rotated["sha256"] != contract["rotated_render"]["sha256"] or rotated["dimensions_px"] != contract["rotated_render"]["dimensions_px"]:
        raise ScanOcrError("spread_split_rotated_render_binding_mismatch")
    original_path = output / "render-original.png"
    rotated_path = output / "render-rotated.png"
    _write_private_bytes(original_path, render_bytes)
    _write_private_bytes(rotated_path, rotated["bytes"])
    region_paths: list[str] = []
    for region in sorted(contract["regions"], key=lambda row: int(row["region_order"])):
        crop = crop_render_png(
            rotated["bytes"],
            {
                "render_dimensions_px": dict(rotated["dimensions_px"]),
                "render_pixel_bbox": list(region["rotated_region_bbox_px"]),
            },
            expected_crop_sha256=str(region["crop_sha256"]),
        )
        filename = f"region-{int(region['region_order']):03d}.png"
        path = output / "regions" / filename
        _write_private_bytes(path, crop["bytes"])
        region_paths.append(str(path))
    _write_json(output / "split-contract.json", contract)
    os.chmod(output / "split-contract.json", 0o600)
    manifest = {
        "schema_version": "tkc.scanned-pdf-spread-split-artifacts/v0.1",
        "contract_sha256": contract["contract_sha256"],
        "source_sha256": contract["source_sha256"],
        "physical_page": contract["physical_page"],
        "render_original": {"path": str(original_path), "sha256": contract["render"]["sha256"]},
        "render_rotated": {"path": str(rotated_path), "sha256": contract["rotated_render"]["sha256"]},
        "regions": [
            {
                "region_id": row["region_id"],
                "region_order": row["region_order"],
                "path": region_paths[index],
                "sha256": row["crop_sha256"],
                "dimensions_px": row["crop_dimensions_px"],
            }
            for index, row in enumerate(sorted(contract["regions"], key=lambda item: int(item["region_order"])))
        ],
        "candidate_only": True,
        "promotion": False,
    }
    _write_json(output / "split-manifest.json", manifest)
    os.chmod(output / "split-manifest.json", 0o600)
    return {"contract": str(output / "split-contract.json"), "manifest": str(output / "split-manifest.json"), "region_paths": region_paths}


def _backend_config_from_qualification(plan: Mapping[str, Any], receipt: Mapping[str, Any]) -> dict[str, Any]:
    contract = plan.get("runtime_contract")
    if not isinstance(contract, Mapping):
        raise ScanOcrError("qualification_runtime_contract_missing")
    inventory = contract.get("runtime_inventory")
    if not isinstance(inventory, Mapping) or not isinstance(inventory.get("distributions"), Mapping):
        raise ScanOcrError("qualification_runtime_inventory_missing")
    runtime_spec = {
        "backend_id": plan.get("backend_id"),
        "interpreter": contract.get("interpreter"),
        "interpreter_allowlist": contract.get("interpreter_allowlist"),
        "runtime_root": contract.get("runtime_root"),
        "runtime_allowlist": contract.get("runtime_allowlist"),
        "model_root": contract.get("model_root"),
        "model_allowlist": contract.get("model_allowlist"),
        "worker_path": contract.get("worker_path"),
        "worker_allowlist": [contract.get("worker_path")],
        "expected_distributions": inventory.get("distributions"),
        "network_enabled": False,
        "model_download": False,
        "mcp_http_dependency": False,
    }
    configuration = contract.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ScanOcrError("qualification_runtime_configuration_missing")
    backend = str(plan.get("backend_id"))
    return {
        "backend_id": backend,
        "backend_options": json.loads(json.dumps(configuration.get("backend_options") or {}, ensure_ascii=False, sort_keys=True)),
        "preprocessing": json.loads(json.dumps(configuration.get("preprocessing") or {}, ensure_ascii=False, sort_keys=True)),
        "recognition": json.loads(json.dumps(configuration.get("recognition") or {}, ensure_ascii=False, sort_keys=True)),
        "user_config": json.loads(json.dumps(configuration.get("user_config") or {}, ensure_ascii=False, sort_keys=True)),
        "external_runtime": runtime_spec,
        "runtime_qualifications": {backend: json.loads(json.dumps(dict(receipt), ensure_ascii=False, sort_keys=True))},
        "network_policy": "offline-enforced",
        "network_enabled": False,
        "model_download": False,
    }


def _split_command(args: argparse.Namespace) -> dict[str, Any]:
    source = args.source.expanduser().resolve()
    if not source.is_file():
        raise ScanOcrError("source_missing")
    output = _ensure_private_empty_output(args.output)
    source_hash = sha256_file(source)
    geometry = _page_geometry_receipts(source, [args.page])[args.page]
    with _render_pages(
        source,
        [args.page],
        args.pdftoppm,
        dpi=args.render_dpi,
        pdf_dimensions={args.page: geometry},
    ) as (render_records, render_paths):
        render = render_records[0]
        render_bytes = render_paths[args.page].read_bytes()
        contract = build_spread_split_contract(
            source_sha256=source_hash,
            physical_page=args.page,
            page_geometry=geometry,
            render_receipt=render,
            render_bytes=render_bytes,
            render_dpi=args.render_dpi,
            content_rotation_clockwise=args.content_rotation_clockwise,
            boundary_px=args.boundary_px,
            printed_page_label_candidates=[args.left_label, args.right_label],
        )
    paths = write_spread_split_artifacts(output, contract=contract, render_bytes=render_bytes)
    return {
        "passed": True,
        "status": "split",
        "contract_sha256": contract["contract_sha256"],
        "split_core_sha256": contract["split_core_sha256"],
        "source_sha256": source_hash,
        "physical_page": args.page,
        "rotation": args.content_rotation_clockwise,
        "region_count": len(contract["regions"]),
        **paths,
    }


def _bind_split_command(args: argparse.Namespace) -> dict[str, Any]:
    source_contract = _load_json(args.split_contract.expanduser().resolve())
    plan = _load_json(args.plan.expanduser().resolve())
    receipt = _load_json(args.receipt.expanduser().resolve())
    try:
        validate_spread_split_contract(source_contract, require_bound=False)
    except ScanOcrError:
        raise
    qualification_tool = plan.get("qualification_tool") if isinstance(plan, Mapping) else None
    if not isinstance(qualification_tool, Mapping):
        raise ScanOcrError("qualification_plan_tool_invalid")
    from paddle_runtime_qualification import validate_qualification_plan, validate_runtime_qualification

    validate_qualification_plan(plan, replay_local=True)
    validate_runtime_qualification(
        receipt,
        contract=plan["runtime_contract"],
        plan=plan,
        backend_id=str(plan["backend_id"]),
        profile_id=str(plan["profile_id"]),
        require_qualified=True,
    )
    plan_split = plan.get("split_contract")
    if not isinstance(plan_split, Mapping) or plan_split.get("split_core_sha256") != source_contract.get("split_core_sha256"):
        raise ScanOcrError("qualification_split_core_mismatch")
    contract = bind_spread_split_contract(
        plan_split,
        runtime_commitments=plan_split.get("runtime_commitments"),
        qualification_commitments={"plan_sha256": plan.get("plan_sha256"), "receipt_sha256": receipt.get("receipt_sha256")},
        require_qualified=True,
    )
    output = _ensure_private_empty_output(args.output)
    _write_json(output / "split-contract.json", contract)
    os.chmod(output / "split-contract.json", 0o600)
    backend_config = _backend_config_from_qualification(plan, receipt)
    _write_json(output / "backend-config.json", backend_config)
    os.chmod(output / "backend-config.json", 0o600)
    return {
        "passed": True,
        "status": "bound",
        "contract": str(output / "split-contract.json"),
        "backend_config": str(output / "backend-config.json"),
        "contract_sha256": contract["contract_sha256"],
        "split_core_sha256": contract["split_core_sha256"],
        "qualification_plan_sha256": plan["plan_sha256"],
        "qualification_receipt_sha256": receipt["receipt_sha256"],
        "region_count": len(contract["regions"]),
    }


def _add_build_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("source", type=Path)
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--end-page", type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--backend", default=PRIMARY_BACKEND, choices=tuple(BACKEND_SPECS))
    parser.add_argument("--challenger-backend", choices=tuple(BACKEND_SPECS))
    parser.add_argument("--baseline-backend", choices=tuple(BACKEND_SPECS))
    parser.add_argument("--model-root", type=Path)
    parser.add_argument("--challenger-model-root", type=Path)
    parser.add_argument("--baseline-model-root", type=Path)
    parser.add_argument("--backend-config", type=Path)
    parser.add_argument("--fake-fixture", type=Path)
    parser.add_argument("--split-contract", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--pdftoppm", type=Path, default=Path("pdftoppm"))
    parser.add_argument("--render-dpi", type=int, default=200)
    parser.add_argument("--minimum-native-characters", type=int, default=40)
    parser.add_argument("--confidence-threshold", type=float, default=0.80)
    parser.add_argument("--structure-review", type=Path, help="Optional bounded scan structure proposal/review JSON.")
    parser.add_argument("--json", action="store_true", dest="json_output")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="Build a candidate-only scanned PDF IR.")
    _add_build_arguments(build)
    resume = subparsers.add_parser("resume", help="Resume a candidate-only scan from a page/region checkpoint.")
    _add_build_arguments(resume)
    split = subparsers.add_parser("split", help="Create a private explicit two-region spread split contract.")
    split.add_argument("--source", required=True, type=Path)
    split.add_argument("--page", required=True, type=int)
    split.add_argument("--output", required=True, type=Path)
    split.add_argument("--pdftoppm", type=Path, default=Path("pdftoppm"))
    split.add_argument("--render-dpi", type=int, default=200)
    split.add_argument("--content-rotation-clockwise", type=int, choices=(0, 90, 180, 270), required=True)
    split.add_argument("--boundary-px", type=int)
    split.add_argument("--left-label")
    split.add_argument("--right-label")
    split.add_argument("--json", action="store_true", dest="json_output")
    bind = subparsers.add_parser("bind-split", help="Bind a split contract to a qualified local runtime receipt.")
    bind.add_argument("--split-contract", required=True, type=Path)
    bind.add_argument("--plan", required=True, type=Path)
    bind.add_argument("--receipt", required=True, type=Path)
    bind.add_argument("--output", required=True, type=Path)
    bind.add_argument("--json", action="store_true", dest="json_output")
    verify = subparsers.add_parser("verify", help="Verify scan receipts and stale dependencies.")
    verify.add_argument("--source", required=True, type=Path)
    verify.add_argument("--ir", required=True, type=Path)
    verify.add_argument("--pdftoppm", type=Path, default=Path("pdftoppm"))
    verify.add_argument("--model-root", type=Path)
    verify.add_argument("--challenger-model-root", type=Path)
    verify.add_argument("--baseline-model-root", type=Path)
    verify.add_argument("--backend-config", type=Path)
    verify.add_argument("--allow-legacy-readonly", action="store_true", help="Read legacy 0.12.0 scan IR without rewriting it.")
    verify.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command in {"build", "resume"}:
            if args.command == "resume" and args.checkpoint is None:
                raise ScanOcrError("scan_checkpoint_required_for_resume")
            config = _load_json(args.backend_config) if args.backend_config else {}
            fixture = _load_fixture(args.fake_fixture)
            structure_review = _load_json(args.structure_review) if args.structure_review else None
            split_contract = _load_json(args.split_contract) if args.split_contract else None
            checkpoint = _load_json(args.checkpoint) if args.checkpoint else None
            result = build_scanned_pdf_ir(
                args.source,
                args.start_page,
                args.end_page,
                primary_backend=args.backend,
                challenger_backend=args.challenger_backend,
                baseline_backend=args.baseline_backend,
                pdftoppm=args.pdftoppm,
                render_dpi=args.render_dpi,
                minimum_native_characters=args.minimum_native_characters,
                confidence_threshold=args.confidence_threshold,
                primary_model_root=args.model_root,
                challenger_model_root=args.challenger_model_root,
                baseline_model_root=args.baseline_model_root,
                backend_config=config,
                fake_fixture=fixture,
                split_contract=split_contract,
                checkpoint=checkpoint,
                scan_structure_review=structure_review,
            )
            write_scanned_pdf_ir(args.output, result)
            worker_errors = list(result.get("ocr_worker_errors", []))
            payload = {
                "passed": not worker_errors,
                "source_sha256": result["source"]["sha256"],
                "classification": result["preflight"]["scope_classification"],
                "ocr_pages": result["scan_ir"]["ocr_pages"],
                "native_pages_preserved": result["scan_ir"]["native_pages_preserved"],
                "manual_review_pages": result["scan_ir"]["manual_review_pages"],
                "backend_selection": result["scan_ir"]["backend_selection"],
                "observation_count": len(result["ocr_observations"]),
                "conflict_count": len(result["ocr_conflicts"]),
                "promotable": False,
                "executable": False,
                "structure_review_status": result["scan_ir"].get("reviewed_scan_structure", {}).get("status") if isinstance(result["scan_ir"].get("reviewed_scan_structure"), Mapping) else None,
                "worker_error_count": len(worker_errors),
                "worker_error_codes": sorted({str(row.get("error_code", "worker_error")) for row in worker_errors if isinstance(row, Mapping)}),
                "checkpoint_status": (result.get("scan_ir", {}).get("scan_checkpoint") or {}).get("status") if isinstance(result.get("scan_ir", {}).get("scan_checkpoint"), Mapping) else None,
                "checkpoint_sha256": (result.get("scan_ir", {}).get("scan_checkpoint") or {}).get("checkpoint_sha256") if isinstance(result.get("scan_ir", {}).get("scan_checkpoint"), Mapping) else None,
                "output": str(args.output.expanduser().resolve()),
            }
        elif args.command == "split":
            payload = _split_command(args)
        elif args.command == "bind-split":
            payload = _bind_split_command(args)
        else:
            config = _load_json(args.backend_config) if args.backend_config else None
            model_roots = {
                PRIMARY_BACKEND: args.model_root,
                CHALLENGER_BACKEND: args.challenger_model_root,
                BASELINE_BACKEND: args.baseline_model_root,
            }
            payload = verify_scanned_pdf_ir(
                args.source,
                args.ir,
                pdftoppm=args.pdftoppm,
                model_roots=model_roots,
                backend_config=config,
                allow_legacy_readonly=args.allow_legacy_readonly,
            )
    except (ScanOcrError, PdfPipelineError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            ("PASS" if payload.get("passed") else "FAIL")
            + f" scanned-pdf-ocr pages={len(payload.get('ocr_pages', []))}"
            + f" observations={payload.get('observation_count', 0)}"
            + f" promotable={payload.get('promotable', False)}"
        )
    return 0 if payload.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
