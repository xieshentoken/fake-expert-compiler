#!/usr/bin/env python3
"""Explicit, candidate-only visual adapter boundary.

The canonical native path lives in :mod:`visual_semantics`.  This module only
describes optional local challengers and normalizes their already-produced
candidate rows.  It never installs packages, loads a model implicitly, or
falls back from one parser to another.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from visual_semantics import COORDINATE_SPACE, VisualSemanticsError, normalize_bbox, normalize_polygon, sha256_json
import re


HASH_RE = re.compile(r"^[0-9a-f]{64}$")


ADAPTERS = {
    "pypdf-native": {
        "kind": "native-evidence",
        "module": "pypdf",
        "canonical": True,
        "model_required": False,
    },
    "docling": {
        "kind": "structural-candidate",
        "module": "docling",
        "canonical": False,
        "model_required": True,
    },
    "pymupdf": {
        "kind": "geometry-image-drawing-table-candidate",
        "module": "fitz",
        "canonical": False,
        "model_required": False,
    },
    "paddleocr-ppstructure": {
        "kind": "explicit-scan-page-route",
        "module": "paddleocr",
        "canonical": False,
        "model_required": True,
    },
    "paddleocr-ppocrv6": {
        "kind": "explicit-scan-region-text-candidate",
        "module": "paddleocr",
        "canonical": False,
        "model_required": True,
    },
    # Deterministic fixture adapter used only by synthetic tests.  It is not a
    # production parser and can never become canonical or promoted.
    "fake": {
        "kind": "synthetic-candidate-fixture",
        "module": None,
        "canonical": False,
        "model_required": False,
        "test_only": True,
    },
}


class VisualAdapterError(RuntimeError):
    """Stable fail-closed adapter error."""


@dataclass(frozen=True)
class AdapterReceipt:
    adapter_id: str
    available: bool
    candidate_only: bool
    model_required: bool
    model_download: bool
    network_enabled: bool
    unavailable_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "available": self.available,
            "candidate_only": self.candidate_only,
            "model_required": self.model_required,
            "model_download": self.model_download,
            "network_enabled": self.network_enabled,
            **({"unavailable_code": self.unavailable_code} if self.unavailable_code else {}),
        }


def adapter_receipt(adapter_id: str, *, model_root: str | None = None, allow_model_download: bool = False, network_enabled: bool = False, external_runtime: bool = False) -> AdapterReceipt:
    spec = ADAPTERS.get(adapter_id)
    if spec is None:
        raise VisualAdapterError(f"visual_adapter_unknown:{adapter_id}")
    if allow_model_download or network_enabled:
        raise VisualAdapterError("visual_adapter_network_or_download_forbidden")
    available = True if spec.get("test_only") or external_runtime else importlib.util.find_spec(str(spec["module"])) is not None
    unavailable_code = None
    if spec["model_required"] and not model_root:
        available = False
        unavailable_code = "local_model_artifacts_required"
    elif spec["model_required"] and not Path(model_root).expanduser().is_dir():
        available = False
        unavailable_code = "local_model_artifacts_missing"
    elif not available:
        unavailable_code = "adapter_not_installed"
    return AdapterReceipt(
        adapter_id=adapter_id,
        available=available,
        candidate_only=True,
        model_required=bool(spec["model_required"]),
        model_download=False,
        network_enabled=False,
        unavailable_code=unavailable_code,
    )


def normalize_candidate_rows(adapter_id: str, rows: Sequence[Mapping[str, Any]], *, model_root: str | None = None, parser_receipt: Mapping[str, Any] | None = None, backend_receipt: Mapping[str, Any] | None = None, model_receipt: Mapping[str, Any] | None = None, config_receipt: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Normalize external adapter output without allowing direct promotion."""

    receipt = adapter_receipt(adapter_id, model_root=model_root)
    if not receipt.available:
        raise VisualAdapterError(f"{adapter_id}:{receipt.unavailable_code or 'unavailable'}")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise VisualAdapterError(f"visual_adapter_row_invalid:{index}")
        row = dict(raw)
        if row.get("status") not in {None, "candidate"}:
            raise VisualAdapterError("visual_adapter_direct_promotion_forbidden")
        row["status"] = "candidate"
        row["parser_receipt"] = dict(parser_receipt or {"id": adapter_id, "candidate_only": True})
        row["backend_receipt"] = dict(backend_receipt or {"id": adapter_id, "candidate_only": True})
        row["model_receipt"] = dict(model_receipt or {"id": "none", "available": False, "download": False})
        row["config_receipt"] = dict(config_receipt or {"id": f"{adapter_id}-visual-v0.1", "candidate_only": True})
        normalized.append(row)
    return normalized


_KIND_MAP = {
    "docling": {
        "table": "table",
        "formula": "equation_display",
        "equation": "equation_display",
        "picture": "figure",
        "figure": "figure",
        "chart": "plot",
        "plot": "plot",
    },
    "pymupdf": {
        "table": "table",
        "image": "figure",
        "picture": "figure",
        "drawing": "diagram",
        "figure": "figure",
    },
    "paddleocr-ppstructure": {
        "table": "table",
        "formula": "equation_display",
        "equation": "equation_display",
        "annotation": "annotation",
        "caption": "caption",
        "diagram": "diagram",
        "chart": "plot",
        "plot": "plot",
        "picture": "figure",
        "figure": "figure",
    },
    "paddleocr-ppocrv6": {
        "table": "table",
        "formula": "equation_display",
        "equation": "equation_display",
        "chart": "plot",
        "plot": "plot",
        "picture": "figure",
        "figure": "figure",
    },
    "fake": {
        "table": "table",
        "formula": "equation_display",
        "equation": "equation_display",
        "chart": "plot",
        "plot": "plot",
        "picture": "figure",
        "figure": "figure",
        "diagram": "diagram",
        "axis": "axis",
        "tick": "tick",
        "legend": "legend",
        "series": "series",
    },
}


def _adapter_mapping_spec(adapter_id: str, model_root: str | None) -> dict[str, Any]:
    spec = ADAPTERS.get(adapter_id)
    if spec is None:
        raise VisualAdapterError(f"visual_adapter_unknown:{adapter_id}")
    if bool(spec["model_required"]):
        if not model_root:
            raise VisualAdapterError("local_model_artifacts_required")
        if not Path(model_root).expanduser().is_dir():
            raise VisualAdapterError("local_model_artifacts_missing")
    return spec


def map_candidate_locators(
    adapter_id: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    source_sha256: str,
    physical_page: int | None = None,
    page_width: float,
    page_height: float,
    render_sha256: str,
    render_dpi: int,
    render_rotation: int = 0,
    coordinate_space: str = COORDINATE_SPACE,
    parser_receipt: Mapping[str, Any] | None = None,
    backend_receipt: Mapping[str, Any] | None = None,
    model_receipt: Mapping[str, Any] | None = None,
    config_receipt: Mapping[str, Any] | None = None,
    model_root: str | None = None,
) -> list[dict[str, Any]]:
    """Map already-produced optional-adapter rows to confirmed-bbox locators.

    This is a pure mapping boundary: it does not import or run Docling,
    PyMuPDF, or PaddleOCR.  Model-required adapters still need an explicit
    existing local model root; no package/model availability is inferred from
    arbitrary input rows.
    """

    _adapter_mapping_spec(adapter_id, model_root)
    if not HASH_RE.fullmatch(str(source_sha256)) or not HASH_RE.fullmatch(str(render_sha256)):
        raise VisualAdapterError("visual_adapter_provenance_hash_invalid")
    if physical_page is not None and (not isinstance(physical_page, int) or physical_page < 1):
        raise VisualAdapterError("visual_adapter_physical_page_invalid")
    if not isinstance(render_dpi, int) or render_dpi < 36 or int(render_rotation) % 90 != 0:
        raise VisualAdapterError("visual_adapter_render_receipt_invalid")
    if not isinstance(parser_receipt, Mapping) or not isinstance(backend_receipt, Mapping) or not isinstance(model_receipt, Mapping) or not isinstance(config_receipt, Mapping):
        raise VisualAdapterError("visual_adapter_provenance_receipts_required")
    for receipt_name, receipt in (("parser", parser_receipt), ("backend", backend_receipt), ("model", model_receipt), ("config", config_receipt)):
        if not isinstance(receipt.get("id"), str) or not receipt.get("id"):
            raise VisualAdapterError(f"visual_adapter_receipt_invalid:{receipt_name}")
        if receipt.get("network_enabled") is True or receipt.get("network") is True:
            raise VisualAdapterError(f"visual_adapter_network_forbidden:{receipt_name}")
        if receipt_name == "model" and receipt.get("download") is True:
            raise VisualAdapterError("visual_adapter_model_download_forbidden")
        if receipt_name != "model" and receipt.get("candidate_only") is not True:
            raise VisualAdapterError(f"visual_adapter_receipt_not_candidate_only:{receipt_name}")
    mapping = _KIND_MAP.get(adapter_id, {})
    locators: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise VisualAdapterError(f"visual_adapter_row_invalid:{index}")
        raw_kind = raw.get("kind") or raw.get("type") or raw.get("label")
        kind = str(raw_kind or "").strip().casefold().replace("_", "-")
        mapped_type = mapping.get(kind)
        if mapped_type is None:
            raise VisualAdapterError(f"visual_adapter_unknown_kind:{adapter_id}:{kind or 'missing'}")
        row_page = raw.get("physical_page", physical_page)
        if not isinstance(row_page, int) or row_page < 1:
            raise VisualAdapterError(f"visual_adapter_physical_page_missing:{index}")
        if physical_page is not None and row_page != physical_page:
            raise VisualAdapterError(f"visual_adapter_physical_page_mismatch:{index}")
        row_source = raw.get("source_sha256", source_sha256)
        row_render = raw.get("render_sha256", render_sha256)
        if row_source != source_sha256 or row_render != render_sha256:
            raise VisualAdapterError(f"visual_adapter_hash_mismatch:{index}")
        bbox = raw.get("bbox") or raw.get("bounding_box")
        if bbox is None and isinstance(raw.get("geometry"), Mapping):
            bbox = raw["geometry"].get("bbox")
        if not isinstance(bbox, (list, tuple)):
            raise VisualAdapterError(f"visual_adapter_bbox_missing:{index}")
        try:
            normalized_bbox = normalize_bbox(bbox, page_width, page_height, coordinate_space, int(render_rotation), render_dpi if coordinate_space in {"render-pixels-top-left-v1", "image-pixels-top-left-v1"} else None)
        except VisualSemanticsError as error:
            raise VisualAdapterError(f"visual_adapter_coordinate_invalid:{index}:{error}") from error
        polygon = raw.get("polygon")
        normalized_polygon = None
        if polygon is not None:
            try:
                normalized_polygon = normalize_polygon(polygon, page_width, page_height, coordinate_space, int(render_rotation), render_dpi if coordinate_space in {"render-pixels-top-left-v1", "image-pixels-top-left-v1"} else None)
            except VisualSemanticsError as error:
                raise VisualAdapterError(f"visual_adapter_polygon_invalid:{index}:{error}") from error
        content = raw.get("content") if raw.get("content") is not None else raw.get("text")
        content_hash = raw.get("content_sha256") or sha256_json(content if content is not None else {key: value for key, value in raw.items() if key not in {"bbox", "bounding_box", "geometry", "polygon"}})
        if not HASH_RE.fullmatch(str(content_hash)):
            raise VisualAdapterError(f"visual_adapter_content_hash_invalid:{index}")
        locators.append({
            "physical_page": row_page,
            "type": mapped_type,
            "subtype": kind,
            "label": raw.get("label") or raw.get("caption") or raw_kind,
            "content_sha256": content_hash,
            "bbox": list(bbox),
            "polygon": list(polygon) if isinstance(polygon, (list, tuple)) else None,
            "coordinate_space": coordinate_space,
            "rotation": int(render_rotation) % 360,
            "render_dpi": int(render_dpi),
            "render_sha256": render_sha256,
            "source_sha256": source_sha256,
            "parser_receipt": dict(parser_receipt),
            "backend_receipt": dict(backend_receipt),
            "model_receipt": dict(model_receipt),
            "config_receipt": dict(config_receipt),
            "candidate_only": True,
        })
    return locators


# Explicit descriptive aliases used by host adapters.
adapter_candidate_locators = map_candidate_locators
map_adapter_candidates = map_candidate_locators


def require_available(adapter_id: str, *, model_root: str | None = None) -> dict[str, Any]:
    receipt = adapter_receipt(adapter_id, model_root=model_root)
    if not receipt.available:
        raise VisualAdapterError(f"{adapter_id}:{receipt.unavailable_code or 'unavailable'}")
    return receipt.to_dict()


# Short compatibility name used by adapter-focused callers.  It deliberately
# retains the explicit ``model_root`` and candidate-only behavior above.
candidate_rows = normalize_candidate_rows


__all__ = ["ADAPTERS", "AdapterReceipt", "VisualAdapterError", "adapter_receipt", "normalize_candidate_rows", "candidate_rows", "map_candidate_locators", "adapter_candidate_locators", "map_adapter_candidates", "require_available"]
