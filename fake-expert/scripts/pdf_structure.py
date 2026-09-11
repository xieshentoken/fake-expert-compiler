#!/usr/bin/env python3
"""Deterministic born-digital PDF preflight and structure reconstruction helpers."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pdf_parser_adapters import (
    ParserAdapterError,
    SUPPORTED_LAYOUT_PARSERS,
    build_parser_receipt,
    build_render_receipts,
    sha256_json,
)
from compiler_version import PDF_IR_SCHEMA_VERSION
from heading_locator import (
    HEADING_LOCATOR_OVERRIDE_SCHEMA,
    HeadingLocatorError,
    build_heading_locator_records,
    load_locator_overrides,
)


SCHEMA_VERSION = PDF_IR_SCHEMA_VERSION
HEADING_OVERRIDE_SCHEMA = "tkc.heading-override/v0.1"
MATH_FONT_PATTERN = re.compile(
    r"(?:CMMI|CMSY|CMEX|MTSY|MTEX|RMTMI|ITALSYMB|SYMBOL|MSAM|MSBM)", re.IGNORECASE
)
EQUATION_LABEL_PATTERN = re.compile(r"\(\s*\d+(?:\.\d+)+\s*\)")
FIGURE_PATTERN = re.compile(r"\b(?:Figure|Fig\.)\s*\d+(?:\.\d+)+", re.IGNORECASE)
TABLE_PATTERN = re.compile(r"\bTable\s*\d+(?:\.\d+)+", re.IGNORECASE)
GLYPH_NAME_PATTERN = re.compile(
    r"/(?:Lambda|Delta|Gamma|Omega|Theta|Phi|Psi|Sigma|alpha|beta|gamma|delta|lambda|omega)"
)
HEADING_PATTERN = re.compile(
    r"^(?P<number>\d+(?:\.\d+)*)\s+(?P<title>[A-Za-z][^\n]{2,})$"
)
TRAILING_PAGE_NUMBER = re.compile(r"\s+(\d{1,4})$")


class PdfPipelineError(RuntimeError):
    """Stable user-facing pipeline failure."""


@dataclass(frozen=True)
class OutlineNode:
    title: str
    depth: int
    start_page: int
    parent_index: int | None


def normalize_text(text: str) -> str:
    """Use the same canonicalization as external anchor verification."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, *parts: object, length: int = 20) -> str:
    canonical = "\x1f".join(normalize_text(str(part)).casefold() for part in parts)
    return f"{prefix}-{sha256_bytes(canonical.encode('utf-8'))[:length]}"


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return str(value)


def dereference(value: Any) -> Any:
    try:
        return value.get_object()
    except AttributeError:
        return value


def page_fonts(page: Any) -> list[dict[str, Any]]:
    resources = dereference(page.get("/Resources") or {})
    fonts = dereference(resources.get("/Font") or {}) if hasattr(resources, "get") else {}
    output: list[dict[str, Any]] = []
    for key, reference in sorted(fonts.items(), key=lambda row: str(row[0])):
        font = dereference(reference)
        base_font = str(font.get("/BaseFont", "")) if hasattr(font, "get") else ""
        output.append(
            {
                "resource": str(key),
                "base_font": base_font.lstrip("/"),
                "subtype": str(font.get("/Subtype", "")).lstrip("/") if hasattr(font, "get") else "",
                "has_to_unicode": bool(font.get("/ToUnicode")) if hasattr(font, "get") else False,
                "math_font": bool(MATH_FONT_PATTERN.search(base_font)),
            }
        )
    return output


def page_image_count(page: Any) -> int:
    resources = dereference(page.get("/Resources") or {})
    xobjects = dereference(resources.get("/XObject") or {}) if hasattr(resources, "get") else {}
    count = 0
    for reference in xobjects.values():
        obj = dereference(reference)
        if hasattr(obj, "get") and str(obj.get("/Subtype", "")) == "/Image":
            count += 1
    return count


def page_dimensions(page: Any) -> dict[str, float]:
    """Record the physical PDF page size without claiming text coordinates."""
    try:
        media_box = page.mediabox
        width = round(float(media_box.width), 4)
        height = round(float(media_box.height), 4)
    except Exception as error:
        raise PdfPipelineError("pdf_page_dimensions_unavailable") from error
    if width <= 0 or height <= 0:
        raise PdfPipelineError("pdf_page_dimensions_invalid")
    return {"width": width, "height": height}


def _edge_lines(text: str, count: int = 5) -> tuple[list[str], list[str]]:
    lines = [normalize_text(line) for line in text.splitlines() if normalize_text(line)]
    return lines[:count], lines[-count:]


def detect_printed_page_label(text: str) -> str | None:
    """Infer a visible Arabic page number from common header/footer positions."""
    first, last = _edge_lines(text)

    for line in reversed(last):
        if re.fullmatch(r"\d{1,3}", line):
            return line

    for line in first:
        match = re.match(r"^(\d{1,3})\s+\D", line)
        if match:
            return match.group(1)

    for line in first:
        match = re.search(r"\D\s+(\d{1,3})$", line)
        if match:
            return match.group(1)

    for line in last:
        match = re.search(r"\D\s+(\d{1,3})$", line)
        if match:
            return match.group(1)
    return None


def _private_use_present(text: str) -> bool:
    return any("Co" == unicodedata.category(character) for character in text)


def inspect_page(
    page: Any,
    physical_page: int,
    pdf_page_label: str,
    minimum_native_characters: int,
) -> tuple[dict[str, Any], str, str]:
    raw_text = page.extract_text() or ""
    normalized = normalize_text(raw_text)
    non_whitespace = len(re.sub(r"\s", "", raw_text))
    alphanumeric = sum(character.isalnum() for character in normalized)
    if not normalized:
        native_status = "missing"
    elif alphanumeric < minimum_native_characters:
        native_status = "low"
    else:
        native_status = "usable"

    fonts = page_fonts(page)
    math_fonts = sorted({font["base_font"] for font in fonts if font["math_font"]})
    fonts_without_unicode = sorted(
        {font["base_font"] for font in fonts if not font["has_to_unicode"]}
    )
    image_count = page_image_count(page)
    equation_labels = sorted(set(EQUATION_LABEL_PATTERN.findall(normalized)))

    ocr_reasons: list[str] = []
    if native_status == "missing":
        ocr_reasons.append("native_text_missing")
    elif native_status == "low":
        ocr_reasons.append("native_text_below_threshold")

    visual_reasons: list[str] = []
    if native_status != "usable":
        visual_reasons.append("native_text_not_usable")
    if math_fonts:
        visual_reasons.append("math_font_present")
    if equation_labels:
        visual_reasons.append("equation_label_present")
    if GLYPH_NAME_PATTERN.search(raw_text) or "\ufffd" in raw_text or _private_use_present(raw_text):
        visual_reasons.append("suspicious_glyph_decoding")
    if FIGURE_PATTERN.search(normalized):
        visual_reasons.append("figure_reference_present")
    if TABLE_PATTERN.search(normalized):
        visual_reasons.append("table_reference_present")
    if image_count:
        visual_reasons.append("raster_image_present")

    printed_label = detect_printed_page_label(raw_text)
    if native_status == "usable":
        page_role = "text-with-raster" if image_count else "text"
        page_disposition = "compile"
    elif image_count:
        page_role = "image-only-or-scanned"
        page_disposition = "ocr-required"
    else:
        page_role = "blank-or-unusable"
        page_disposition = "manual-review-required"
    page_record = {
        "physical_page": physical_page,
        "pdf_page_label": str(pdf_page_label),
        "printed_page_label_candidate": printed_label,
        "pdf_label_matches_printed_candidate": (
            None if printed_label is None else str(pdf_page_label) == printed_label
        ),
        "native_text": {
            "status": native_status,
            "raw_characters": len(raw_text),
            "normalized_characters": len(normalized),
            "non_whitespace_characters": non_whitespace,
            "alphanumeric_characters": alphanumeric,
            "sha256": sha256_bytes(normalized.encode("utf-8")),
        },
        "font_summary": {
            "font_count": len(fonts),
            "math_fonts": math_fonts,
            "fonts_without_to_unicode": fonts_without_unicode,
        },
        "raster_image_count": image_count,
        "page_role": page_role,
        "page_disposition": page_disposition,
        "equation_labels": equation_labels,
        "ocr_reasons": ocr_reasons,
        "visual_verification_reasons": visual_reasons,
    }
    return page_record, normalized, raw_text


def flatten_outline(reader: Any) -> list[OutlineNode]:
    nodes: list[OutlineNode] = []

    def visit(items: Iterable[Any], depth: int, parent_index: int | None) -> None:
        previous_index: int | None = None
        for item in items:
            if isinstance(item, list):
                visit(item, depth + 1, previous_index if previous_index is not None else parent_index)
                continue
            try:
                page_number = reader.get_destination_page_number(item)
                title = normalize_text(str(item.title))
            except Exception:
                previous_index = None
                continue
            if page_number is None or page_number < 0 or not title:
                previous_index = None
                continue
            nodes.append(
                OutlineNode(
                    title=title,
                    depth=depth,
                    start_page=page_number + 1,
                    parent_index=parent_index,
                )
            )
            previous_index = len(nodes) - 1

    try:
        visit(reader.outline, 0, None)
    except Exception:
        return []
    return nodes


def heading_fallback(page_texts: dict[int, str]) -> list[OutlineNode]:
    nodes: list[OutlineNode] = []
    seen: set[str] = set()
    last_at_depth: dict[int, int] = {}
    for physical_page, text in page_texts.items():
        for line in text.splitlines()[:24]:
            line = normalize_text(line)
            match = HEADING_PATTERN.match(line)
            if not match:
                continue
            number = match.group("number")
            title = re.sub(r"\s+\d{1,3}$", "", match.group("title")).strip()
            normalized_title = f"{number} {title}"
            key = normalized_title.casefold()
            if key in seen:
                continue
            seen.add(key)
            depth = number.count(".")
            parent_index = last_at_depth.get(depth - 1) if depth else None
            nodes.append(
                OutlineNode(
                    title=normalized_title,
                    depth=depth,
                    start_page=physical_page,
                    parent_index=parent_index,
                )
            )
            for stale_depth in [value for value in last_at_depth if value >= depth]:
                del last_at_depth[stale_depth]
            last_at_depth[depth] = len(nodes) - 1
    return nodes


def _node_end_pages(nodes: list[OutlineNode], page_count: int) -> list[int]:
    end_pages: list[int] = []
    for index, node in enumerate(nodes):
        next_boundary = page_count + 1
        for candidate in nodes[index + 1 :]:
            if candidate.depth <= node.depth:
                next_boundary = candidate.start_page
                break
        # Full-page evidence cannot split a page at a heading coordinate. Include
        # the next same-or-higher-level heading page so continuation text above
        # that heading is not silently dropped. Adjacent envelopes may overlap.
        end_pages.append(max(node.start_page, min(page_count, next_boundary)))
    return end_pages


def resolve_outline_headings(
    nodes: list[OutlineNode],
    raw_page_texts: dict[int, str],
    page_count: int,
) -> tuple[dict[int, int], list[dict[str, Any]]]:
    """Resolve outline destinations to likely body-heading pages.

    PDF outlines often land on a chapter contents list or one page before the
    visible heading.  This resolver records bounded candidates and a reasoned
    choice in the IR; it never edits evidence text and falls back to the outline
    destination when the choice is ambiguous.
    """

    resolved: dict[int, int] = {}
    receipts: list[dict[str, Any]] = []
    titles = [node.title for node in nodes]
    for index, node in enumerate(nodes):
        candidate_rows: list[dict[str, Any]] = []
        search_start = max(1, node.start_page - 1)
        search_end = min(page_count, node.start_page + 3)
        for page in range(search_start, search_end + 1):
            raw = raw_page_texts.get(page)
            if raw is None:
                continue
            lines = [line.strip() for line in raw.splitlines() if line.strip()]
            sibling_hits = sum(
                1
                for line in lines
                for title in titles
                if normalize_text(line) == title
                or normalize_text(line).startswith(f"{title} ")
            )
            for line_index, line in enumerate(lines):
                normalized = normalize_text(line)
                if not normalized:
                    continue
                exact = normalized == node.title
                prefixed = normalized.startswith(f"{node.title} ")
                trailing_page = bool(TRAILING_PAGE_NUMBER.search(normalized[len(node.title) :])) if prefixed else False
                if not exact and not prefixed and node.title not in normalized:
                    continue
                if trailing_page:
                    role = "running-head" if line_index <= 2 else "chapter-toc-entry"
                    score = 18 if role == "running-head" else 8
                elif exact:
                    role = "body-heading"
                    score = 100
                    if page == node.start_page and line_index <= 1:
                        role = "top-heading"
                        score = 82
                    if page == node.start_page and sibling_hits >= 3 and line_index <= 8:
                        role = "chapter-toc-entry"
                        score = 12
                else:
                    role = "inline-heading-candidate"
                    score = 20
                score -= abs(page - node.start_page) * 4
                if page > node.start_page:
                    score += 10
                candidate_rows.append(
                    {
                        "physical_page": page,
                        "line_index": line_index,
                        "role": role,
                        "score": score,
                        "line_sha256": sha256_bytes(normalized.encode("utf-8")),
                    }
                )
        candidate_rows.sort(
            key=lambda row: (-int(row["score"]), int(row["physical_page"]), int(row["line_index"]))
        )
        selected_page = node.start_page
        status = "outline-fallback"
        if candidate_rows:
            top_score = candidate_rows[0]["score"]
            top = [row for row in candidate_rows if row["score"] == top_score]
            if len(top) == 1:
                selected_page = int(top[0]["physical_page"])
                status = "resolved"
            else:
                status = "ambiguous"
        resolved[index] = selected_page
        receipts.append(
            {
                "node_index": index,
                "title": node.title,
                "outline_page": node.start_page,
                "selected_page": selected_page,
                "status": status,
                "candidates": candidate_rows[:12],
            }
        )
    return resolved, receipts


def _heading_position_in_page(title: str, page_text: str) -> int:
    """Return the normalized title's offset in the normalized page text.

    Uses the same canonicalization as the workpack heading locator so the
    physical ordering computed here matches the validator that later rejects
    `heading_order_invalid`; a missing title falls back to offset zero and the
    workpack build still fails closed with `heading_locator_missing`.
    """
    position = page_text.find(normalize_text(title))
    return position if position >= 0 else 0


def reconstruct_segments(
    reader: Any,
    source_id: str,
    start_page: int,
    end_page: int,
    page_records: dict[int, dict[str, Any]],
    page_texts: dict[int, str],
    raw_page_texts: dict[int, str],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Rebuild the outline as segment records.

    Segments are returned in outline order; the physical ordering sort is
    applied by :func:`sort_segments_physically` inside the compiler after
    heading overrides have been applied (offsets must use the final titles).
    A PDF outline whose bookmark order differs from physical page order
    therefore still yields a document map that passes the workpack
    `heading_order_invalid` check, and the ordering is baked into the hash lock
    instead of requiring a host to reorder the map by hand.
    """
    all_nodes = flatten_outline(reader)
    source = "pdf-outline"
    selected_indices = [
        index for index, node in enumerate(all_nodes) if start_page <= node.start_page <= end_page
    ]
    if not selected_indices:
        all_nodes = heading_fallback(raw_page_texts)
        source = "native-text-heading-heuristic"
        selected_indices = list(range(len(all_nodes)))

    resolved_start_pages, heading_resolutions = resolve_outline_headings(
        all_nodes, raw_page_texts, len(reader.pages)
    ) if source == "pdf-outline" else ({index: node.start_page for index, node in enumerate(all_nodes)}, [])
    end_pages = []
    for index, node in enumerate(all_nodes):
        next_boundary = len(reader.pages) + 1
        for candidate_index in range(index + 1, len(all_nodes)):
            candidate = all_nodes[candidate_index]
            if candidate.depth <= node.depth:
                next_boundary = resolved_start_pages.get(candidate_index, candidate.start_page)
                break
        end_pages.append(max(resolved_start_pages.get(index, node.start_page), min(len(reader.pages), next_boundary)))
    child_counts: dict[int, int] = {}
    for node in all_nodes:
        if node.parent_index is not None:
            child_counts[node.parent_index] = child_counts.get(node.parent_index, 0) + 1

    segment_ids = {
        index: stable_id(
            "seg",
            source_id,
            node.depth,
            node.title,
            node.start_page,
            end_pages[index],
        )
        for index, node in enumerate(all_nodes)
    }

    segments: list[dict[str, Any]] = []
    for index in selected_indices:
        node = all_nodes[index]
        selected_start = resolved_start_pages.get(index, node.start_page)
        clipped_start = min(max(start_page, selected_start), end_page)
        clipped_end = max(clipped_start, min(end_page, end_pages[index]))
        pages = list(range(clipped_start, clipped_end + 1))
        segments.append(
            {
                "id": segment_ids[index],
                "title": node.title,
                "structure_order": index,
                "depth": node.depth,
                "parent_id": segment_ids.get(node.parent_index),
                "has_children": bool(child_counts.get(index)),
                "page_envelope_policy": "include-next-heading-page",
                "outline_page": node.start_page,
                "heading_resolution": (
                    next(
                        resolution
                        for resolution in heading_resolutions
                        if resolution["node_index"] == index
                    )
                    if heading_resolutions
                    else None
                ),
                "source_page_range": [selected_start, end_pages[index]],
                "scope_page_range": [clipped_start, clipped_end],
                "physical_pages": pages,
                "pdf_page_labels": [page_records[page]["pdf_page_label"] for page in pages],
                "printed_page_label_candidates": [
                    page_records[page]["printed_page_label_candidate"] for page in pages
                ],
            }
        )

    ancestor_indices: set[int] = set()
    for index in selected_indices:
        parent = all_nodes[index].parent_index
        while parent is not None:
            if parent not in selected_indices:
                ancestor_indices.add(parent)
            parent = all_nodes[parent].parent_index
    ancestors = [
        {
            "id": segment_ids[index],
            "title": all_nodes[index].title,
            "depth": all_nodes[index].depth,
            "start_page": all_nodes[index].start_page,
        }
        for index in sorted(ancestor_indices)
    ]
    return source, segments, ancestors, heading_resolutions


def sort_segments_physically(
    segments: list[dict[str, Any]], page_texts: dict[int, str]
) -> None:
    """Sort segments by (first physical page, final-title offset) in place.

    Must run after heading overrides are applied: the workpack heading locator
    computes offsets from the final (renamed) titles, so sorting on original
    titles first would leave the map out of order whenever an override changes
    the title's position in the page text.
    """
    segments.sort(
        key=lambda segment: (
            segment["physical_pages"][0],
            _heading_position_in_page(
                segment["title"], page_texts.get(segment["physical_pages"][0], "")
            ),
        )
    )


def build_anchor_candidates(
    source_id: str,
    segments: list[dict[str, Any]],
    page_records: dict[int, dict[str, Any]],
    page_texts: dict[int, str],
    extractor: str,
    strategy: str,
) -> list[dict[str, Any]]:
    if strategy == "leaf":
        selected = [segment for segment in segments if not segment["has_children"]]
    else:
        selected = list(segments)

    candidates: list[dict[str, Any]] = []
    for segment in selected:
        pages = segment["physical_pages"]
        joined_text = "\f".join(page_texts[page] for page in pages)
        visual_pages = [
            page
            for page in pages
            if page_records[page]["visual_verification_reasons"]
        ]
        first_page, last_page = pages[0], pages[-1]
        page_word = "page" if first_page == last_page else "pages"
        page_range = str(first_page) if first_page == last_page else f"{first_page}-{last_page}"
        candidate_id = stable_id("evc", source_id, segment["id"], joined_text)
        candidates.append(
            {
                "schema_version": SCHEMA_VERSION,
                "id": candidate_id,
                "candidate_status": "needs-semantic-review",
                "source_id": source_id,
                "pages": pages,
                "printed_page_labels": segment["printed_page_label_candidates"],
                "segment_id": segment["id"],
                "locator": {
                    "title": segment["title"],
                    "outline_depth": segment["depth"],
                    "hash_scope": (
                        f"normalized full native text of physical PDF {page_word} {page_range}"
                    ),
                    "extractor": extractor,
                },
                "excerpt_sha256": sha256_bytes(joined_text.encode("utf-8")),
                "visual_verification_required": bool(visual_pages),
                "visual_verification_pages": visual_pages,
                "supports": [],
            }
        )
    return candidates


def load_heading_overrides(path: Path | None) -> list[dict[str, Any]]:
    """Load and validate heading-override rows from a JSONL file (empty when absent)."""
    if path is None:
        return []
    path = path.expanduser().resolve()
    if not path.is_file():
        raise PdfPipelineError(f"heading_overrides_missing: {path}")
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise PdfPipelineError(
                f"heading_override_invalid: line={index + 1}"
            ) from error
        before_title = row.get("before_title") if isinstance(row, dict) else None
        new_title = row.get("new_title") if isinstance(row, dict) else None
        physical_page = row.get("physical_page") if isinstance(row, dict) else None
        segment_id = row.get("segment_id") if isinstance(row, dict) else None
        rationale = row.get("rationale") if isinstance(row, dict) else None
        reviewer_instance = row.get("reviewer_instance") if isinstance(row, dict) else None
        before_sha256 = row.get("before_sha256") if isinstance(row, dict) else None
        if (
            not isinstance(row, dict)
            or row.get("schema_version") != HEADING_OVERRIDE_SCHEMA
            or not isinstance(before_title, str)
            or not before_title
            or not isinstance(new_title, str)
            or not new_title
            or not isinstance(physical_page, int)
            or physical_page < 1
            or (segment_id is not None and (not isinstance(segment_id, str) or not segment_id))
            or not isinstance(rationale, str)
            or not rationale
            or not isinstance(reviewer_instance, str)
            or not reviewer_instance
            or not isinstance(before_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", before_sha256)
        ):
            raise PdfPipelineError(f"heading_override_invalid: line={index + 1}")
        rows.append(row)
    return rows


def apply_heading_overrides(
    segments: list[dict[str, Any]],
    heading_resolutions: list[dict[str, Any]],
    overrides: list[dict[str, Any]],
    scope: tuple[int, int],
) -> None:
    """Deterministically apply bounded heading corrections and record receipts.

    An override may only rename an existing segment title; it never adds,
    removes, or reorders segments, and every application is recorded on the
    segment so the locked IR remains auditable. Direct document-map edits are
    still detected because the full map is hash-bound in source.json.

    The override binds by normalized title first. When several segments share
    the title, an optional ``segment_id`` narrows the match, and the required
    ``physical_page`` must agree with the selected segment's first physical
    page; conflicting locators fail closed as unresolved. Without a
    disambiguator, a duplicate title stays unresolved.
    """
    if not overrides:
        return
    start_page, end_page = scope
    by_title: dict[str, list[dict[str, Any]]] = {}
    for segment in segments:
        title = segment.get("title")
        if isinstance(title, str):
            by_title.setdefault(title, []).append(segment)
    applied: list[dict[str, Any]] = []
    for index, row in enumerate(overrides):
        physical_page = row["physical_page"]
        if not start_page <= physical_page <= end_page:
            raise PdfPipelineError(
                f"heading_override_page_out_of_scope: line={index + 1} page={physical_page}"
            )
        before_title = row["before_title"]
        matches = by_title.get(before_title, [])
        if len(matches) > 1:
            segment_id = row.get("segment_id")
            if segment_id:
                matches = [segment for segment in matches if segment.get("id") == segment_id]
            if len(matches) == 1 and segment_id and matches[0].get("physical_pages"):
                if matches[0]["physical_pages"][0] != physical_page:
                    matches = []
            elif len(matches) > 1:
                matches = [
                    segment
                    for segment in matches
                    if segment.get("physical_pages")
                    and segment["physical_pages"][0] == physical_page
                ]
        if len(matches) != 1:
            raise PdfPipelineError(
                f"heading_override_unresolved: line={index + 1} title={before_title!r}"
            )
        segment = matches[0]
        if segment.get("heading_override") is not None:
            raise PdfPipelineError(f"heading_override_duplicate: line={index + 1}")
        expected_hash = sha256_bytes(normalize_text(before_title).encode("utf-8"))
        if row["before_sha256"] != expected_hash:
            raise PdfPipelineError(f"heading_override_binding_invalid: line={index + 1}")
        segment["title"] = row["new_title"]
        segment["heading_override"] = {
            "schema_version": HEADING_OVERRIDE_SCHEMA,
            "before_title": before_title,
            "new_title": row["new_title"],
            "physical_page": physical_page,
            "rationale": row["rationale"],
            "reviewer_instance": row["reviewer_instance"],
            "override_sha256": sha256_json(row),
        }
        applied.append(segment)
    changed = {
        segment["heading_override"]["before_title"]: segment["title"]
        for segment in applied
    }
    for resolution in heading_resolutions:
        if (
            isinstance(resolution, dict)
            and isinstance(resolution.get("title"), str)
            and resolution["title"] in changed
        ):
            resolution["title"] = changed[resolution["title"]]


def compile_pdf_intermediate_representation(
    source_path: Path,
    start_page: int,
    end_page: int | None,
    minimum_native_characters: int = 40,
    anchor_strategy: str = "all",
    layout_parser: str = "pypdf",
    pdftoppm: Path | str | None = None,
    render_dpi: int = 150,
    docling_formulas: bool = False,
    docling_allow_network: bool = False,
    heading_overrides: Path | None = None,
    heading_locator_overrides: Path | None = None,
) -> dict[str, Any]:
    try:
        from pypdf import PdfReader, __version__ as pypdf_version
    except ModuleNotFoundError as error:
        raise PdfPipelineError(
            "pypdf is required; use the bundled workspace Python or install pypdf"
        ) from error

    source_path = source_path.expanduser().resolve()
    if not source_path.is_file():
        raise PdfPipelineError(f"source does not exist: {source_path}")
    if source_path.suffix.casefold() != ".pdf":
        raise PdfPipelineError("unsupported_input_format: current version accepts PDF only")
    with source_path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise PdfPipelineError("invalid_pdf_signature")

    source_hash = sha256_file(source_path)
    source_id = f"src-{source_hash[:20]}"
    try:
        reader = PdfReader(str(source_path))
    except Exception as error:
        raise PdfPipelineError(f"malformed_pdf: {error}") from error
    if reader.is_encrypted:
        raise PdfPipelineError("unsupported_encrypted_pdf")

    page_count = len(reader.pages)
    actual_end = page_count if end_page is None else end_page
    if start_page < 1 or actual_end < start_page or actual_end > page_count:
        raise PdfPipelineError(
            f"invalid_page_scope: require 1 <= start <= end <= {page_count}"
        )
    if minimum_native_characters < 1:
        raise PdfPipelineError("minimum_native_characters must be positive")
    if anchor_strategy not in {"leaf", "all"}:
        raise PdfPipelineError("anchor_strategy must be leaf or all")
    if layout_parser not in SUPPORTED_LAYOUT_PARSERS:
        raise PdfPipelineError(
            f"unsupported_layout_parser: {layout_parser}; choose from {','.join(SUPPORTED_LAYOUT_PARSERS)}"
        )

    labels = reader.page_labels
    page_records: dict[int, dict[str, Any]] = {}
    page_texts: dict[int, str] = {}
    raw_page_texts: dict[int, str] = {}
    page_sizes: dict[int, dict[str, float]] = {}
    for physical_page in range(start_page, actual_end + 1):
        record, text, raw_text = inspect_page(
            reader.pages[physical_page - 1],
            physical_page,
            str(labels[physical_page - 1]),
            minimum_native_characters,
        )
        page_records[physical_page] = record
        page_texts[physical_page] = text
        raw_page_texts[physical_page] = raw_text
        page_sizes[physical_page] = page_dimensions(reader.pages[physical_page - 1])

    page_values = list(page_records.values())
    usable_count = sum(page["native_text"]["status"] == "usable" for page in page_values)
    if usable_count == len(page_values):
        classification = "text-based"
    elif usable_count == 0:
        classification = "image-based-or-unusable"
    else:
        classification = "mixed"

    pages_needing_ocr = [
        page["physical_page"] for page in page_values if page["ocr_reasons"]
    ]
    pages_needing_visual = [
        page["physical_page"]
        for page in page_values
        if page["visual_verification_reasons"]
    ]
    label_mismatches = [
        page["physical_page"]
        for page in page_values
        if page["pdf_label_matches_printed_candidate"] is False
    ]
    page_role_counts: dict[str, int] = {}
    for page in page_values:
        role = page["page_role"]
        page_role_counts[role] = page_role_counts.get(role, 0) + 1

    structure_source, segments, ancestors, heading_resolutions = reconstruct_segments(
        reader,
        source_id,
        start_page,
        actual_end,
        page_records,
        page_texts,
        raw_page_texts,
    )
    overrides = load_heading_overrides(heading_overrides)
    apply_heading_overrides(
        segments,
        heading_resolutions,
        overrides,
        (start_page, actual_end),
    )
    sort_segments_physically(segments, page_texts)
    locator_overrides = load_locator_overrides(heading_locator_overrides)
    extractor = f"pypdf-{pypdf_version}"
    try:
        parser_receipt, layout_candidates = build_parser_receipt(
            source_path,
            source_hash,
            start_page,
            actual_end,
            page_sizes,
            extractor,
            layout_parser,
            docling_formulas=docling_formulas,
            docling_allow_network=docling_allow_network,
        )
    except ParserAdapterError as error:
        raise PdfPipelineError(str(error)) from error

    render_profile: dict[str, Any] | None = None
    render_receipts: list[dict[str, Any]] = []
    if pdftoppm is not None:
        try:
            render_profile, render_receipts = build_render_receipts(
                source_path,
                source_hash,
                range(start_page, actual_end + 1),
                pdftoppm,
                dpi=render_dpi,
            )
        except ParserAdapterError as error:
            raise PdfPipelineError(str(error)) from error

    try:
        heading_candidates, heading_resolution_records, applied_locator_overrides = build_heading_locator_records(
            source_sha256=source_hash,
            segments=segments,
            raw_page_texts=raw_page_texts,
            page_texts=page_texts,
            parser_receipt=parser_receipt,
            layout_candidates=layout_candidates,
            render_receipts=render_receipts,
            scope=(start_page, actual_end),
            overrides=locator_overrides,
        )
    except HeadingLocatorError as error:
        raise PdfPipelineError(str(error)) from error
    resolution_by_segment = {
        row["segment_id"]: row
        for row in heading_resolution_records
        if isinstance(row, dict) and isinstance(row.get("segment_id"), str)
    }
    for segment in segments:
        resolution = resolution_by_segment.get(segment.get("id"))
        if resolution is not None:
            segment["heading_locator_resolution_id"] = resolution["resolution_id"]

    anchor_candidates = build_anchor_candidates(
        source_id,
        segments,
        page_records,
        page_texts,
        extractor,
        anchor_strategy,
    )

    metadata = json_safe(reader.metadata or {})
    preflight = {
        "schema_version": SCHEMA_VERSION,
        "source_id": source_id,
        "scope": {"start_page": start_page, "end_page": actual_end},
        "extractor": extractor,
        "layout_parser": parser_receipt["layout_parser"],
        "minimum_native_characters": minimum_native_characters,
        "scope_classification": classification,
        "eligible_for_current_compiler": not pages_needing_ocr,
        "pages_needing_ocr": pages_needing_ocr,
        "pages_needing_visual_verification": pages_needing_visual,
        "pages_with_pdf_vs_printed_label_mismatch": label_mismatches,
        "page_role_counts": page_role_counts,
        "page_routing": {
            "compile_pages": [page["physical_page"] for page in page_values if page["page_disposition"] == "compile"],
            "ocr_required_pages": [page["physical_page"] for page in page_values if page["page_disposition"] == "ocr-required"],
            "manual_review_pages": [page["physical_page"] for page in page_values if page["page_disposition"] == "manual-review-required"],
        },
        "pages": page_values,
    }
    document_map = {
        "schema_version": SCHEMA_VERSION,
        "source_id": source_id,
        "scope": {"start_page": start_page, "end_page": actual_end},
        "structure_source": structure_source,
        "context_ancestors": ancestors,
        "segments": segments,
        "heading_resolutions": heading_resolutions,
    }
    visual_review = {
        "schema_version": SCHEMA_VERSION,
        "source_id": source_id,
        "render_recommendation": {"format": "png", "dpi": 150},
        "pages": [
            {
                "physical_page": page["physical_page"],
                "reasons": page["visual_verification_reasons"],
                "ocr_also_required": bool(page["ocr_reasons"]),
            }
            for page in page_values
            if page["visual_verification_reasons"]
        ],
    }
    source_record = {
        "schema_version": SCHEMA_VERSION,
        "source_id": source_id,
        "sha256": source_hash,
        "media_type": "application/pdf",
        "size_bytes": source_path.stat().st_size,
        "original_filename": source_path.name,
        "page_count": page_count,
        "encrypted": False,
        "metadata": metadata,
        "pdf_ir_receipts": {
            "parser_receipt": "parser-receipt.json",
            "layout_candidates": "layout-candidates.jsonl",
            "render_receipts": "render-receipts.jsonl",
            "preflight_sha256": sha256_json(preflight),
            "document_map_sha256": sha256_json(document_map),
            "visual_review_sha256": sha256_json(visual_review),
            "anchor_candidates_sha256": sha256_json(anchor_candidates),
            "heading_candidates_sha256": sha256_json(heading_candidates),
            "heading_resolutions_sha256": sha256_json(heading_resolution_records),
            "parser_receipt_sha256": sha256_json(parser_receipt),
            "layout_candidates_sha256": sha256_json(layout_candidates),
            "render_receipts_sha256": sha256_json(render_receipts),
            "render_receipt_status": "complete" if render_profile is not None else "deferred",
            **(
                {"heading_overrides_sha256": sha256_json(overrides)}
                if overrides
                else {}
            ),
            **(
                {"heading_locator_overrides_sha256": sha256_json(applied_locator_overrides)}
                if applied_locator_overrides
                else {}
            ),
        },
    }
    return {
        "source": source_record,
        "preflight": preflight,
        "document_map": document_map,
        "anchor_candidates": anchor_candidates,
        "visual_review": visual_review,
        "parser_receipt": parser_receipt,
        "layout_candidates": layout_candidates,
        "render_receipts": render_receipts,
        "heading_overrides": overrides,
        "heading_candidates": heading_candidates,
        "heading_resolutions": heading_resolution_records,
        "heading_locator_overrides": applied_locator_overrides,
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for value in values
        ),
        encoding="utf-8",
    )


def write_intermediate_representation(output: Path, result: dict[str, Any]) -> None:
    output = output.expanduser().resolve()
    if output.exists():
        if not output.is_dir():
            raise PdfPipelineError(f"output_not_directory: {output}")
        if any(output.iterdir()):
            raise PdfPipelineError(f"output_not_empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "source.json", result["source"])
    write_json(output / "preflight.json", result["preflight"])
    write_json(output / "document-map.json", result["document_map"])
    write_jsonl(output / "anchor-candidates.jsonl", result["anchor_candidates"])
    write_json(output / "visual-review.json", result["visual_review"])
    write_json(output / "parser-receipt.json", result["parser_receipt"])
    write_jsonl(output / "layout-candidates.jsonl", result["layout_candidates"])
    write_jsonl(output / "render-receipts.jsonl", result["render_receipts"])
    write_jsonl(output / "heading-candidates.jsonl", result.get("heading_candidates", []))
    write_jsonl(output / "heading-resolutions.jsonl", result.get("heading_resolutions", []))
    if result.get("heading_overrides"):
        write_jsonl(output / "heading-overrides.jsonl", result["heading_overrides"])
    if result.get("heading_locator_overrides"):
        write_jsonl(
            output / "heading-locator-overrides.jsonl",
            result["heading_locator_overrides"],
        )
