#!/usr/bin/env python3
"""Candidate-only, parser-independent visual semantics for fake-expert.

The module intentionally keeps visual extraction separate from semantic truth.  It
owns canonical coordinate/hash/provenance records, deterministic candidate
deduplication and conflict reporting, and validation of externally authored visual
review fragments.  It never writes a review verdict, promotes an object, or stores
pixels in a distributable artifact.

The native baseline uses only pypdf's visitor-text API and local pdftoppm.  Docling,
PyMuPDF, and PaddleOCR adapters may provide candidate rows through the same input
contract, but this module does not import or download those optional dependencies.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from compiler_version import (
    TABLE_GRID_SCHEMA,
    VISUAL_ASSURANCE_MANIFEST_SCHEMA,
    VISUAL_CONFLICT_SCHEMA,
    VISUAL_OBJECT_SCHEMA,
    VISUAL_RELATION_SCHEMA,
    VISUAL_REVIEW_ATTESTATION_SCHEMA,
    VISUAL_REVIEW_OVERLAY_SCHEMA,
    VISUAL_REVIEW_PROTOCOL,
    VISUAL_SEMANTICS_PROTOCOL,
)

VISUAL_RECEIPT_SCHEMA = "tkc.visual-receipt/v0.1"
VISUAL_PROTOCOL = VISUAL_SEMANTICS_PROTOCOL
COORDINATE_SPACE = "pdf-page-top-left-points-v1"
STATUS_VALUES = {"candidate", "reviewed", "promoted"}
OBJECT_TYPES = {
    "figure",
    "diagram",
    "plot",
    "table",
    "equation_display",
    "caption",
    "axis",
    "tick",
    "legend",
    "series",
    "table_cell",
    "annotation",
}
RELATION_TYPES = {
    "contains",
    "caption_of",
    "axis_of",
    "legend_maps",
    "series_of",
    "cell_of",
    "labels",
    "supports",
}
CONFLICT_TYPES = {"parser", "locator", "type", "overlap", "duplicate", "missing"}
VISUAL_REVIEW_CHECKS = (
    "source_page_opened",
    "full_page_opened",
    "crop_opened",
    "bbox_checked",
    "object_type_checked",
    "relation_checked",
    "table_grid_checked",
    "input_hash_checked",
)
VISUAL_DIFFERENCE_STATUSES = {"confirmed", "correction-required", "unsupported"}
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,127}$")
NUMBER_TOKEN = r"(?:[A-Za-z]+|[IVXLCDM]+|\d+)(?:[.-](?:[A-Za-z]+|[IVXLCDM]+|\d+))*"
FIGURE_RE = re.compile(r"(?<!\w)(?:Figure|Fig(?:ure)?\.?|图)\s*(" + NUMBER_TOKEN + r")", re.I)
TABLE_RE = re.compile(r"(?<!\w)(?:Table|Tab(?:le)?\.?|表)\s*(" + NUMBER_TOKEN + r")", re.I)
EQUATION_RE = re.compile(r"(?<!\w)(?:Equation|Eq(?:uation)?\.?|式)\s*(" + NUMBER_TOKEN + r")|\((" + NUMBER_TOKEN + r")\)", re.I)
PLOT_MARKER_RE = re.compile(r"(?:\b(?:plot|axis|legend|series|tick)\b|坐标图|坐标轴|图例|曲线|刻度)", re.I)
PLOT_MARKER_LABEL_MAX_CHARS = 64
NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
EQUATION_LABEL_TOKEN_RE = re.compile(r"(?:\d+(?:[.-][A-Za-z0-9]+)*|[IVXLCDM]+(?:[.-][A-Za-z0-9]+)*|[A-Z](?:[.-][A-Za-z0-9]+)+|[A-Z])", re.I)


class VisualSemanticsError(RuntimeError):
    """Stable fail-closed visual contract error."""


@dataclass(frozen=True)
class VisualIssue:
    severity: str
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


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


def stable_id(prefix: str, *parts: Any) -> str:
    return f"{prefix}-{sha256_json(list(parts))[:20]}"


def _receipt_commitment(value: Any) -> Any:
    """Keep semantic receipt identity separate from runtime measurements."""

    if isinstance(value, Mapping):
        has_canonical_request = "canonical_request_sha256" in value
        has_response_content = "worker_response_content_sha256" in value
        return {
            str(key): _receipt_commitment(item)
            for key, item in value.items()
            if str(key) not in {"elapsed_ms", "response_bytes"}
            and not (has_canonical_request and str(key) == "worker_request_sha256")
            and not (has_response_content and str(key) == "worker_response_sha256")
        }
    if isinstance(value, list):
        return [_receipt_commitment(item) for item in value]
    if isinstance(value, tuple):
        return [_receipt_commitment(item) for item in value]
    return value


def stable_visual_id(
    source_sha256: str,
    physical_page: int,
    object_type: str,
    subtype: str,
    *,
    bbox: Sequence[float] | None = None,
    polygon: Sequence[Sequence[float]] | None = None,
    label: str | None = None,
    content_sha256: str | None = None,
    render_sha256: str | None = None,
    render_dpi: int | None = None,
    rotation: int = 0,
    coordinate_space: str = COORDINATE_SPACE,
    crop_sha256: str | None = None,
    parser_receipt: Mapping[str, Any] | None = None,
    backend_receipt: Mapping[str, Any] | None = None,
    model_receipt: Mapping[str, Any] | None = None,
    config_receipt: Mapping[str, Any] | None = None,
    page_geometry: Mapping[str, Any] | None = None,
) -> str:
    """Derive a deterministic canonical visual identity from its full commitment.

    ``candidate_id`` values emitted by an adapter are intentionally not accepted
    as a canonical override.  A canonical object identity includes the mapped
    page geometry and every receipt that can change the interpretation of that
    geometry, including render/crop and parser/backend/model/config bindings.
    """

    commitment = {
        "source_sha256": source_sha256,
        "physical_page": int(physical_page),
        "object_type": object_type,
        "subtype": subtype,
        "coordinate_space": coordinate_space,
        "mapped_pdf_bbox": list(bbox) if bbox is not None else None,
        "mapped_pdf_polygon": [list(point) for point in polygon] if polygon is not None else None,
        "label": label,
        "content_sha256": content_sha256,
        "render_sha256": render_sha256,
        "render_dpi": int(render_dpi) if render_dpi is not None else None,
        "render_rotation": int(rotation) % 360,
        "crop_sha256": crop_sha256,
        "parser_receipt": _receipt_commitment(parser_receipt or {}),
        "backend_receipt": _receipt_commitment(backend_receipt or {}),
        "model_receipt": _receipt_commitment(model_receipt or {}),
        "config_receipt": _receipt_commitment(config_receipt or {}),
        "page_geometry": dict(page_geometry or {}),
    }
    return stable_id("vo", commitment)


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _page_size(page_width: Any, page_height: Any) -> tuple[float, float]:
    if not _finite_number(page_width) or not _finite_number(page_height) or float(page_width) <= 0 or float(page_height) <= 0:
        raise VisualSemanticsError("page_geometry_invalid")
    return float(page_width), float(page_height)


def _transform_point(x: float, y: float, width: float, height: float, coordinate_space: str, rotation: int, dpi: int | None) -> tuple[float, float]:
    space = str(coordinate_space)
    if space in {"pdf-page-bottom-left-points-v1", "pdf-points-bottom-left-v1"}:
        y = height - y
    elif space in {"render-pixels-top-left-v1", "image-pixels-top-left-v1"}:
        if not isinstance(dpi, int) or dpi <= 0:
            raise VisualSemanticsError("render_coordinate_dpi_missing")
        scale = 72.0 / float(dpi)
        x *= scale
        y *= scale
    elif space not in {COORDINATE_SPACE, "pdf-page-top-left-points-v1"}:
        raise VisualSemanticsError(f"coordinate_space_unknown:{space}")
    rotation = int(rotation) % 360
    if rotation == 0:
        return x, y
    if rotation == 90:
        # Render/image coordinates are expressed in the clockwise-rotated
        # viewport. Invert that viewport rotation back into canonical page
        # coordinates (the input extent is height x width).
        return y, height - x
    if rotation == 180:
        return width - x, height - y
    if rotation == 270:
        return width - y, x
    raise VisualSemanticsError("rotation_invalid")


def normalize_polygon(polygon: Sequence[Sequence[Any]], page_width: float, page_height: float, coordinate_space: str = COORDINATE_SPACE, rotation: int = 0, dpi: int | None = None) -> list[list[float]]:
    width, height = _page_size(page_width, page_height)
    if not isinstance(polygon, (list, tuple)) or len(polygon) < 3:
        raise VisualSemanticsError("polygon_invalid")
    points: list[list[float]] = []
    for point in polygon:
        if not isinstance(point, (list, tuple)) or len(point) != 2 or not all(_finite_number(v) for v in point):
            raise VisualSemanticsError("polygon_invalid")
        x, y = _transform_point(float(point[0]), float(point[1]), width, height, coordinate_space, rotation, dpi)
        if x < -1e-6 or y < -1e-6 or x > width + 1e-6 or y > height + 1e-6:
            raise VisualSemanticsError("bbox_page_mismatch")
        points.append([round(max(0.0, min(width, x)), 6), round(max(0.0, min(height, y)), 6)])
    return points


def normalize_bbox(bbox: Sequence[Any], page_width: float, page_height: float, coordinate_space: str = COORDINATE_SPACE, rotation: int = 0, dpi: int | None = None) -> list[float]:
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or not all(_finite_number(v) for v in bbox):
        raise VisualSemanticsError("bbox_invalid")
    x0, y0, x1, y1 = (float(value) for value in bbox)
    corners = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    polygon = normalize_polygon(corners, page_width, page_height, coordinate_space, rotation, dpi)
    xs = [point[0] for point in polygon]
    ys = [point[1] for point in polygon]
    result = [round(min(xs), 6), round(min(ys), 6), round(max(xs), 6), round(max(ys), 6)]
    if result[2] <= result[0] or result[3] <= result[1]:
        raise VisualSemanticsError("bbox_degenerate")
    return result


def _affine_matrix(width: float, height: float, input_space: str, rotation: int, dpi: int | None) -> list[list[float]]:
    scale = 1.0
    matrix = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    if input_space in {"pdf-page-bottom-left-points-v1", "pdf-points-bottom-left-v1"}:
        matrix = [[1.0, 0.0, 0.0], [0.0, -1.0, height], [0.0, 0.0, 1.0]]
    elif input_space in {"render-pixels-top-left-v1", "image-pixels-top-left-v1"}:
        if not isinstance(dpi, int) or dpi <= 0:
            raise VisualSemanticsError("render_coordinate_dpi_missing")
        scale = 72.0 / float(dpi)
        matrix = [[scale, 0.0, 0.0], [0.0, scale, 0.0], [0.0, 0.0, 1.0]]
    elif input_space not in {COORDINATE_SPACE, "pdf-page-top-left-points-v1"}:
        raise VisualSemanticsError(f"coordinate_space_unknown:{input_space}")
    rotation = int(rotation) % 360
    rotations = {
        0: [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        90: [[0.0, 1.0, 0.0], [-1.0, 0.0, height], [0.0, 0.0, 1.0]],
        180: [[-1.0, 0.0, width], [0.0, -1.0, height], [0.0, 0.0, 1.0]],
        270: [[0.0, -1.0, width], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]],
    }
    rotation_matrix = rotations.get(rotation)
    if rotation_matrix is None:
        raise VisualSemanticsError("rotation_invalid")
    # The receipt is evidence for the declared transform.  The point normalizer
    # still evaluates all four corners, so this matrix cannot silently turn a
    # rotated rectangle into a non-rotated bbox.
    return [
        [round(rotation_matrix[0][0] * matrix[0][0] + rotation_matrix[0][1] * matrix[1][0], 9), round(rotation_matrix[0][0] * matrix[0][1] + rotation_matrix[0][1] * matrix[1][1], 9), round(rotation_matrix[0][0] * matrix[0][2] + rotation_matrix[0][1] * matrix[1][2] + rotation_matrix[0][2], 9)],
        [round(rotation_matrix[1][0] * matrix[0][0] + rotation_matrix[1][1] * matrix[1][0], 9), round(rotation_matrix[1][0] * matrix[0][1] + rotation_matrix[1][1] * matrix[1][1], 9), round(rotation_matrix[1][0] * matrix[0][2] + rotation_matrix[1][1] * matrix[1][2] + rotation_matrix[1][2], 9)],
        [0.0, 0.0, 1.0],
    ]


def _inverse_affine(matrix: Sequence[Sequence[float]]) -> list[list[float]]:
    a, b, c = (float(value) for value in matrix[0])
    d, e, f = (float(value) for value in matrix[1])
    determinant = a * e - b * d
    if abs(determinant) < 1e-12:
        raise VisualSemanticsError("coordinate_transform_not_invertible")
    return [[round(e / determinant, 9), round(-b / determinant, 9), round((b * f - e * c) / determinant, 9)], [round(-d / determinant, 9), round(a / determinant, 9), round((d * c - a * f) / determinant, 9)], [0.0, 0.0, 1.0]]


def coordinate_transform_receipt(*, page_width: float, page_height: float, input_space: str, rotation: int = 0, dpi: int | None = None) -> dict[str, Any]:
    width, height = _page_size(page_width, page_height)
    matrix = _affine_matrix(width, height, input_space, rotation, dpi)
    inverse = _inverse_affine(matrix)
    return {
        "schema_version": "tkc.visual-coordinate-transform/v0.1",
        "input_coordinate_space": input_space,
        "output_coordinate_space": COORDINATE_SPACE,
        "page_width": round(width, 6),
        "page_height": round(height, 6),
        "rotation": int(rotation) % 360,
        "dpi": dpi,
        "matrix_3x3": matrix,
        "inverse_matrix_3x3": inverse,
        "roundtrip_check": "affine-inverse-required",
        "matrix_sha256": sha256_json({"input": input_space, "output": COORDINATE_SPACE, "width": width, "height": height, "rotation": int(rotation) % 360, "dpi": dpi, "matrix": matrix, "inverse": inverse}),
    }


def crop_commitment_sha256(render_bytes: bytes, bbox: Sequence[float], *, page_width: float | None = None, page_height: float | None = None, dpi: int = 150) -> str:
    """Hash the rendered crop pixels when possible, with a portable fallback.

    pdftoppm's PNGs are normally 8-bit RGB.  A tiny stdlib decoder keeps the core
    compiler dependency-free; unsupported PNG variants still receive a bound
    commitment over render bytes and geometry rather than an unbound claim.
    """

    try:
        width, height, color_type, pixels = _decode_png_rgb(render_bytes)
        if page_width and page_height and page_width > 0 and page_height > 0:
            x0 = max(0, min(width, int(math.floor(float(bbox[0]) / page_width * width))))
            y0 = max(0, min(height, int(math.floor(float(bbox[1]) / page_height * height))))
            x1 = max(x0 + 1, min(width, int(math.ceil(float(bbox[2]) / page_width * width))))
            y1 = max(y0 + 1, min(height, int(math.ceil(float(bbox[3]) / page_height * height))))
        else:
            x0, y0, x1, y1 = 0, 0, width, height
        channels = 4 if color_type == 6 else 3
        rows = []
        for row in range(y0, y1):
            start = row * width * channels + x0 * channels
            rows.append(pixels[start : row * width * channels + x1 * channels])
        payload = b"tkc.crop/v0.1\0" + canonical_json({"width": x1 - x0, "height": y1 - y0, "channels": channels, "dpi": dpi, "bbox": list(bbox)}) + b"\0" + b"".join(rows)
        return sha256_bytes(payload)
    except (ValueError, zlib.error, IndexError, struct_error):
        return sha256_bytes(b"tkc.crop/fallback-v0.1\0" + render_bytes + b"\0" + canonical_json({"bbox": list(bbox), "dpi": dpi}))


class struct_error(Exception):
    """Internal sentinel used to avoid importing struct for unsupported PNGs."""


def _decode_png_rgb(data: bytes) -> tuple[int, int, int, bytes]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("png_signature_invalid")
    position = 8
    width = height = color_type = bit_depth = None
    idat: list[bytes] = []
    while position + 12 <= len(data):
        length = int.from_bytes(data[position : position + 4], "big")
        kind = data[position + 4 : position + 8]
        payload = data[position + 8 : position + 8 + length]
        crc = data[position + 8 + length : position + 12 + length]
        if len(payload) != length or len(crc) != 4 or zlib.crc32(kind + payload) & 0xFFFFFFFF != int.from_bytes(crc, "big"):
            raise ValueError("png_crc_invalid")
        if kind == b"IHDR":
            width = int.from_bytes(payload[0:4], "big")
            height = int.from_bytes(payload[4:8], "big")
            bit_depth, color_type, compression, filtering, interlace = payload[8:13]
            if bit_depth != 8 or color_type not in {2, 6} or compression != 0 or filtering != 0 or interlace != 0:
                raise struct_error("png_variant_unsupported")
        elif kind == b"IDAT":
            idat.append(payload)
        elif kind == b"IEND":
            break
        position += 12 + length
    if not width or not height or color_type not in {2, 6} or not idat:
        raise ValueError("png_header_missing")
    channels = 4 if color_type == 6 else 3
    row_size = width * channels
    decoded = zlib.decompress(b"".join(idat))
    if len(decoded) != height * (row_size + 1):
        raise ValueError("png_data_size_invalid")
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
                raise ValueError("png_filter_invalid")
        rows.append(bytes(row))
        previous = row
    return width, height, int(color_type), b"".join(rows)


def _render_page(source: Path, page: int, pdftoppm: Path, dpi: int) -> tuple[str, bytes, str]:
    binary = pdftoppm.expanduser().resolve()
    if not binary.is_file():
        resolved = shutil.which(str(pdftoppm))
        if not resolved:
            raise VisualSemanticsError("pdftoppm_missing")
        binary = Path(resolved)
    version_result = subprocess.run([str(binary), "-v"], capture_output=True, text=True, check=False)
    if version_result.returncode not in {0, 1}:  # pdftoppm reports version on stderr with 1 in some builds
        raise VisualSemanticsError("pdftoppm_version_failed")
    version = (version_result.stderr or version_result.stdout).strip().splitlines()[0] if (version_result.stderr or version_result.stdout).strip() else "unknown"
    with tempfile.TemporaryDirectory(prefix="tkc-visual-render-") as temporary:
        target = Path(temporary) / f"page-{page:04d}"
        result = subprocess.run([str(binary), "-f", str(page), "-l", str(page), "-singlefile", "-png", "-r", str(dpi), str(source), str(target)], capture_output=True, text=True, check=False)
        image = target.with_suffix(".png")
        if result.returncode != 0 or not image.is_file():
            raise VisualSemanticsError(f"pdftoppm_failed:page={page}")
        payload = image.read_bytes()
    return version, payload, sha256_bytes(payload)


def render_receipt(*, source_sha256: str, physical_page: int, render_sha256: str, dpi: int, rotation: int = 0, renderer_version: str = "unknown") -> dict[str, Any]:
    if not HASH_RE.fullmatch(str(source_sha256)) or not HASH_RE.fullmatch(str(render_sha256)):
        raise VisualSemanticsError("render_hash_invalid")
    if int(physical_page) < 1 or int(dpi) < 36:
        raise VisualSemanticsError("render_receipt_invalid")
    return {
        "schema_version": "tkc.visual-render-receipt/v0.1",
        "source_sha256": source_sha256,
        "physical_page": int(physical_page),
        "renderer": "pdftoppm",
        "renderer_version": renderer_version,
        "dpi": int(dpi),
        "rotation": int(rotation) % 360,
        "format": "png",
        "render_sha256": render_sha256,
        "render_receipt_sha256": sha256_json({"source_sha256": source_sha256, "physical_page": int(physical_page), "renderer": "pdftoppm", "renderer_version": renderer_version, "dpi": int(dpi), "rotation": int(rotation) % 360, "format": "png", "render_sha256": render_sha256}),
    }


def visual_input_sha256(*, source_sha256: str, physical_page: int, render_sha256: str, render_dpi: int, rotation: int, coordinate_space: str, bbox: Sequence[float], crop_sha256: str | None, parser_receipt: Mapping[str, Any], backend_receipt: Mapping[str, Any], model_receipt: Mapping[str, Any], config_receipt: Mapping[str, Any], page_geometry: Mapping[str, Any] | None = None, attributes: Mapping[str, Any] | None = None, uncertainty: Mapping[str, Any] | None = None) -> str:
    return sha256_json({
        "source_sha256": source_sha256,
        "physical_page": int(physical_page),
        "render_sha256": render_sha256,
        "render_dpi": int(render_dpi),
        "rotation": int(rotation) % 360,
        "coordinate_space": coordinate_space,
        "bbox": list(bbox),
        "crop_sha256": crop_sha256,
        "parser_receipt": _receipt_commitment(parser_receipt),
        "backend_receipt": _receipt_commitment(backend_receipt),
        "model_receipt": _receipt_commitment(model_receipt),
        "config_receipt": _receipt_commitment(config_receipt),
        # Geometry and uncertainty are part of the visual input identity.  A
        # crop/render hash alone cannot detect a changed MediaBox/CropBox
        # interpretation or a changed candidate-only numeric claim.
        "page_geometry": dict(page_geometry or {}),
        "attributes": dict(attributes or {}),
        "uncertainty": dict(uncertainty or {}),
    })


def _object_hash(row: Mapping[str, Any]) -> str:
    return sha256_json({key: value for key, value in row.items() if key != "object_sha256"})


def build_visual_object(*, source_sha256: str, physical_page: int, object_type: str, subtype: str = "", page_width: float, page_height: float, bbox: Sequence[Any], polygon: Sequence[Sequence[Any]] | None = None, coordinate_space: str = COORDINATE_SPACE, rotation: int = 0, render_sha256: str, render_dpi: int, crop_sha256: str | None = None, parent: str | None = None, label: str | None = None, content_sha256: str | None = None, parser_receipt: Mapping[str, Any] | None = None, backend_receipt: Mapping[str, Any] | None = None, model_receipt: Mapping[str, Any] | None = None, config_receipt: Mapping[str, Any] | None = None, page_geometry: Mapping[str, Any] | None = None, attributes: Mapping[str, Any] | None = None, uncertainty: Mapping[str, Any] | None = None, status: str = "candidate", visual_object_id: str | None = None) -> dict[str, Any]:
    if object_type not in OBJECT_TYPES:
        raise VisualSemanticsError("visual_object_type_invalid")
    if status not in STATUS_VALUES:
        raise VisualSemanticsError("visual_object_status_invalid")
    if not HASH_RE.fullmatch(str(source_sha256)) or not HASH_RE.fullmatch(str(render_sha256)):
        raise VisualSemanticsError("visual_object_hash_invalid")
    parser = dict(parser_receipt or {"id": "pypdf-native-baseline", "version": "pypdf-6.10.0", "candidate_only": True})
    backend = dict(backend_receipt or {"id": "reviewer-locator-confirmed-bbox", "candidate_only": True})
    model = dict(model_receipt or {"id": "none", "available": False, "download": False})
    config = dict(config_receipt or {"id": "visual-semantics-default-v0.1", "candidate_only": True})
    geometry = dict(page_geometry or {"media_box": [0.0, 0.0, round(float(page_width), 6), round(float(page_height), 6)], "crop_box": [0.0, 0.0, round(float(page_width), 6), round(float(page_height), 6)], "rotation": int(rotation) % 360, "coordinate_origin": "mediabox-top-left", "physical_page": int(physical_page)})
    canonical_rotation = int(geometry.get("rotation", rotation)) % 360
    normalized_bbox = normalize_bbox(bbox, page_width, page_height, coordinate_space, rotation, render_dpi if coordinate_space in {"render-pixels-top-left-v1", "image-pixels-top-left-v1"} else None)
    normalized_polygon = normalize_polygon(polygon, page_width, page_height, coordinate_space, rotation, render_dpi if coordinate_space in {"render-pixels-top-left-v1", "image-pixels-top-left-v1"} else None) if polygon is not None else None
    object_attributes = dict(attributes or {})
    object_uncertainty = dict(uncertainty or {"level": "candidate", "numeric_values_verified": False})
    canonical_id = stable_visual_id(
        source_sha256,
        physical_page,
        object_type,
        subtype,
        bbox=normalized_bbox,
        polygon=normalized_polygon,
        label=label,
        content_sha256=content_sha256,
        render_sha256=render_sha256,
        render_dpi=render_dpi,
        rotation=canonical_rotation,
        coordinate_space=COORDINATE_SPACE,
        crop_sha256=crop_sha256,
        parser_receipt=parser,
        backend_receipt=backend,
        model_receipt=model,
        config_receipt=config,
        page_geometry=geometry,
    )
    if visual_object_id is not None and str(visual_object_id) != canonical_id:
        raise VisualSemanticsError("visual_object_id_commitment_mismatch")
    object_id = canonical_id
    row: dict[str, Any] = {
        "schema_version": VISUAL_OBJECT_SCHEMA,
        "visual_object_id": object_id,
        "type": object_type,
        "subtype": subtype,
        "label": label,
        "parent": parent,
        "source_sha256": source_sha256,
        "physical_page": int(physical_page),
        "canonical_render_sha256": render_sha256,
        "canonical_render_dpi": int(render_dpi),
        "canonical_render_rotation": canonical_rotation,
        "coordinate_space": COORDINATE_SPACE,
        "page_width": round(float(page_width), 6),
        "page_height": round(float(page_height), 6),
        "bbox": normalized_bbox,
        "polygon": normalized_polygon,
        "page_geometry": geometry,
        "attributes": object_attributes,
        "uncertainty": object_uncertainty,
        "crop_sha256": crop_sha256,
        "parser_receipt": parser,
        "backend_receipt": backend,
        "model_receipt": model,
        "config_receipt": config,
        "content_sha256": content_sha256,
        "status": status,
    }
    row["input_sha256"] = visual_input_sha256(source_sha256=source_sha256, physical_page=physical_page, render_sha256=render_sha256, render_dpi=render_dpi, rotation=canonical_rotation, coordinate_space=COORDINATE_SPACE, bbox=normalized_bbox, crop_sha256=crop_sha256, parser_receipt=parser, backend_receipt=backend, model_receipt=model, config_receipt=config, page_geometry=geometry, attributes=object_attributes, uncertainty=object_uncertainty)
    row["object_sha256"] = _object_hash(row)
    if parent is not None and str(parent) == str(row["visual_object_id"]):
        raise VisualSemanticsError("visual_object_self_parent")
    return row


def build_visual_relation(*, source_sha256: str, relation_type: str, source_visual_object_id: str, target_visual_object_id: str, physical_page: int, status: str = "candidate", evidence_hashes: Sequence[str] = ()) -> dict[str, Any]:
    if relation_type not in RELATION_TYPES:
        raise VisualSemanticsError("visual_relation_type_invalid")
    if status not in STATUS_VALUES:
        raise VisualSemanticsError("visual_relation_status_invalid")
    if str(source_visual_object_id) == str(target_visual_object_id):
        raise VisualSemanticsError("visual_relation_self_loop")
    row = {
        "schema_version": VISUAL_RELATION_SCHEMA,
        "visual_relation_id": stable_id("vr", source_sha256, relation_type, source_visual_object_id, target_visual_object_id),
        "relation_type": relation_type,
        "source_visual_object_id": source_visual_object_id,
        "target_visual_object_id": target_visual_object_id,
        "source_sha256": source_sha256,
        "physical_page": int(physical_page),
        "evidence_hashes": sorted(set(str(value) for value in evidence_hashes)),
        "status": status,
    }
    # Lifecycle status is deliberately excluded from the semantic commitment:
    # candidate -> reviewed/promoted transitions must not invalidate an
    # attestation that is bound to the relation's actual endpoints/evidence.
    row["relation_sha256"] = sha256_json({key: value for key, value in row.items() if key not in {"relation_sha256", "status"}})
    return row


def build_table_grid(*, source_sha256: str, table_visual_object_id: str, physical_page: int, rows: Sequence[Mapping[str, Any]], columns: Sequence[Mapping[str, Any]], cells: Sequence[Mapping[str, Any]], topology_evidence: Sequence[Mapping[str, Any]] = (), caption_object_ids: Sequence[str] = (), table_note_candidates: Sequence[str] = (), continuation_candidate: Mapping[str, Any] | None = None, status: str = "candidate") -> dict[str, Any]:
    if status not in STATUS_VALUES:
        raise VisualSemanticsError("table_grid_status_invalid")
    normalized_rows: list[dict[str, Any]] = []
    row_ids: set[str] = set()
    row_indices: set[int] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise VisualSemanticsError("table_row_invalid")
        row_id = str(row.get("row_id", ""))
        row_index = row.get("index", index)
        if not row_id or isinstance(row_index, bool) or not isinstance(row_index, int) or row_index < 0 or row_id in row_ids or row_index in row_indices:
            raise VisualSemanticsError("table_row_topology_invalid")
        row_ids.add(row_id)
        row_indices.add(row_index)
        normalized_rows.append({"row_id": row_id, "index": row_index})
    normalized_columns: list[dict[str, Any]] = []
    column_ids: set[str] = set()
    column_indices: set[int] = set()
    for index, column in enumerate(columns):
        if not isinstance(column, Mapping):
            raise VisualSemanticsError("table_column_invalid")
        column_id = str(column.get("column_id", ""))
        column_index = column.get("index", index)
        if not column_id or isinstance(column_index, bool) or not isinstance(column_index, int) or column_index < 0 or column_id in column_ids or column_index in column_indices:
            raise VisualSemanticsError("table_column_topology_invalid")
        column_ids.add(column_id)
        column_indices.add(column_index)
        normalized_columns.append({"column_id": column_id, "index": column_index, "label": column.get("label")})
    if row_indices != set(range(len(normalized_rows))) or column_indices != set(range(len(normalized_columns))):
        raise VisualSemanticsError("table_topology_indices_not_contiguous")
    normalized_cells: list[dict[str, Any]] = []
    occupied: set[tuple[int, int]] = set()
    cell_ids: set[str] = set()
    for index, cell in enumerate(cells):
        if not isinstance(cell, Mapping):
            raise VisualSemanticsError("table_cell_invalid")
        cell_id = str(cell.get("cell_id") or stable_id("tc", source_sha256, table_visual_object_id, index))
        if cell_id in cell_ids:
            raise VisualSemanticsError("table_cell_duplicate")
        cell_ids.add(cell_id)
        row_id = str(cell.get("row_id", ""))
        column_id = str(cell.get("column_id", ""))
        if row_id not in row_ids or column_id not in column_ids:
            raise VisualSemanticsError("table_cell_topology_unresolved")
        row_index = next(row["index"] for row in normalized_rows if row["row_id"] == row_id)
        column_index = next(column["index"] for column in normalized_columns if column["column_id"] == column_id)
        row_span = cell.get("row_span", 1)
        column_span = cell.get("column_span", 1)
        if isinstance(row_span, bool) or not isinstance(row_span, int) or row_span < 1 or isinstance(column_span, bool) or not isinstance(column_span, int) or column_span < 1:
            raise VisualSemanticsError("table_cell_span_invalid")
        if row_index + row_span > len(normalized_rows) or column_index + column_span > len(normalized_columns):
            raise VisualSemanticsError("table_cell_span_out_of_bounds")
        for row_slot in range(row_index, row_index + row_span):
            for column_slot in range(column_index, column_index + column_span):
                slot = (row_slot, column_slot)
                if slot in occupied:
                    raise VisualSemanticsError("table_cell_overlap")
                occupied.add(slot)
        value_candidate = cell.get("value_candidate")
        unit_candidate = cell.get("unit_candidate")
        symbol_candidate = cell.get("symbol_candidate")
        typed_candidates = dict(cell.get("typed_candidates") or {})
        unknown_typed = set(typed_candidates) - {"value", "unit", "symbol", "confidence"}
        if unknown_typed:
            raise VisualSemanticsError("table_cell_typed_candidates_unknown")
        typed_candidates.setdefault("value", value_candidate)
        typed_candidates.setdefault("unit", unit_candidate)
        typed_candidates.setdefault("symbol", symbol_candidate)
        typed_candidates.setdefault("confidence", "candidate")
        bbox = list(cell.get("bbox")) if isinstance(cell.get("bbox"), (list, tuple)) else None
        source_anchor = dict(cell.get("source_anchor") or {})
        unknown_anchor = set(source_anchor) - {"source_sha256", "table_visual_object_id", "cell_id", "physical_page", "bbox", "text_sha256", "anchor_id"}
        if unknown_anchor:
            raise VisualSemanticsError("table_cell_source_anchor_unknown")
        source_anchor.setdefault("source_sha256", source_sha256)
        source_anchor.setdefault("table_visual_object_id", table_visual_object_id)
        source_anchor.setdefault("cell_id", cell_id)
        source_anchor.setdefault("physical_page", int(physical_page))
        source_anchor.setdefault("bbox", bbox)
        text_candidate = cell.get("text_candidate")
        if text_candidate is not None:
            source_anchor.setdefault("text_sha256", sha256_json(str(text_candidate)))
        if source_anchor["source_sha256"] != source_sha256 or source_anchor["table_visual_object_id"] != table_visual_object_id or source_anchor["cell_id"] != cell_id:
            raise VisualSemanticsError("table_cell_source_anchor_mismatch")
        crop_sha256 = cell.get("crop_sha256")
        if crop_sha256 is not None and not HASH_RE.fullmatch(str(crop_sha256)):
            raise VisualSemanticsError("table_cell_crop_hash_invalid")
        normalized_cells.append({
            "cell_id": cell_id,
            "row_id": row_id,
            "column_id": column_id,
            "row_span": row_span,
            "column_span": column_span,
            "text_candidate": text_candidate,
            "value_candidate": value_candidate,
            "unit_candidate": unit_candidate,
            "symbol_candidate": symbol_candidate,
            "bbox": bbox,
            "typed_candidates": typed_candidates,
            "source_anchor": source_anchor,
            "crop_sha256": crop_sha256,
        })
    row = {
        "schema_version": TABLE_GRID_SCHEMA,
        "table_grid_id": stable_id("tg", source_sha256, table_visual_object_id, normalized_rows, normalized_columns, normalized_cells),
        "table_visual_object_id": table_visual_object_id,
        "source_sha256": source_sha256,
        "physical_page": int(physical_page),
        "rows": normalized_rows,
        "columns": normalized_columns,
        "cells": sorted(normalized_cells, key=lambda item: item["cell_id"]),
        "topology_evidence": [dict(value) for value in topology_evidence if isinstance(value, Mapping)],
        "caption_object_ids": sorted(set(str(value) for value in caption_object_ids)),
        "table_note_candidates": [str(value) for value in table_note_candidates],
        "continuation_candidate": dict(continuation_candidate) if isinstance(continuation_candidate, Mapping) else None,
        "status": status,
    }
    # See the relation hash note above; grid lifecycle is not visual content.
    row["grid_sha256"] = sha256_json({key: value for key, value in row.items() if key not in {"grid_sha256", "status"}})
    return row


def rekey_table_grid(
    grid: Mapping[str, Any],
    *,
    table_visual_object_id: str,
    adapter_local_table_visual_object_id: str | None = None,
) -> dict[str, Any]:
    """Atomically rebind a mapped table grid and all cell anchors to its object.

    PP-Structure emits crop-local table IDs before the coordinator knows the
    canonical page-space receipts.  This helper changes the table object ID,
    derives new cell IDs, rewrites anchors, and recomputes the grid commitment
    in one deterministic operation.  The old adapter ID is retained only as
    explicit topology attribution.
    """

    if not isinstance(grid, Mapping):
        raise VisualSemanticsError("table_grid_rekey_input_invalid")
    source_sha256 = str(grid.get("source_sha256", ""))
    physical_page = int(grid.get("physical_page", 0))
    old_table_id = adapter_local_table_visual_object_id or str(grid.get("table_visual_object_id", ""))
    cells: list[dict[str, Any]] = []
    for index, raw_cell in enumerate(grid.get("cells", []) if isinstance(grid.get("cells"), list) else []):
        if not isinstance(raw_cell, Mapping):
            raise VisualSemanticsError("table_grid_rekey_cell_invalid")
        cell = dict(raw_cell)
        new_cell_id = stable_id(
            "cell",
            source_sha256,
            physical_page,
            table_visual_object_id,
            cell.get("row_id"),
            cell.get("column_id"),
            cell.get("row_span"),
            cell.get("column_span"),
            cell.get("bbox"),
            cell.get("text_candidate"),
            index,
        )
        anchor = dict(cell.get("source_anchor") or {})
        anchor["source_sha256"] = source_sha256
        anchor["table_visual_object_id"] = table_visual_object_id
        anchor["cell_id"] = new_cell_id
        anchor["physical_page"] = physical_page
        anchor["bbox"] = cell.get("bbox")
        anchor["anchor_id"] = stable_id("anchor", source_sha256, physical_page, table_visual_object_id, new_cell_id)
        cell["cell_id"] = new_cell_id
        cell["source_anchor"] = anchor
        cells.append(cell)
    evidence = [dict(value) for value in grid.get("topology_evidence", []) if isinstance(value, Mapping)]
    if old_table_id and old_table_id != table_visual_object_id:
        evidence.append({"adapter_local_table_visual_object_id": old_table_id, "binding": "attribution-only"})
    return build_table_grid(
        source_sha256=source_sha256,
        table_visual_object_id=table_visual_object_id,
        physical_page=physical_page,
        rows=[dict(value) for value in grid.get("rows", []) if isinstance(value, Mapping)],
        columns=[dict(value) for value in grid.get("columns", []) if isinstance(value, Mapping)],
        cells=cells,
        topology_evidence=evidence,
        caption_object_ids=[str(value) for value in grid.get("caption_object_ids", [])],
        table_note_candidates=[str(value) for value in grid.get("table_note_candidates", [])],
        continuation_candidate=grid.get("continuation_candidate") if isinstance(grid.get("continuation_candidate"), Mapping) else None,
        status=str(grid.get("status", "candidate")),
    )


def build_visual_conflict(*, source_sha256: str, physical_page: int, conflict_type: str, object_ids: Sequence[str] = (), details: Mapping[str, Any] | None = None, status: str = "candidate") -> dict[str, Any]:
    if conflict_type not in CONFLICT_TYPES:
        raise VisualSemanticsError("visual_conflict_type_invalid")
    row = {
        "schema_version": VISUAL_CONFLICT_SCHEMA,
        "visual_conflict_id": stable_id("vc", source_sha256, physical_page, conflict_type, sorted(set(object_ids)), dict(details or {})),
        "source_sha256": source_sha256,
        "physical_page": int(physical_page),
        "conflict_type": conflict_type,
        "object_ids": sorted(set(str(value) for value in object_ids)),
        "details": dict(details or {}),
        "status": status,
    }
    row["conflict_sha256"] = sha256_json({key: value for key, value in row.items() if key != "conflict_sha256"})
    return row


def build_table_continuation_candidates(objects: Sequence[Mapping[str, Any]], grids: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Emit review-required cross-page continuation hypotheses.

    The compiler records a possible link only when neighbouring pages carry
    compatible table labels or explicit continuation wording.  It never merges
    grids or asserts that the continuation is confirmed.
    """

    table_rows = sorted((dict(row) for row in objects if isinstance(row, Mapping) and row.get("type") == "table"), key=lambda row: (int(row.get("physical_page", 0)), str(row.get("label", ""))))
    grid_by_table = {str(row.get("table_visual_object_id")): row for row in grids if isinstance(row, Mapping)}
    result: list[dict[str, Any]] = []
    for left, right in zip(table_rows, table_rows[1:]):
        left_label = str(left.get("label") or "").casefold()
        right_label = str(right.get("label") or "").casefold()
        consecutive = int(right.get("physical_page", 0)) == int(left.get("physical_page", 0)) + 1
        wording = "continu" in right_label or "continued" in right_label or "续" in right_label
        same_label = left_label and right_label and left_label == right_label
        if not consecutive or not (same_label or wording):
            continue
        left_grid = grid_by_table.get(str(left.get("visual_object_id")))
        right_grid = grid_by_table.get(str(right.get("visual_object_id")))
        row = {
            "schema_version": "tkc.table-continuation-candidate/v0.1",
            "continuation_id": stable_id("tcont", left.get("visual_object_id"), right.get("visual_object_id")),
            "source_sha256": left.get("source_sha256"),
            "from_table_visual_object_id": left.get("visual_object_id"),
            "to_table_visual_object_id": right.get("visual_object_id"),
            "from_table_grid_id": left_grid.get("table_grid_id") if left_grid else None,
            "to_table_grid_id": right_grid.get("table_grid_id") if right_grid else None,
            "physical_pages": [int(left.get("physical_page")), int(right.get("physical_page"))],
            "status": "candidate",
            "requires_external_review": True,
            "auto_merge": False,
            "evidence": {"same_label": bool(same_label), "continuation_wording": bool(wording), "page_gap": int(right.get("physical_page", 0)) - int(left.get("physical_page", 0))},
        }
        row["continuation_sha256"] = sha256_json({key: value for key, value in row.items() if key != "continuation_sha256"})
        result.append(row)
    return sorted(result, key=lambda row: str(row.get("continuation_id")))


def _text_boxes(page: Any, page_height: float, *, media_left: float = 0.0, media_bottom: float = 0.0) -> list[dict[str, Any]]:
    boxes: list[dict[str, Any]] = []
    try:
        def visitor(text: str, cm: Sequence[float], tm: Sequence[float], font_dict: Mapping[str, Any] | None, font_size: float) -> None:
            if not text or not text.strip():
                return
            # pypdf visitor coordinates use the PDF page's native bottom-left
            # coordinate system.  Canonical visual geometry is MediaBox-local
            # top-left points, so an offset MediaBox must be translated before
            # bounds checks and candidate construction.
            x = (float(tm[4]) if len(tm) > 4 and _finite_number(tm[4]) else 0.0) - float(media_left)
            y_bottom = (float(tm[5]) if len(tm) > 5 and _finite_number(tm[5]) else 0.0) - float(media_bottom)
            size = max(1.0, float(font_size) if _finite_number(font_size) else 8.0)
            for line_index, line in enumerate(str(text).splitlines() or [str(text)]):
                if not line.strip():
                    continue
                width = max(size * 0.45 * len(line), size * 0.5)
                y_top = page_height - y_bottom - size * (1.15 + line_index)
                bbox = [x, y_top, x + width, y_top + size * 1.2]
                boxes.append(
                    {
                        "text": line,
                        "bbox": bbox,
                        "font_size": size,
                        "fragment_index": len(boxes),
                        "fragment_sha256": sha256_json(
                            {
                                "text": line,
                                "bbox": [round(float(value), 6) for value in bbox],
                                "font_size": round(size, 6),
                            }
                        ),
                    }
                )
        page.extract_text(visitor_text=visitor)
    except Exception:
        # pypdf's visitor API is optional across supported minor releases.  The
        # canonical text stream still remains available; geometry is simply a
        # candidate-unavailable condition for this page.
        return []
    return _recombine_text_boxes(boxes)


def _join_text_fragments(left: str, right: str) -> str:
    """Join visitor fragments without inventing spaces in CJK labels."""

    left = str(left)
    right = str(right)
    if not left:
        return right
    if not right:
        return left
    if left[-1].isspace() or right[0].isspace():
        return f"{left.rstrip()} {right.lstrip()}"
    if left[-1].isascii() and right[0].isascii() and left[-1].isalnum() and right[0].isalnum():
        return f"{left} {right}"
    return left + right


def _fragment_provenance(row: Mapping[str, Any]) -> dict[str, Any]:
    text = str(row.get("text", ""))
    bbox = [round(float(value), 6) for value in row.get("bbox", [0.0, 0.0, 0.0, 0.0])]
    font_size = round(float(row.get("font_size", 0.0)), 6)
    fragment_sha256 = str(row.get("fragment_sha256") or sha256_json({"text": text, "bbox": bbox, "font_size": font_size}))
    return {
        "fragment_sha256": fragment_sha256,
        "text_sha256": sha256_json(text),
        "bbox": bbox,
        "font_size": font_size,
        "fragment_index": int(row.get("fragment_index", 0)),
    }


def _row_provenance(row: Mapping[str, Any]) -> dict[str, Any]:
    fragments = row.get("fragment_provenance")
    if isinstance(fragments, Mapping):
        return {
            "method": str(fragments.get("method", "pypdf-visitor-text-recombine-v0.1")),
            "fragment_hashes": [str(value) for value in fragments.get("fragment_hashes", [])],
            "fragments": [dict(value) for value in fragments.get("fragments", []) if isinstance(value, Mapping)],
        }
    fragment = _fragment_provenance(row)
    return {
        "method": "pypdf-visitor-text-recombine-v0.1",
        "fragment_hashes": [fragment["fragment_sha256"]],
        "fragments": [fragment],
    }


def _merge_text_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"text": "", "bbox": [0.0, 0.0, 0.0, 0.0], "font_size": 0.0, "fragment_provenance": {"method": "pypdf-visitor-text-recombine-v0.1", "fragment_hashes": [], "fragments": []}}
    text = str(rows[0].get("text", ""))
    boxes = [list(map(float, rows[0].get("bbox", [0.0, 0.0, 0.0, 0.0])))]
    fragment_rows: list[dict[str, Any]] = []
    for row in rows:
        provenance = _row_provenance(row)
        fragment_rows.extend(provenance["fragments"])
    for row in rows[1:]:
        text = _join_text_fragments(text, str(row.get("text", "")))
        boxes.append(list(map(float, row.get("bbox", [0.0, 0.0, 0.0, 0.0]))))
    return {
        "text": text,
        "bbox": [
            round(min(box[0] for box in boxes), 6),
            round(min(box[1] for box in boxes), 6),
            round(max(box[2] for box in boxes), 6),
            round(max(box[3] for box in boxes), 6),
        ],
        "font_size": max(float(row.get("font_size", 0.0)) for row in rows),
        "fragment_provenance": {
            "method": "pypdf-visitor-text-recombine-v0.1",
            "fragment_hashes": [str(value["fragment_sha256"]) for value in fragment_rows],
            "fragments": fragment_rows,
        },
    }


def _same_text_line(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_box = list(map(float, left.get("bbox", [0.0, 0.0, 0.0, 0.0])))
    right_box = list(map(float, right.get("bbox", [0.0, 0.0, 0.0, 0.0])))
    left_size = max(1.0, float(left.get("font_size", 8.0)))
    right_size = max(1.0, float(right.get("font_size", 8.0)))
    return abs(((left_box[1] + left_box[3]) / 2.0) - ((right_box[1] + right_box[3]) / 2.0)) <= max(2.0, min(left_size, right_size) * 0.65)


def _adjacent_text_fragment(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_box = list(map(float, left.get("bbox", [0.0, 0.0, 0.0, 0.0])))
    right_box = list(map(float, right.get("bbox", [0.0, 0.0, 0.0, 0.0])))
    size = max(1.0, float(left.get("font_size", 8.0)), float(right.get("font_size", 8.0)))
    gap = right_box[0] - left_box[2]
    return gap <= max(2.0, size * 2.0)


def _caption_prefix(text: str) -> bool:
    return bool(re.fullmatch(r"\s*(?:Figure|Fig(?:ure)?\.?|图|Table|Tab(?:le)?\.?|表|Equation|Eq(?:uation)?\.?|式)\s*", str(text), re.I))


def _number_fragment(text: str) -> bool:
    return bool(re.fullmatch(r"\s*[-A-Za-z0-9IVXLCDM.]+\s*", str(text), re.I))


def _recombine_text_boxes(boxes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild deterministic visual text rows from pypdf visitor fragments.

    pypdf may emit a CJK caption as ``图``, ``2``, ``-``, ``2`` ... instead of
    one text item. Same-line adjacent fragments are merged first. A label prefix
    may then merge with one immediately following number row for PDFs that split
    ``图`` and ``2-2-1`` across a line break. The raw fragment hashes and boxes
    remain attached as locator provenance.
    """

    indexed = [(index, dict(row)) for index, row in enumerate(boxes) if isinstance(row, Mapping)]
    indexed.sort(key=lambda item: (round(float(item[1].get("bbox", [0, 0, 0, 0])[1]), 6), round(float(item[1].get("bbox", [0, 0, 0, 0])[0]), 6), item[0]))
    line_rows: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for _, row in indexed:
        if current and _same_text_line(current[-1], row) and _adjacent_text_fragment(current[-1], row):
            current.append(row)
            continue
        if current:
            line_rows.append(_merge_text_rows(current))
        current = [row]
    if current:
        line_rows.append(_merge_text_rows(current))

    merged: list[dict[str, Any]] = []
    index = 0
    while index < len(line_rows):
        current_row = line_rows[index]
        if index + 1 < len(line_rows) and _caption_prefix(str(current_row.get("text", ""))):
            next_row = line_rows[index + 1]
            current_box = current_row["bbox"]
            next_box = next_row["bbox"]
            current_size = max(1.0, float(current_row.get("font_size", 8.0)))
            vertical_gap = float(next_box[1]) - float(current_box[3])
            aligned = abs(float(next_box[0]) - float(current_box[0])) <= max(3.0, current_size * 3.0)
            if _number_fragment(str(next_row.get("text", ""))) and vertical_gap <= max(4.0, current_size * 2.2) and aligned:
                current_row = _merge_text_rows([current_row, next_row])
                index += 1
        merged.append(current_row)
        index += 1
    return merged


def _page_drawings(page: Any, width: float, height: float, *, media_left: float = 0.0, media_bottom: float = 0.0) -> list[list[float]]:
    drawings: list[list[float]] = []
    try:
        contents = page.get_contents()
        payload = contents.get_data() if contents is not None else b""
        text = payload.decode("latin1", errors="ignore")
    except Exception:
        return drawings

    def multiply(left: Sequence[float], right: Sequence[float]) -> list[float]:
        a, b, c, d, e, f = left
        g, h, i, j, k, l = right
        return [a * g + c * h, b * g + d * h, a * i + c * j, b * i + d * j, a * k + c * l + e, b * k + d * l + f]

    def point(matrix: Sequence[float], x: float, y: float) -> tuple[float, float]:
        return matrix[0] * x + matrix[2] * y + matrix[4], matrix[1] * x + matrix[3] * y + matrix[5]

    def flush(path: list[tuple[float, float]]) -> None:
        if len(path) < 2:
            return
        xs = [item[0] for item in path]
        ys = [item[1] for item in path]
        try:
            # PDF content coordinates are bottom-left; the compiler contract is
            # top-left physical page coordinates after all cm transforms.
            drawings.append(normalize_bbox([min(xs) - media_left, media_bottom + height - max(ys), max(xs) - media_left, media_bottom + height - min(ys)], width, height))
        except VisualSemanticsError:
            # A path can intentionally extend beyond a crop box.  Clamp it as a
            # candidate only when a non-degenerate in-page intersection exists.
            candidate = [max(0.0, min(width, min(xs) - media_left)), max(0.0, min(height, media_bottom + height - max(ys))), max(0.0, min(width, max(xs) - media_left)), max(0.0, min(height, media_bottom + height - min(ys)))]
            if candidate[2] > candidate[0] and candidate[3] > candidate[1]:
                drawings.append([round(value, 6) for value in candidate])

    numbers: list[float] = []
    path: list[tuple[float, float]] = []
    current_matrix = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    matrix_stack: list[list[float]] = []
    tokens = re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|[A-Za-z][A-Za-z*']*", text)
    for token in tokens:
        try:
            numbers.append(float(token))
            continue
        except ValueError:
            pass
        operator = token
        try:
            if operator == "q":
                matrix_stack.append(list(current_matrix))
            elif operator == "Q":
                if matrix_stack:
                    current_matrix = matrix_stack.pop()
            elif operator == "cm" and len(numbers) >= 6:
                current_matrix = multiply(current_matrix, numbers[-6:])
                del numbers[-6:]
            elif operator in {"m", "l"} and len(numbers) >= 2:
                x, y = numbers[-2:]
                del numbers[-2:]
                transformed = point(current_matrix, x, y)
                if operator == "m" and path:
                    flush(path)
                    path = []
                path.append(transformed)
            elif operator == "c" and len(numbers) >= 6:
                values = numbers[-6:]
                del numbers[-6:]
                path.extend([point(current_matrix, values[0], values[1]), point(current_matrix, values[2], values[3]), point(current_matrix, values[4], values[5])])
            elif operator == "re" and len(numbers) >= 4:
                x, y, w, h = numbers[-4:]
                del numbers[-4:]
                path.extend([point(current_matrix, x, y), point(current_matrix, x + w, y), point(current_matrix, x + w, y + h), point(current_matrix, x, y + h)])
            elif operator in {"h", "S", "s", "f", "F", "B", "b", "b*", "n"}:
                if operator == "h" and path:
                    path.append(path[0])
                flush(path)
                path = []
            elif operator not in {"BT", "ET", "Tf", "Td", "TD", "Tj", "TJ", "Tm", "T*", "Do", "W", "W*", "CS", "cs", "SC", "SCN", "sc", "scn", "rg", "RG", "g", "G", "w", "J", "j", "M", "d", "ri", "gs", "sh", "BI", "ID", "EI", "BX", "EX"}:
                # Keep the tokenizer bounded when an unfamiliar operator is
                # encountered in a content stream.
                numbers.clear()
        except (IndexError, TypeError, ValueError):
            numbers.clear()
    flush(path)
    return drawings


def _pdf_matrix(value: Any, *, default: Sequence[float] = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)) -> list[float]:
    values = list(value) if isinstance(value, (list, tuple)) else list(default)
    if len(values) != 6 or not all(_finite_number(item) for item in values):
        return list(default)
    return [float(item) for item in values]


def _pdf_matrix_multiply(left: Sequence[float], right: Sequence[float]) -> list[float]:
    a, b, c, d, e, f = _pdf_matrix(left)
    g, h, i, j, k, l = _pdf_matrix(right)
    return [
        a * g + c * h,
        b * g + d * h,
        a * i + c * j,
        b * i + d * j,
        a * k + c * l + e,
        b * k + d * l + f,
    ]


def _pdf_matrix_point(matrix: Sequence[float], x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = _pdf_matrix(matrix)
    return a * float(x) + c * float(y) + e, b * float(x) + d * float(y) + f


def _pdf_object_reference(value: Any) -> dict[str, Any] | None:
    reference = getattr(value, "indirect_reference", None)
    if reference is None:
        return None
    result: dict[str, Any] = {}
    if getattr(reference, "idnum", None) is not None:
        result["idnum"] = int(reference.idnum)
    if getattr(reference, "generation", None) is not None:
        result["generation"] = int(reference.generation)
    return result or None


def _pdf_stream_sha256(value: Any) -> str | None:
    try:
        payload = value.get_data()
    except Exception:
        return None
    if not isinstance(payload, bytes):
        payload = bytes(payload)
    return sha256_bytes(payload)


def _pdf_xobject_identity(name: str, value: Any, subtype: str) -> dict[str, Any]:
    bbox = value.get("/BBox") if hasattr(value, "get") else None
    matrix = value.get("/Matrix") if hasattr(value, "get") else None
    width = value.get("/Width") if hasattr(value, "get") else None
    height = value.get("/Height") if hasattr(value, "get") else None
    stream_sha256 = _pdf_stream_sha256(value)
    identity = {
        "resource_name": str(name),
        "subtype": str(subtype),
        "bbox": [float(item) for item in bbox] if isinstance(bbox, (list, tuple)) and len(bbox) == 4 and all(_finite_number(item) for item in bbox) else None,
        "matrix": _pdf_matrix(matrix) if matrix is not None else None,
        "width": int(width) if _finite_number(width) else None,
        "height": int(height) if _finite_number(height) else None,
        "stream_sha256": stream_sha256,
        "indirect_reference": _pdf_object_reference(value),
    }
    identity["object_sha256"] = sha256_json(identity)
    return identity


def _pdf_transformed_bbox(matrix: Sequence[float], bbox: Sequence[Any], width: float, height: float, *, media_left: float = 0.0, media_bottom: float = 0.0) -> list[float] | None:
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or not all(_finite_number(item) for item in bbox):
        return None
    x0, y0, x1, y1 = (float(item) for item in bbox)
    points = [_pdf_matrix_point(matrix, x, y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    raw = [min(point[0] for point in points) - media_left, media_bottom + height - max(point[1] for point in points), max(point[0] for point in points) - media_left, media_bottom + height - min(point[1] for point in points)]
    clipped = [max(0.0, min(float(width), raw[0])), max(0.0, min(float(height), raw[1])), max(0.0, min(float(width), raw[2])), max(0.0, min(float(height), raw[3]))]
    if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
        return None
    return [round(value, 6) for value in clipped]


def _page_xobject_candidates(page: Any, width: float, height: float, reader: Any | None = None, *, media_left: float = 0.0, media_bottom: float = 0.0) -> list[dict[str, Any]]:
    """Resolve page/Form/Image Do geometry into candidate page bboxes.

    The content stream is interpreted with the same affine convention as PDF:
    ``q/Q`` save and restore the CTM, ``cm`` concatenates it, and ``Do`` enters
    a Form or places an Image unit square. Form ``/Matrix`` and ``/BBox`` are
    retained, while nested resources are followed without promoting any
    object to a semantic truth.  A malformed or recursive form is simply not a
    usable geometry candidate; the caller can keep an explicit gap/conflict.
    """

    try:
        from pypdf.generic import ContentStream
    except ModuleNotFoundError:
        return []

    def dereference(value: Any) -> Any:
        try:
            return value.get_object()
        except AttributeError:
            return value
        except Exception:
            return value

    def resources_xobjects(resources: Any) -> Any:
        resources = dereference(resources)
        if not hasattr(resources, "get"):
            return {}
        xobjects = dereference(resources.get("/XObject"))
        return xobjects if hasattr(xobjects, "get") else {}

    candidates: list[dict[str, Any]] = []
    if reader is None:
        # pypdf's PageObject does not expose its reader consistently.  The
        # extractor passes the owning reader explicitly; a direct unit caller
        # without one gets no XObject candidate rather than an unbound parse.
        return []

    root_resources = dereference(page.get("/Resources")) if hasattr(page, "get") else {}

    def walk(stream: Any, resources: Any, matrix: Sequence[float], chain: tuple[str, ...], depth: int) -> None:
        if depth > 12:
            return
        try:
            operations = ContentStream(stream, reader)
        except Exception:
            return
        current_matrix = list(matrix)
        stack: list[list[float]] = []
        xobjects = resources_xobjects(resources)
        for operands, operator in operations.operations:
            op = operator.decode("latin1") if isinstance(operator, bytes) else str(operator)
            if op == "q":
                stack.append(list(current_matrix))
                continue
            if op == "Q":
                if stack:
                    current_matrix = stack.pop()
                continue
            if op == "cm" and len(operands) >= 6:
                current_matrix = _pdf_matrix_multiply(current_matrix, _pdf_matrix(operands[-6:]))
                continue
            if op != "Do" or not operands:
                continue
            name = str(operands[0])
            if name not in xobjects:
                alternate = name if name.startswith("/") else f"/{name}"
                if alternate in xobjects:
                    name = alternate
                else:
                    continue
            xobject = dereference(xobjects.get(name))
            if not hasattr(xobject, "get"):
                continue
            subtype = str(xobject.get("/Subtype", "")).lstrip("/")
            identity = _pdf_xobject_identity(name, xobject, subtype)
            if subtype == "Image":
                bbox = _pdf_transformed_bbox(current_matrix, [0.0, 0.0, 1.0, 1.0], width, height, media_left=media_left, media_bottom=media_bottom)
                if bbox is not None:
                    candidates.append(
                        {
                            "kind": "image",
                            "bbox": bbox,
                            "resource_name": name,
                            "xobject_provenance": {
                                "geometry_evidence": "pdf-xobject-image-do-v0.1",
                                "xobject_path": [*chain, name],
                                "xobject_subtype": "Image",
                                "xobject_stream_sha256": identity.get("stream_sha256"),
                                "xobject_object_sha256": identity.get("object_sha256"),
                                "xobject_indirect_reference": identity.get("indirect_reference"),
                                "do_ctm": [round(value, 9) for value in current_matrix],
                                "transformed_corners_bottom_left": [
                                    list(_pdf_matrix_point(current_matrix, x, y))
                                    for x, y in ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
                                ],
                                "candidate_only": True,
                            },
                        }
                    )
                continue
            if subtype != "Form":
                continue
            form_matrix = _pdf_matrix(xobject.get("/Matrix"))
            form_ctm = _pdf_matrix_multiply(current_matrix, form_matrix)
            form_bbox = _pdf_transformed_bbox(form_ctm, xobject.get("/BBox"), width, height, media_left=media_left, media_bottom=media_bottom)
            form_identity = dict(identity)
            form_identity["form_matrix"] = form_matrix
            if form_bbox is not None:
                candidates.append(
                    {
                        "kind": "form",
                        "bbox": form_bbox,
                        "resource_name": name,
                        "xobject_provenance": {
                            "geometry_evidence": "pdf-xobject-form-bbox-v0.1",
                            "xobject_path": [*chain, name],
                            "xobject_subtype": "Form",
                            "xobject_stream_sha256": identity.get("stream_sha256"),
                            "xobject_object_sha256": identity.get("object_sha256"),
                            "xobject_indirect_reference": identity.get("indirect_reference"),
                            "form_bbox": [float(item) for item in xobject.get("/BBox")],
                            "form_matrix": [round(value, 9) for value in form_matrix],
                            "form_ctm": [round(value, 9) for value in form_ctm],
                            "candidate_only": True,
                        },
                    }
                )
            form_resources = dereference(xobject.get("/Resources")) or resources
            form_path = (*chain, name)
            if form_path.count(name) > 2:
                continue
            walk(xobject, form_resources, form_ctm, form_path, depth + 1)

    try:
        walk(page.get_contents(), root_resources, [1.0, 0.0, 0.0, 1.0, 0.0, 0.0], (), 0)
    except Exception:
        return []
    return sorted(candidates, key=lambda row: (str(row.get("kind")), tuple(float(value) for value in row.get("bbox", [])), str(row.get("resource_name")), json.dumps(row.get("xobject_provenance"), sort_keys=True)))


def _unpaired_xobject_candidate(candidates: Sequence[Mapping[str, Any]], width: float, height: float, *, used: set[str]) -> dict[str, Any] | None:
    """Choose a bounded non-page XObject when a text caption bbox is unusable.

    Some image-first technical PDFs carry a useful text layer whose bottom
    captions are outside the declared MediaBox even though a separate figure
    image has valid geometry.  Keep that image as a review-required candidate,
    while excluding page-background images and tiny header forms.
    """

    page_area = max(1.0, float(width) * float(height))
    eligible: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or candidate.get("kind") not in {"image", "form"}:
            continue
        key = _xobject_candidate_key(candidate)
        bbox = candidate.get("bbox")
        if key in used or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        area = max(0.0, float(bbox[2]) - float(bbox[0])) * max(0.0, float(bbox[3]) - float(bbox[1]))
        ratio = area / page_area
        if ratio < 0.01 or ratio > 0.75:
            continue
        eligible.append(((0 if candidate.get("kind") == "image" else 1, -round(area, 6), key), dict(candidate)))
    return sorted(eligible, key=lambda item: item[0])[0][1] if eligible else None


def _bounded_page_bbox(value: Any, width: float, height: float) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4 or not all(_finite_number(item) for item in value):
        return None
    clipped = [
        max(0.0, min(float(width), float(value[0]))),
        max(0.0, min(float(height), float(value[1]))),
        max(0.0, min(float(width), float(value[2]))),
        max(0.0, min(float(height), float(value[3]))),
    ]
    if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
        return None
    return [round(item, 6) for item in clipped]


def _union_boxes(boxes: Sequence[Sequence[float]], width: float, height: float, *, fallback: Sequence[float] | None = None) -> list[float]:
    valid = [list(map(float, box)) for box in boxes if isinstance(box, (list, tuple)) and len(box) == 4]
    if not valid:
        return list(fallback or [0.0, 0.0, width, height])
    return [round(max(0.0, min(width, min(box[0] for box in valid))), 6), round(max(0.0, min(height, min(box[1] for box in valid))), 6), round(max(0.0, min(width, max(box[2] for box in valid))), 6), round(max(0.0, min(height, max(box[3] for box in valid))), 6)]


def _concise_plot_marker_rows(page_boxes: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Return bounded chart-label rows, excluding ordinary prose mentions.

    A single occurrence of words such as ``axis`` inside technical prose is not
    evidence that the page contains a chart. The native baseline therefore
    requires either an explicit plot label or at least two distinct concise
    chart-label roles before it turns drawing geometry into a plot candidate.
    """

    rows: list[Mapping[str, Any]] = []
    roles: set[str] = set()
    for row in page_boxes:
        raw_text = str(row.get("text", "")).strip()
        if not raw_text or len(raw_text) > PLOT_MARKER_LABEL_MAX_CHARS or not PLOT_MARKER_RE.search(raw_text):
            continue
        row_roles: set[str] = set()
        if re.search(r"\bplot\b|坐标图", raw_text, re.I):
            row_roles.add("plot")
        if re.search(r"\b(?:x|y)[ -]?axis\b|\baxis\b|坐标轴", raw_text, re.I):
            row_roles.add("axis")
        if re.search(r"\blegend\b|图例", raw_text, re.I):
            row_roles.add("legend")
        if re.search(r"\bseries\b|曲线", raw_text, re.I):
            row_roles.add("series")
        if re.search(r"\btick\b|刻度", raw_text, re.I):
            row_roles.add("tick")
        if row_roles:
            rows.append(row)
            roles.update(row_roles)
    return rows if "plot" in roles or len(roles) >= 2 else []


def _bbox_axis_gap(left: Sequence[float], right: Sequence[float]) -> tuple[float, float, float, float]:
    horizontal_gap = max(0.0, float(right[0]) - float(left[2]), float(left[0]) - float(right[2]))
    vertical_gap = max(0.0, float(right[1]) - float(left[3]), float(left[1]) - float(right[3]))
    horizontal_overlap = max(0.0, min(float(left[2]), float(right[2])) - max(float(left[0]), float(right[0])))
    vertical_overlap = max(0.0, min(float(left[3]), float(right[3])) - max(float(left[1]), float(right[1])))
    return horizontal_gap, vertical_gap, horizontal_overlap, vertical_overlap


def _caption_adjacent_xobject(label_box: Sequence[float], candidates: Sequence[Mapping[str, Any]], width: float, height: float, *, used: set[str] | None = None) -> dict[str, Any] | None:
    """Choose only a same-page image/form candidate adjacent to a caption.

    A nearest arbitrary path is not a visual object boundary.  The candidate
    must share one axis with the caption or be close on both axes; otherwise
    the caller records a gap/conflict instead of silently using a small path.
    """

    used = used or set()
    eligible: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or candidate.get("kind") not in {"image", "form"}:
            continue
        bbox = candidate.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        area = max(0.0, float(bbox[2]) - float(bbox[0])) * max(0.0, float(bbox[3]) - float(bbox[1]))
        area_ratio = area / max(1.0, float(width) * float(height))
        if area_ratio < 0.01 or area_ratio > 0.75:
            # A page-background scan is useful Context evidence, not the
            # geometry of the captioned object inside that page.
            continue
        provenance = candidate.get("xobject_provenance") if isinstance(candidate.get("xobject_provenance"), Mapping) else {}
        candidate_key = str(provenance.get("xobject_object_sha256") or sha256_json(candidate))
        if candidate_key in used:
            continue
        horizontal_gap, vertical_gap, horizontal_overlap, vertical_overlap = _bbox_axis_gap(label_box, bbox)
        adjacent = (
            vertical_overlap > 0.0 and horizontal_gap <= max(24.0, width * 0.25)
        ) or (
            horizontal_overlap > 0.0 and vertical_gap <= max(24.0, height * 0.22)
        ) or (
            horizontal_gap <= max(24.0, width * 0.16) and vertical_gap <= max(24.0, height * 0.16)
        )
        if not adjacent:
            continue
        score = (
            0 if candidate.get("kind") == "image" else 1,
            round(horizontal_gap / max(1.0, width) + vertical_gap / max(1.0, height), 8),
            -round(area, 6),
            candidate_key,
        )
        eligible.append((score, dict(candidate)))
    if not eligible:
        return None
    return sorted(eligible, key=lambda item: item[0])[0][1]


def _caption_candidate_distance(label_box: Sequence[float], candidate: Mapping[str, Any], width: float, height: float) -> float:
    """Return the deterministic geometric distance used for caption ownership."""

    bbox = candidate.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return float("inf")
    horizontal_gap, vertical_gap, _horizontal_overlap, _vertical_overlap = _bbox_axis_gap(label_box, bbox)
    return round(horizontal_gap / max(1.0, float(width)) + vertical_gap / max(1.0, float(height)), 8)


def _xobject_candidate_key(candidate: Mapping[str, Any]) -> str:
    provenance = candidate.get("xobject_provenance") if isinstance(candidate.get("xobject_provenance"), Mapping) else {}
    return str(provenance.get("xobject_object_sha256") or sha256_json(candidate))


def _xobject_same_placement(left: Mapping[str, Any], right: Mapping[str, Any], *, iou_threshold: float = 0.92) -> bool:
    left_bbox = left.get("bbox")
    right_bbox = right.get("bbox")
    if _bbox_iou(left_bbox, right_bbox) >= iou_threshold:
        return True
    left_provenance = left.get("xobject_provenance") if isinstance(left.get("xobject_provenance"), Mapping) else {}
    right_provenance = right.get("xobject_provenance") if isinstance(right.get("xobject_provenance"), Mapping) else {}
    left_path = list(left_provenance.get("xobject_path") or [])
    right_path = list(right_provenance.get("xobject_path") or [])
    return bool(left_path and right_path and (left_path[: len(right_path)] == right_path or right_path[: len(left_path)] == left_path))


def _consume_xobject_candidate(selected: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]], used: set[str]) -> None:
    """Consume nested Form/Image siblings as one placement candidate."""

    selected_bbox = selected.get("bbox")
    selected_path = (selected.get("xobject_provenance") or {}).get("xobject_path", []) if isinstance(selected.get("xobject_provenance"), Mapping) else []
    for candidate in candidates:
        candidate_bbox = candidate.get("bbox")
        candidate_path = (candidate.get("xobject_provenance") or {}).get("xobject_path", []) if isinstance(candidate.get("xobject_provenance"), Mapping) else []
        overlap = _bbox_iou(selected_bbox, candidate_bbox) if selected_bbox is not None and candidate_bbox is not None else 0.0
        nested_path = bool(selected_path and candidate_path and (list(candidate_path[: len(selected_path)]) == list(selected_path) or list(selected_path[: len(candidate_path)]) == list(candidate_path)))
        if overlap >= 0.9 or nested_path:
            used.add(_xobject_candidate_key(candidate))


def _xobject_panel_group(primary: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]], caption_box: Sequence[float], width: float, height: float, *, used: set[str] | None = None, caption_boxes: Sequence[Sequence[float]] = ()) -> list[dict[str, Any]]:
    """Return a bounded multi-panel XObject group for one caption.

    The group is deliberately conservative: it contains the caption-selected
    image/form and only nearby, same-axis candidates.  Unrelated page images are
    left for another locator and never become panels merely because they are
    present on the page.
    """

    used = used or set()
    primary_box = primary.get("bbox")
    if not isinstance(primary_box, (list, tuple)) or len(primary_box) != 4:
        return [dict(primary)]
    primary_width = max(1.0, float(primary_box[2]) - float(primary_box[0]))
    primary_height = max(1.0, float(primary_box[3]) - float(primary_box[1]))
    proximity = max(36.0, min(max(width, height) * 0.18, max(primary_width, primary_height) * 1.35))
    grouped: list[dict[str, Any]] = [dict(primary)]
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or candidate.get("kind") not in {"image", "form"}:
            continue
        key = _xobject_candidate_key(candidate)
        if key in used or key == _xobject_candidate_key(primary):
            continue
        bbox = candidate.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        area = max(0.0, float(bbox[2]) - float(bbox[0])) * max(0.0, float(bbox[3]) - float(bbox[1]))
        if area / max(1.0, float(width) * float(height)) < 0.01 or area / max(1.0, float(width) * float(height)) > 0.75:
            continue
        # A nearby image is not automatically a panel.  If a different
        # same-page figure caption has a strictly shorter deterministic
        # caption-to-XObject distance, leave this placement for that caption.
        # This prevents vertically stacked independent figures from being
        # unioned into one parent and consumed before their own locator runs.
        current_distance = _caption_candidate_distance(caption_box, candidate, width, height)
        belongs_to_other_caption = any(
            list(other_box) != list(caption_box)
            and _caption_adjacent_xobject(other_box, [candidate], width, height, used=set()) is not None
            and _caption_candidate_distance(other_box, candidate, width, height) < current_distance
            for other_box in caption_boxes
            if isinstance(other_box, (list, tuple)) and len(other_box) == 4
        )
        if belongs_to_other_caption:
            continue
        horizontal_gap, vertical_gap, horizontal_overlap, vertical_overlap = _bbox_axis_gap(primary_box, bbox)
        caption_horizontal_gap, caption_vertical_gap, caption_horizontal_overlap, caption_vertical_overlap = _bbox_axis_gap(caption_box, bbox)
        same_axis = horizontal_overlap > 0.0 or vertical_overlap > 0.0
        near_primary = same_axis and min(horizontal_gap, vertical_gap) <= proximity
        caption_near = (
            (caption_vertical_overlap > 0.0 and caption_horizontal_gap <= max(24.0, width * 0.25))
            or (caption_horizontal_overlap > 0.0 and caption_vertical_gap <= max(24.0, height * 0.22))
        )
        if near_primary or (caption_near and min(horizontal_gap, vertical_gap) <= proximity * 1.5):
            grouped.append(dict(candidate))
    representatives: list[dict[str, Any]] = []
    for candidate in sorted(grouped, key=lambda row: (0 if row.get("kind") == "image" else 1, float(row["bbox"][1]), float(row["bbox"][0]), _xobject_candidate_key(row))):
        if any(_xobject_same_placement(candidate, previous) for previous in representatives):
            continue
        representatives.append(candidate)
    return sorted(representatives, key=lambda row: (float(row["bbox"][1]), float(row["bbox"][0]), _xobject_candidate_key(row)))


def _bbox_contains(container: Sequence[float], child: Sequence[float], *, tolerance: float = 0.0) -> bool:
    return (
        float(child[0]) >= float(container[0]) - tolerance
        and float(child[1]) >= float(container[1]) - tolerance
        and float(child[2]) <= float(container[2]) + tolerance
        and float(child[3]) <= float(container[3]) + tolerance
    )


def _arrow_like_box(box: Sequence[float], *, page_width: float, page_height: float) -> bool:
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return False
    box_width = max(0.0, float(box[2]) - float(box[0]))
    box_height = max(0.0, float(box[3]) - float(box[1]))
    thickness = min(box_width, box_height)
    length = max(box_width, box_height)
    return thickness <= max(5.0, min(page_width, page_height) * 0.012) and length >= max(14.0, thickness * 3.5)


def _candidate_annotation_rows(page_boxes: Sequence[Mapping[str, Any]], drawing_boxes: Sequence[Sequence[float]], parents: Sequence[Mapping[str, Any]], *, page_width: float, page_height: float) -> list[dict[str, Any]]:
    """Build uncertain label/arrow annotations inside visual parent regions."""

    visual_parents = [row for row in parents if row.get("type") in {"figure", "diagram", "plot"} and isinstance(row.get("bbox"), (list, tuple))]
    if not visual_parents:
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[float, float, float, float], str]] = set()
    for row in page_boxes:
        text = str(row.get("text", "")).strip()
        bbox = row.get("bbox")
        if not text or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        if FIGURE_RE.search(text) or TABLE_RE.search(text) or EQUATION_RE.search(text):
            continue
        containing = [candidate for candidate in visual_parents if _bbox_contains(candidate["bbox"], bbox, tolerance=max(2.0, page_width * 0.01))]
        parent = min(containing, key=lambda candidate: max(0.0, float(candidate["bbox"][2]) - float(candidate["bbox"][0])) * max(0.0, float(candidate["bbox"][3]) - float(candidate["bbox"][1]))) if containing else None
        if parent is None:
            continue
        key = (str(parent["visual_object_id"]), tuple(round(float(value), 6) for value in bbox), text)
        if key in seen:
            continue
        seen.add(key)
        result.append({"subtype": "label", "text": text, "bbox": list(bbox), "parent": parent, "attributes": {"annotation_kind": "label", "visual_confirmation": False, "text_sha256": sha256_json(text)}})
    for drawing in drawing_boxes:
        if not _arrow_like_box(drawing, page_width=page_width, page_height=page_height):
            continue
        containing = [candidate for candidate in visual_parents if _bbox_contains(candidate["bbox"], drawing, tolerance=max(3.0, page_width * 0.015))]
        parent = min(containing, key=lambda candidate: max(0.0, float(candidate["bbox"][2]) - float(candidate["bbox"][0])) * max(0.0, float(candidate["bbox"][3]) - float(candidate["bbox"][1]))) if containing else None
        if parent is None:
            continue
        key = (str(parent["visual_object_id"]), tuple(round(float(value), 6) for value in drawing), "arrow")
        if key in seen:
            continue
        seen.add(key)
        direction = "horizontal" if (float(drawing[2]) - float(drawing[0])) >= (float(drawing[3]) - float(drawing[1])) else "vertical"
        result.append({"subtype": "arrow", "text": None, "bbox": list(drawing), "parent": parent, "attributes": {"annotation_kind": "arrow", "direction_candidate": direction, "target_visual_object_id": None, "visual_confirmation": False}})
    return sorted(result, key=lambda row: (str(row["parent"]["visual_object_id"]), float(row["bbox"][1]), float(row["bbox"][0]), str(row["subtype"]), str(row.get("text") or "")))


def _ruling_table_candidate(label_box: Sequence[float], drawing_boxes: Sequence[Sequence[float]], width: float, height: float) -> tuple[list[float] | None, dict[str, Any]]:
    """Find a dense ruling-line/rect cluster without swallowing page text.

    The native path parser emits many overlapping cell rectangles and a few
    abnormal tall clipping/path boxes.  We retain the dominant near-caption
    component made of short-height rules/cells and explicitly exclude tall
    boxes before taking its union.  Text is used later for cell candidates,
    never as the table frame itself.
    """

    normalized: list[list[float]] = []
    seen: set[tuple[float, float, float, float]] = set()
    max_rule_height = max(24.0, height * 0.12)
    lower = float(label_box[3]) - max(4.0, height * 0.01)
    upper = min(float(height), float(label_box[3]) + height * 0.32)
    for raw in drawing_boxes:
        if not isinstance(raw, (list, tuple)) or len(raw) != 4:
            continue
        box = [float(value) for value in raw]
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        if box[3] < lower or box[1] > upper:
            continue
        if box[3] - box[1] > max_rule_height:
            continue
        key = tuple(round(value, 4) for value in box)
        if key not in seen:
            seen.add(key)
            normalized.append(box)
    if len(normalized) < 4:
        return None, {"geometry_evidence": "ruling-line-cluster-gap", "cluster_box_count": len(normalized), "candidate_only": True}

    tolerance = max(1.0, width * 0.002)
    components: list[list[list[float]]] = []

    def near(left: Sequence[float], right: Sequence[float]) -> bool:
        return not (
            float(left[2]) + tolerance < float(right[0])
            or float(right[2]) + tolerance < float(left[0])
            or float(left[3]) + tolerance < float(right[1])
            or float(right[3]) + tolerance < float(left[1])
        )

    for box in sorted(normalized, key=lambda value: (value[1], value[0], value[2], value[3])):
        attached: list[int] = [index for index, component in enumerate(components) if any(near(box, other) for other in component)]
        if not attached:
            components.append([box])
            continue
        target = attached[0]
        components[target].append(box)
        for index in reversed(attached[1:]):
            components[target].extend(components.pop(index))

    ranked: list[tuple[tuple[Any, ...], list[list[float]]]] = []
    for component in components:
        cluster_box = _union_boxes(component, width, height)
        span_x = cluster_box[2] - cluster_box[0]
        span_y = cluster_box[3] - cluster_box[1]
        distinct_rows = len({round((box[1] + box[3]) / 2.0, 2) for box in component})
        if len(component) < 4 or span_x < width * 0.25 or span_y < max(8.0, height * 0.03) or distinct_rows < 2:
            continue
        horizontal_overlap = max(0.0, min(cluster_box[2], float(label_box[2])) - max(cluster_box[0], float(label_box[0])))
        distance = abs(((cluster_box[1] + cluster_box[3]) / 2.0) - ((float(label_box[1]) + float(label_box[3])) / 2.0))
        ranked.append(((-len(component), -round(horizontal_overlap, 6), round(distance, 6), -round(span_x * span_y, 6)), component))
    if not ranked:
        return None, {"geometry_evidence": "ruling-line-cluster-gap", "cluster_box_count": len(normalized), "candidate_only": True}
    component = sorted(ranked, key=lambda item: item[0])[0][1]
    cluster_box = _union_boxes(component, width, height)
    component_payload = [[round(value, 6) for value in box] for box in sorted(component)]
    return cluster_box, {
        "geometry_evidence": "pdf-ruling-line-cluster-v0.1",
        "cluster_box_count": len(component),
        "cluster_bbox": cluster_box,
        "cluster_sha256": sha256_json(component_payload),
        "excluded_tall_path_rule": f"height>{round(max_rule_height, 6)}",
        "text_union_used_for_frame": False,
        "candidate_only": True,
    }


def _text_table_candidate(label_box: Sequence[float], page_boxes: Sequence[Mapping[str, Any]], width: float, height: float) -> tuple[list[float] | None, dict[str, Any]]:
    """Bound a text-only table fallback to one consecutive row cluster.

    This route exists for synthetic/text-only tables and is explicitly a gap
    candidate. It never unions an unbounded caption neighborhood or page text;
    the first sufficiently large vertical gap terminates the cluster.
    """

    rows = []
    for row in page_boxes:
        bbox = row.get("bbox")
        text = str(row.get("text", "")).strip()
        if not text or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        box = [float(value) for value in bbox]
        if box[3] <= float(label_box[3]) - max(2.0, height * 0.005) or box[1] > float(label_box[3]) + height * 0.35:
            continue
        if re.match(r"^\s*(?:Table|Tab(?:le)?\.?|表)\s*" + NUMBER_TOKEN, text, re.I):
            continue
        rows.append((box[1], box[0], box, text))
    rows.sort(key=lambda item: (item[0], item[1], item[2][2], item[2][3]))
    if not rows:
        return None, {"geometry_evidence": "text-row-cluster-gap", "text_row_count": 0, "candidate_only": True}
    selected: list[tuple[list[float], str]] = []
    previous_bottom = float(label_box[3])
    max_row_gap = max(20.0, height * 0.035)
    for _, _, box, text in rows:
        gap = float(box[1]) - previous_bottom
        if selected and gap > max_row_gap:
            break
        selected.append((box, text))
        previous_bottom = max(previous_bottom, float(box[3]))
    if len(selected) < 2:
        return None, {"geometry_evidence": "text-row-cluster-gap", "text_row_count": len(selected), "candidate_only": True}
    boxes = [row[0] for row in selected]
    candidate = _union_boxes(boxes, width, height)
    if candidate[2] - candidate[0] < max(24.0, width * 0.12) or candidate[3] <= candidate[1]:
        return None, {"geometry_evidence": "text-row-cluster-gap", "text_row_count": len(selected), "candidate_only": True}
    return candidate, {
        "geometry_evidence": "text-row-cluster-gap",
        "text_row_count": len(selected),
        "text_cluster_sha256": sha256_json([[round(value, 6) for value in box] + [text] for box, text in selected]),
        "ruling_cluster_available": False,
        "candidate_only": True,
    }


def _candidate_text(page_boxes: Sequence[Mapping[str, Any]]) -> str:
    return " ".join(str(row.get("text", "")) for row in page_boxes)


def _find_label_boxes(page_boxes: Sequence[Mapping[str, Any]], pattern: re.Pattern[str], *, line_start_only: bool = False) -> list[tuple[str, list[float], str, dict[str, Any]]]:
    """Locate caption-style labels while retaining prose references as negatives."""

    rows = [row for row in page_boxes if isinstance(row, Mapping)]
    result: list[tuple[str, list[float], str, dict[str, Any]]] = []
    for index, row in enumerate(rows):
        text = str(row.get("text", ""))
        match = pattern.search(text)
        stripped = text.lstrip()
        if not match or (line_start_only and not stripped.casefold().startswith(match.group(0).casefold())):
            continue
        if line_start_only:
            suffix = text[match.end():].lstrip()
            # A caption label normally continues with a title.  A label that
            # immediately closes a parenthetical/bracketed phrase is a body
            # reference, e.g. ``结构图（图2-3-4）。``; it must stay a negative
            # sample and must not create a missing visual candidate.
            if suffix.startswith((")", "）", "]", "】", "}", "，", ",", "。", ";", "；")):
                continue
            box = row.get("bbox", [0, 0, 1, 1])
            if isinstance(box, (list, tuple)) and len(box) == 4:
                font_size = max(1.0, float(row.get("font_size", 8.0)))
                for previous in rows[:index]:
                    previous_box = previous.get("bbox", [0, 0, 0, 0])
                    if not isinstance(previous_box, (list, tuple)) or len(previous_box) != 4 or not _same_text_line(previous, row):
                        continue
                    previous_text = str(previous.get("text", "")).rstrip()
                    previous_gap = float(box[0]) - float(previous_box[2])
                    if previous_gap >= 0.0 and previous_gap <= max(36.0, font_size * 4.0) and previous_text.endswith(("(", "（", "[", "【")):
                        break
                else:
                    previous = None
                if previous is not None:
                    continue
        label = next((group for group in match.groups() if group), match.group(0))
        result.append((label, list(row.get("bbox", [0, 0, 1, 1])), text, _row_provenance(row)))
    return result


def _label_region(label_box: Sequence[float], drawing_boxes: Sequence[Sequence[float]], width: float, height: float, *, multiple_labels: bool, scale: float = 0.38, nearby_limit: float = 0.45, lower_scale: float = 0.1) -> list[float]:
    """Choose a local drawing region without letting two captions share a page bbox."""

    if multiple_labels:
        # The native baseline cannot reliably attribute a transformed vector
        # path to one caption. A deterministic caption-local envelope is safer
        # than assigning one page-wide drawing bbox to every figure. Include a
        # horizontal neighborhood so side-by-side labels cannot collapse into
        # one duplicate candidate.
        left = max(0.0, float(label_box[0]) - width * 0.05)
        right = min(width, float(label_box[2]) + width * 0.45)
        return [left, max(0.0, float(label_box[1]) - height * scale), right, min(height, float(label_box[3]) + height * 0.1)]
    center_y = (float(label_box[1]) + float(label_box[3])) / 2.0
    nearby = sorted(
        (list(map(float, box)) for box in drawing_boxes if isinstance(box, (list, tuple)) and len(box) == 4),
        key=lambda box: abs(((box[1] + box[3]) / 2.0) - center_y),
    )
    if nearby:
        candidate = nearby[0]
        # A rectangle far from the caption is usually a page border or another
        # figure; retain it only when it is vertically plausible.
        if abs(((candidate[1] + candidate[3]) / 2.0) - center_y) <= height * nearby_limit:
            return candidate
    return [0.0, max(0.0, float(label_box[1]) - height * scale), width, min(height, float(label_box[3]) + height * lower_scale)]


def _make_render_context(source: Path, source_sha256: str, page: int, page_obj: Any, pdftoppm: Path | None, render_receipts: Mapping[int, Mapping[str, Any]] | None, render_dpi: int) -> tuple[dict[str, Any], bytes]:
    media_box = page_obj.mediabox
    crop_box = getattr(page_obj, "cropbox", media_box)
    # Keep the canonical physical coordinate extent tied to MediaBox.  CropBox
    # and its offset remain explicit evidence in page_geometry; pypdf visitor
    # text coordinates are commonly MediaBox-based even when a CropBox exists.
    width = float(media_box.width)
    height = float(media_box.height)
    rotation = int(page_obj.get("/Rotate", 0) or 0) % 360
    page_geometry = {
        "media_box": [float(media_box.left), float(media_box.bottom), float(media_box.right), float(media_box.top)],
        "crop_box": [float(crop_box.left), float(crop_box.bottom), float(crop_box.right), float(crop_box.top)],
        "rotation": rotation,
        "coordinate_origin": "mediabox-top-left",
        "physical_page": int(page),
    }
    provided = dict(render_receipts.get(page, {})) if render_receipts else {}
    if not provided or not HASH_RE.fullmatch(str(provided.get("render_sha256", ""))):
        if pdftoppm is None:
            raise VisualSemanticsError(f"visual_render_receipt_required:page={page}")
        version, payload, render_hash = _render_page(source, page, pdftoppm, render_dpi)
        receipt = render_receipt(source_sha256=source_sha256, physical_page=page, render_sha256=render_hash, dpi=render_dpi, rotation=rotation, renderer_version=version)
        return {"width": width, "height": height, "rotation": rotation, "page_geometry": page_geometry, "render": receipt}, payload
    receipt = dict(provided)
    if receipt.get("source_sha256") != source_sha256 or int(receipt.get("physical_page", -1)) != page:
        raise VisualSemanticsError("render_receipt_source_mismatch")
    if receipt.get("renderer") not in {"pdftoppm", "pdftoppm-local"}:
        raise VisualSemanticsError("render_receipt_renderer_invalid")
    return {"width": width, "height": height, "rotation": int(receipt.get("rotation", rotation)) % 360, "page_geometry": page_geometry, "render": receipt}, b""


def extract_visual_candidates(source_path: Path, *, pages: Sequence[int] | None = None, pdftoppm: Path | None = None, render_receipts: Mapping[int, Mapping[str, Any]] | None = None, confirmed_locators: Sequence[Mapping[str, Any]] | None = None, parser_receipt: Mapping[str, Any] | None = None, backend_receipt: Mapping[str, Any] | None = None, model_receipt: Mapping[str, Any] | None = None, config_receipt: Mapping[str, Any] | None = None, render_dpi: int = 150) -> dict[str, Any]:
    """Build candidate visual objects/relations/grids/conflicts for bounded pages.

    ``confirmed_locators`` is an external, hash-bound locator input.  It is still
    emitted with ``status=candidate``; a reviewer attestation is required before
    any object can become reviewed/promoted.
    """

    source = source_path.expanduser().resolve()
    if not source.is_file():
        raise VisualSemanticsError("source_missing")
    source_hash = sha256_file(source)
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as error:
        raise VisualSemanticsError("pypdf_unavailable") from error
    reader = PdfReader(str(source))
    if reader.is_encrypted:
        raise VisualSemanticsError("unsupported_encrypted_pdf")
    selected = sorted(set(int(page) for page in (pages or range(1, len(reader.pages) + 1))))
    if any(page < 1 or page > len(reader.pages) for page in selected):
        raise VisualSemanticsError("physical_page_out_of_range")
    locator_rows = [dict(row) for row in (confirmed_locators or []) if isinstance(row, Mapping)]
    objects: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    grids: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    render_contexts: dict[int, tuple[dict[str, Any], bytes]] = {}
    for page_number in selected:
        page_obj = reader.pages[page_number - 1]
        context, render_bytes = _make_render_context(source, source_hash, page_number, page_obj, pdftoppm, render_receipts, render_dpi)
        width = float(context["width"])
        height = float(context["height"])
        page_geometry = dict(context.get("page_geometry") or {})
        render_contexts[page_number] = (context, render_bytes)
        receipt = context["render"]
        media_box = page_geometry.get("media_box", [0.0, 0.0, width, height])
        media_left = float(media_box[0])
        media_bottom = float(media_box[1])
        page_boxes = _text_boxes(page_obj, height, media_left=media_left, media_bottom=media_bottom)
        drawing_boxes = _page_drawings(page_obj, width, height, media_left=media_left, media_bottom=media_bottom)
        xobject_candidates = _page_xobject_candidates(page_obj, width, height, reader, media_left=media_left, media_bottom=media_bottom)
        text = _candidate_text(page_boxes)
        page_locators = [row for row in locator_rows if int(row.get("physical_page", page_number)) == page_number]
        created: list[dict[str, Any]] = []
        used_xobject_candidates: set[str] = set()
        if page_locators:
            for row in page_locators:
                try:
                    box = row.get("bbox")
                    if not isinstance(box, (list, tuple)):
                        raise VisualSemanticsError("bbox_invalid")
                    if row.get("source_sha256", source_hash) != source_hash:
                        raise VisualSemanticsError("locator_source_hash_mismatch")
                    render_hash = str(row.get("render_sha256", receipt["render_sha256"]))
                    if render_hash != str(receipt["render_sha256"]):
                        raise VisualSemanticsError("locator_render_hash_mismatch")
                    row_rotation = int(row.get("rotation", receipt.get("rotation", 0))) % 360
                    row_dpi = int(row.get("render_dpi", receipt.get("dpi", render_dpi)))
                    row_coordinate_space = str(row.get("coordinate_space", COORDINATE_SPACE))
                    crop_hash = row.get("crop_sha256")
                    if not isinstance(crop_hash, str) or not HASH_RE.fullmatch(crop_hash):
                        crop_hash = crop_commitment_sha256(render_bytes, normalize_bbox(box, width, height, row_coordinate_space, row_rotation, row_dpi if row_coordinate_space in {"render-pixels-top-left-v1", "image-pixels-top-left-v1"} else None), page_width=width, page_height=height, dpi=row_dpi) if render_bytes else sha256_json({"render_sha256": render_hash, "bbox": list(box), "dpi": row_dpi})
                    object_row = build_visual_object(source_sha256=source_hash, physical_page=page_number, object_type=str(row.get("type")), subtype=str(row.get("subtype", "")), page_width=width, page_height=height, bbox=box, polygon=row.get("polygon"), coordinate_space=row_coordinate_space, rotation=row_rotation, render_sha256=render_hash, render_dpi=row_dpi, crop_sha256=crop_hash, parent=row.get("parent"), label=row.get("label"), content_sha256=row.get("content_sha256"), page_geometry=page_geometry, parser_receipt=row.get("parser_receipt") or parser_receipt or {"id": "reviewer-locator-confirmed-bbox", "version": "v0.1", "candidate_only": True}, backend_receipt=row.get("backend_receipt") or backend_receipt or {"id": "reviewer-locator-confirmed-bbox", "candidate_only": True}, model_receipt=row.get("model_receipt") or model_receipt or {"id": "none", "available": False, "download": False}, config_receipt=row.get("config_receipt") or config_receipt or {"id": "confirmed-bbox-v0.1", "candidate_only": True})
                    created.append(object_row)
                except VisualSemanticsError as error:
                    conflicts.append(build_visual_conflict(source_sha256=source_hash, physical_page=page_number, conflict_type="locator", details={"locator": row, "code": str(error)}))
        else:
            figure_labels = _find_label_boxes(page_boxes, FIGURE_RE, line_start_only=True)
            table_labels = _find_label_boxes(page_boxes, TABLE_RE, line_start_only=True)
            # Parenthetical prose references are not equation objects.  Keep only
            # lines containing an equation cue or a label at the line boundary.
            equation_labels = [
                row for row in _find_label_boxes(page_boxes, EQUATION_RE)
                if EQUATION_LABEL_TOKEN_RE.fullmatch(str(row[0])) and ("=" in row[2] or "∫" in row[2] or "→" in row[2] or row[2].strip().endswith(f"({row[0]})"))
            ]
            for label, label_box, raw, locator_provenance in figure_labels:
                locator_attributes = {"locator_provenance": locator_provenance}
                panel_group: list[dict[str, Any]] = []
                bounded_label_box = _bounded_page_bbox(label_box, width, height)
                xobject_candidate = _caption_adjacent_xobject(bounded_label_box, xobject_candidates, width, height, used=used_xobject_candidates) if bounded_label_box is not None else _unpaired_xobject_candidate(xobject_candidates, width, height, used=used_xobject_candidates)
                if xobject_candidate is not None:
                    panel_group = _xobject_panel_group(xobject_candidate, xobject_candidates, bounded_label_box, width, height, used=used_xobject_candidates, caption_boxes=[row[1] for row in figure_labels]) if bounded_label_box is not None else [xobject_candidate]
                    figure_box = _union_boxes([row["bbox"] for row in panel_group], width, height, fallback=xobject_candidate["bbox"])
                    xobject_provenance = dict(xobject_candidate.get("xobject_provenance") or {})
                    if bounded_label_box is None:
                        xobject_provenance["caption_geometry_status"] = "outside-page-review-required"
                    for group_candidate in panel_group:
                        used_xobject_candidates.add(_xobject_candidate_key(group_candidate))
                        _consume_xobject_candidate(group_candidate, xobject_candidates, used_xobject_candidates)
                    locator_attributes["geometry_provenance"] = xobject_provenance
                    if len(panel_group) > 1:
                        locator_attributes["panel_candidate_count"] = len(panel_group)
                        locator_attributes["panel_group_geometry_evidence"] = "caption-adjacent-xobject-group-v0.1"
                elif xobject_candidates:
                    # There is a real XObject on the page, but no deterministic
                    # caption adjacency. Keep only a caption candidate and a
                    # gap conflict; never substitute an arbitrary small path or
                    # use the caption bbox as false figure geometry.
                    figure_box = None
                    locator_attributes["geometry_provenance"] = {
                        "geometry_evidence": "caption-only-xobject-gap",
                        "xobject_candidate_count": len(xobject_candidates),
                        "candidate_only": True,
                    }
                    conflicts.append(build_visual_conflict(source_sha256=source_hash, physical_page=page_number, conflict_type="missing", details={"candidate": "figure", "label": f"Figure {label}", "reason": "caption_not_adjacent_to_xobject"}))
                else:
                    figure_box = _label_region(bounded_label_box, drawing_boxes, width, height, multiple_labels=len(figure_labels) > 1, scale=0.45) if bounded_label_box is not None else None
                    locator_attributes["geometry_provenance"] = {
                        "geometry_evidence": "native-path-caption-local-candidate-v0.1" if bounded_label_box is not None else "caption-outside-page-no-figure-geometry",
                        "candidate_only": True,
                    }
                figure = None
                if figure_box is not None:
                    figure = build_visual_object(source_sha256=source_hash, physical_page=page_number, object_type="figure", subtype="figure", page_width=width, page_height=height, bbox=figure_box, render_sha256=receipt["render_sha256"], render_dpi=int(receipt["dpi"]), crop_sha256=crop_commitment_sha256(render_bytes, figure_box, page_width=width, page_height=height, dpi=int(receipt["dpi"])) if render_bytes else sha256_json({"render_sha256": receipt["render_sha256"], "bbox": figure_box}), label=f"Figure {label}", page_geometry=page_geometry, attributes=locator_attributes, parser_receipt=parser_receipt or {"id": "pypdf-native-baseline", "version": "pypdf-6.10.0", "candidate_only": True}, backend_receipt=backend_receipt or {"id": "reviewer-locator-confirmed-bbox", "candidate_only": True}, model_receipt=model_receipt or {"id": "none", "available": False, "download": False}, config_receipt=config_receipt or {"id": "native-label-locator-v0.1", "candidate_only": True})
                    created.append(figure)
                caption = None
                if bounded_label_box is not None:
                    caption = build_visual_object(source_sha256=source_hash, physical_page=page_number, object_type="caption", subtype="figure-caption", page_width=width, page_height=height, bbox=bounded_label_box, render_sha256=receipt["render_sha256"], render_dpi=int(receipt["dpi"]), crop_sha256=crop_commitment_sha256(render_bytes, bounded_label_box, page_width=width, page_height=height, dpi=int(receipt["dpi"])) if render_bytes else sha256_json({"render_sha256": receipt["render_sha256"], "bbox": bounded_label_box}), parent=figure["visual_object_id"] if figure else None, label=f"Figure {label}", content_sha256=sha256_json(raw), page_geometry=page_geometry, attributes=locator_attributes, parser_receipt=figure["parser_receipt"] if figure else parser_receipt or {"id": "pypdf-native-baseline", "version": "v0.1", "candidate_only": True}, backend_receipt=figure["backend_receipt"] if figure else backend_receipt or {"id": "reviewer-locator-confirmed-bbox", "candidate_only": True}, model_receipt=figure["model_receipt"] if figure else model_receipt or {"id": "none", "available": False, "download": False}, config_receipt=figure["config_receipt"] if figure else config_receipt or {"id": "native-label-locator-v0.1", "candidate_only": True})
                    created.append(caption)
                else:
                    conflicts.append(build_visual_conflict(source_sha256=source_hash, physical_page=page_number, conflict_type="locator", details={"candidate": "caption", "label": f"Figure {label}", "reason": "caption_bbox_outside_page"}))
                if figure is not None and caption is not None:
                    relations.append(build_visual_relation(source_sha256=source_hash, relation_type="contains", source_visual_object_id=figure["visual_object_id"], target_visual_object_id=caption["visual_object_id"], physical_page=page_number, evidence_hashes=[figure["input_sha256"], caption["input_sha256"]]))
                    relations.append(build_visual_relation(source_sha256=source_hash, relation_type="caption_of", source_visual_object_id=caption["visual_object_id"], target_visual_object_id=figure["visual_object_id"], physical_page=page_number, evidence_hashes=[caption["input_sha256"]]))
                if len(panel_group) > 1:
                    if figure is None:
                        raise VisualSemanticsError("visual_panel_group_without_parent")
                    for panel_index, panel_candidate in enumerate(panel_group, start=1):
                        panel_box = list(panel_candidate["bbox"])
                        panel_provenance = dict(panel_candidate.get("xobject_provenance") or {})
                        panel_attributes = {
                            "geometry_provenance": panel_provenance,
                            "panel_index": panel_index,
                            "panel_group_size": len(panel_group),
                            "visual_confirmation": False,
                        }
                        panel = build_visual_object(
                            source_sha256=source_hash,
                            physical_page=page_number,
                            object_type="figure",
                            subtype="panel",
                            page_width=width,
                            page_height=height,
                            bbox=panel_box,
                            render_sha256=receipt["render_sha256"],
                            render_dpi=int(receipt["dpi"]),
                            crop_sha256=crop_commitment_sha256(render_bytes, panel_box, page_width=width, page_height=height, dpi=int(receipt["dpi"])) if render_bytes else sha256_json({"render_sha256": receipt["render_sha256"], "bbox": panel_box}),
                            parent=figure["visual_object_id"],
                            label=f"{figure['label']} panel {panel_index}",
                            page_geometry=page_geometry,
                            attributes=panel_attributes,
                            uncertainty={"level": "candidate", "geometry_confirmed": False, "numeric_values_verified": False},
                            parser_receipt=figure["parser_receipt"],
                            backend_receipt=figure["backend_receipt"],
                            model_receipt=figure["model_receipt"],
                            config_receipt=figure["config_receipt"],
                        )
                        created.append(panel)
                        relations.append(build_visual_relation(source_sha256=source_hash, relation_type="contains", source_visual_object_id=figure["visual_object_id"], target_visual_object_id=panel["visual_object_id"], physical_page=page_number, evidence_hashes=[figure["input_sha256"], panel["input_sha256"]]))
            for label, label_box, raw, locator_provenance in table_labels:
                locator_attributes = {"locator_provenance": locator_provenance}
                table_box, table_geometry = _ruling_table_candidate(label_box, drawing_boxes, width, height)
                if table_box is None:
                    xobject_candidate = _caption_adjacent_xobject(label_box, xobject_candidates, width, height, used=used_xobject_candidates)
                    if xobject_candidate is not None:
                        table_box = list(xobject_candidate["bbox"])
                        table_geometry = dict(xobject_candidate.get("xobject_provenance") or {})
                        table_geometry["geometry_evidence"] = "pdf-xobject-table-candidate-v0.1"
                        _consume_xobject_candidate(xobject_candidate, xobject_candidates, used_xobject_candidates)
                    else:
                        text_table_box, text_table_geometry = _text_table_candidate(label_box, page_boxes, width, height)
                        if text_table_box is not None:
                            table_box = text_table_box
                            table_geometry = text_table_geometry
                            conflicts.append(build_visual_conflict(source_sha256=source_hash, physical_page=page_number, conflict_type="missing", details={"candidate": "table", "label": f"Table {label}", "reason": "text_only_table_geometry_gap"}))
                        else:
                            table_box = list(label_box)
                            table_geometry = {
                                "geometry_evidence": "table-caption-only-gap",
                                "candidate_only": True,
                            }
                            conflicts.append(build_visual_conflict(source_sha256=source_hash, physical_page=page_number, conflict_type="missing", details={"candidate": "table", "label": f"Table {label}", "reason": "ruling_cluster_or_adjacent_xobject_unavailable"}))
                locator_attributes["geometry_provenance"] = table_geometry
                table = build_visual_object(source_sha256=source_hash, physical_page=page_number, object_type="table", subtype="table", page_width=width, page_height=height, bbox=table_box, render_sha256=receipt["render_sha256"], render_dpi=int(receipt["dpi"]), crop_sha256=crop_commitment_sha256(render_bytes, table_box, page_width=width, page_height=height, dpi=int(receipt["dpi"])) if render_bytes else sha256_json({"render_sha256": receipt["render_sha256"], "bbox": table_box}), label=f"Table {label}", page_geometry=page_geometry, attributes=locator_attributes, parser_receipt=parser_receipt or {"id": "pypdf-native-baseline", "version": "pypdf-6.10.0", "candidate_only": True}, backend_receipt=backend_receipt or {"id": "reviewer-locator-confirmed-bbox", "candidate_only": True}, model_receipt=model_receipt or {"id": "none", "available": False, "download": False}, config_receipt=config_receipt or {"id": "native-label-locator-v0.1", "candidate_only": True})
                created.append(table)
                caption = build_visual_object(source_sha256=source_hash, physical_page=page_number, object_type="caption", subtype="table-caption", page_width=width, page_height=height, bbox=label_box, render_sha256=receipt["render_sha256"], render_dpi=int(receipt["dpi"]), crop_sha256=crop_commitment_sha256(render_bytes, label_box, page_width=width, page_height=height, dpi=int(receipt["dpi"])) if render_bytes else sha256_json({"render_sha256": receipt["render_sha256"], "bbox": label_box}), parent=table["visual_object_id"], label=f"Table {label}", content_sha256=sha256_json(raw), page_geometry=page_geometry, attributes=locator_attributes, parser_receipt=table["parser_receipt"], backend_receipt=table["backend_receipt"], model_receipt=table["model_receipt"], config_receipt=table["config_receipt"])
                created.append(caption)
                relations.extend([
                    build_visual_relation(source_sha256=source_hash, relation_type="contains", source_visual_object_id=table["visual_object_id"], target_visual_object_id=caption["visual_object_id"], physical_page=page_number, evidence_hashes=[table["input_sha256"], caption["input_sha256"]]),
                    build_visual_relation(source_sha256=source_hash, relation_type="caption_of", source_visual_object_id=caption["visual_object_id"], target_visual_object_id=table["visual_object_id"], physical_page=page_number, evidence_hashes=[caption["input_sha256"]]),
                ])
                cells, row_defs, col_defs = _grid_cells(page_boxes, table_box, source_hash, table["visual_object_id"], page_width=width, page_height=height)
                if cells:
                    grid = build_table_grid(source_sha256=source_hash, table_visual_object_id=table["visual_object_id"], physical_page=page_number, rows=row_defs, columns=col_defs, cells=cells)
                    grids.append(grid)
                    for cell in grid["cells"]:
                        cell_obj = build_visual_object(source_sha256=source_hash, physical_page=page_number, object_type="table_cell", subtype="cell", page_width=width, page_height=height, bbox=cell["bbox"] or table_box, render_sha256=receipt["render_sha256"], render_dpi=int(receipt["dpi"]), crop_sha256=sha256_json({"render_sha256": receipt["render_sha256"], "bbox": cell["bbox"] or table_box}), parent=table["visual_object_id"], label=cell.get("text_candidate"), content_sha256=sha256_json(cell), page_geometry=page_geometry, parser_receipt=table["parser_receipt"], backend_receipt=table["backend_receipt"], model_receipt=table["model_receipt"], config_receipt=table["config_receipt"])
                        created.append(cell_obj)
                        relations.append(build_visual_relation(source_sha256=source_hash, relation_type="cell_of", source_visual_object_id=cell_obj["visual_object_id"], target_visual_object_id=table["visual_object_id"], physical_page=page_number, evidence_hashes=[grid["grid_sha256"]]))
            for label, label_box, raw, locator_provenance in equation_labels:
                equation = build_visual_object(source_sha256=source_hash, physical_page=page_number, object_type="equation_display", subtype="numbered-equation", page_width=width, page_height=height, bbox=label_box, render_sha256=receipt["render_sha256"], render_dpi=int(receipt["dpi"]), crop_sha256=crop_commitment_sha256(render_bytes, label_box, page_width=width, page_height=height, dpi=int(receipt["dpi"])) if render_bytes else sha256_json({"render_sha256": receipt["render_sha256"], "bbox": label_box}), label=f"({label})", content_sha256=sha256_json(raw), page_geometry=page_geometry, attributes={"locator_provenance": locator_provenance}, parser_receipt=parser_receipt or {"id": "pypdf-native-baseline", "version": "pypdf-6.10.0", "candidate_only": True}, backend_receipt=backend_receipt or {"id": "reviewer-locator-confirmed-bbox", "candidate_only": True}, model_receipt=model_receipt or {"id": "none", "available": False, "download": False}, config_receipt=config_receipt or {"id": "native-equation-label-locator-v0.1", "candidate_only": True})
                created.append(equation)
            marker_rows = _concise_plot_marker_rows(page_boxes)
            if marker_rows and drawing_boxes:
                marker_boxes = [list(row.get("bbox", drawing_boxes[0])) for row in marker_rows]
                local_drawings = [box for box in drawing_boxes if any(_bbox_intersects(box, marker_box) or abs(((box[1] + box[3]) / 2.0) - ((marker_box[1] + marker_box[3]) / 2.0)) < height * 0.35 for marker_box in marker_boxes)] or list(drawing_boxes)
                plot_box = _union_boxes(local_drawings, width, height, fallback=[0.0, height * 0.25, width, height * 0.8])
                plot = build_visual_object(source_sha256=source_hash, physical_page=page_number, object_type="plot", subtype="plot", page_width=width, page_height=height, bbox=plot_box, render_sha256=receipt["render_sha256"], render_dpi=int(receipt["dpi"]), crop_sha256=crop_commitment_sha256(render_bytes, plot_box, page_width=width, page_height=height, dpi=int(receipt["render_dpi"]) if "render_dpi" in receipt else int(receipt["dpi"])) if render_bytes else sha256_json({"render_sha256": receipt["render_sha256"], "bbox": plot_box}), label="plot", page_geometry=page_geometry, attributes={"geometry_evidence": "path-or-image-candidate", "axis_scale_candidate": "unknown", "series_values_candidate_only": True, "marker_policy": "concise-explicit-or-two-role-v0.2", "marker_label_max_chars": PLOT_MARKER_LABEL_MAX_CHARS}, uncertainty={"level": "candidate", "geometry_confirmed": False, "numeric_values_verified": False}, parser_receipt=parser_receipt or {"id": "pypdf-native-baseline", "version": "pypdf-6.10.0", "candidate_only": True}, backend_receipt=backend_receipt or {"id": "reviewer-locator-confirmed-bbox", "candidate_only": True}, model_receipt=model_receipt or {"id": "none", "available": False, "download": False}, config_receipt=config_receipt or {"id": "native-plot-marker-locator-v0.2-concise64", "candidate_only": True})
                created.append(plot)
                for row in marker_rows:
                    marker_text = str(row.get("text", ""))
                    marker_box = list(row.get("bbox", plot_box))
                    marker_type = "tick" if re.search(r"tick|刻度", marker_text, re.I) else ("axis" if re.search(r"axis|坐标轴|\bx\b|\by\b", marker_text, re.I) else ("legend" if re.search(r"legend|图例", marker_text, re.I) else "series"))
                    child_attributes = {"value_candidate": _first_number(marker_text), "unit_candidate": None, "scale_type_candidate": "unknown"} if marker_type in {"axis", "tick"} else {"series_mapping_candidate": "unresolved"} if marker_type == "series" else {"legend_mapping_candidate": "unresolved"}
                    child = build_visual_object(source_sha256=source_hash, physical_page=page_number, object_type=marker_type, subtype=marker_type, page_width=width, page_height=height, bbox=marker_box, render_sha256=receipt["render_sha256"], render_dpi=int(receipt["dpi"]), crop_sha256=crop_commitment_sha256(render_bytes, marker_box, page_width=width, page_height=height, dpi=int(receipt["dpi"])) if render_bytes else sha256_json({"render_sha256": receipt["render_sha256"], "bbox": marker_box}), parent=plot["visual_object_id"], label=marker_text, content_sha256=sha256_json(marker_text), page_geometry=page_geometry, attributes=child_attributes, uncertainty={"level": "candidate", "axis_or_series_visual_confirmation": False, "numeric_values_verified": False}, parser_receipt=plot["parser_receipt"], backend_receipt=plot["backend_receipt"], model_receipt=plot["model_receipt"], config_receipt=plot["config_receipt"])
                    created.append(child)
                    relation_type = {"axis": "axis_of", "tick": "axis_of", "legend": "legend_maps", "series": "series_of"}[marker_type]
                    relations.append(build_visual_relation(source_sha256=source_hash, relation_type=relation_type, source_visual_object_id=child["visual_object_id"], target_visual_object_id=plot["visual_object_id"], physical_page=page_number, evidence_hashes=[child["input_sha256"], plot["input_sha256"]]))
                    value_candidate = _first_number(marker_text)
                    if value_candidate is not None and marker_type in {"axis", "tick", "series"}:
                        digitized_attributes = {
                            "digitization_kind": "chart-value-candidate",
                            "value_candidate": value_candidate,
                            "unit_candidate": None,
                            "axis_scale_candidate": "unknown",
                            "error_model": {
                                "kind": "axis-resolution-candidate",
                                "absolute_error_candidate": None,
                                "relative_error_candidate": None,
                                "reason": "axis scale and pixel calibration require independent visual review",
                            },
                            "visual_confirmation": False,
                        }
                        digitized_parent = child["visual_object_id"] if marker_type == "series" else plot["visual_object_id"]
                        digitized = build_visual_object(source_sha256=source_hash, physical_page=page_number, object_type="annotation", subtype="digitized_value", page_width=width, page_height=height, bbox=marker_box, render_sha256=receipt["render_sha256"], render_dpi=int(receipt["dpi"]), crop_sha256=crop_commitment_sha256(render_bytes, marker_box, page_width=width, page_height=height, dpi=int(receipt["dpi"])) if render_bytes else sha256_json({"render_sha256": receipt["render_sha256"], "bbox": marker_box}), parent=digitized_parent, label=marker_text, content_sha256=sha256_json({"text": marker_text, "value": value_candidate}), page_geometry=page_geometry, attributes=digitized_attributes, uncertainty={"level": "candidate", "numeric_values_verified": False, "axis_or_series_visual_confirmation": False}, parser_receipt=plot["parser_receipt"], backend_receipt=plot["backend_receipt"], model_receipt=plot["model_receipt"], config_receipt=plot["config_receipt"])
                        created.append(digitized)
                        relations.append(build_visual_relation(source_sha256=source_hash, relation_type="supports", source_visual_object_id=digitized["visual_object_id"], target_visual_object_id=digitized_parent, physical_page=page_number, evidence_hashes=[digitized["input_sha256"], child["input_sha256"]]))
            elif PLOT_MARKER_RE.search(text):
                reason = "keyword-only-no-vector-or-image-geometry" if marker_rows else "prose-only-or-insufficient-chart-label-evidence"
                conflicts.append(build_visual_conflict(source_sha256=source_hash, physical_page=page_number, conflict_type="missing", details={"candidate": "plot", "reason": reason, "text_sha256": sha256_json(text)}))
        annotation_rows = _candidate_annotation_rows(page_boxes, drawing_boxes, created, page_width=width, page_height=height)
        for annotation_row in annotation_rows:
            parent = annotation_row["parent"]
            annotation_box = list(annotation_row["bbox"])
            annotation = build_visual_object(source_sha256=source_hash, physical_page=page_number, object_type="annotation", subtype=str(annotation_row["subtype"]), page_width=width, page_height=height, bbox=annotation_box, render_sha256=receipt["render_sha256"], render_dpi=int(receipt["dpi"]), crop_sha256=crop_commitment_sha256(render_bytes, annotation_box, page_width=width, page_height=height, dpi=int(receipt["dpi"])) if render_bytes else sha256_json({"render_sha256": receipt["render_sha256"], "bbox": annotation_box}), parent=parent["visual_object_id"], label=annotation_row.get("text"), content_sha256=sha256_json(annotation_row.get("text") or annotation_row["attributes"]), page_geometry=page_geometry, attributes=dict(annotation_row["attributes"]), uncertainty={"level": "candidate", "geometry_confirmed": False, "numeric_values_verified": False}, parser_receipt=parent["parser_receipt"], backend_receipt=parent["backend_receipt"], model_receipt=parent["model_receipt"], config_receipt=parent["config_receipt"])
            created.append(annotation)
            relation_type = "labels" if annotation_row["subtype"] == "label" else "supports"
            relations.append(build_visual_relation(source_sha256=source_hash, relation_type=relation_type, source_visual_object_id=annotation["visual_object_id"], target_visual_object_id=parent["visual_object_id"], physical_page=page_number, evidence_hashes=[annotation["input_sha256"], parent["input_sha256"]]))
        objects.extend(created)
        if len(created) > 1:
            by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for row in created:
                by_key.setdefault((str(row["type"]), str(row.get("label"))), []).append(row)
            for (object_type, label), rows in sorted(by_key.items()):
                if label and len(rows) > 1:
                    conflicts.append(build_visual_conflict(source_sha256=source_hash, physical_page=page_number, conflict_type="duplicate", object_ids=[row["visual_object_id"] for row in rows], details={"type": object_type, "label": label}))
    objects, relations, grids, conflicts = deduplicate_visual_candidates(objects, relations, grids, conflicts)
    table_continuations = build_table_continuation_candidates(objects, grids)
    return {
        "schema_version": VISUAL_PROTOCOL,
        "source_sha256": source_hash,
        "pages": selected,
        "objects": objects,
        "relations": relations,
        "table_grids": grids,
        "table_continuations": table_continuations,
        "conflicts": conflicts,
        "render_receipts": [render_contexts[page][0]["render"] for page in selected],
        "candidate_only": True,
        "whole_book_completeness_claimed": False,
    }


def _grid_cells(page_boxes: Sequence[Mapping[str, Any]], table_bbox: Sequence[float], source_sha256: str, table_id: str, *, page_width: float | None = None, page_height: float | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    source_rows = [
        row for row in page_boxes
        if _bbox_intersects(row.get("bbox"), table_bbox)
        and str(row.get("text", "")).strip()
        and not re.match(r"^\s*(?:Table|Tab(?:le)?\.?|表)\s*" + NUMBER_TOKEN, str(row.get("text", "")), re.I)
    ]
    if not source_rows:
        return [], [], []
    heights = sorted(max(1.0, float(row["bbox"][3]) - float(row["bbox"][1])) for row in source_rows)
    row_tolerance = max(3.0, heights[len(heights) // 2] * 0.75)
    row_centers: list[float] = []
    for row in sorted(source_rows, key=lambda item: (float(item["bbox"][1]), float(item["bbox"][0]))):
        center = (float(row["bbox"][1]) + float(row["bbox"][3])) / 2.0
        if not row_centers or abs(center - row_centers[-1]) > row_tolerance:
            row_centers.append(center)
        else:
            row_centers[-1] = (row_centers[-1] + center) / 2.0
    tokens: list[dict[str, Any]] = []
    for row in source_rows:
        text = str(row.get("text", "")).strip()
        bbox = list(map(float, row["bbox"]))
        words = list(re.finditer(r"\S+", text))
        if len(words) <= 1:
            tokens.append({"text": text, "bbox": bbox, "row_bbox": bbox})
            continue
        char_width = max(1.0, (bbox[2] - bbox[0]) / max(1, len(text)))
        for match in words:
            left = bbox[0] + match.start() * char_width
            right = max(left + char_width, bbox[0] + match.end() * char_width)
            tokens.append({"text": match.group(0), "bbox": [left, bbox[1], min(bbox[2], right), bbox[3]], "row_bbox": bbox})
    widths = sorted(max(1.0, float(row["bbox"][2]) - float(row["bbox"][0])) for row in tokens)
    column_tolerance = max(8.0, (widths[len(widths) // 2] if widths else 10.0) * 0.55)
    column_centers: list[float] = []
    for token in sorted(tokens, key=lambda item: (float(item["bbox"][0]), float(item["bbox"][1]))):
        center = (float(token["bbox"][0]) + float(token["bbox"][2])) / 2.0
        if not column_centers or all(abs(center - candidate) > column_tolerance for candidate in column_centers):
            column_centers.append(center)
        else:
            nearest = min(range(len(column_centers)), key=lambda index: abs(column_centers[index] - center))
            column_centers[nearest] = (column_centers[nearest] + center) / 2.0
    column_centers.sort()
    if not row_centers or not column_centers:
        return [], [], []
    rows = [{"row_id": stable_id("tr", source_sha256, table_id, index), "index": index} for index in range(len(row_centers))]
    columns = [{"column_id": stable_id("tcol", source_sha256, table_id, index), "index": index, "label": None} for index in range(len(column_centers))]
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for token in tokens:
        bbox = token["bbox"]
        center_y = (bbox[1] + bbox[3]) / 2.0
        center_x = (bbox[0] + bbox[2]) / 2.0
        row_index = min(range(len(row_centers)), key=lambda index: abs(row_centers[index] - center_y))
        col_index = min(range(len(column_centers)), key=lambda index: abs(column_centers[index] - center_x))
        grouped.setdefault((row_index, col_index), []).append(token)
    cells: list[dict[str, Any]] = []
    for (row_index, col_index), group in sorted(grouped.items()):
        group = sorted(group, key=lambda item: float(item["bbox"][0]))
        text = " ".join(str(item["text"]) for item in group).strip()
        raw_bbox = [min(float(item["bbox"][0]) for item in group), min(float(item["bbox"][1]) for item in group), max(float(item["bbox"][2]) for item in group), max(float(item["bbox"][3]) for item in group)]
        # Text visitors can emit a reversed glyph box and binary-float tails.
        # Freeze cell geometry to the same six-decimal page contract used by
        # visual objects before source/crop hashes are created.
        left, bottom = min(raw_bbox[0], raw_bbox[2]), min(raw_bbox[1], raw_bbox[3])
        right, top = max(raw_bbox[0], raw_bbox[2]), max(raw_bbox[1], raw_bbox[3])
        if page_width is not None and page_height is not None:
            # Visitor glyph estimates can cross the physical page by a tiny
            # amount. Clamp only to the known table object's MediaBox; never
            # invent a page-wide bbox. A degenerate result remains explicitly
            # unresolved and is emitted as bbox=null below.
            left, right = max(0.0, min(float(page_width), left)), max(0.0, min(float(page_width), right))
            bottom, top = max(0.0, min(float(page_height), bottom)), max(0.0, min(float(page_height), top))
        bbox = [round(left, 6), round(bottom, 6), round(right, 6), round(top, 6)] if left < right and bottom < top else None
        value = _first_number(text)
        words = text.split()
        unit = next((word for word in words if re.fullmatch(r"[A-Za-zµμ/%][A-Za-z0-9µμ/%^*.-]{0,15}", word) and not NUMBER_RE.fullmatch(word)), None)
        symbol = next((word for word in words if re.fullmatch(r"(?:[A-Za-z_][A-Za-z0-9_]*|[α-ωΑ-Ω]+)", word) and word != unit), None)
        cells.append({
            "cell_id": stable_id("tc", source_sha256, table_id, row_index, col_index, text),
            "row_id": rows[row_index]["row_id"],
            "column_id": columns[col_index]["column_id"],
            "row_span": 1,
            "column_span": 1,
            "text_candidate": text,
            "value_candidate": value,
            "unit_candidate": unit,
            "symbol_candidate": symbol,
            "typed_candidates": {"value": value, "unit": unit, "symbol": symbol, "confidence": "candidate"},
            "source_anchor": {"source_sha256": source_sha256, "table_visual_object_id": table_id, "text_sha256": sha256_json(text)},
            "crop_sha256": sha256_json({"table": table_id, "bbox": bbox}),
            "bbox": bbox,
        })
    return cells, rows, columns


def _first_number(value: Any) -> str | None:
    match = NUMBER_RE.search(str(value or ""))
    return match.group(0) if match else None


def _bbox_intersects(a: Any, b: Sequence[float]) -> bool:
    if not isinstance(a, (list, tuple)) or len(a) != 4 or len(b) != 4:
        return False
    return not (float(a[2]) < float(b[0]) or float(a[0]) > float(b[2]) or float(a[3]) < float(b[1]) or float(a[1]) > float(b[3]))


def deduplicate_visual_candidates(objects: Sequence[Mapping[str, Any]], relations: Sequence[Mapping[str, Any]], grids: Sequence[Mapping[str, Any]], conflicts: Sequence[Mapping[str, Any]], *, iou_threshold: float = 0.92) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    duplicate_map: dict[str, str] = {}
    extra_conflicts = [dict(row) for row in conflicts if isinstance(row, Mapping)]
    for raw in sorted((dict(row) for row in objects if isinstance(row, Mapping)), key=lambda row: str(row.get("visual_object_id"))):
        duplicate = None
        for previous in kept:
            if previous.get("source_sha256") == raw.get("source_sha256") and previous.get("physical_page") == raw.get("physical_page") and previous.get("type") == raw.get("type") and previous.get("subtype") == raw.get("subtype") and previous.get("parent") == raw.get("parent") and _bbox_iou(previous.get("bbox"), raw.get("bbox")) >= iou_threshold:
                duplicate = previous
                break
        if duplicate is not None:
            # The duplicate candidate is deliberately not emitted twice.  Keep
            # its deterministic ID in conflict details, while the ledger's
            # object_ids remain resolvable inside the normalized candidate set.
            duplicate_map[str(raw.get("visual_object_id"))] = str(duplicate.get("visual_object_id"))
            extra_conflicts.append(build_visual_conflict(source_sha256=str(raw.get("source_sha256")), physical_page=int(raw.get("physical_page")), conflict_type="duplicate", object_ids=[str(duplicate.get("visual_object_id"))], details={"duplicate_candidate_id": str(raw.get("visual_object_id")), "iou": _bbox_iou(duplicate.get("bbox"), raw.get("bbox"))}))
        else:
            kept.append(raw)
    ids = {str(row.get("visual_object_id")) for row in kept}
    # Preserve parentage when a parent was itself deduplicated.  If a caller
    # supplies only a child (as a bounded adapter result can), clear the
    # otherwise-unresolvable parent and refresh the content commitment rather
    # than emitting a dangling reference.
    for row in kept:
        parent = row.get("parent")
        mapped_parent = duplicate_map.get(str(parent), str(parent)) if parent is not None else None
        if mapped_parent not in ids:
            mapped_parent = None
        if mapped_parent == row.get("visual_object_id"):
            extra_conflicts.append(build_visual_conflict(source_sha256=str(row.get("source_sha256")), physical_page=int(row.get("physical_page")), conflict_type="overlap", object_ids=[str(row.get("visual_object_id"))], details={"reason": "self_parent_removed"}))
            mapped_parent = None
        if row.get("parent") != mapped_parent:
            row["parent"] = mapped_parent
            row["object_sha256"] = _object_hash(row)
    # A parser can report a duplicate group before the normalizer drops a
    # member. Keep the ledger entry, but never leave dangling object
    # references in the candidate bundle. Dropped IDs remain auditable in
    # ``details`` when the deduplicator created the conflict.
    normalized_conflicts: list[dict[str, Any]] = []
    for conflict in extra_conflicts:
        row = dict(conflict)
        row["object_ids"] = sorted(
            duplicate_map.get(str(identifier), str(identifier))
            for identifier in row.get("object_ids", [])
            if duplicate_map.get(str(identifier), str(identifier)) in ids
        )
        row["conflict_sha256"] = sha256_json(
            {key: value for key, value in row.items() if key != "conflict_sha256"}
        )
        normalized_conflicts.append(row)
    kept_relations: list[dict[str, Any]] = []
    for raw in relations:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        row["source_visual_object_id"] = duplicate_map.get(str(row.get("source_visual_object_id")), str(row.get("source_visual_object_id")))
        row["target_visual_object_id"] = duplicate_map.get(str(row.get("target_visual_object_id")), str(row.get("target_visual_object_id")))
        if row["source_visual_object_id"] == row["target_visual_object_id"]:
            extra_conflicts.append(build_visual_conflict(source_sha256=str(row.get("source_sha256")), physical_page=int(row.get("physical_page")), conflict_type="overlap", object_ids=[row["source_visual_object_id"]], details={"reason": "self_relation_removed", "relation_type": row.get("relation_type")}))
            continue
        if row["source_visual_object_id"] not in ids or row["target_visual_object_id"] not in ids:
            continue
        row["relation_sha256"] = sha256_json({key: value for key, value in row.items() if key not in {"relation_sha256", "status"}})
        kept_relations.append(row)
    kept_grids: list[dict[str, Any]] = []
    for raw in grids:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        row["table_visual_object_id"] = duplicate_map.get(str(row.get("table_visual_object_id")), str(row.get("table_visual_object_id")))
        if row["table_visual_object_id"] not in ids:
            continue
        row["grid_sha256"] = sha256_json({key: value for key, value in row.items() if key not in {"grid_sha256", "status"}})
        kept_grids.append(row)
    return kept, sorted(kept_relations, key=lambda row: str(row.get("visual_relation_id"))), sorted(kept_grids, key=lambda row: str(row.get("table_grid_id"))), sorted(normalized_conflicts, key=lambda row: str(row.get("visual_conflict_id")))


def _bbox_iou(a: Any, b: Any) -> float:
    if not isinstance(a, (list, tuple)) or not isinstance(b, (list, tuple)) or len(a) != 4 or len(b) != 4:
        return 0.0
    x0, y0, x1, y1 = max(float(a[0]), float(b[0])), max(float(a[1]), float(b[1])), min(float(a[2]), float(b[2])), min(float(a[3]), float(b[3]))
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    area_a = max(0.0, float(a[2]) - float(a[0])) * max(0.0, float(a[3]) - float(a[1]))
    area_b = max(0.0, float(b[2]) - float(b[0])) * max(0.0, float(b[3]) - float(b[1]))
    union = area_a + area_b - intersection
    return intersection / union if union else 0.0


def _finite_box(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and len(value) == 4 and all(
        isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(float(item))
        for item in value
    ) and float(value[0]) < float(value[2]) and float(value[1]) < float(value[3])


def _visual_input_hash(row: Mapping[str, Any]) -> str:
    return visual_input_sha256(
        source_sha256=str(row.get("source_sha256", "")),
        physical_page=int(row.get("physical_page", 0) or 0),
        render_sha256=str(row.get("canonical_render_sha256", "")),
        render_dpi=int(row.get("canonical_render_dpi", 0) or 0),
        rotation=int(row.get("canonical_render_rotation", 0) or 0),
        coordinate_space=str(row.get("coordinate_space", COORDINATE_SPACE)),
        bbox=row.get("bbox", []),
        crop_sha256=row.get("crop_sha256"),
        parser_receipt=row.get("parser_receipt", {}),
        backend_receipt=row.get("backend_receipt", {}),
        model_receipt=row.get("model_receipt", {}),
        config_receipt=row.get("config_receipt", {}),
        page_geometry=row.get("page_geometry", {}),
        attributes=row.get("attributes", {}),
        uncertainty=row.get("uncertainty", {}),
    )


def validate_visual_bundle(bundle: Mapping[str, Any], *, source_sha256: str | None = None, page_sizes: Mapping[int, Sequence[float]] | None = None, require_candidate_only: bool = True) -> list[VisualIssue]:
    issues: list[VisualIssue] = []
    expected_source = source_sha256 or bundle.get("source_sha256")
    objects = bundle.get("objects", []) if isinstance(bundle, Mapping) else []
    relations = bundle.get("relations", []) if isinstance(bundle, Mapping) else []
    grids = bundle.get("table_grids", []) if isinstance(bundle, Mapping) else []
    conflicts = bundle.get("conflicts", []) if isinstance(bundle, Mapping) else []
    render_by_page = {
        int(row.get("physical_page")): str(row.get("render_sha256"))
        for row in bundle.get("render_receipts", [])
        if isinstance(row, Mapping) and isinstance(row.get("physical_page"), int)
    } if isinstance(bundle, Mapping) and isinstance(bundle.get("render_receipts"), list) else {}
    object_by_id: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(objects if isinstance(objects, list) else []):
        path = f"visual-objects.jsonl:{index + 1}"
        if not isinstance(raw, Mapping) or raw.get("schema_version") != VISUAL_OBJECT_SCHEMA:
            issues.append(VisualIssue("error", "visual_object_invalid", path, "Unexpected visual object schema."))
            continue
        identifier = raw.get("visual_object_id")
        if not isinstance(identifier, str) or identifier in object_by_id:
            issues.append(VisualIssue("error", "visual_object_duplicate", path, "Visual object ID missing or duplicated."))
            continue
        object_by_id[identifier] = dict(raw)
        if raw.get("type") not in OBJECT_TYPES or raw.get("status") not in STATUS_VALUES:
            issues.append(VisualIssue("error", "visual_object_invalid", path, "Type or status is outside the contract."))
        if expected_source and raw.get("source_sha256") != expected_source:
            issues.append(VisualIssue("error", "source_hash_mismatch", path, "Visual object source hash differs."))
        if not isinstance(raw.get("physical_page"), int) or raw.get("physical_page", 0) < 1:
            issues.append(VisualIssue("error", "visual_object_invalid", path, "Physical page must be 1-based."))
        bbox = raw.get("bbox")
        width, height = (page_sizes or {}).get(int(raw.get("physical_page", 0)), (raw.get("page_width"), raw.get("page_height")))
        geometry = raw.get("page_geometry")
        if not isinstance(geometry, Mapping):
            issues.append(VisualIssue("error", "visual_provenance_incomplete", f"{path}.page_geometry", "MediaBox/CropBox geometry receipt is required."))
        else:
            geometry_allowed = {"media_box", "crop_box", "rotation", "coordinate_origin", "physical_page"}
            unknown_geometry = set(geometry) - geometry_allowed
            missing_geometry = geometry_allowed - set(geometry)
            if unknown_geometry or missing_geometry:
                issues.append(VisualIssue("error", "visual_page_geometry_invalid", f"{path}.page_geometry", "Page geometry has unknown or missing fields."))
            if not _finite_box(geometry.get("media_box")) or not _finite_box(geometry.get("crop_box")):
                issues.append(VisualIssue("error", "visual_page_geometry_invalid", f"{path}.page_geometry", "MediaBox and CropBox must be finite four-number boxes."))
            if geometry.get("coordinate_origin") != "mediabox-top-left":
                issues.append(VisualIssue("error", "visual_page_geometry_invalid", f"{path}.page_geometry.coordinate_origin", "The canonical bbox origin must be mediabox-top-left."))
            if geometry.get("rotation") not in {0, 90, 180, 270} or geometry.get("rotation") != raw.get("canonical_render_rotation"):
                issues.append(VisualIssue("error", "visual_page_geometry_invalid", f"{path}.page_geometry.rotation", "Page rotation is missing or inconsistent with the render receipt."))
            if geometry.get("physical_page") != raw.get("physical_page"):
                issues.append(VisualIssue("error", "visual_page_geometry_invalid", f"{path}.page_geometry.physical_page", "Page geometry is bound to a different physical page."))
            if _finite_box(geometry.get("media_box")) and isinstance(width, (int, float)) and isinstance(height, (int, float)):
                media = geometry["media_box"]
                if not math.isclose(float(media[2]) - float(media[0]), float(width), rel_tol=0.0, abs_tol=1e-5) or not math.isclose(float(media[3]) - float(media[1]), float(height), rel_tol=0.0, abs_tol=1e-5):
                    issues.append(VisualIssue("error", "visual_page_geometry_invalid", f"{path}.page_geometry.media_box", "MediaBox extent differs from page dimensions."))
        attributes = raw.get("attributes")
        if not isinstance(attributes, Mapping):
            issues.append(VisualIssue("error", "visual_provenance_incomplete", f"{path}.attributes", "Visual attributes map is required."))
        geometry_provenance = attributes.get("geometry_provenance") if isinstance(attributes, Mapping) else None
        if (
            raw.get("type") == "figure"
            and raw.get("subtype") == "figure"
            and isinstance(geometry_provenance, Mapping)
            and str(geometry_provenance.get("geometry_evidence", "")).startswith("caption-only")
        ):
            # A caption-only path may remain a caption plus a missing conflict,
            # but it can never be represented as a figure with a precise bbox.
            # Rejecting the whole figure also catches legacy records whose
            # bbox happens to be nearly identical to their caption bbox.
            issues.append(VisualIssue("error", "visual_unlocalized_bbox_forbidden", path, "Caption-only figure geometry cannot be emitted as a figure bbox."))
        uncertainty = raw.get("uncertainty")
        uncertainty_allowed = {"level", "numeric_values_verified", "geometry_confirmed", "series_values_verified", "axis_or_series_visual_confirmation"}
        if not isinstance(uncertainty, Mapping) or "level" not in uncertainty or set(uncertainty) - uncertainty_allowed:
            issues.append(VisualIssue("error", "visual_uncertainty_invalid", f"{path}.uncertainty", "Candidate uncertainty must be a closed, level-bearing object."))
        try:
            normalized = normalize_bbox(bbox, float(width), float(height))
            if normalized != bbox:
                issues.append(VisualIssue("error", "bbox_page_mismatch", path, "Bbox is not canonical top-left points."))
        except (TypeError, ValueError, VisualSemanticsError):
            issues.append(VisualIssue("error", "bbox_page_mismatch", path, "Bbox is malformed or outside page geometry."))
        if not HASH_RE.fullmatch(str(raw.get("canonical_render_sha256", ""))):
            issues.append(VisualIssue("error", "render_hash_mismatch", path, "Canonical render SHA-256 is missing."))
        elif render_by_page.get(int(raw.get("physical_page", 0))) not in {None, raw.get("canonical_render_sha256")}:
            issues.append(VisualIssue("error", "render_hash_mismatch", path, "Canonical render SHA-256 differs from the page receipt."))
        crop = raw.get("crop_sha256")
        if crop is not None and not HASH_RE.fullmatch(str(crop)):
            issues.append(VisualIssue("error", "crop_hash_mismatch", path, "Crop SHA-256 is malformed."))
        expected_input_sha256 = None
        try:
            expected_input_sha256 = _visual_input_hash(raw)
        except (TypeError, ValueError, KeyError):
            pass
        if expected_input_sha256 is not None and raw.get("input_sha256") != expected_input_sha256:
            issues.append(VisualIssue("error", "visual_input_hash_mismatch", path, "Visual input hash does not bind geometry, receipts, and uncertainty."))
        if raw.get("object_sha256") != _object_hash(raw):
            # The object hash is deliberately a whole-record commitment.  A
            # crop is stored as a hash only, so a validator that does not have
            # the render bytes cannot recompute its pixels.  Still expose the
            # stable, actionable crop-bound failure when the input commitment
            # no longer agrees with the record; retain the generic integrity
            # issue as well so callers cannot mistake it for verified data.
            if raw.get("crop_sha256") is not None and HASH_RE.fullmatch(str(raw.get("crop_sha256"))) and raw.get("input_sha256") != expected_input_sha256:
                issues.append(VisualIssue("error", "crop_hash_mismatch", path, "Crop commitment no longer matches the bound visual input."))
            issues.append(VisualIssue("error", "visual_object_invalid", path, "Visual object content hash mismatch."))
        if require_candidate_only and raw.get("status") != "candidate":
            issues.append(VisualIssue("error", "visual_candidate_promotion_forbidden", path, "Candidate extraction cannot self-promote."))
        for receipt_key in ("parser_receipt", "backend_receipt", "model_receipt", "config_receipt"):
            if not isinstance(raw.get(receipt_key), Mapping):
                issues.append(VisualIssue("error", "visual_provenance_incomplete", f"{path}.{receipt_key}", "Provenance receipt is required."))
        # Parent IDs are checked after the complete object set is indexed; a
        # caption can sort before its figure's stable ID.
    for index, raw in enumerate(objects if isinstance(objects, list) else []):
        if isinstance(raw, Mapping) and raw.get("parent") is not None:
            if raw.get("parent") == raw.get("visual_object_id"):
                issues.append(VisualIssue("error", "visual_parent_self_loop", f"visual-objects.jsonl:{index + 1}.parent", "A visual object cannot parent itself."))
            elif raw.get("parent") not in object_by_id:
                issues.append(VisualIssue("error", "visual_parent_unresolved", f"visual-objects.jsonl:{index + 1}.parent", "Parent visual object is absent."))
    for index, raw in enumerate(relations if isinstance(relations, list) else []):
        path = f"visual-relations.jsonl:{index + 1}"
        if not isinstance(raw, Mapping) or raw.get("schema_version") != VISUAL_RELATION_SCHEMA or raw.get("relation_type") not in RELATION_TYPES:
            issues.append(VisualIssue("error", "visual_relation_invalid", path, "Relation schema/type invalid."))
            continue
        if raw.get("source_visual_object_id") == raw.get("target_visual_object_id"):
            issues.append(VisualIssue("error", "visual_relation_self_loop", path, "A visual relation cannot connect an object to itself."))
            continue
        if raw.get("source_visual_object_id") not in object_by_id or raw.get("target_visual_object_id") not in object_by_id:
            issues.append(VisualIssue("error", "visual_relation_unresolved", path, "Relation endpoint is absent."))
        elif raw.get("source_sha256") != object_by_id[raw.get("source_visual_object_id")].get("source_sha256"):
            issues.append(VisualIssue("error", "source_hash_mismatch", path, "Relation source hash differs from endpoints."))
        if raw.get("relation_sha256") != sha256_json({key: value for key, value in raw.items() if key not in {"relation_sha256", "status"}}):
            issues.append(VisualIssue("error", "visual_relation_invalid", path, "Relation content hash mismatch."))
    grid_allowed = {"schema_version", "table_grid_id", "table_visual_object_id", "source_sha256", "physical_page", "rows", "columns", "cells", "topology_evidence", "caption_object_ids", "table_note_candidates", "continuation_candidate", "status", "grid_sha256"}
    row_allowed = {"row_id", "index"}
    column_allowed = {"column_id", "index", "label"}
    cell_allowed = {"cell_id", "row_id", "column_id", "row_span", "column_span", "text_candidate", "value_candidate", "unit_candidate", "symbol_candidate", "bbox", "typed_candidates", "source_anchor", "crop_sha256"}
    typed_allowed = {"value", "unit", "symbol", "confidence"}
    anchor_allowed = {"source_sha256", "table_visual_object_id", "cell_id", "physical_page", "bbox", "text_sha256", "anchor_id"}
    for index, raw in enumerate(grids if isinstance(grids, list) else []):
        path = f"table-grids.jsonl:{index + 1}"
        if not isinstance(raw, Mapping) or raw.get("schema_version") != TABLE_GRID_SCHEMA or raw.get("table_visual_object_id") not in object_by_id:
            issues.append(VisualIssue("error", "table_grid_invalid", path, "Table grid is unbound."))
            continue
        table_object = object_by_id[raw["table_visual_object_id"]]
        if set(raw) - grid_allowed:
            issues.append(VisualIssue("error", "table_grid_invalid", path, "Table grid has unknown top-level fields."))
        if raw.get("source_sha256") != table_object.get("source_sha256") or raw.get("physical_page") != table_object.get("physical_page"):
            issues.append(VisualIssue("error", "table_grid_binding_invalid", path, "Table grid source/page differs from its table object."))
        if raw.get("grid_sha256") != sha256_json({key: value for key, value in raw.items() if key not in {"grid_sha256", "status"}}):
            issues.append(VisualIssue("error", "table_grid_invalid", path, "Table grid content hash mismatch."))
        rows = raw.get("rows")
        columns = raw.get("columns")
        cells = raw.get("cells")
        if not isinstance(rows, list) or not isinstance(columns, list) or not isinstance(cells, list):
            issues.append(VisualIssue("error", "table_grid_invalid", path, "Table grid rows, columns, and cells must be arrays."))
            continue
        row_ids = set()
        row_index_by_id: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, Mapping) or set(row) - row_allowed or set(row) != row_allowed or not isinstance(row.get("row_id"), str) or not isinstance(row.get("index"), int) or row.get("row_id") in row_ids:
                issues.append(VisualIssue("error", "table_topology_invalid", path, "Rows use a closed row topology shape."))
            elif row.get("row_id"):
                row_ids.add(row["row_id"])
                row_index_by_id[row["row_id"]] = row["index"]
        if sorted(row_index_by_id.values()) != list(range(len(row_index_by_id))):
            issues.append(VisualIssue("error", "table_topology_invalid", path, "Row indices must be contiguous from zero."))
        column_ids = set()
        column_index_by_id: dict[str, int] = {}
        for column in columns:
            if not isinstance(column, Mapping) or set(column) - column_allowed or not {"column_id", "index"}.issubset(column) or not isinstance(column.get("column_id"), str) or not isinstance(column.get("index"), int) or column.get("column_id") in column_ids:
                issues.append(VisualIssue("error", "table_topology_invalid", path, "Columns use a closed column topology shape."))
            elif column.get("column_id"):
                column_ids.add(column["column_id"])
                column_index_by_id[column["column_id"]] = column["index"]
        if sorted(column_index_by_id.values()) != list(range(len(column_index_by_id))):
            issues.append(VisualIssue("error", "table_topology_invalid", path, "Column indices must be contiguous from zero."))
        cell_ids = set()
        occupied: set[tuple[int, int]] = set()
        page_width, page_height = table_object.get("page_width"), table_object.get("page_height")
        for cell in cells:
            if not isinstance(cell, Mapping) or set(cell) - cell_allowed or not cell_allowed.issubset(set(cell)):
                issues.append(VisualIssue("error", "table_cell_invalid", path, "Cells use a closed provenance/topology shape."))
                continue
            if cell.get("cell_id") in cell_ids or cell.get("row_id") not in row_ids or cell.get("column_id") not in column_ids:
                issues.append(VisualIssue("error", "table_cell_unresolved", path, "Cell row/column binding is unresolved."))
            cell_ids.add(cell.get("cell_id"))
            if not isinstance(cell.get("row_span"), int) or cell.get("row_span", 0) < 1 or not isinstance(cell.get("column_span"), int) or cell.get("column_span", 0) < 1:
                issues.append(VisualIssue("error", "table_cell_invalid", path, "Cell spans must be positive integers."))
            elif cell.get("row_id") in row_index_by_id and cell.get("column_id") in column_index_by_id:
                row_start = row_index_by_id[cell["row_id"]]
                column_start = column_index_by_id[cell["column_id"]]
                row_end = row_start + cell["row_span"]
                column_end = column_start + cell["column_span"]
                if row_end > len(row_index_by_id) or column_end > len(column_index_by_id):
                    issues.append(VisualIssue("error", "table_topology_span_out_of_bounds", path, "Cell span exceeds the declared row or column topology."))
                else:
                    for row_slot in range(row_start, row_end):
                        for column_slot in range(column_start, column_end):
                            slot = (row_slot, column_slot)
                            if slot in occupied:
                                issues.append(VisualIssue("error", "table_topology_overlap", path, "Cell spans overlap an occupied topology slot."))
                            occupied.add(slot)
            if cell.get("bbox") is not None:
                try:
                    if not _finite_box(cell.get("bbox")) or normalize_bbox(cell.get("bbox"), float(page_width), float(page_height)) != list(cell.get("bbox")):
                        raise ValueError
                except (TypeError, ValueError, VisualSemanticsError):
                    issues.append(VisualIssue("error", "table_cell_bbox_invalid", path, "Cell bbox is not canonical page geometry."))
            typed = cell.get("typed_candidates")
            if not isinstance(typed, Mapping) or set(typed) != typed_allowed or typed.get("confidence") not in {"candidate", "low", "medium", "high"}:
                issues.append(VisualIssue("error", "table_cell_typed_candidates_invalid", path, "Typed candidates must use the closed value/unit/symbol/confidence shape."))
            elif typed.get("value") != cell.get("value_candidate") or typed.get("unit") != cell.get("unit_candidate") or typed.get("symbol") != cell.get("symbol_candidate"):
                issues.append(VisualIssue("error", "table_cell_typed_candidates_invalid", path, "Typed candidate values differ from the cell candidates."))
            anchor = cell.get("source_anchor")
            if not isinstance(anchor, Mapping) or set(anchor) - anchor_allowed or not {"source_sha256", "table_visual_object_id", "cell_id", "physical_page", "bbox"}.issubset(set(anchor)):
                issues.append(VisualIssue("error", "table_cell_source_anchor_invalid", path, "Cell source anchor is incomplete or has unknown fields."))
            elif anchor.get("source_sha256") != raw.get("source_sha256") or anchor.get("table_visual_object_id") != raw.get("table_visual_object_id") or anchor.get("cell_id") != cell.get("cell_id") or anchor.get("physical_page") != raw.get("physical_page") or anchor.get("bbox") != cell.get("bbox"):
                issues.append(VisualIssue("error", "table_cell_source_anchor_mismatch", path, "Cell source anchor is not bound to the cell/grid."))
            if cell.get("crop_sha256") is not None and not HASH_RE.fullmatch(str(cell.get("crop_sha256"))):
                issues.append(VisualIssue("error", "table_cell_crop_hash_mismatch", path, "Cell crop SHA-256 is malformed."))
    for index, raw in enumerate(conflicts if isinstance(conflicts, list) else []):
        path = f"visual-conflicts.jsonl:{index + 1}"
        if not isinstance(raw, Mapping) or raw.get("schema_version") != VISUAL_CONFLICT_SCHEMA or raw.get("conflict_type") not in CONFLICT_TYPES:
            issues.append(VisualIssue("error", "visual_conflict_invalid", path, "Conflict schema/type invalid."))
            continue
        if any(identifier not in object_by_id for identifier in raw.get("object_ids", [])):
            issues.append(VisualIssue("error", "visual_conflict_invalid", path, "Conflict references an absent object."))
        if raw.get("conflict_sha256") != sha256_json({key: value for key, value in raw.items() if key != "conflict_sha256"}):
            issues.append(VisualIssue("error", "visual_conflict_invalid", path, "Conflict content hash mismatch."))
    unique = {(issue.code, issue.path, issue.message): issue for issue in issues}
    return sorted(unique.values(), key=lambda issue: (issue.code, issue.path, issue.message))


def _visual_review_input_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact, reviewable fields bound by an attestation hash."""
    def _values(name: str) -> list[str]:
        value = raw.get(name, [])
        return sorted(str(item) for item in value) if isinstance(value, list) else []

    payload = {
        "review_plan_sha256": raw.get("review_plan_sha256"),
        "source_sha256": raw.get("source_sha256"),
        "visual_object_ids": _values("visual_object_ids"),
        "table_cell_ids": _values("table_cell_ids"),
        "series_ids": _values("series_ids"),
        "full_page_sha256": raw.get("full_page_sha256"),
        "crop_sha256s": _values("crop_sha256s"),
        "bbox_sha256s": _values("bbox_sha256s"),
        "render_sha256": raw.get("render_sha256"),
        "render_dpi": raw.get("render_dpi"),
        "render_rotation": raw.get("render_rotation"),
    }
    # Relation/grid commitments are part of the payload whenever the v0.2
    # attestation binds them.  Empty object-only attestations retain the
    # original payload shape for compatibility with bounded legacy callers.
    if raw.get("visual_relation_ids") or raw.get("relation_sha256s"):
        payload["visual_relation_ids"] = _values("visual_relation_ids")
        payload["relation_sha256s"] = _values("relation_sha256s")
    if raw.get("table_grid_ids") or raw.get("table_grid_sha256s"):
        payload["table_grid_ids"] = _values("table_grid_ids")
        payload["table_grid_sha256s"] = _values("table_grid_sha256s")
    return payload


def build_visual_review_attestation(*, reviewer_instance: str, proposer_instance: str, review_session_id: str, review_plan_sha256: str, source_sha256: str, visual_object_ids: Sequence[str], table_cell_ids: Sequence[str] = (), series_ids: Sequence[str] = (), visual_relation_ids: Sequence[str] = (), relation_sha256s: Sequence[str] = (), table_grid_ids: Sequence[str] = (), table_grid_sha256s: Sequence[str] = (), full_page_sha256: str, crop_sha256s: Sequence[str], render_sha256: str, render_dpi: int, render_rotation: int, bbox_sha256s: Sequence[str], input_sha256: str, verdict: str, differences: Sequence[Mapping[str, Any]], rationale: str, confidence: Mapping[str, float], reviewer_independence: Mapping[str, Any], issue_codes: Sequence[str] = (), checks: Mapping[str, bool] | None = None, attestation_id: str | None = None) -> dict[str, Any]:
    """Build a structurally complete externally authored v0.2 attestation."""

    if verdict not in {"accepted", "rejected", "quarantined"}:
        raise VisualSemanticsError("visual_review_verdict_invalid")
    for field, value in (("reviewer_instance", reviewer_instance), ("proposer_instance", proposer_instance), ("review_session_id", review_session_id)):
        if not isinstance(value, str) or not value.strip():
            raise VisualSemanticsError(f"visual_review_{field}_invalid")
    if reviewer_instance == proposer_instance:
        raise VisualSemanticsError("visual_reviewer_not_independent")
    if not HASH_RE.fullmatch(str(review_plan_sha256)) or not HASH_RE.fullmatch(str(source_sha256)):
        raise VisualSemanticsError("visual_review_hash_invalid")
    sequence_fields = {
        "visual_object_ids": visual_object_ids,
        "table_cell_ids": table_cell_ids,
        "series_ids": series_ids,
        "visual_relation_ids": visual_relation_ids,
        "relation_sha256s": relation_sha256s,
        "table_grid_ids": table_grid_ids,
        "table_grid_sha256s": table_grid_sha256s,
        "crop_sha256s": crop_sha256s,
        "bbox_sha256s": bbox_sha256s,
    }
    if any(isinstance(value, (str, bytes)) or not isinstance(value, Sequence) for value in sequence_fields.values()):
        raise VisualSemanticsError("visual_review_binding_invalid")
    bound_ids = sorted(set(str(value) for value in visual_object_ids))
    cell_ids = sorted(set(str(value) for value in table_cell_ids))
    series_object_ids = sorted(set(str(value) for value in series_ids))
    relation_ids = sorted(set(str(value) for value in visual_relation_ids))
    relation_hashes = sorted(set(str(value) for value in relation_sha256s))
    grid_ids = sorted(set(str(value) for value in table_grid_ids))
    grid_hashes = sorted(set(str(value) for value in table_grid_sha256s))
    if not bound_ids or len(bound_ids) != len(list(visual_object_ids)):
        raise VisualSemanticsError("visual_review_object_ids_invalid")
    if len(cell_ids) != len(list(table_cell_ids)) or len(series_object_ids) != len(list(series_ids)):
        raise VisualSemanticsError("visual_review_binding_ids_invalid")
    if len(relation_ids) != len(relation_hashes) or len(grid_ids) != len(grid_hashes):
        raise VisualSemanticsError("visual_review_binding_hashes_invalid")
    for field, values in (("crop_sha256s", crop_sha256s), ("bbox_sha256s", bbox_sha256s), ("relation_sha256s", relation_sha256s), ("table_grid_sha256s", table_grid_sha256s)):
        if len(set(str(value) for value in values)) != len(values):
            raise VisualSemanticsError(f"visual_review_{field}_invalid")
    for field, value in (("full_page_sha256", full_page_sha256), ("render_sha256", render_sha256), ("input_sha256", input_sha256)):
        if not HASH_RE.fullmatch(str(value)):
            raise VisualSemanticsError(f"visual_review_{field}_invalid")
    for field, values in (("crop_sha256s", crop_sha256s), ("bbox_sha256s", bbox_sha256s), ("relation_sha256s", relation_hashes), ("table_grid_sha256s", grid_hashes)):
        if any(not HASH_RE.fullmatch(str(value)) for value in values):
            raise VisualSemanticsError(f"visual_review_{field}_invalid")
    if not isinstance(render_dpi, int) or render_dpi < 36 or int(render_rotation) % 90 != 0:
        raise VisualSemanticsError("visual_review_render_receipt_invalid")
    if not isinstance(rationale, str) or not rationale.strip():
        raise VisualSemanticsError("visual_review_rationale_missing")
    if checks is not None and not isinstance(checks, Mapping):
        raise VisualSemanticsError("visual_review_checks_missing")
    checks_row = dict(checks or {name: True for name in VISUAL_REVIEW_CHECKS})
    if any(checks_row.get(name) is not True for name in VISUAL_REVIEW_CHECKS):
        raise VisualSemanticsError("visual_review_checks_missing")
    if not isinstance(reviewer_independence, Mapping) or reviewer_independence.get("independent") is not True:
        raise VisualSemanticsError("visual_reviewer_not_independent")
    if not isinstance(confidence, Mapping) or any(
        not isinstance(confidence.get(name), (int, float))
        or isinstance(confidence.get(name), bool)
        or not 0.0 <= float(confidence.get(name)) <= 1.0
        for name in ("extraction", "interpretation")
    ):
        raise VisualSemanticsError("visual_review_confidence_invalid")
    if isinstance(differences, (str, bytes)) or not isinstance(differences, Sequence):
        raise VisualSemanticsError("visual_review_difference_invalid")
    if any(not isinstance(value, Mapping) for value in differences):
        raise VisualSemanticsError("visual_review_difference_invalid")
    diff_rows = [dict(value) for value in differences]
    diff_ids = [str(row.get("visual_object_id")) for row in diff_rows]
    if len(diff_rows) != len(bound_ids) or len(diff_ids) != len(set(diff_ids)) or set(diff_ids) != set(bound_ids):
        raise VisualSemanticsError("visual_review_difference_missing")
    if any(row.get("status") not in VISUAL_DIFFERENCE_STATUSES or not isinstance(row.get("observation"), str) or not row.get("observation", "").strip() for row in diff_rows):
        raise VisualSemanticsError("visual_review_difference_invalid")
    normalized_issue_codes = sorted(set(str(value) for value in issue_codes))
    if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) for value in normalized_issue_codes):
        raise VisualSemanticsError("visual_review_issue_codes_invalid")
    if verdict == "accepted" and (normalized_issue_codes or any(row.get("status") != "confirmed" for row in diff_rows)):
        raise VisualSemanticsError("visual_review_acceptance_inconsistent")
    if verdict in {"rejected", "quarantined"} and not normalized_issue_codes:
        raise VisualSemanticsError("visual_review_issue_codes_missing")
    expected_attestation_id = stable_id("vra", review_plan_sha256, reviewer_instance, bound_ids, cell_ids, series_object_ids, relation_ids, grid_ids)
    if attestation_id is not None and attestation_id != expected_attestation_id:
        raise VisualSemanticsError("visual_review_attestation_id_invalid")
    row = {
        "schema_version": VISUAL_REVIEW_ATTESTATION_SCHEMA,
        "attestation_id": expected_attestation_id,
        "attestation_level": "host-orchestrator-recorded-not-cryptographic",
        "review_protocol": VISUAL_REVIEW_PROTOCOL,
        "reviewer_instance": reviewer_instance,
        "proposer_instance": proposer_instance,
        "reviewer_independence": dict(reviewer_independence),
        "review_session_id": review_session_id,
        "review_plan_sha256": review_plan_sha256,
        "source_sha256": source_sha256,
        "visual_object_ids": bound_ids,
        "table_cell_ids": cell_ids,
        "series_ids": series_object_ids,
        "visual_relation_ids": relation_ids,
        "relation_sha256s": relation_hashes,
        "table_grid_ids": grid_ids,
        "table_grid_sha256s": grid_hashes,
        "full_page_sha256": full_page_sha256,
        "crop_sha256s": sorted(set(str(value) for value in crop_sha256s)),
        "render_sha256": render_sha256,
        "render_dpi": int(render_dpi),
        "render_rotation": int(render_rotation) % 360,
        "bbox_sha256s": sorted(set(str(value) for value in bbox_sha256s)),
        "input_sha256": input_sha256,
        "checks": checks_row,
        "differences": diff_rows,
        "verdict": verdict,
        "issue_codes": normalized_issue_codes,
        "rationale": rationale,
        "confidence": dict(confidence),
    }
    row["attestation_sha256"] = sha256_json({key: value for key, value in row.items() if key != "attestation_sha256"})
    return row


def validate_visual_review_attestations(records: Sequence[Mapping[str, Any]], objects: Sequence[Mapping[str, Any]], *, review_plan_sha256: str, source_sha256: str, proposer_instances: Sequence[str] = (), required_reviewers: Mapping[str, int] | None = None, table_grids: Sequence[Mapping[str, Any]] = (), visual_relations: Sequence[Mapping[str, Any]] = (), expected_review_session_id: str | None = None, review_session_id: str | None = None, registered_reviewers: Sequence[str] = (), reviewer_instances: Sequence[str] = (), review_plan: Mapping[str, Any] | None = None) -> list[VisualIssue]:
    """Validate externally authored v0.2 attestations against frozen rows."""
    issues: list[VisualIssue] = []
    if isinstance(review_plan, Mapping):
        expected_review_session_id = expected_review_session_id or review_plan.get("review_session_id")
        if not registered_reviewers:
            registered_reviewers = review_plan.get("reviewer_instances", ())
        if not proposer_instances:
            proposer_instances = review_plan.get("proposer_instances", ())
        if required_reviewers is None and isinstance(review_plan.get("visual_reviewer_requirements"), Mapping):
            required_reviewers = review_plan.get("visual_reviewer_requirements")
    expected_review_session_id = expected_review_session_id or review_session_id
    if not registered_reviewers:
        registered_reviewers = reviewer_instances
    strict_plan_binding = required_reviewers is not None or review_plan is not None or expected_review_session_id is not None
    if strict_plan_binding and not expected_review_session_id:
        issues.append(VisualIssue("error", "visual_review_session_missing", "visual-review-attestations.jsonl", "The v0.2 validation call must provide the expected review session ID."))
    if strict_plan_binding and not registered_reviewers:
        issues.append(VisualIssue("error", "visual_reviewer_registry_missing", "visual-review-attestations.jsonl", "The v0.2 validation call must provide the registered reviewer instances."))
    if strict_plan_binding and not proposer_instances:
        issues.append(VisualIssue("error", "visual_proposer_registry_missing", "visual-review-attestations.jsonl", "The v0.2 validation call must provide the proposal-instance registry."))
    if not isinstance(table_grids, Sequence) or isinstance(table_grids, (str, bytes)):
        issues.append(VisualIssue("error", "table_grid_invalid", "table-grids.jsonl", "Table-grid rows must be an array."))
        table_grids = ()
    if not isinstance(visual_relations, Sequence) or isinstance(visual_relations, (str, bytes)):
        issues.append(VisualIssue("error", "visual_relation_invalid", "visual-relations.jsonl", "Visual relation rows must be an array."))
        visual_relations = ()
    object_by_id: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(objects):
        if not isinstance(row, Mapping):
            continue
        object_id = str(row.get("visual_object_id"))
        if object_id in object_by_id:
            issues.append(VisualIssue("error", "visual_object_duplicate", f"visual-objects.jsonl:{index + 1}", f"Duplicate object: {object_id}"))
        object_by_id[object_id] = dict(row)
    expected_counts = dict(required_reviewers or {})
    for object_id, count in expected_counts.items():
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            issues.append(VisualIssue("error", "visual_reviewer_requirement_invalid", str(object_id), "Reviewer requirement must be a positive integer."))

    def _required_count(value: Any) -> int:
        return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else 1
    table_cell_ids = {
        str(cell.get("cell_id"))
        for grid in table_grids
        if isinstance(grid, Mapping)
        for cell in grid.get("cells", [])
        if isinstance(cell, Mapping) and isinstance(cell.get("cell_id"), str)
    }
    table_cell_ids.update(
        str(row.get("visual_object_id"))
        for row in objects
        if isinstance(row, Mapping) and row.get("type") == "table_cell"
    )
    series_object_ids = {
        str(row.get("visual_object_id"))
        for row in objects
        if isinstance(row, Mapping) and row.get("type") == "series"
    }
    relation_by_id = {
        str(row.get("visual_relation_id")): dict(row)
        for row in visual_relations
        if isinstance(row, Mapping) and isinstance(row.get("visual_relation_id"), str)
    }
    grid_by_id = {
        str(row.get("table_grid_id")): dict(row)
        for row in table_grids
        if isinstance(row, Mapping) and isinstance(row.get("table_grid_id"), str)
    }
    cell_by_id: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for grid in grid_by_id.values():
        for cell in grid.get("cells", []):
            if isinstance(cell, Mapping) and isinstance(cell.get("cell_id"), str):
                cell_by_id[str(cell["cell_id"])] = (grid, dict(cell))
    seen: set[tuple[str, str]] = set()
    seen_attestation_ids: set[str] = set()
    by_object: dict[str, list[Mapping[str, Any]]] = {}
    for index, raw in enumerate(records):
        path = f"visual-review-attestations.jsonl:{index + 1}"
        if not isinstance(raw, Mapping) or raw.get("schema_version") != VISUAL_REVIEW_ATTESTATION_SCHEMA:
            issues.append(VisualIssue("error", "visual_review_attestation_invalid", path, "Unexpected attestation schema."))
            continue
        attestation_id = raw.get("attestation_id")
        if not isinstance(attestation_id, str) or attestation_id in seen_attestation_ids:
            issues.append(VisualIssue("error", "visual_review_attestation_replay", path, "Attestation ID is missing or repeated."))
        if isinstance(attestation_id, str):
            seen_attestation_ids.add(attestation_id)
        if raw.get("attestation_level") != "host-orchestrator-recorded-not-cryptographic" or raw.get("review_protocol") != VISUAL_REVIEW_PROTOCOL:
            issues.append(VisualIssue("error", "visual_review_protocol_invalid", path, "Attestation level or protocol is not the frozen v0.2 contract."))
        reviewer = raw.get("reviewer_instance")
        proposer = raw.get("proposer_instance")
        if not isinstance(reviewer, str) or not reviewer or (registered_reviewers and reviewer not in set(registered_reviewers)):
            issues.append(VisualIssue("error", "visual_reviewer_unregistered", path, "Reviewer is not registered in the frozen review plan."))
        if not isinstance(proposer, str) or not proposer or (proposer_instances and proposer not in set(proposer_instances)):
            issues.append(VisualIssue("error", "visual_proposer_unregistered", path, "Proposer is not registered in the frozen review plan."))
        independence = raw.get("reviewer_independence")
        if reviewer == proposer or reviewer in set(proposer_instances) or not isinstance(independence, Mapping) or independence.get("independent") is not True:
            issues.append(VisualIssue("error", "visual_reviewer_not_independent", path, "Reviewer independence is not externally recorded."))
        if expected_review_session_id is not None and raw.get("review_session_id") != expected_review_session_id:
            issues.append(VisualIssue("error", "visual_review_session_mismatch", path, "Review session differs from the frozen plan."))
        if raw.get("review_plan_sha256") != review_plan_sha256 or raw.get("source_sha256") != source_sha256:
            issues.append(VisualIssue("error", "visual_review_attestation_invalid", path, "Review plan/source hash mismatch."))
        list_fields = ("visual_object_ids", "table_cell_ids", "series_ids", "visual_relation_ids", "relation_sha256s", "table_grid_ids", "table_grid_sha256s", "crop_sha256s", "bbox_sha256s")
        field_values: dict[str, list[Any]] = {}
        for field in list_fields:
            value = raw.get(field, [])
            if not isinstance(value, list):
                issues.append(VisualIssue("error", "visual_review_binding_invalid", f"{path}.{field}", "Attestation binding fields must be arrays."))
                field_values[field] = []
            else:
                field_values[field] = value
        for field in ("visual_relation_ids", "table_grid_ids", "table_cell_ids", "series_ids"):
            values = field_values[field]
            if len(values) != len(set(str(value) for value in values)) or any(not isinstance(value, str) or not value for value in values):
                issues.append(VisualIssue("error", "visual_review_binding_ids_invalid", f"{path}.{field}", "Binding IDs must be unique non-empty strings."))
        for field in ("crop_sha256s", "bbox_sha256s", "relation_sha256s", "table_grid_sha256s"):
            values = field_values[field]
            if len(values) != len(set(str(value) for value in values)) or any(not isinstance(value, str) or not HASH_RE.fullmatch(value) for value in values):
                issues.append(VisualIssue("error", "visual_review_binding_hashes_invalid", f"{path}.{field}", "Binding hashes must be unique lowercase SHA-256 values."))
        if len(field_values["visual_relation_ids"]) != len(field_values["relation_sha256s"]):
            issues.append(VisualIssue("error", "visual_review_binding_hashes_invalid", path, "Relation IDs and hashes must be paired."))
        if len(field_values["table_grid_ids"]) != len(field_values["table_grid_sha256s"]):
            issues.append(VisualIssue("error", "visual_review_binding_hashes_invalid", path, "Table-grid IDs and hashes must be paired."))
        bound = list(field_values["visual_object_ids"])
        if not bound:
            issues.append(VisualIssue("error", "visual_review_attestation_invalid", path, "At least one visual object is required."))
        if len(bound) != len({str(value) for value in bound}):
            issues.append(VisualIssue("error", "visual_review_binding_ids_invalid", path, "Visual object IDs must be unique."))
        expected_attestation_id = stable_id(
            "vra",
            review_plan_sha256,
            reviewer,
            sorted(str(value) for value in bound),
            sorted(str(value) for value in field_values["table_cell_ids"]),
            sorted(str(value) for value in field_values["series_ids"]),
            sorted(str(value) for value in field_values["visual_relation_ids"]),
            sorted(str(value) for value in field_values["table_grid_ids"]),
        )
        if attestation_id != expected_attestation_id:
            issues.append(VisualIssue("error", "visual_review_attestation_invalid", path, "Attestation ID is not bound to the plan, reviewer, and exact visual bindings."))
        for object_id in bound:
            if object_id not in object_by_id:
                issues.append(VisualIssue("error", "visual_review_object_unresolved", path, str(object_id)))
            pair = (str(object_id), str(reviewer))
            if pair in seen:
                issues.append(VisualIssue("error", "visual_review_attestation_replay", path, f"Repeated reviewer/object pair: {pair}"))
            seen.add(pair)
            by_object.setdefault(str(object_id), []).append(raw)
        object_rows = [object_by_id[object_id] for object_id in bound if object_id in object_by_id]
        pages = {int(row.get("physical_page")) for row in object_rows if isinstance(row.get("physical_page"), int)}
        renders = {str(row.get("canonical_render_sha256")) for row in object_rows}
        dpis = {int(row.get("canonical_render_dpi")) for row in object_rows if isinstance(row.get("canonical_render_dpi"), int)}
        rotations = {int(row.get("canonical_render_rotation", 0)) % 360 for row in object_rows}
        if len(pages) != 1 or len(renders) != 1 or len(dpis) != 1 or len(rotations) != 1:
            issues.append(VisualIssue("error", "visual_review_binding_inconsistent", path, "One attestation may bind only one physical page/render/DPI/rotation."))
        if any(row.get("source_sha256") != source_sha256 for row in object_rows):
            issues.append(VisualIssue("error", "source_hash_mismatch", path, "Bound object source differs from the review source."))
        expected_render = {str(row.get("canonical_render_sha256")) for row in object_rows}
        expected_crop = {str(row.get("crop_sha256")) for row in object_rows if row.get("crop_sha256") is not None}
        expected_bbox = {sha256_json(row.get("bbox")) for row in object_rows}
        if any(not HASH_RE.fullmatch(value) for value in (str(raw.get("full_page_sha256")), str(raw.get("render_sha256")), str(raw.get("input_sha256")))):
            issues.append(VisualIssue("error", "visual_review_hash_invalid", path, "Full-page/render/input commitments must be lowercase SHA-256 values."))
        if raw.get("full_page_sha256") not in expected_render or raw.get("render_sha256") not in expected_render:
            issues.append(VisualIssue("error", "visual_render_hash_mismatch", path, "Full-page/render hash does not match bound objects."))
        if set(field_values["crop_sha256s"]) != expected_crop:
            issues.append(VisualIssue("error", "crop_hash_mismatch", path, "Crop hash set differs from bound objects."))
        if set(field_values["bbox_sha256s"]) != expected_bbox:
            issues.append(VisualIssue("error", "bbox_hash_mismatch", path, "Bbox hash set differs from bound objects."))
        for cell_id in field_values["table_cell_ids"]:
            if cell_id not in table_cell_ids or cell_id not in cell_by_id:
                issues.append(VisualIssue("error", "table_cell_unresolved", path, "Attestation references an absent table-grid cell."))
                continue
            grid, _cell = cell_by_id[cell_id]
            if (pages and grid.get("physical_page") not in pages) or grid.get("table_visual_object_id") not in set(bound):
                issues.append(VisualIssue("error", "table_cell_binding_invalid", path, f"Cell is not bound to the attested table/page: {cell_id}"))
        if any(series_id not in series_object_ids for series_id in field_values["series_ids"]):
            issues.append(VisualIssue("error", "visual_series_unresolved", path, "Attestation references an absent series object."))
        for series_id in field_values["series_ids"]:
            series = object_by_id.get(str(series_id))
            if series is None:
                continue
            if pages and series.get("physical_page") not in pages:
                issues.append(VisualIssue("error", "visual_series_binding_invalid", path, f"Series is not on the attested page: {series_id}"))
            parent = series.get("parent")
            has_relation = any(
                relation.get("relation_type") == "series_of"
                and relation.get("source_visual_object_id") == series_id
                and relation.get("target_visual_object_id") in set(bound)
                for relation in relation_by_id.values()
            )
            if parent not in set(bound) and not has_relation:
                issues.append(VisualIssue("error", "visual_series_binding_invalid", path, f"Series is not bound to an attested plot/object: {series_id}"))
        raw_relation_ids = sorted(str(value) for value in field_values["visual_relation_ids"])
        raw_relation_hashes = sorted(str(value) for value in field_values["relation_sha256s"])
        expected_relation_rows = [
            relation for relation in relation_by_id.values()
            if relation.get("source_visual_object_id") in set(bound)
            or relation.get("target_visual_object_id") in set(bound)
        ]
        expected_relation_ids = sorted(str(row.get("visual_relation_id")) for row in expected_relation_rows)
        expected_relation_hashes = sorted(str(row.get("relation_sha256")) for row in expected_relation_rows)
        if visual_relations and raw_relation_ids != expected_relation_ids:
            issues.append(VisualIssue("error", "visual_relation_binding_missing", path, "Attestation relation IDs do not cover the bound object relations."))
        if visual_relations and raw_relation_hashes != expected_relation_hashes:
            issues.append(VisualIssue("error", "visual_relation_hash_mismatch", path, "Attestation relation hashes are stale or incomplete."))
        for relation in expected_relation_rows:
            if relation.get("relation_sha256") != sha256_json({key: value for key, value in relation.items() if key not in {"relation_sha256", "status"}}):
                issues.append(VisualIssue("error", "visual_relation_hash_mismatch", path, "Candidate relation content hash is invalid."))
            if pages and relation.get("physical_page") not in pages:
                issues.append(VisualIssue("error", "visual_relation_binding_invalid", path, "Bound relation is on another physical page."))
        raw_grid_ids = sorted(str(value) for value in field_values["table_grid_ids"])
        raw_grid_hashes = sorted(str(value) for value in field_values["table_grid_sha256s"])
        expected_grid_rows = [
            grid for grid in grid_by_id.values()
            if grid.get("table_visual_object_id") in set(bound)
            or any(str(cell.get("cell_id")) in set(field_values["table_cell_ids"]) for cell in grid.get("cells", []) if isinstance(cell, Mapping))
        ]
        expected_grid_ids = sorted(str(row.get("table_grid_id")) for row in expected_grid_rows)
        expected_grid_hashes = sorted(str(row.get("grid_sha256")) for row in expected_grid_rows)
        if table_grids and raw_grid_ids != expected_grid_ids:
            issues.append(VisualIssue("error", "table_grid_binding_missing", path, "Attestation table-grid IDs do not cover the bound table/cells."))
        if table_grids and raw_grid_hashes != expected_grid_hashes:
            issues.append(VisualIssue("error", "table_grid_hash_mismatch", path, "Attestation table-grid hashes are stale or incomplete."))
        for grid in expected_grid_rows:
            if grid.get("grid_sha256") != sha256_json({key: value for key, value in grid.items() if key not in {"grid_sha256", "status"}}):
                issues.append(VisualIssue("error", "table_grid_hash_mismatch", path, "Candidate table-grid content hash is invalid."))
        if raw.get("input_sha256") != sha256_json(_visual_review_input_payload(raw)):
            issues.append(VisualIssue("error", "visual_input_hash_mismatch", path, "Input hash is stale."))
        checks = raw.get("checks")
        if not isinstance(checks, Mapping) or any(checks.get(name) is not True for name in VISUAL_REVIEW_CHECKS):
            issues.append(VisualIssue("error", "visual_review_checks_missing", path, "Full-page/crop/bbox/relation/table-grid inspection checks are incomplete."))
        differences = raw.get("differences")
        diff_ids = [str(row.get("visual_object_id")) for row in differences if isinstance(row, Mapping)] if isinstance(differences, list) else []
        if len(diff_ids) != len(bound) or len(set(diff_ids)) != len(diff_ids) or set(diff_ids) != set(bound):
            issues.append(VisualIssue("error", "visual_review_difference_missing", path, "Exactly one differentiated observation per object is required."))
        if isinstance(differences, list) and any(not isinstance(row, Mapping) or row.get("status") not in VISUAL_DIFFERENCE_STATUSES or not str(row.get("observation", "")).strip() for row in differences):
            issues.append(VisualIssue("error", "visual_review_difference_invalid", path, "Difference status/observation is invalid."))
        confidence = raw.get("confidence")
        confidence_valid = isinstance(confidence, Mapping) and all(
            isinstance(confidence.get(name), (int, float))
            and not isinstance(confidence.get(name), bool)
            and 0.0 <= float(confidence.get(name)) <= 1.0
            for name in ("extraction", "interpretation")
        )
        if not confidence_valid:
            issues.append(VisualIssue("error", "visual_review_confidence_invalid", path, "Extraction and interpretation confidence must be within [0,1]."))
        verdict = raw.get("verdict")
        issue_values = raw.get("issue_codes")
        issue_valid = isinstance(issue_values, list) and all(isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) for value in issue_values) and len(set(issue_values)) == len(issue_values)
        if not issue_valid:
            issues.append(VisualIssue("error", "visual_review_issue_codes_invalid", path, "Issue codes must be unique stable codes."))
        if verdict == "accepted" and (issue_values or not isinstance(raw.get("rationale"), str) or not raw.get("rationale", "").strip() or not isinstance(differences, list) or any(row.get("status") != "confirmed" for row in differences if isinstance(row, Mapping))):
            issues.append(VisualIssue("error", "visual_review_acceptance_inconsistent", path, "Accepted review requires confirmed differences, no issue codes, and rationale."))
        elif verdict in {"rejected", "quarantined"} and not issue_values:
            issues.append(VisualIssue("error", "visual_review_issue_codes_missing", path, "Rejected/quarantined review requires issue codes."))
        if raw.get("attestation_sha256") != sha256_json({key: value for key, value in raw.items() if key != "attestation_sha256"}):
            issues.append(VisualIssue("error", "visual_review_attestation_invalid", path, "Attestation content hash mismatch."))
    # Anti-blanket protection applies only to multi-object/multi-page batches;
    # two reviewers of one object may legitimately use similar scores.
    def _normalized_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip().casefold()

    valid_rows = [row for row in records if isinstance(row, Mapping) and isinstance(row.get("visual_object_ids"), list)]
    for row in valid_rows:
        bound = [str(value) for value in row.get("visual_object_ids", [])]
        differences = row.get("differences", []) if isinstance(row.get("differences"), list) else []
        observations = {_normalized_text(item.get("observation")) for item in differences if isinstance(item, Mapping)}
        confidence = row.get("confidence") if isinstance(row.get("confidence"), Mapping) else {}
        if len(bound) > 1 and len(observations) == 1 and confidence.get("extraction") == confidence.get("interpretation"):
            issues.append(VisualIssue("error", "visual_review_blanket_pattern", "visual-review-attestations.jsonl", "One generic observation/confidence pattern cannot review multiple objects."))
    distinct_batch_objects = {
        str(identifier)
        for row in valid_rows
        for identifier in row.get("visual_object_ids", [])
        if isinstance(identifier, str)
    }
    if len(distinct_batch_objects) > 1 and len(valid_rows) > 1:
        batch_signatures = {
            (
                _normalized_text(row.get("rationale")),
                _normalized_text("|".join(str(item.get("observation", "")) for item in row.get("differences", []) if isinstance(item, Mapping))),
                str((row.get("confidence") or {}).get("extraction")),
                str((row.get("confidence") or {}).get("interpretation")),
            )
            for row in valid_rows
        }
        if len(batch_signatures) == 1:
            issues.append(VisualIssue("error", "visual_review_blanket_pattern", "visual-review-attestations.jsonl", "A repeated rationale/observation/confidence pattern spans multiple visual objects."))
    page_groups: dict[int, list[Mapping[str, Any]]] = {}
    for row in valid_rows:
        pages = {int(object_by_id[identifier].get("physical_page")) for identifier in row.get("visual_object_ids", []) if identifier in object_by_id and isinstance(object_by_id[identifier].get("physical_page"), int)}
        for page in pages:
            page_groups.setdefault(page, []).append(row)
    if len(page_groups) > 1 and len(valid_rows) > 1:
        signatures = {
            (
                _normalized_text(row.get("rationale")),
                _normalized_text("|".join(str(item.get("observation", "")) for item in row.get("differences", []) if isinstance(item, Mapping))),
                str((row.get("confidence") or {}).get("extraction")),
                str((row.get("confidence") or {}).get("interpretation")),
            )
            for row in valid_rows
        }
        if len(signatures) == 1:
            issues.append(VisualIssue("error", "visual_review_blanket_pattern", "visual-review-attestations.jsonl", "A repeated rationale/observation/confidence pattern spans multiple pages."))
    for object_id, rows in by_object.items():
        required = _required_count(expected_counts.get(object_id, 1))
        reviewers = {str(row.get("reviewer_instance")) for row in rows}
        if len(rows) != required or len(reviewers) != required:
            issues.append(VisualIssue("error", "visual_reviewer_coverage_insufficient", object_id, f"Required {required} independent reviewers; found {len(rows)}."))
    # Iterate the frozen requirements themselves, including objects with zero
    # records.  This closes the P0 hole where missing object IDs disappeared
    # because ``by_object`` had no key for them.
    for object_id, required in expected_counts.items():
        rows = by_object.get(str(object_id), [])
        if str(object_id) not in object_by_id:
            issues.append(VisualIssue("error", "visual_review_object_unresolved", str(object_id), "Required visual object is absent."))
        reviewers = {str(row.get("reviewer_instance")) for row in rows}
        if len(rows) != _required_count(required) or len(reviewers) != _required_count(required):
            issues.append(VisualIssue("error", "visual_reviewer_coverage_insufficient", str(object_id), f"Required {required} independent reviewers; found {len(rows)}."))
    if expected_counts:
        for object_id in object_by_id:
            if object_id not in expected_counts:
                issues.append(VisualIssue("error", "visual_reviewer_requirement_missing", object_id, "Frozen review plan has no reviewer requirement for this object."))
    unique = {(issue.code, issue.path, issue.message): issue for issue in issues}
    return sorted(unique.values(), key=lambda issue: (issue.code, issue.path, issue.message))


def merge_visual_review_fragments(fragment_paths: Sequence[Path], *, workpack_root: Path | None = None, objects: Sequence[Mapping[str, Any]], review_plan_sha256: str, source_sha256: str, proposer_instances: Sequence[str] = (), required_reviewers: Mapping[str, int] | None = None, table_grids: Sequence[Mapping[str, Any]] = (), visual_relations: Sequence[Mapping[str, Any]] = (), expected_review_session_id: str | None = None, registered_reviewers: Sequence[str] = ()) -> dict[str, Any]:
    if not fragment_paths:
        raise VisualSemanticsError("visual_review_fragments_required")
    rows: list[dict[str, Any]] = []
    seen_fragments: set[str] = set()
    for path in fragment_paths:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise VisualSemanticsError("visual_review_fragment_missing")
        if workpack_root is not None:
            try:
                resolved.relative_to(workpack_root.expanduser().resolve())
            except ValueError:
                pass
            else:
                raise VisualSemanticsError("visual_review_fragment_inside_workpack")
        digest = sha256_file(resolved)
        if digest in seen_fragments:
            raise VisualSemanticsError("visual_review_fragment_replay")
        seen_fragments.add(digest)
        try:
            loaded = json.loads("[" + ",".join(line for line in resolved.read_text(encoding="utf-8").splitlines() if line.strip()) + "]")
        except (OSError, json.JSONDecodeError) as error:
            raise VisualSemanticsError("visual_review_fragment_invalid") from error
        if not isinstance(loaded, list):
            raise VisualSemanticsError("visual_review_fragment_invalid")
        rows.extend(dict(row) for row in loaded if isinstance(row, Mapping))
    issues = validate_visual_review_attestations(rows, objects, review_plan_sha256=review_plan_sha256, source_sha256=source_sha256, proposer_instances=proposer_instances, required_reviewers=required_reviewers, table_grids=table_grids, visual_relations=visual_relations, expected_review_session_id=expected_review_session_id, registered_reviewers=registered_reviewers)
    if issues:
        first = issues[0]
        raise VisualSemanticsError(f"{first.code}:{first.path}")
    return {"schema_version": VISUAL_REVIEW_PROTOCOL, "attestations": sorted(rows, key=lambda row: str(row.get("attestation_id"))), "fragment_sha256s": sorted(seen_fragments), "candidate_only": False, "compiler_authored_conclusion": False}


def visual_assurance_manifest(*, source_sha256: str, objects: Sequence[Mapping[str, Any]], relations: Sequence[Mapping[str, Any]], grids: Sequence[Mapping[str, Any]], conflicts: Sequence[Mapping[str, Any]], attestations: Sequence[Mapping[str, Any]], whole_book_completeness_claimed: bool = False, review_plan_sha256: str | None = None, review_session_id: str | None = None, registered_reviewers: Sequence[str] = (), required_reviewers: Mapping[str, int] | None = None) -> dict[str, Any]:
    files = {
        "visual_objects": {"path": "references/visual/visual-objects.jsonl", "sha256": sha256_json(list(objects)), "record_count": len(objects)},
        "visual_relations": {"path": "references/visual/visual-relations.jsonl", "sha256": sha256_json(list(relations)), "record_count": len(relations)},
        "table_grids": {"path": "references/visual/table-grids.jsonl", "sha256": sha256_json(list(grids)), "record_count": len(grids)},
        "visual_conflicts": {"path": "references/visual/visual-conflicts.jsonl", "sha256": sha256_json(list(conflicts)), "record_count": len(conflicts)},
        "review_attestations": {"path": "references/visual/review-attestations.jsonl", "sha256": sha256_json(list(attestations)), "record_count": len(attestations)},
    }
    requirements = {
        str(identifier): int(count)
        for identifier, count in (required_reviewers or {}).items()
        if isinstance(count, int) and not isinstance(count, bool) and count > 0
    }
    accepted_attestations: dict[str, dict[str, list[str]]] = {}
    for obj in objects:
        if not isinstance(obj, Mapping) or not isinstance(obj.get("visual_object_id"), str):
            continue
        identifier = str(obj["visual_object_id"])
        accepted = [
            row for row in attestations
            if isinstance(row, Mapping)
            and row.get("verdict") == "accepted"
            and identifier in row.get("visual_object_ids", [])
        ]
        accepted_attestations[identifier] = {
            "attestation_ids": sorted(str(row.get("attestation_id")) for row in accepted if isinstance(row.get("attestation_id"), str)),
            "reviewer_instances": sorted({str(row.get("reviewer_instance")) for row in accepted if isinstance(row.get("reviewer_instance"), str)}),
        }
    review_binding = {
        "review_plan_sha256": review_plan_sha256,
        "review_session_id": review_session_id,
        "registered_reviewer_instances": sorted(set(str(value) for value in registered_reviewers)),
        "required_reviewers": dict(sorted(requirements.items())),
        "accepted_attestations": dict(sorted(accepted_attestations.items())),
    }
    manifest = {"schema_version": VISUAL_ASSURANCE_MANIFEST_SCHEMA, "protocol": VISUAL_PROTOCOL, "review_protocol": VISUAL_REVIEW_PROTOCOL, "source_sha256": source_sha256, "candidate_only": not bool(attestations), "whole_book_completeness_claimed": bool(whole_book_completeness_claimed), "files": files, "review_binding": review_binding}
    manifest["manifest_sha256"] = sha256_json({key: value for key, value in manifest.items() if key != "manifest_sha256"})
    return manifest


def visual_review_overlay_manifest(*, source_sha256: str, objects: Sequence[Mapping[str, Any]], grids: Sequence[Mapping[str, Any]], relations: Sequence[Mapping[str, Any]], synthetic_only: bool = False, receipt_hashes: Sequence[str] = ()) -> dict[str, Any]:
    """Build a source-free, candidate-only visual review routing manifest.

    This is an overlay index, not a reviewer workpack and not an attestation.
    It contains IDs, page geometry and hashes only; no pixels, reviewer identity,
    verdict, or compiler-authored semantic conclusion is permitted.
    """

    if not HASH_RE.fullmatch(str(source_sha256)):
        raise VisualSemanticsError("visual_review_overlay_source_hash_invalid")
    object_rows = [dict(row) for row in objects if isinstance(row, Mapping) and row.get("type") not in {"table_cell", "series"}]
    series_rows = [dict(row) for row in objects if isinstance(row, Mapping) and row.get("type") == "series"]
    cell_rows: list[dict[str, Any]] = []
    for grid in grids:
        if not isinstance(grid, Mapping):
            continue
        for cell in grid.get("cells", []) if isinstance(grid.get("cells"), list) else []:
            if not isinstance(cell, Mapping):
                continue
            cell_rows.append({
                "cell_id": str(cell.get("cell_id")),
                "table_grid_id": str(grid.get("table_grid_id")),
                "table_grid_sha256": str(grid.get("grid_sha256")),
                "physical_page": int(grid.get("physical_page", 0)),
                "bbox": list(cell.get("bbox")) if isinstance(cell.get("bbox"), (list, tuple)) else None,
                "crop_sha256": cell.get("crop_sha256"),
                "source_anchor_sha256": sha256_json(cell.get("source_anchor")),
            })
    object_refs = [
        {
            "visual_object_id": str(row.get("visual_object_id")),
            "type": str(row.get("type")),
            "physical_page": int(row.get("physical_page", 0)),
            "bbox": list(row.get("bbox")) if isinstance(row.get("bbox"), (list, tuple)) else None,
            "render_sha256": str(row.get("canonical_render_sha256")),
            "crop_sha256": row.get("crop_sha256"),
            "input_sha256": str(row.get("input_sha256")),
            "object_sha256": str(row.get("object_sha256")),
        }
        for row in object_rows
    ]
    series_refs = [
        {
            "visual_object_id": str(row.get("visual_object_id")),
            "parent": str(row.get("parent")),
            "physical_page": int(row.get("physical_page", 0)),
            "bbox": list(row.get("bbox")) if isinstance(row.get("bbox"), (list, tuple)) else None,
            "input_sha256": str(row.get("input_sha256")),
            "object_sha256": str(row.get("object_sha256")),
        }
        for row in series_rows
    ]
    relation_refs = [
        {
            "visual_relation_id": str(row.get("visual_relation_id")),
            "relation_type": str(row.get("relation_type")),
            "physical_page": int(row.get("physical_page", 0)),
            "relation_sha256": str(row.get("relation_sha256")),
        }
        for row in relations
        if isinstance(row, Mapping)
    ]
    object_by_id = {str(row.get("visual_object_id")): row for row in objects if isinstance(row, Mapping)}
    tasks: list[dict[str, Any]] = []

    def add_task(kind: str, identifier: str, page: int, reason: str, reviewers: int) -> None:
        tasks.append({
            "task_id": stable_id("vrot", source_sha256, kind, identifier),
            "kind": kind,
            "identifier": identifier,
            "physical_page": int(page),
            "reason": reason,
            "required_reviewers": int(reviewers),
            "candidate_only": True,
        })

    for row in object_refs:
        source_row = object_by_id.get(row["visual_object_id"], {})
        add_task("object", row["visual_object_id"], row["physical_page"], "visual presence, extent, caption, and candidate type require external inspection", 1)
        if source_row.get("type") == "annotation" and source_row.get("subtype") == "digitized_value":
            tasks[-1]["reason"] = "digitized chart value and error model require two independent visual reviewers"
            tasks[-1]["required_reviewers"] = 2
    for row in cell_rows:
        source_row = next((cell for grid in grids if isinstance(grid, Mapping) and grid.get("table_grid_id") == row["table_grid_id"] for cell in grid.get("cells", []) if isinstance(cell, Mapping) and cell.get("cell_id") == row["cell_id"]), {})
        numeric = any(source_row.get(key) is not None for key in ("value_candidate", "unit_candidate", "symbol_candidate"))
        add_task("cell", row["cell_id"], row["physical_page"], "table cell topology and bbox require external inspection" + ("; numeric/unit/symbol value requires a second independent reviewer" if numeric else ""), 2 if numeric else 1)
    for row in series_refs:
        add_task("series", row["visual_object_id"], row["physical_page"], "series identity and mapping require two independent visual reviewers", 2)
    for row in relation_refs:
        add_task("relation", row["visual_relation_id"], row["physical_page"], "visual relation and arrow/label mapping require external inspection", 1)
    manifest: dict[str, Any] = {
        "schema_version": VISUAL_REVIEW_OVERLAY_SCHEMA,
        "source_sha256": source_sha256,
        "candidate_only": True,
        "synthetic_only": bool(synthetic_only),
        "review_authoring": False,
        "promotion": False,
        "objects": sorted(object_refs, key=lambda row: row["visual_object_id"]),
        "cells": sorted(cell_rows, key=lambda row: (row["physical_page"], row["table_grid_id"], row["cell_id"])),
        "series": sorted(series_refs, key=lambda row: row["visual_object_id"]),
        "relations": sorted(relation_refs, key=lambda row: row["visual_relation_id"]),
        "review_tasks": sorted(tasks, key=lambda row: row["task_id"]),
        "receipt_hashes": sorted(set(str(value) for value in receipt_hashes)),
    }
    manifest["overlay_sha256"] = sha256_json(manifest)
    return manifest


def validate_visual_review_overlay_manifest(manifest: Mapping[str, Any], *, source_sha256: str, objects: Sequence[Mapping[str, Any]], grids: Sequence[Mapping[str, Any]], relations: Sequence[Mapping[str, Any]], synthetic_only: bool = False, receipt_hashes: Sequence[str] = ()) -> list[VisualIssue]:
    """Validate overlay identity, replay resistance, and external-review boundary."""

    issues: list[VisualIssue] = []
    allowed = {"schema_version", "source_sha256", "candidate_only", "synthetic_only", "review_authoring", "promotion", "objects", "cells", "series", "relations", "review_tasks", "receipt_hashes", "overlay_sha256"}
    if not isinstance(manifest, Mapping) or set(manifest) - allowed:
        return [VisualIssue("error", "visual_review_overlay_unknown_field", "visual-review-overlay.json", "Overlay has unknown fields; reviewer attestations cannot be embedded.")]
    if manifest.get("schema_version") != VISUAL_REVIEW_OVERLAY_SCHEMA:
        issues.append(VisualIssue("error", "visual_review_overlay_invalid", "visual-review-overlay.json.schema_version", "Unexpected overlay schema."))
    if manifest.get("source_sha256") != source_sha256:
        issues.append(VisualIssue("error", "source_hash_mismatch", "visual-review-overlay.json.source_sha256", "Overlay source differs."))
    if manifest.get("candidate_only") is not True or manifest.get("review_authoring") is not False or manifest.get("promotion") is not False or manifest.get("synthetic_only") is not bool(synthetic_only):
        issues.append(VisualIssue("error", "visual_review_overlay_boundary", "visual-review-overlay.json", "Overlay must remain candidate-only, non-authoring, non-promoting, and synthetic-mode bound."))
    if not isinstance(manifest.get("receipt_hashes"), list) or any(not HASH_RE.fullmatch(str(value)) for value in manifest.get("receipt_hashes", [])):
        issues.append(VisualIssue("error", "visual_review_overlay_receipts_invalid", "visual-review-overlay.json.receipt_hashes", "Receipt hashes must be a closed list of SHA-256 values."))
    if manifest.get("overlay_sha256") != sha256_json({key: value for key, value in manifest.items() if key != "overlay_sha256"}):
        issues.append(VisualIssue("error", "visual_review_overlay_hash_mismatch", "visual-review-overlay.json.overlay_sha256", "Overlay content hash differs."))
    try:
        expected = visual_review_overlay_manifest(source_sha256=source_sha256, objects=objects, grids=grids, relations=relations, synthetic_only=synthetic_only, receipt_hashes=receipt_hashes)
        for key in ("objects", "cells", "series", "relations", "review_tasks"):
            if manifest.get(key) != expected.get(key):
                issues.append(VisualIssue("error", "visual_review_overlay_replay_mismatch", f"visual-review-overlay.json.{key}", "Overlay rows do not match current candidate hashes or topology."))
    except (TypeError, ValueError, VisualSemanticsError) as error:
        issues.append(VisualIssue("error", "visual_review_overlay_invalid", "visual-review-overlay.json", str(error)))
    tasks = manifest.get("review_tasks") if isinstance(manifest.get("review_tasks"), list) else []
    task_ids = [row.get("task_id") for row in tasks if isinstance(row, Mapping)]
    identifiers = [(row.get("kind"), row.get("identifier")) for row in tasks if isinstance(row, Mapping)]
    if len(task_ids) != len(set(task_ids)) or len(identifiers) != len(set(identifiers)):
        issues.append(VisualIssue("error", "visual_review_overlay_replay", "visual-review-overlay.json.review_tasks", "Duplicate review task identity is forbidden."))
    return sorted(issues, key=lambda issue: (issue.code, issue.path, issue.message))


def validate_visual_assurance_manifest(manifest: Mapping[str, Any], *, source_sha256: str, objects: Sequence[Mapping[str, Any]], relations: Sequence[Mapping[str, Any]], grids: Sequence[Mapping[str, Any]], conflicts: Sequence[Mapping[str, Any]], attestations: Sequence[Mapping[str, Any]], review_plan_sha256: str | None = None, review_session_id: str | None = None, required_reviewers: Mapping[str, int] | None = None, registered_reviewers: Sequence[str] = ()) -> list[VisualIssue]:
    """Validate a source-free visual assurance index against canonical rows."""

    issues: list[VisualIssue] = []
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != VISUAL_ASSURANCE_MANIFEST_SCHEMA:
        return [VisualIssue("error", "visual_assurance_manifest_invalid", "assurance-manifest.json", "Unexpected assurance manifest schema.")]
    if manifest.get("source_sha256") != source_sha256:
        issues.append(VisualIssue("error", "source_hash_mismatch", "assurance-manifest.json.source_sha256", "Manifest source hash differs."))
    if manifest.get("protocol") != VISUAL_PROTOCOL or manifest.get("review_protocol") != VISUAL_REVIEW_PROTOCOL:
        issues.append(VisualIssue("error", "visual_assurance_manifest_invalid", "assurance-manifest.json", "Visual protocol differs."))
    if manifest.get("whole_book_completeness_claimed") is not False:
        issues.append(VisualIssue("error", "whole_book_completeness_forbidden", "assurance-manifest.json", "Visual semantics may not claim whole-book completeness."))
    binding = manifest.get("review_binding")
    if not isinstance(binding, Mapping):
        issues.append(VisualIssue("error", "visual_review_binding_missing", "assurance-manifest.json.review_binding", "Frozen review requirements and accepted attestation IDs are required."))
        binding = {}
    frozen_plan = binding.get("review_plan_sha256")
    frozen_session = binding.get("review_session_id")
    if not manifest.get("candidate_only"):
        if not isinstance(frozen_plan, str) or not HASH_RE.fullmatch(frozen_plan) or not isinstance(frozen_session, str) or not frozen_session:
            issues.append(VisualIssue("error", "visual_review_plan_binding_missing", "assurance-manifest.json.review_binding", "Promoted visual runtime requires review plan/session binding."))
    if review_plan_sha256 is not None and frozen_plan != review_plan_sha256:
        issues.append(VisualIssue("error", "visual_review_plan_binding_mismatch", "assurance-manifest.json.review_binding.review_plan_sha256", "Frozen review plan hash differs."))
    if review_session_id is not None and frozen_session != review_session_id:
        issues.append(VisualIssue("error", "visual_review_session_mismatch", "assurance-manifest.json.review_binding.review_session_id", "Frozen review session differs."))
    frozen_requirements = binding.get("required_reviewers")
    if not isinstance(frozen_requirements, Mapping):
        issues.append(VisualIssue("error", "visual_reviewer_requirement_missing", "assurance-manifest.json.review_binding.required_reviewers", "Frozen reviewer requirements are required."))
        frozen_requirements = {}
    if required_reviewers is not None and dict(frozen_requirements) != {str(key): int(value) for key, value in required_reviewers.items()}:
        issues.append(VisualIssue("error", "visual_reviewer_requirement_mismatch", "assurance-manifest.json.review_binding.required_reviewers", "Frozen reviewer requirements differ from the review plan."))
    frozen_registered = {str(value) for value in binding.get("registered_reviewer_instances", [])} if isinstance(binding.get("registered_reviewer_instances"), list) else set()
    if registered_reviewers and frozen_registered != {str(value) for value in registered_reviewers}:
        issues.append(VisualIssue("error", "visual_reviewer_registry_mismatch", "assurance-manifest.json.review_binding.registered_reviewer_instances", "Frozen reviewer registry differs."))
    rows_by_key = {
        "visual_objects": objects,
        "visual_relations": relations,
        "table_grids": grids,
        "visual_conflicts": conflicts,
        "review_attestations": attestations,
    }
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        issues.append(VisualIssue("error", "visual_assurance_manifest_invalid", "assurance-manifest.json.files", "File index is required."))
    else:
        for key, rows in rows_by_key.items():
            entry = files.get(key)
            if not isinstance(entry, Mapping) or entry.get("sha256") != sha256_json(list(rows)) or entry.get("record_count") != len(rows):
                issues.append(VisualIssue("error", "visual_assurance_manifest_mismatch", f"assurance-manifest.json.files.{key}", "Row digest or count differs."))
    accepted_map = binding.get("accepted_attestations")
    if not isinstance(accepted_map, Mapping):
        issues.append(VisualIssue("error", "visual_review_attestation_binding_missing", "assurance-manifest.json.review_binding.accepted_attestations", "Accepted attestation map is required."))
        accepted_map = {}
    object_ids = {str(row.get("visual_object_id")) for row in objects if isinstance(row, Mapping)}
    accepted_rows = {
        str(row.get("attestation_id")): row
        for row in attestations
        if isinstance(row, Mapping) and row.get("verdict") == "accepted" and isinstance(row.get("attestation_id"), str)
    }
    for object_id in object_ids:
        entry = accepted_map.get(object_id)
        required = frozen_requirements.get(object_id)
        if not manifest.get("candidate_only") and (not isinstance(required, int) or required < 1):
            issues.append(VisualIssue("error", "visual_reviewer_requirement_missing", object_id, "Every promoted visual object needs a frozen reviewer count."))
        if not isinstance(entry, Mapping):
            if not manifest.get("candidate_only"):
                issues.append(VisualIssue("error", "visual_review_attestation_binding_missing", object_id, "Every promoted visual object needs accepted attestation IDs."))
            continue
        attestation_ids = entry.get("attestation_ids")
        reviewer_ids = entry.get("reviewer_instances")
        expected_ids = sorted(
            str(row.get("attestation_id"))
            for row in attestations
            if isinstance(row, Mapping) and row.get("verdict") == "accepted" and object_id in row.get("visual_object_ids", [])
        )
        expected_reviewers = sorted({str(accepted_rows[identifier].get("reviewer_instance")) for identifier in expected_ids if identifier in accepted_rows})
        if attestation_ids != expected_ids or reviewer_ids != expected_reviewers:
            issues.append(VisualIssue("error", "visual_review_attestation_binding_mismatch", object_id, "Accepted attestation/reviewer map differs from rows."))
        if not manifest.get("candidate_only") and (len(expected_ids) != int(required or 0) or len(expected_reviewers) != int(required or 0)):
            issues.append(VisualIssue("error", "visual_reviewer_coverage_insufficient", object_id, "Accepted attestation count differs from the frozen requirement."))
        if any(identifier not in accepted_rows for identifier in (attestation_ids if isinstance(attestation_ids, list) else [])):
            issues.append(VisualIssue("error", "visual_review_attestation_unresolved", object_id, "Frozen accepted attestation ID is absent."))
    if not manifest.get("candidate_only") and set(accepted_map) - object_ids:
        issues.append(VisualIssue("error", "visual_review_attestation_binding_unexpected", "assurance-manifest.json.review_binding.accepted_attestations", "Accepted map references an absent promoted object."))
    if manifest.get("manifest_sha256") != sha256_json({key: value for key, value in manifest.items() if key != "manifest_sha256"}):
        issues.append(VisualIssue("error", "visual_assurance_manifest_invalid", "assurance-manifest.json.manifest_sha256", "Manifest content hash differs."))
    return sorted(issues, key=lambda issue: (issue.code, issue.path, issue.message))


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
    os.chmod(path, 0o600)


def write_visual_bundle(root: Path, bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Write candidate-only JSONL artifacts under a local workpack."""

    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    paths = {"objects": root / "visual-objects.jsonl", "relations": root / "visual-relations.jsonl", "table_grids": root / "table-grids.jsonl", "conflicts": root / "visual-conflicts.jsonl", "table_continuations": root / "table-continuations.jsonl"}
    _write_jsonl(paths["objects"], bundle.get("objects", []))
    _write_jsonl(paths["relations"], bundle.get("relations", []))
    _write_jsonl(paths["table_grids"], bundle.get("table_grids", []))
    _write_jsonl(paths["conflicts"], bundle.get("conflicts", []))
    _write_jsonl(paths["table_continuations"], bundle.get("table_continuations", []))
    return {key: {"path": path.name, "sha256": sha256_file(path), "record_count": len(bundle.get(key, []))} for key, path in paths.items()}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    candidate = sub.add_parser("candidates")
    candidate.add_argument("source", type=Path)
    candidate.add_argument("--page", action="append", type=int)
    candidate.add_argument("--pdftoppm", type=Path)
    candidate.add_argument("--output", type=Path, required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("bundle", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "candidates":
            bundle = extract_visual_candidates(args.source, pages=args.page, pdftoppm=args.pdftoppm)
            write_visual_bundle(args.output, bundle)
            print(json.dumps({"source_sha256": bundle["source_sha256"], "object_count": len(bundle["objects"]), "relation_count": len(bundle["relations"]), "table_grid_count": len(bundle["table_grids"]), "conflict_count": len(bundle["conflicts"]), "candidate_only": True}, ensure_ascii=False, sort_keys=True))
        else:
            payload = json.loads(args.bundle.read_text(encoding="utf-8"))
            issues = validate_visual_bundle(payload)
            print(json.dumps({"passed": not issues, "issues": [issue.to_dict() for issue in issues]}, ensure_ascii=False, sort_keys=True))
            return 0 if not issues else 1
    except (VisualSemanticsError, OSError, ValueError) as error:
        print(f"error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "VISUAL_OBJECT_SCHEMA", "VISUAL_RELATION_SCHEMA", "TABLE_GRID_SCHEMA", "VISUAL_CONFLICT_SCHEMA", "VISUAL_REVIEW_ATTESTATION_SCHEMA", "VISUAL_ASSURANCE_MANIFEST_SCHEMA", "VISUAL_REVIEW_OVERLAY_SCHEMA", "VISUAL_PROTOCOL", "VISUAL_REVIEW_PROTOCOL", "VISUAL_REVIEW_CHECKS", "COORDINATE_SPACE", "OBJECT_TYPES", "RELATION_TYPES", "VisualIssue", "VisualSemanticsError", "canonical_json", "sha256_bytes", "sha256_file", "sha256_json", "stable_id", "stable_visual_id", "normalize_bbox", "normalize_polygon", "coordinate_transform_receipt", "crop_commitment_sha256", "render_receipt", "visual_input_sha256", "build_visual_object", "build_visual_relation", "build_table_grid", "rekey_table_grid", "build_visual_conflict", "extract_visual_candidates", "deduplicate_visual_candidates", "validate_visual_bundle", "build_visual_review_attestation", "validate_visual_review_attestations", "merge_visual_review_fragments", "visual_assurance_manifest", "visual_review_overlay_manifest", "validate_visual_review_overlay_manifest", "validate_visual_assurance_manifest", "write_visual_bundle"
]
