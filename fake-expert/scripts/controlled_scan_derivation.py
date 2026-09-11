#!/usr/bin/env python3
"""Build and verify a private image-only scan derivation for bounded testing.

This tool is deliberately narrower than the scanned-PDF OCR adapter.  It renders
an explicitly selected local page range, embeds each complete page render as the
only page content in a new PDF, and proves pixel identity plus text-layer
absence.  It never runs OCR, rewrites Gold, promotes knowledge, or creates a
release.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import platform
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

import PIL
import pypdf
import reportlab
from PIL import Image
from pypdf import PdfReader
from pypdf.generic import ContentStream
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from compiler_version import (
    CONTROLLED_SCAN_DERIVATION_COMPILER_VERSION,
    CONTROLLED_SCAN_DERIVATION_PROTOCOL,
    CONTROLLED_SCAN_DERIVATION_SCHEMA,
)


PDF_NAME = "controlled-image-only-scan.pdf"
MANIFEST_NAME = "controlled-scan-derivation.json"
COORDINATE_SPACE = "pdf-page-top-left-points-v1"
_RFC3339_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)


class ControlledScanError(RuntimeError):
    """Stable fail-closed error for controlled scan derivation."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _secure_file(path: Path, label: str) -> Path:
    path = path.expanduser().resolve()
    if not path.is_file() or path.is_symlink():
        raise ControlledScanError(f"{label}_file_invalid")
    return path


def _secure_directory(path: Path, *, empty: bool = False) -> Path:
    path = path.expanduser().resolve()
    if path.exists():
        if not path.is_dir() or path.is_symlink():
            raise ControlledScanError("output_directory_invalid")
        if empty and any(path.iterdir()):
            raise ControlledScanError("output_not_empty")
    else:
        path.mkdir(parents=True, mode=0o700)
    return path


def _secure_input_directory(path: Path, label: str) -> Path:
    path = path.expanduser().resolve()
    if not path.is_dir() or path.is_symlink():
        raise ControlledScanError(f"{label}_directory_invalid")
    return path


def _safe_child_file(root: Path, relative: Any, label: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ControlledScanError(f"{label}_path_invalid")
    parts = Path(relative).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ControlledScanError(f"{label}_path_invalid")
    candidate = (root / relative).resolve()
    if candidate.parent != root and root not in candidate.parents:
        raise ControlledScanError(f"{label}_path_escape")
    return _secure_file(candidate, label)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    path = _secure_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ControlledScanError(f"{label}_json_invalid") from error
    if not isinstance(value, dict):
        raise ControlledScanError(f"{label}_object_required")
    return value


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    path = _secure_file(path, label)
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ControlledScanError(f"{label}_jsonl_invalid:{line_number}") from error
        if not isinstance(value, dict):
            raise ControlledScanError(f"{label}_row_object_required:{line_number}")
        rows.append(value)
    return rows


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _validate_created_at(value: str) -> str:
    if not _RFC3339_RE.fullmatch(value):
        raise ControlledScanError("created_at_invalid")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ControlledScanError("created_at_invalid") from error
    if parsed.tzinfo is None:
        raise ControlledScanError("created_at_invalid")
    return value


def _run(command: list[str], label: str, *, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ControlledScanError(f"{label}_failed") from error
    if result.returncode != 0:
        raise ControlledScanError(f"{label}_failed")
    return result


def _renderer_version(pdftoppm: Path) -> str:
    result = _run([str(pdftoppm), "-v"], "renderer_version", timeout=30)
    lines = (result.stderr or result.stdout).splitlines()
    if not lines:
        raise ControlledScanError("renderer_version_missing")
    return lines[0].strip()


def _render_page(
    source: Path,
    physical_page: int,
    output: Path,
    pdftoppm: Path,
    dpi: int,
) -> Path:
    prefix = output.with_suffix("")
    _run(
        [
            str(pdftoppm),
            "-f",
            str(physical_page),
            "-l",
            str(physical_page),
            "-r",
            str(dpi),
            "-png",
            "-singlefile",
            str(source),
            str(prefix),
        ],
        f"render_page_{physical_page}",
    )
    rendered = prefix.with_suffix(".png")
    if not rendered.is_file() or rendered.is_symlink():
        raise ControlledScanError(f"render_page_{physical_page}_missing")
    os.chmod(rendered, 0o600)
    return rendered


def _pixel_fingerprint(path: Path) -> dict[str, Any]:
    try:
        with Image.open(path) as image:
            image.load()
            normalized = image.convert("RGB")
            width, height = normalized.size
            digest = hashlib.sha256()
            digest.update(f"RGB:{width}x{height}:".encode("ascii"))
            digest.update(normalized.tobytes())
    except (OSError, ValueError) as error:
        raise ControlledScanError("render_image_invalid") from error
    return {
        "width": width,
        "height": height,
        "mode": "RGB",
        "pixel_sha256": digest.hexdigest(),
        "png_sha256": _sha256_file(path),
    }


def _pixel_comparison(left: Path, right: Path) -> dict[str, Any]:
    try:
        with Image.open(left) as left_image, Image.open(right) as right_image:
            left_rgb = left_image.convert("RGB")
            right_rgb = right_image.convert("RGB")
            if left_rgb.size != right_rgb.size:
                raise ControlledScanError("render_dimensions_changed")
            left_bytes = left_rgb.tobytes()
            right_bytes = right_rgb.tobytes()
    except (OSError, ValueError) as error:
        raise ControlledScanError("render_image_invalid") from error
    absolute_error_sum = 0
    squared_error_sum = 0
    changed_pixels = 0
    max_error = 0
    for offset in range(0, len(left_bytes), 3):
        changed = False
        for channel in range(3):
            error = abs(left_bytes[offset + channel] - right_bytes[offset + channel])
            absolute_error_sum += error
            squared_error_sum += error * error
            max_error = max(max_error, error)
            changed = changed or error != 0
        changed_pixels += int(changed)
    channel_count = len(left_bytes)
    pixel_count = channel_count // 3
    mean_absolute_error = absolute_error_sum / channel_count
    root_mean_square_error = math.sqrt(squared_error_sum / channel_count)
    changed_ratio = changed_pixels / pixel_count
    fidelity_passed = mean_absolute_error <= 12.0 and changed_ratio <= 0.45
    if not fidelity_passed:
        raise ControlledScanError("derived_render_fidelity_failed")
    return {
        "dimensions_equal": True,
        "pixel_exact": changed_pixels == 0,
        "changed_pixel_count": changed_pixels,
        "changed_pixel_ratio": changed_ratio,
        "mean_absolute_error": mean_absolute_error,
        "root_mean_square_error": root_mean_square_error,
        "max_channel_error": max_error,
        "fidelity_policy": {
            "mean_absolute_error_max": 12.0,
            "changed_pixel_ratio_max": 0.45,
        },
        "fidelity_passed": True,
    }


def _page_geometry(page: Any) -> dict[str, Any]:
    rotation = int(page.get("/Rotate", 0) or 0) % 360
    if rotation != 0:
        raise ControlledScanError("nonzero_page_rotation_not_supported")
    media = [float(value) for value in page.mediabox]
    crop = [float(value) for value in page.cropbox]
    width = media[2] - media[0]
    height = media[3] - media[1]
    if not all(math.isfinite(value) for value in (*media, *crop, width, height)):
        raise ControlledScanError("page_geometry_invalid")
    if width <= 0 or height <= 0:
        raise ControlledScanError("page_geometry_invalid")
    return {
        "source_mediabox": media,
        "source_cropbox": crop,
        "source_rotation": rotation,
        "derived_mediabox": [0.0, 0.0, width, height],
        "canonical_page_bbox": [0.0, 0.0, width, height],
        "width_points": width,
        "height_points": height,
        "coordinate_space": COORDINATE_SPACE,
        "bbox_transform": {
            "method": "identity-after-full-mediabox-render",
            "forward_3x3": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            "inverse_3x3": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        },
    }


def _validate_page_dimensions(
    image: Mapping[str, Any], geometry: Mapping[str, Any], dpi: int
) -> None:
    expected_width = float(geometry["width_points"]) * dpi / 72.0
    expected_height = float(geometry["height_points"]) * dpi / 72.0
    if abs(int(image["width"]) - expected_width) > 1.0:
        raise ControlledScanError("render_width_geometry_mismatch")
    if abs(int(image["height"]) - expected_height) > 1.0:
        raise ControlledScanError("render_height_geometry_mismatch")


def _create_image_only_pdf(
    destination: Path, page_rows: Iterable[Mapping[str, Any]], render_root: Path
) -> None:
    rows = list(page_rows)
    if not rows:
        raise ControlledScanError("page_scope_empty")
    first = rows[0]["geometry"]
    document = canvas.Canvas(
        str(destination),
        pagesize=(float(first["width_points"]), float(first["height_points"])),
        pageCompression=1,
        invariant=1,
        pdfVersion=(1, 4),
    )
    document.setTitle("Controlled image-only scan derivation")
    document.setAuthor("fake-expert")
    document.setSubject("Private Phase 7D.6E qualification source")
    for row in rows:
        geometry = row["geometry"]
        width = float(geometry["width_points"])
        height = float(geometry["height_points"])
        document.setPageSize((width, height))
        render = _safe_child_file(render_root, row["render_path"], "origin_render")
        document.drawImage(
            ImageReader(str(render)),
            0,
            0,
            width=width,
            height=height,
            preserveAspectRatio=False,
            mask=None,
        )
        document.showPage()
    document.save()
    if not destination.is_file() or destination.stat().st_size == 0:
        raise ControlledScanError("derived_pdf_missing")
    os.chmod(destination, 0o600)


def _inspect_image_only_pdf(path: Path, expected_pages: int) -> dict[str, Any]:
    try:
        reader = PdfReader(str(path))
    except Exception as error:
        raise ControlledScanError("derived_pdf_invalid") from error
    if len(reader.pages) != expected_pages:
        raise ControlledScanError("derived_page_count_mismatch")
    extracted_chars: list[int] = []
    font_counts: list[int] = []
    text_show_operator_counts: list[int] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        extracted_chars.append(len(text.strip()))
        resources = page.get("/Resources") or {}
        fonts = resources.get("/Font") if hasattr(resources, "get") else None
        if hasattr(fonts, "get_object"):
            fonts = fonts.get_object()
        font_counts.append(len(fonts or {}))
        contents = page.get_contents()
        operations = ContentStream(contents, reader).operations if contents is not None else []
        text_show_operator_counts.append(
            sum(operator in {b"Tj", b"TJ", b"'", b'"'} for _, operator in operations)
        )
    if any(extracted_chars) or any(text_show_operator_counts):
        raise ControlledScanError("derived_text_layer_present")
    return {
        "page_count": len(reader.pages),
        "text_layer_absent": True,
        "extracted_text_char_counts": extracted_chars,
        "font_resource_counts": font_counts,
        "text_show_operator_counts": text_show_operator_counts,
    }


def _embedded_image_fingerprints(
    path: Path, page_rows: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    rows = list(page_rows)
    try:
        reader = PdfReader(str(path))
    except Exception as error:
        raise ControlledScanError("derived_pdf_invalid") from error
    if len(reader.pages) != len(rows):
        raise ControlledScanError("derived_page_count_mismatch")
    fingerprints: list[dict[str, Any]] = []
    for page_number, (page, row) in enumerate(zip(reader.pages, rows), 1):
        resources = page.get("/Resources") or {}
        xobjects = resources.get("/XObject") if hasattr(resources, "get") else None
        if hasattr(xobjects, "get_object"):
            xobjects = xobjects.get_object()
        images = []
        for name, reference in (xobjects or {}).items():
            value = reference.get_object() if hasattr(reference, "get_object") else reference
            if value.get("/Subtype") == "/Image":
                images.append((str(name), value))
        if len(images) != 1:
            raise ControlledScanError(f"derived_page_image_count_invalid:{page_number}")
        name, image = images[0]
        width = int(image.get("/Width", 0))
        height = int(image.get("/Height", 0))
        bits = int(image.get("/BitsPerComponent", 0))
        if image.get("/ColorSpace") != "/DeviceRGB" or bits != 8:
            raise ControlledScanError(f"derived_page_image_format_invalid:{page_number}")
        data = image.get_data()
        if len(data) != width * height * 3:
            raise ControlledScanError(f"derived_page_image_bytes_invalid:{page_number}")
        digest = hashlib.sha256()
        digest.update(f"RGB:{width}x{height}:".encode("ascii"))
        digest.update(data)
        fingerprint = {
            "xobject_name": name,
            "width": width,
            "height": height,
            "mode": "RGB",
            "bits_per_component": bits,
            "decoded_pixel_sha256": digest.hexdigest(),
            "matches_origin_render_pixels": digest.hexdigest()
            == row["origin_render"]["pixel_sha256"],
        }
        if fingerprint["matches_origin_render_pixels"] is not True:
            raise ControlledScanError(f"embedded_origin_pixel_mismatch:{page_number}")
        fingerprints.append(fingerprint)
    return fingerprints


def _bind_reviewed_visual_evidence(
    source_sha256: str,
    pages: list[int],
    gold_revision: Path | None,
    recall_revision: Path | None,
) -> dict[str, Any] | None:
    if (gold_revision is None) != (recall_revision is None):
        raise ControlledScanError("gold_and_recall_must_be_supplied_together")
    if gold_revision is None or recall_revision is None:
        return None
    gold_root = _secure_input_directory(gold_revision, "gold_revision")
    recall_root = _secure_input_directory(recall_revision, "recall_revision")
    gold_manifest_path = gold_root / "gold-review-revision.json"
    gold_fragments_path = gold_root / "gold-fragments.jsonl"
    recall_manifest_path = recall_root / "recall-revision.json"
    recall_objects_path = recall_root / "gold-objects.jsonl"
    gold_manifest = _read_json(gold_manifest_path, "gold_revision_manifest")
    gold_fragments = _read_jsonl(gold_fragments_path, "gold_fragments")
    recall_manifest = _read_json(recall_manifest_path, "recall_revision_manifest")
    recall_objects = _read_jsonl(recall_objects_path, "recall_gold_objects")
    if gold_manifest.get("status") != "accepted-for-import":
        raise ControlledScanError("gold_revision_not_accepted")
    if recall_manifest.get("status") != "accepted-incremental-bounded-recall":
        raise ControlledScanError("recall_revision_not_accepted")
    if recall_manifest.get("source_sha256") != source_sha256:
        raise ControlledScanError("recall_source_mismatch")
    if recall_manifest.get("scope", {}).get("physical_pages") != pages:
        raise ControlledScanError("recall_scope_mismatch")
    bindings = gold_manifest.get("workpack_identity", {}).get("item_bindings")
    if not isinstance(bindings, list) or not bindings:
        raise ControlledScanError("gold_item_bindings_missing")
    if any(row.get("source_sha256") != source_sha256 for row in bindings):
        raise ControlledScanError("gold_source_mismatch")
    if any(int(row.get("physical_page", 0)) not in pages for row in bindings):
        raise ControlledScanError("gold_scope_mismatch")
    if gold_manifest.get("file_hashes", {}).get("gold-fragments.jsonl") != _sha256_file(gold_fragments_path):
        raise ControlledScanError("gold_fragment_hash_mismatch")
    if recall_manifest.get("file_hashes", {}).get("gold-objects.jsonl") != _sha256_file(recall_objects_path):
        raise ControlledScanError("recall_object_hash_mismatch")
    present = []
    for row in recall_objects:
        origin_page = int(row.get("physical_page", 0))
        if origin_page not in pages:
            raise ControlledScanError("recall_object_scope_mismatch")
        bbox = row.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ControlledScanError("recall_object_bbox_invalid")
        present.append(
            {
                "gold_object_id": row.get("gold_object_id"),
                "source_gold_item_id": row.get("source_gold_item_id"),
                "kind": row.get("kind"),
                "origin_physical_page": origin_page,
                "derived_physical_page": pages.index(origin_page) + 1,
                "bbox": bbox,
                "coordinate_space": COORDINATE_SPACE,
                "bbox_transform": "identity-after-full-mediabox-render",
            }
        )
    return {
        "portable_gold_revision": {
            "manifest_sha256": _sha256_file(gold_manifest_path),
            "gold_fragments_sha256": _sha256_file(gold_fragments_path),
            "gold_row_count": len(gold_fragments),
        },
        "bounded_recall_revision": {
            "manifest_sha256": _sha256_file(recall_manifest_path),
            "gold_objects_sha256": _sha256_file(recall_objects_path),
            "object_count": len(recall_objects),
        },
        "render_equivalent_object_mappings": present,
        "reuse_policy": {
            "source_sha_change_prevents_direct_import": True,
            "gold_rows_rewritten": False,
            "accepted_status_inherited": False,
            "eligible_for_private_render_equivalent_rebind_plan": True,
            "requires_downstream_rebind_validation": True,
        },
    }


def _build_manifest(
    *,
    created_at: str,
    source: Path,
    source_sha256: str,
    source_page_count: int,
    source_text_counts: list[int],
    derived_pdf: Path,
    inspection: Mapping[str, Any],
    renderer_version: str,
    renderer_sha256: str,
    dpi: int,
    page_rows: list[dict[str, Any]],
    evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": CONTROLLED_SCAN_DERIVATION_SCHEMA,
        "protocol": CONTROLLED_SCAN_DERIVATION_PROTOCOL,
        "compiler_version": CONTROLLED_SCAN_DERIVATION_COMPILER_VERSION,
        "created_at": created_at,
        "status": "controlled-image-only-scan-ready",
        "purpose": "private-phase7d6e-qualification-source",
        "tool": {
            "id": "controlled_scan_derivation.py",
            "sha256": _sha256_file(Path(__file__).resolve()),
            "runtime": {
                "python": platform.python_version(),
                "pypdf": pypdf.__version__,
                "reportlab": reportlab.Version,
                "pillow": PIL.__version__,
            },
        },
        "source": {
            "filename": source.name,
            "sha256": source_sha256,
            "page_count": source_page_count,
            "selected_physical_pages": [row["origin_physical_page"] for row in page_rows],
            "selected_text_char_counts": source_text_counts,
            "selected_scope_had_text_layer": any(source_text_counts),
        },
        "derived": {
            "path": PDF_NAME,
            "sha256": _sha256_file(derived_pdf),
            "byte_count": derived_pdf.stat().st_size,
            **inspection,
        },
        "renderer": {
            "id": "pdftoppm",
            "version": renderer_version,
            "executable_sha256": renderer_sha256,
            "dpi": dpi,
            "format": "png",
            "full_mediabox": True,
        },
        "page_map": page_rows,
        "reviewed_visual_evidence": evidence,
        "policy": {
            "private": True,
            "release_included": False,
            "source_pdf_in_release": False,
            "ocr_invocations": 0,
            "model_invocations": 0,
            "mcp_calls": 0,
            "network_access": False,
            "gold_authored": False,
            "promotion_allowed": False,
            "mvp_acceptance": False,
        },
    }
    manifest["manifest_sha256"] = _sha256_json(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    return manifest


def build(
    *,
    source: Path,
    output: Path,
    pdftoppm: Path,
    start_page: int,
    end_page: int,
    render_dpi: int,
    created_at: str,
    gold_revision: Path | None,
    recall_revision: Path | None,
) -> dict[str, Any]:
    source = _secure_file(source, "source_pdf")
    pdftoppm = _secure_file(pdftoppm, "pdftoppm")
    destination = _secure_directory(output, empty=True)
    created_at = _validate_created_at(created_at)
    if start_page < 1 or end_page < start_page:
        raise ControlledScanError("page_range_invalid")
    if render_dpi < 72 or render_dpi > 600:
        raise ControlledScanError("render_dpi_invalid")
    try:
        reader = PdfReader(str(source))
    except Exception as error:
        raise ControlledScanError("source_pdf_invalid") from error
    if reader.is_encrypted:
        raise ControlledScanError("source_pdf_encrypted")
    if end_page > len(reader.pages):
        raise ControlledScanError("page_range_invalid")
    pages = list(range(start_page, end_page + 1))
    source_sha256 = _sha256_file(source)
    render_root = destination / "page-renders"
    render_root.mkdir(mode=0o700)
    page_rows: list[dict[str, Any]] = []
    source_text_counts: list[int] = []
    for derived_page, origin_page in enumerate(pages, 1):
        page = reader.pages[origin_page - 1]
        geometry = _page_geometry(page)
        source_text_counts.append(len((page.extract_text() or "").strip()))
        relative = f"page-{derived_page:04d}-origin-{origin_page:04d}.png"
        rendered = _render_page(
            source, origin_page, render_root / relative, pdftoppm, render_dpi
        )
        fingerprint = _pixel_fingerprint(rendered)
        _validate_page_dimensions(fingerprint, geometry, render_dpi)
        page_rows.append(
            {
                "origin_physical_page": origin_page,
                "derived_physical_page": derived_page,
                "render_path": f"page-renders/{relative}",
                "origin_render": fingerprint,
                "geometry": geometry,
            }
        )
    derived_pdf = destination / PDF_NAME
    _create_image_only_pdf(derived_pdf, page_rows, destination)
    inspection = _inspect_image_only_pdf(derived_pdf, len(pages))
    embedded = _embedded_image_fingerprints(derived_pdf, page_rows)
    for row, fingerprint in zip(page_rows, embedded):
        row["embedded_image"] = fingerprint
    with tempfile.TemporaryDirectory(prefix="fake-expert-controlled-scan-") as temp_name:
        temp = Path(temp_name)
        for row in page_rows:
            rendered = _render_page(
                derived_pdf,
                int(row["derived_physical_page"]),
                temp / f"derived-{int(row['derived_physical_page']):04d}.png",
                pdftoppm,
                render_dpi,
            )
            derived_fingerprint = _pixel_fingerprint(rendered)
            row["derived_render"] = derived_fingerprint
            row["render_fidelity"] = _pixel_comparison(
                destination / str(row["render_path"]), rendered
            )
    evidence = _bind_reviewed_visual_evidence(
        source_sha256, pages, gold_revision, recall_revision
    )
    manifest = _build_manifest(
        created_at=created_at,
        source=source,
        source_sha256=source_sha256,
        source_page_count=len(reader.pages),
        source_text_counts=source_text_counts,
        derived_pdf=derived_pdf,
        inspection=inspection,
        renderer_version=_renderer_version(pdftoppm),
        renderer_sha256=_sha256_file(pdftoppm),
        dpi=render_dpi,
        page_rows=page_rows,
        evidence=evidence,
    )
    _write_json(destination / MANIFEST_NAME, manifest)
    return {
        "passed": True,
        "status": manifest["status"],
        "output": str(destination),
        "derived_pdf": str(derived_pdf),
        "derived_pdf_sha256": manifest["derived"]["sha256"],
        "manifest_sha256": manifest["manifest_sha256"],
        "page_count": len(pages),
        "text_layer_absent": True,
        "reviewed_visual_evidence_bound": evidence is not None,
        "next_gate": "fresh-authorized-offline-ppocrv6-run",
    }


def verify(
    *,
    source: Path,
    derivation: Path,
    pdftoppm: Path,
    gold_revision: Path | None,
    recall_revision: Path | None,
) -> dict[str, Any]:
    source = _secure_file(source, "source_pdf")
    pdftoppm = _secure_file(pdftoppm, "pdftoppm")
    root = _secure_input_directory(derivation, "derivation")
    manifest = _read_json(root / MANIFEST_NAME, "derivation_manifest")
    if manifest.get("schema_version") != CONTROLLED_SCAN_DERIVATION_SCHEMA:
        raise ControlledScanError("manifest_schema_invalid")
    if manifest.get("protocol") != CONTROLLED_SCAN_DERIVATION_PROTOCOL:
        raise ControlledScanError("manifest_protocol_invalid")
    if manifest.get("compiler_version") != CONTROLLED_SCAN_DERIVATION_COMPILER_VERSION:
        raise ControlledScanError("manifest_compiler_version_invalid")
    expected_manifest_sha = _sha256_json(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    if manifest.get("manifest_sha256") != expected_manifest_sha:
        raise ControlledScanError("manifest_sha256_mismatch")
    if manifest.get("source", {}).get("sha256") != _sha256_file(source):
        raise ControlledScanError("source_sha256_mismatch")
    expected_tool = {
        "id": "controlled_scan_derivation.py",
        "sha256": _sha256_file(Path(__file__).resolve()),
        "runtime": {
            "python": platform.python_version(),
            "pypdf": pypdf.__version__,
            "reportlab": reportlab.Version,
            "pillow": PIL.__version__,
        },
    }
    if manifest.get("tool") != expected_tool:
        raise ControlledScanError("derivation_tool_or_runtime_drift")
    try:
        source_reader = PdfReader(str(source))
    except Exception as error:
        raise ControlledScanError("source_pdf_invalid") from error
    if source_reader.is_encrypted:
        raise ControlledScanError("source_pdf_encrypted")
    if manifest.get("source", {}).get("page_count") != len(source_reader.pages):
        raise ControlledScanError("source_page_count_mismatch")
    derived_relative = manifest.get("derived", {}).get("path")
    if derived_relative != PDF_NAME:
        raise ControlledScanError("derived_path_invalid")
    derived_pdf = _secure_file(root / PDF_NAME, "derived_pdf")
    if manifest.get("derived", {}).get("sha256") != _sha256_file(derived_pdf):
        raise ControlledScanError("derived_pdf_sha256_mismatch")
    page_rows = manifest.get("page_map")
    if not isinstance(page_rows, list) or not page_rows:
        raise ControlledScanError("page_map_invalid")
    expected_pages = list(range(1, len(page_rows) + 1))
    if [row.get("derived_physical_page") for row in page_rows] != expected_pages:
        raise ControlledScanError("derived_page_order_invalid")
    origin_pages = [int(row.get("origin_physical_page", 0)) for row in page_rows]
    if origin_pages != list(range(min(origin_pages), max(origin_pages) + 1)):
        raise ControlledScanError("origin_page_scope_not_contiguous")
    if manifest.get("source", {}).get("selected_physical_pages") != origin_pages:
        raise ControlledScanError("source_selected_pages_mismatch")
    if origin_pages[0] < 1 or origin_pages[-1] > len(source_reader.pages):
        raise ControlledScanError("origin_page_scope_invalid")
    source_text_counts = [
        len((source_reader.pages[page - 1].extract_text() or "").strip())
        for page in origin_pages
    ]
    if manifest.get("source", {}).get("selected_text_char_counts") != source_text_counts:
        raise ControlledScanError("source_text_count_mismatch")
    if manifest.get("source", {}).get("selected_scope_had_text_layer") is not any(source_text_counts):
        raise ControlledScanError("source_text_layer_status_mismatch")
    for row, origin_page in zip(page_rows, origin_pages):
        if row.get("geometry") != _page_geometry(source_reader.pages[origin_page - 1]):
            raise ControlledScanError(f"page_geometry_binding_mismatch:{origin_page}")
    inspection = _inspect_image_only_pdf(derived_pdf, len(page_rows))
    if inspection != {
        key: manifest["derived"].get(key)
        for key in (
            "page_count",
            "text_layer_absent",
            "extracted_text_char_counts",
            "font_resource_counts",
            "text_show_operator_counts",
        )
    }:
        raise ControlledScanError("derived_text_inspection_mismatch")
    if manifest.get("renderer", {}).get("version") != _renderer_version(pdftoppm):
        raise ControlledScanError("renderer_version_drift")
    if manifest.get("renderer", {}).get("executable_sha256") != _sha256_file(pdftoppm):
        raise ControlledScanError("renderer_executable_drift")
    embedded = _embedded_image_fingerprints(derived_pdf, page_rows)
    if [row.get("embedded_image") for row in page_rows] != embedded:
        raise ControlledScanError("embedded_image_binding_mismatch")
    dpi = int(manifest.get("renderer", {}).get("dpi", 0))
    with tempfile.TemporaryDirectory(prefix="fake-expert-controlled-scan-verify-") as temp_name:
        temp = Path(temp_name)
        for row in page_rows:
            origin_page = int(row.get("origin_physical_page", 0))
            derived_page = int(row.get("derived_physical_page", 0))
            stored_render = _safe_child_file(root, row.get("render_path"), "origin_render")
            stored_fingerprint = _pixel_fingerprint(stored_render)
            if stored_fingerprint != row.get("origin_render"):
                raise ControlledScanError(f"stored_origin_render_mismatch:{origin_page}")
            source_render = _render_page(
                source,
                origin_page,
                temp / f"source-{origin_page:04d}.png",
                pdftoppm,
                dpi,
            )
            derived_render = _render_page(
                derived_pdf,
                derived_page,
                temp / f"derived-{derived_page:04d}.png",
                pdftoppm,
                dpi,
            )
            source_fingerprint = _pixel_fingerprint(source_render)
            derived_fingerprint = _pixel_fingerprint(derived_render)
            if source_fingerprint["pixel_sha256"] != stored_fingerprint["pixel_sha256"]:
                raise ControlledScanError(f"source_render_replay_mismatch:{origin_page}")
            if derived_fingerprint != row.get("derived_render"):
                raise ControlledScanError(f"derived_render_replay_mismatch:{derived_page}")
            comparison = _pixel_comparison(source_render, derived_render)
            if comparison != row.get("render_fidelity"):
                raise ControlledScanError(f"render_fidelity_replay_mismatch:{derived_page}")
    pages = [int(row["origin_physical_page"]) for row in page_rows]
    evidence = _bind_reviewed_visual_evidence(
        manifest["source"]["sha256"], pages, gold_revision, recall_revision
    )
    if evidence != manifest.get("reviewed_visual_evidence"):
        raise ControlledScanError("reviewed_visual_evidence_binding_mismatch")
    policy = manifest.get("policy", {})
    if policy.get("ocr_invocations") != 0 or policy.get("model_invocations") != 0:
        raise ControlledScanError("model_policy_invalid")
    if policy.get("network_access") is not False or policy.get("gold_authored") is not False:
        raise ControlledScanError("privacy_or_gold_policy_invalid")
    return {
        "passed": True,
        "status": manifest["status"],
        "page_count": len(page_rows),
        "text_layer_absent": True,
        "render_fidelity_pages": len(page_rows),
        "pixel_exact_pages": sum(
            1 for row in page_rows if row.get("render_fidelity", {}).get("pixel_exact") is True
        ),
        "reviewed_visual_evidence_bound": evidence is not None,
        "manifest_sha256": manifest["manifest_sha256"],
        "next_gate": "fresh-authorized-offline-ppocrv6-run",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build")
    build_parser.add_argument("--source", type=Path, required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    build_parser.add_argument("--pdftoppm", type=Path, required=True)
    build_parser.add_argument("--start-page", type=int, required=True)
    build_parser.add_argument("--end-page", type=int, required=True)
    build_parser.add_argument("--render-dpi", type=int, default=120)
    build_parser.add_argument("--created-at", required=True)
    build_parser.add_argument("--gold-revision", type=Path)
    build_parser.add_argument("--recall-revision", type=Path)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--source", type=Path, required=True)
    verify_parser.add_argument("--derivation", type=Path, required=True)
    verify_parser.add_argument("--pdftoppm", type=Path, required=True)
    verify_parser.add_argument("--gold-revision", type=Path)
    verify_parser.add_argument("--recall-revision", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "build":
            result = build(
                source=args.source,
                output=args.output,
                pdftoppm=args.pdftoppm,
                start_page=args.start_page,
                end_page=args.end_page,
                render_dpi=args.render_dpi,
                created_at=args.created_at,
                gold_revision=args.gold_revision,
                recall_revision=args.recall_revision,
            )
        else:
            result = verify(
                source=args.source,
                derivation=args.derivation,
                pdftoppm=args.pdftoppm,
                gold_revision=args.gold_revision,
                recall_revision=args.recall_revision,
            )
    except ControlledScanError as error:
        print(json.dumps({"passed": False, "error": str(error)}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
