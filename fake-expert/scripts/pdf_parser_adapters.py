#!/usr/bin/env python3
"""Optional local layout parsers normalized into the TKC PDF-IR boundary.

The canonical evidence text is deliberately *not* produced here.  It remains
the pinned pypdf text stream used by :mod:`pdf_structure` and the later source
anchor verifier.  These adapters may contribute only page-attributed layout
candidates (coordinates plus content hashes) and never source text, knowledge
objects, evidence anchors, or promotion decisions.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable


PARSER_RECEIPT_SCHEMA = "tkc.pdf-parser-receipt/v0.1"
LAYOUT_CANDIDATE_SCHEMA = "tkc.pdf-layout-candidate/v0.1"
RENDER_RECEIPT_SCHEMA = "tkc.pdf-render-receipt/v0.1"
COORDINATE_SPACE = "pdf-page-top-left-points-v1"
SUPPORTED_LAYOUT_PARSERS = ("pypdf", "pymupdf", "docling")


class ParserAdapterError(RuntimeError):
    """A selected local parser cannot produce the constrained adapter output."""


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _version(distribution: str, fallback: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return fallback


def _point(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ParserAdapterError("parser_coordinate_invalid") from error
    if not math.isfinite(number):
        raise ParserAdapterError("parser_coordinate_invalid")
    # The round-trip representation is deterministic while retaining sub-point
    # precision useful for human visual checks.
    return round(number, 4)


def _dimensions(value: Any) -> dict[str, float]:
    if isinstance(value, dict):
        width, height = value.get("width"), value.get("height")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        width, height = value
    else:
        width, height = getattr(value, "width", None), getattr(value, "height", None)
    width, height = _point(width), _point(height)
    if width <= 0 or height <= 0:
        raise ParserAdapterError("parser_page_dimensions_invalid")
    return {"width": width, "height": height}


def _bbox(values: Iterable[Any], dimensions: dict[str, float]) -> list[float]:
    values = [_point(value) for value in values]
    if len(values) != 4:
        raise ParserAdapterError("parser_coordinate_invalid")
    left, top, right, bottom = values
    if left < 0 or top < 0 or right < left or bottom < top:
        raise ParserAdapterError("parser_coordinate_invalid")
    if right > dimensions["width"] + 0.01 or bottom > dimensions["height"] + 0.01:
        raise ParserAdapterError("parser_coordinate_outside_page")
    return values


def _candidate(
    *,
    source_sha256: str,
    parser_id: str,
    parser_version: str,
    physical_page: int,
    sequence: int,
    kind: str,
    text: str,
    bbox: list[float],
    order: list[int] | None,
) -> dict[str, Any]:
    normalized = normalize_text(text)
    if not normalized:
        raise ParserAdapterError("parser_candidate_empty")
    payload = {
        "source_sha256": source_sha256,
        "parser_id": parser_id,
        "parser_version": parser_version,
        "physical_page": physical_page,
        "sequence": sequence,
        "kind": kind,
        "bbox": bbox,
        "content_sha256": sha256_text(normalized),
        "order": order,
    }
    return {
        "schema_version": LAYOUT_CANDIDATE_SCHEMA,
        "id": f"plc-{sha256_json(payload)[:20]}",
        "coordinate_space": COORDINATE_SPACE,
        **payload,
    }


def _page_receipt(
    physical_page: int,
    dimensions: dict[str, float],
    candidates: list[dict[str, Any]],
    status: str,
) -> dict[str, Any]:
    return {
        "physical_page": physical_page,
        "page_dimensions_points": dimensions,
        "coordinate_status": status,
        "candidate_count": len(candidates),
        "candidates_sha256": sha256_json(candidates),
    }


def _pypdf_receipt(
    source_sha256: str,
    start_page: int,
    end_page: int,
    page_dimensions: dict[int, dict[str, float]],
    canonical_text_extractor: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    parser_version = canonical_text_extractor.removeprefix("pypdf-")
    pages = [
        _page_receipt(page, page_dimensions[page], [], "unavailable")
        for page in range(start_page, end_page + 1)
    ]
    receipt = {
        "schema_version": PARSER_RECEIPT_SCHEMA,
        "source_sha256": source_sha256,
        "scope": {"start_page": start_page, "end_page": end_page},
        "canonical_text_extractor": canonical_text_extractor,
        "layout_parser": {
            "id": "pypdf",
            "version": parser_version,
            "configuration": {"layout_output": "none"},
            "coordinate_space": None,
            "candidate_status": "unavailable",
        },
        "pages": pages,
        "layout_candidates_sha256": sha256_json([]),
    }
    return receipt, []


def _import_pymupdf() -> tuple[Any, str]:
    try:
        import pymupdf as fitz  # PyMuPDF 1.24+
    except ModuleNotFoundError:
        try:
            import fitz  # PyMuPDF <= 1.23
        except ModuleNotFoundError as error:
            raise ParserAdapterError(
                "pymupdf_not_installed: install the optional local parser extra"
            ) from error
    return fitz, _version("PyMuPDF", str(getattr(fitz, "VersionBind", "unknown")))


def _pymupdf_receipt(
    source_path: Path,
    source_sha256: str,
    start_page: int,
    end_page: int,
    canonical_text_extractor: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    fitz, parser_version = _import_pymupdf()
    candidates: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    try:
        document = fitz.open(str(source_path))
    except Exception as error:
        raise ParserAdapterError(f"pymupdf_open_failed: {error}") from error
    try:
        if len(document) < end_page:
            raise ParserAdapterError("pymupdf_page_count_mismatch")
        for physical_page in range(start_page, end_page + 1):
            page = document[physical_page - 1]
            dimensions = _dimensions(page.rect)
            page_candidates: list[dict[str, Any]] = []
            try:
                words = page.get_text("words", sort=False)
            except Exception as error:
                raise ParserAdapterError(
                    f"pymupdf_word_extraction_failed: page={physical_page}: {error}"
                ) from error
            for sequence, word in enumerate(words):
                if not isinstance(word, (list, tuple)) or len(word) < 5:
                    raise ParserAdapterError("pymupdf_word_shape_invalid")
                text = str(word[4])
                if not normalize_text(text):
                    continue
                order = [int(value) for value in word[5:8]] if len(word) >= 8 else None
                page_candidates.append(
                    _candidate(
                        source_sha256=source_sha256,
                        parser_id="pymupdf",
                        parser_version=parser_version,
                        physical_page=physical_page,
                        sequence=sequence,
                        kind="word",
                        text=text,
                        bbox=_bbox(word[0:4], dimensions),
                        order=order,
                    )
                )
            candidates.extend(page_candidates)
            pages.append(
                _page_receipt(
                    physical_page,
                    dimensions,
                    page_candidates,
                    "available" if page_candidates else "no-text-candidates",
                )
            )
    finally:
        document.close()
    receipt = {
        "schema_version": PARSER_RECEIPT_SCHEMA,
        "source_sha256": source_sha256,
        "scope": {"start_page": start_page, "end_page": end_page},
        "canonical_text_extractor": canonical_text_extractor,
        "layout_parser": {
            "id": "pymupdf",
            "version": parser_version,
            "configuration": {"text_mode": "words", "sort": False},
            "coordinate_space": COORDINATE_SPACE,
            "candidate_status": "layout-candidate-only",
        },
        "pages": pages,
        "layout_candidates_sha256": sha256_json(candidates),
    }
    return receipt, candidates


def _docling_bbox(value: Any, dimensions: dict[str, float]) -> list[float]:
    if isinstance(value, dict):
        left = value.get("l", value.get("left"))
        top = value.get("t", value.get("top"))
        right = value.get("r", value.get("right"))
        bottom = value.get("b", value.get("bottom"))
        origin = value.get("coord_origin", value.get("origin"))
    else:
        left = getattr(value, "l", getattr(value, "left", None))
        top = getattr(value, "t", getattr(value, "top", None))
        right = getattr(value, "r", getattr(value, "right", None))
        bottom = getattr(value, "b", getattr(value, "bottom", None))
        origin = getattr(value, "coord_origin", getattr(value, "origin", None))
    origin_text = str(origin).upper()
    if "BOTTOM" in origin_text:
        return _bbox(
            [left, dimensions["height"] - bottom, right, dimensions["height"] - top],
            dimensions,
        )
    if "TOP" in origin_text:
        return _bbox([left, top, right, bottom], dimensions)
    raise ParserAdapterError("docling_coordinate_origin_unknown")


def _docling_dimensions(document: Any, page: int, fallback: dict[str, float]) -> dict[str, float]:
    pages = getattr(document, "pages", None)
    page_data: Any = None
    if isinstance(pages, dict):
        page_data = pages.get(page)
    elif isinstance(pages, (list, tuple)) and len(pages) >= page:
        page_data = pages[page - 1]
    size = getattr(page_data, "size", page_data) if page_data is not None else None
    try:
        return _dimensions(size)
    except ParserAdapterError:
        return fallback


def _docling_item_text(item: Any, collection: str) -> str:
    text = getattr(item, "text", None)
    if isinstance(text, str) and normalize_text(text):
        return text
    if collection == "tables":
        exporter = getattr(item, "export_to_markdown", None)
        if callable(exporter):
            exported = exporter()
            if isinstance(exported, str):
                return exported
    return ""


def _docling_kind(item: Any, collection: str) -> str:
    if collection == "tables":
        return "table"
    label = str(getattr(item, "label", "text")).casefold()
    if "formula" in label or "equation" in label:
        return "formula"
    if "title" in label or "heading" in label:
        return "heading"
    return "text"


DOCLING_FORMULA_ENV = "EFIREBLE_DOCLING_FORMULAS"


def _docling_device() -> str:
    """Select the best available torch device for Docling enrichment models.

    Absorbed from the v1.0.0 engine: mps -> cuda -> cpu. The selected device is
    recorded in the parser receipt so accelerated runs stay auditable.
    """
    try:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except (ImportError, AttributeError):
        pass
    return "cpu"


def _docling_accel(device: str) -> Any:
    """Build Docling AcceleratorOptions using the AcceleratorDevice enum.

    Docling >= 2.x requires the enum value; a plain string silently falls back
    to CPU there. Older versions accept the string directly, so the enum is
    tried first and the string is used as a fallback.
    """
    from docling.datamodel.pipeline_options import AcceleratorOptions

    try:
        from docling.datamodel.pipeline_options import AcceleratorDevice

        mapping = {
            "mps": AcceleratorDevice.MPS,
            "cuda": AcceleratorDevice.CUDA,
            "cpu": AcceleratorDevice.CPU,
        }
        return AcceleratorOptions(device=mapping.get(device, AcceleratorDevice.CPU))
    except (ImportError, AttributeError):
        return AcceleratorOptions(device=device)


def _effective_docling_formulas(explicit: bool) -> tuple[bool, bool]:
    """Resolve formula enrichment from the explicit flag and the env var.

    The explicit ``--docling-formulas`` flag wins. Otherwise the
    ``EFIREBLE_DOCLING_FORMULAS`` env var (any value other than 0/false/off
    enables it, matching the v1.0.0 engine) is honored. Returns
    ``(effective, from_env)`` so the receipt can record the source.
    """
    if explicit:
        return True, False
    value = os.environ.get(DOCLING_FORMULA_ENV)
    if value is None:
        return False, False
    return value.strip().casefold() not in {"0", "false", "off"}, True


def _docling_receipt(
    source_path: Path,
    source_sha256: str,
    start_page: int,
    end_page: int,
    page_dimensions: dict[int, dict[str, float]],
    canonical_text_extractor: str,
    *,
    formulas: bool,
    allow_network: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        import docling
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ModuleNotFoundError as error:
        raise ParserAdapterError(
            "docling_not_installed: install the optional local parser extra"
        ) from error
    except Exception as error:
        raise ParserAdapterError(f"docling_api_unsupported: {error}") from error

    formulas, formulas_from_env = _effective_docling_formulas(formulas)
    device = _docling_device()

    try:
        with _docling_network_policy(allow_network):
            try:
                accel = _docling_accel(device)
                options = PdfPipelineOptions(accelerator_options=accel)
                accelerator_applied = True
            except Exception:
                options = PdfPipelineOptions()
                accelerator_applied = False
            options.do_formula_enrichment = formulas
            options.do_table_structure = True
            converter = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
            )
            document = converter.convert(str(source_path)).document
    except Exception as error:
        raise ParserAdapterError(f"docling_conversion_failed: {error}") from error

    parser_version = _version("docling", str(getattr(docling, "__version__", "unknown")))
    grouped: dict[int, list[dict[str, Any]]] = {
        page: [] for page in range(start_page, end_page + 1)
    }
    sequence: dict[int, int] = {page: 0 for page in grouped}
    for collection in ("texts", "tables"):
        for item in getattr(document, collection, []) or []:
            text = _docling_item_text(item, collection)
            if not normalize_text(text):
                continue
            for provenance in getattr(item, "prov", None) or []:
                page = getattr(provenance, "page_no", None)
                if not isinstance(page, int) or page not in grouped:
                    continue
                bbox_value = getattr(provenance, "bbox", None)
                if bbox_value is None:
                    raise ParserAdapterError("docling_coordinate_missing")
                dimensions = _docling_dimensions(document, page, page_dimensions[page])
                grouped[page].append(
                    _candidate(
                        source_sha256=source_sha256,
                        parser_id="docling",
                        parser_version=parser_version,
                        physical_page=page,
                        sequence=sequence[page],
                        kind=_docling_kind(item, collection),
                        text=text,
                        bbox=_docling_bbox(bbox_value, dimensions),
                        order=None,
                    )
                )
                sequence[page] += 1

    candidates = [candidate for page in sorted(grouped) for candidate in grouped[page]]
    pages = [
        _page_receipt(
            page,
            _docling_dimensions(document, page, page_dimensions[page]),
            grouped[page],
            "available" if grouped[page] else "no-parser-items",
        )
        for page in range(start_page, end_page + 1)
    ]
    receipt = {
        "schema_version": PARSER_RECEIPT_SCHEMA,
        "source_sha256": source_sha256,
        "scope": {"start_page": start_page, "end_page": end_page},
        "canonical_text_extractor": canonical_text_extractor,
        "layout_parser": {
            "id": "docling",
            "version": parser_version,
            "configuration": {
                "formula_enrichment": formulas,
                "formula_enrichment_from_env": formulas_from_env,
                "table_structure": True,
                "accelerator": {"device": device, "applied": accelerator_applied},
                "model_network": "caller-authorized" if allow_network else "offline-enforced",
            },
            "coordinate_space": COORDINATE_SPACE,
            "candidate_status": "layout-candidate-only",
        },
        "pages": pages,
        "layout_candidates_sha256": sha256_json(candidates),
    }
    return receipt, candidates


@contextmanager
def _docling_network_policy(allow_network: bool) -> Any:
    """Prevent common Docling/Hugging Face model-download paths by default."""

    if allow_network:
        yield
        return
    names = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ[name] = "1"
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def build_parser_receipt(
    source_path: Path,
    source_sha256: str,
    start_page: int,
    end_page: int,
    page_dimensions: dict[int, dict[str, float]],
    canonical_text_extractor: str,
    layout_parser: str,
    *,
    docling_formulas: bool = False,
    docling_allow_network: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return only bounded, source-hash-bound layout candidates.

    A selected optional backend never falls back to another backend.  Falling
    back would make an IR look like it used a parser that did not actually
    produce its coordinate data.
    """

    if layout_parser not in SUPPORTED_LAYOUT_PARSERS:
        raise ParserAdapterError(f"unsupported_layout_parser: {layout_parser}")
    if layout_parser == "pypdf":
        return _pypdf_receipt(
            source_sha256,
            start_page,
            end_page,
            page_dimensions,
            canonical_text_extractor,
        )
    if layout_parser == "pymupdf":
        return _pymupdf_receipt(
            source_path,
            source_sha256,
            start_page,
            end_page,
            canonical_text_extractor,
        )
    return _docling_receipt(
        source_path,
        source_sha256,
        start_page,
        end_page,
        page_dimensions,
        canonical_text_extractor,
        formulas=docling_formulas,
        allow_network=docling_allow_network,
    )


def _resolve_renderer(command: Path | str) -> Path:
    raw = str(command)
    candidate = Path(raw).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    resolved = shutil.which(raw)
    if resolved:
        return Path(resolved).resolve()
    raise ParserAdapterError(f"pdftoppm_missing: {raw}")


def build_render_receipts(
    source_path: Path,
    source_sha256: str,
    pages: Iterable[int],
    renderer: Path | str,
    *,
    dpi: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Render each selected physical page, retain only its deterministic receipt."""

    if dpi < 36 or dpi > 1200:
        raise ParserAdapterError("render_dpi_out_of_range")
    pdftoppm = _resolve_renderer(renderer)
    try:
        version_process = subprocess.run(
            [str(pdftoppm), "-v"], check=False, capture_output=True, text=True
        )
    except OSError as error:
        raise ParserAdapterError(f"pdftoppm_failed: {error}") from error
    version_lines = (version_process.stderr or version_process.stdout).strip().splitlines()
    if version_process.returncode != 0 or not version_lines:
        raise ParserAdapterError("pdftoppm_version_unavailable")
    renderer_version = version_lines[0]

    records: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="tkc-pdf-ir-render-") as temporary:
        temporary_root = Path(temporary)
        for page in sorted(set(pages)):
            prefix = temporary_root / f"page-{page:04d}"
            target = prefix.with_suffix(".png")
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
                raise ParserAdapterError(
                    f"pdf_render_failed: page={page}: {completed.stderr.strip()}"
                )
            data = target.read_bytes()
            if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
                raise ParserAdapterError(f"pdf_render_invalid_png: page={page}")
            records.append(
                {
                    "schema_version": RENDER_RECEIPT_SCHEMA,
                    "source_sha256": source_sha256,
                    "physical_page": page,
                    "renderer": {
                        "id": "pdftoppm",
                        "version": renderer_version,
                        "dpi": dpi,
                        "format": "png",
                        "page_selection": "single-physical-page",
                    },
                    "render_sha256": hashlib.sha256(data).hexdigest(),
                }
            )
    profile = {
        "id": "pdftoppm",
        "version": renderer_version,
        "dpi": dpi,
        "format": "png",
        "page_selection": "single-physical-page",
        "source_sha256": source_sha256,
        "receipt_sha256": sha256_json(records),
    }
    return profile, records
