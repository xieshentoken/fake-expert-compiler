#!/usr/bin/env python3
"""Hash-locked heading candidate generation and deterministic resolution.

The locator deliberately operates on the canonical pypdf page text only for
character spans.  Optional layout/parser candidates may add a bbox, but they
never replace the canonical text stream.  Matching normalization is a locator
aid; it is never used for evidence hashes or source-anchor promotion.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Iterable


HEADING_CANDIDATE_SCHEMA = "tkc.heading-candidate/v0.2"
HEADING_RESOLUTION_SCHEMA = "tkc.heading-resolution/v0.2"
HEADING_LOCATOR_OVERRIDE_SCHEMA = "tkc.heading-locator-override/v0.2"
HEADING_LOCATOR_PROTOCOL = "heading-locator-v0.2"
MATCH_NORMALIZATION_PROFILE = "nfkc-casefold-dash-ligature-dehyphenated-v2"
DETERMINISTIC_MARGIN = 12
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TRAILING_PAGE_NUMBER = re.compile(r"(?:\s|\b)\d{1,4}$")
LEADING_PAGE_NUMBER = re.compile(r"^\d{1,4}(?:\s|\b)")
DASHES = str.maketrans(
    {
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "﹘": "-",
        "﹣": "-",
        "－": "-",
        "‘": "'",
        "’": "'",
        "‚": "'",
        "“": '"',
        "”": '"',
        "„": '"',
    }
)


class HeadingLocatorError(RuntimeError):
    """Stable heading-locator contract failure."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def normalize_heading_match(text: str) -> str:
    """Normalize only for title matching, never for canonical evidence text."""

    value = unicodedata.normalize("NFKC", str(text)).translate(DASHES).casefold()
    value = value.replace("\u00ad", "")
    return re.sub(r"\s+", " ", value).strip()


def _compact_projection(text: str) -> tuple[str, list[int]]:
    """Return compact match text and offsets into the supplied page string."""

    compact: list[str] = []
    positions: list[int] = []
    for index, character in enumerate(text):
        folded = unicodedata.normalize("NFKC", character).translate(DASHES).casefold()
        for item in folded:
            if item.isspace() or item == "-":
                continue
            compact.append(item)
            positions.append(index)
    return "".join(compact), positions


def compact_heading_match(text: str) -> str:
    return _compact_projection(text)[0]


def _line_entries(raw_text: str) -> list[dict[str, Any]]:
    normalized_lines: list[str] = []
    for raw_line in raw_text.splitlines():
        line = unicodedata.normalize("NFKC", raw_line).translate(DASHES)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            normalized_lines.append(line)
    entries: list[dict[str, Any]] = []
    cursor = 0
    for line in normalized_lines:
        # Keep offsets compatible with the canonical page string. Matching
        # case-folding is intentionally not applied here because some Unicode
        # folds change character count (for example, ``ß`` -> ``ss``).
        start = cursor
        end = start + len(line)
        entries.append(
            {
                "line_index": len(entries),
                "text": line,
                "start": start,
                "end": end,
                "line_sha256": sha256_text(line),
            }
        )
        cursor = end + 1
    return entries


def _line_for_span(lines: list[dict[str, Any]], start: int, end: int) -> dict[str, Any] | None:
    overlaps = [row for row in lines if row["start"] <= start < row["end"] or row["start"] < end <= row["end"]]
    if not overlaps:
        return None
    return min(overlaps, key=lambda row: (abs(row["start"] - start), row["line_index"]))


def _is_full_line_match(line: str, title: str) -> bool:
    line_compact = compact_heading_match(line)
    title_compact = compact_heading_match(title)
    if line_compact == title_compact:
        return True
    without_leading = LEADING_PAGE_NUMBER.sub("", line)
    without_trailing = TRAILING_PAGE_NUMBER.sub("", without_leading)
    return compact_heading_match(without_trailing) == title_compact


def _line_boundary(line: dict[str, Any], start: int, end: int, title: str) -> str:
    if _is_full_line_match(str(line["text"]), title):
        return "exact-line"
    relative_start = max(0, start - int(line["start"]))
    relative_end = max(0, end - int(line["start"]))
    line_length = max(1, int(line["end"]) - int(line["start"]))
    if relative_start <= 1 or line_length - relative_end <= 1:
        return "line-boundary"
    return "substring"


def _layout_bbox(
    page_candidates: list[dict[str, Any]],
    actual_title: str,
    line: dict[str, Any],
) -> tuple[list[float] | None, str | None, list[str]]:
    """Best-effort bbox union from hash-only word candidates.

    Layout adapters intentionally do not expose source text.  We therefore
    match only per-word content hashes and keep the result absent when the
    mapping is not unique enough to be auditable.
    """

    words = [word for word in re.findall(r"[\w]+", actual_title, flags=re.UNICODE) if word]
    if not words or not page_candidates:
        return None, None, []
    hashes = [sha256_text(normalize_heading_match(word)) for word in words]
    by_line: dict[Any, list[dict[str, Any]]] = {}
    for candidate in page_candidates:
        order = candidate.get("order")
        line_key = order[1] if isinstance(order, list) and len(order) > 1 else None
        by_line.setdefault(line_key, []).append(candidate)
    possible: list[tuple[Any, list[dict[str, Any]]]] = []
    for line_key, rows in by_line.items():
        available = [row for row in rows if row.get("content_sha256") in hashes]
        if len({row.get("content_sha256") for row in available}) >= min(len(hashes), 2):
            possible.append((line_key, available))
    if len(possible) != 1:
        return None, None, []
    rows = possible[0][1]
    if not all(isinstance(row.get("bbox"), list) and len(row["bbox"]) == 4 for row in rows):
        return None, None, []
    bbox = [
        round(min(float(row["bbox"][0]) for row in rows), 4),
        round(min(float(row["bbox"][1]) for row in rows), 4),
        round(max(float(row["bbox"][2]) for row in rows), 4),
        round(max(float(row["bbox"][3]) for row in rows), 4),
    ]
    return bbox, str(possible[0][0]) if possible[0][0] is not None else None, sorted(
        str(row.get("id")) for row in rows if isinstance(row.get("id"), str)
    )


def _candidate_hash(row: dict[str, Any]) -> str:
    return sha256_json({key: value for key, value in row.items() if key != "candidate_sha256"})


def _resolution_hash(row: dict[str, Any]) -> str:
    return sha256_json({key: value for key, value in row.items() if key != "resolution_sha256"})


def _override_hash(row: dict[str, Any]) -> str:
    return sha256_json({key: value for key, value in row.items() if key != "override_sha256"})


def _candidate_method(title: str, actual_title: str, span_text: str) -> str:
    if actual_title == title:
        return "normalized-exact"
    actual_basic = unicodedata.normalize("NFKC", actual_title).casefold()
    requested_basic = unicodedata.normalize("NFKC", title).casefold()
    if actual_basic == requested_basic:
        return "unicode-ligature-equivalent"
    actual = normalize_heading_match(actual_title)
    requested = normalize_heading_match(title)
    if actual.replace("- ", "") == requested.replace("-", "") or "-" in span_text:
        return "dehyphenated-line-match"
    if compact_heading_match(actual_title) == compact_heading_match(title):
        return "unicode-ligature-equivalent"
    return "compact-token-boundary-match"


def _candidate_score(
    method: str,
    boundary: str,
    role: str,
    page: int,
    outline_page: int,
    line_index: int,
    bbox: list[float] | None,
) -> int:
    score = {
        "normalized-exact": 80,
        "unicode-ligature-equivalent": 76,
        "dehyphenated-line-match": 72,
        "compact-token-boundary-match": 68,
    }.get(method, 60)
    score += {"exact-line": 35, "line-boundary": 20, "substring": -12}.get(boundary, -20)
    score += {"body-heading": 30, "inline-heading-candidate": 0, "running-header": -80, "footer": -80, "body-text": -20}.get(role, 0)
    score += max(-20, 20 - abs(page - outline_page) * 8)
    if line_index <= 1 and role == "body-heading":
        score += 4
    if bbox is not None:
        score += 3
    return score


def _raw_title_from_span(page_text: str, start: int, end: int) -> str:
    return page_text[start:end]


def build_heading_locator_records(
    *,
    source_sha256: str,
    segments: list[dict[str, Any]],
    raw_page_texts: dict[int, str],
    page_texts: dict[int, str],
    parser_receipt: dict[str, Any] | None,
    layout_candidates: list[dict[str, Any]] | None,
    render_receipts: list[dict[str, Any]] | None,
    scope: tuple[int, int],
    overrides: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build candidates and resolutions without editing canonical page text."""

    if not SHA256_RE.fullmatch(source_sha256):
        raise HeadingLocatorError("heading_source_hash_invalid")
    start_page, end_page = scope
    parser = (parser_receipt or {}).get("layout_parser") if isinstance(parser_receipt, dict) else {}
    parser_id = parser.get("id") if isinstance(parser, dict) else None
    parser_version = parser.get("version") if isinstance(parser, dict) else None
    canonical_extractor = (parser_receipt or {}).get("canonical_text_extractor") if isinstance(parser_receipt, dict) else None
    if not isinstance(parser_id, str):
        parser_id = "pypdf"
    if not isinstance(parser_version, str):
        parser_version = "unknown"
    if not isinstance(canonical_extractor, str):
        canonical_extractor = f"pypdf-{parser_version}"
    layout_by_page: dict[int, list[dict[str, Any]]] = {}
    for row in layout_candidates or []:
        if isinstance(row, dict) and isinstance(row.get("physical_page"), int):
            layout_by_page.setdefault(int(row["physical_page"]), []).append(row)
    render_by_page = {
        int(row["physical_page"]): row.get("render_sha256")
        for row in (render_receipts or [])
        if isinstance(row, dict) and isinstance(row.get("physical_page"), int)
    }
    candidates: list[dict[str, Any]] = []
    segment_candidates: dict[str, list[dict[str, Any]]] = {}
    for segment_index, segment in enumerate(segments):
        segment_id = segment.get("id")
        title = segment.get("title")
        pages = segment.get("physical_pages")
        if not isinstance(segment_id, str) or not isinstance(title, str) or not title or not isinstance(pages, list) or not pages:
            raise HeadingLocatorError(f"heading_segment_invalid:{segment_index + 1}")
        title_correction = segment.get("heading_override")
        # A legacy heading override is a title correction, not an occurrence
        # selection.  The corrected title is therefore still located as an
        # occurrence in the page text; the correction metadata remains bound
        # separately in the resolution below.
        matching_title = title
        first_page = int(pages[0])
        outline_page = int(segment.get("outline_page", first_page))
        old_resolution = segment.get("heading_resolution")
        old_selected_page = old_resolution.get("selected_page") if isinstance(old_resolution, dict) else None
        search_pages = sorted(
            {
                page
                for base in (first_page, old_selected_page if isinstance(old_selected_page, int) else first_page)
                for page in range(max(start_page, base - 1), min(end_page, base + 2) + 1)
            }
        )
        neighbor_ids = [str(item.get("id")) for item in segments[max(0, segment_index - 1): segment_index + 2] if isinstance(item, dict) and isinstance(item.get("id"), str) and item.get("id") != segment_id]
        local: list[dict[str, Any]] = []
        for page in search_pages:
            page_text = page_texts.get(page, "")
            raw_text = raw_page_texts.get(page, page_text)
            if not page_text:
                continue
            lines = _line_entries(raw_text)
            compact_page, positions = _compact_projection(page_text)
            compact_title = compact_heading_match(matching_title)
            if not compact_title or not positions:
                continue
            offset = 0
            while True:
                found = compact_page.find(compact_title, offset)
                if found < 0:
                    break
                start = positions[found]
                end = positions[found + len(compact_title) - 1] + 1
                offset = found + max(1, len(compact_title))
                line = _line_for_span(lines, start, end)
                if line is None:
                    continue
                overlapping_lines = [
                    row
                    for row in lines
                    if row["start"] < end and row["end"] > start
                ]
                if len(overlapping_lines) > 1:
                    # PDF text extraction commonly emits a chapter number and
                    # its title as separate lines. Treat the contiguous line
                    # span as one heading for boundary/role classification;
                    # the canonical page span and each source hash remain
                    # untouched.
                    combined_text = " ".join(row["text"] for row in overlapping_lines)
                    line = {
                        **line,
                        "text": combined_text,
                        "end": overlapping_lines[-1]["end"],
                        "line_sha256": sha256_text(combined_text),
                    }
                span_text = page_text[start:end]
                actual_title = _raw_title_from_span(page_text, start, end)
                boundary = _line_boundary(line, start, end, matching_title)
                full_line = boundary == "exact-line"
                line_text = str(line["text"])
                trailing_page = bool(TRAILING_PAGE_NUMBER.search(line_text)) and not _is_full_line_match(line_text, matching_title)
                leading_page = bool(LEADING_PAGE_NUMBER.search(line_text)) and not _is_full_line_match(line_text, matching_title)
                role = "body-text"
                if full_line:
                    role = "body-heading"
                elif boundary == "line-boundary":
                    role = "inline-heading-candidate"
                if line["line_index"] <= 1 and (trailing_page or leading_page):
                    role = "running-header"
                if line["line_index"] >= max(0, len(lines) - 2) and (trailing_page or leading_page):
                    role = "footer"
                method = _candidate_method(matching_title, actual_title, span_text)
                bbox, layout_line, layout_ids = _layout_bbox(layout_by_page.get(page, []), actual_title, line)
                candidate_seed = {
                    "source_sha256": source_sha256,
                    "segment_id": segment_id,
                    "physical_page": page,
                    "char_span": [start, end],
                    "raw_title": actual_title,
                    "match_method": method,
                    "line_sha256": line["line_sha256"],
                }
                candidate_id = f"hgc-{sha256_json(candidate_seed)[:20]}"
                score = _candidate_score(method, boundary, role, page, outline_page, int(line["line_index"]), bbox)
                candidate = {
                    "schema_version": HEADING_CANDIDATE_SCHEMA,
                    "candidate_id": candidate_id,
                    "source_sha256": source_sha256,
                    "segment_id": segment_id,
                    "physical_page": page,
                    "raw_title": actual_title,
                    "requested_title": title,
                    "matching_title": matching_title,
                    "match_normalization": {
                        "profile": MATCH_NORMALIZATION_PROFILE,
                        "value": normalize_heading_match(actual_title),
                        "requested_value": normalize_heading_match(title),
                    },
                    "char_span": [start, end],
                    "page_text_sha256": sha256_text(page_text),
                    "span_sha256": sha256_text(span_text),
                    "line_index": int(line["line_index"]),
                    "line_sha256": line["line_sha256"],
                    "line_boundary": boundary,
                    "role": role,
                    "candidate_method": method,
                    "parser": {
                        "id": parser_id,
                        "version": parser_version,
                        "canonical_text_extractor": canonical_extractor,
                    },
                    "render_sha256": render_by_page.get(page),
                    "coordinate_space": (parser.get("coordinate_space") if isinstance(parser, dict) else None),
                    "bbox": bbox,
                    "layout_line": layout_line,
                    "layout_candidate_ids": layout_ids,
                    "constraints": {
                        "outline_page": outline_page,
                        "outline_distance": abs(page - outline_page),
                        "parent_segment_id": segment.get("parent_id"),
                        "neighbor_segment_ids": neighbor_ids,
                        "page_in_segment_envelope": page in pages,
                    },
                    "score": score,
                    "selection_reason": "not-yet-resolved",
                }
                candidate["candidate_sha256"] = _candidate_hash(candidate)
                local.append(candidate)
        # De-duplicate compact matches that map to the same canonical span.
        deduped: dict[tuple[int, int], dict[str, Any]] = {}
        for candidate in local:
            key = (int(candidate["physical_page"]), int(candidate["char_span"][0]))
            previous = deduped.get(key)
            if previous is None or int(candidate["score"]) > int(previous["score"]):
                deduped[key] = candidate
        segment_candidates[segment_id] = sorted(
            deduped.values(), key=lambda row: (-int(row["score"]), int(row["physical_page"]), int(row["char_span"][0]), row["candidate_id"])
        )

    resolutions: list[dict[str, Any]] = []
    candidate_by_id: dict[str, dict[str, Any]] = {}
    for segment_index, segment in enumerate(segments):
        segment_id = str(segment["id"])
        title = str(segment["title"])
        title_correction = segment.get("heading_override")
        local = segment_candidates.get(segment_id, [])
        candidate_by_id.update({row["candidate_id"]: row for row in local})
        allow_title_correction_substring = isinstance(title_correction, dict)
        eligible = [
            row
            for row in local
            if row["role"] not in {"running-header", "footer"}
            and (
                row["line_boundary"] in {"exact-line", "line-boundary"}
                or (allow_title_correction_substring and row["line_boundary"] == "substring")
            )
        ]
        eligible.sort(key=lambda row: (-int(row["score"]), int(row["physical_page"]), int(row["char_span"][0]), row["candidate_id"]))
        selected: dict[str, Any] | None = None
        status = "heading_resolution_required"
        policy = "manual-required"
        failure_code = "heading_locator_missing"
        reason = "No eligible body-heading candidate was found."
        margin: int | None = None
        if len(eligible) == 1:
            selected = eligible[0]
            status = "resolved"
            policy = "unique-candidate"
            failure_code = None
            reason = "Unique eligible candidate after boundary and running-header exclusion."
        elif len(eligible) > 1:
            margin = int(eligible[0]["score"]) - int(eligible[1]["score"])
            if margin >= DETERMINISTIC_MARGIN:
                selected = eligible[0]
                status = "resolved"
                policy = "deterministic-margin"
                failure_code = None
                reason = f"Top eligible candidate exceeds the next candidate by deterministic margin {margin}."
            else:
                failure_code = "heading_locator_ambiguous"
                reason = f"{len(eligible)} eligible candidates remain below deterministic margin {DETERMINISTIC_MARGIN}."
        elif local:
            failure_code = "heading_locator_missing"
            reason = "Only running-header/footer or non-boundary matches were found."
        if selected is not None:
            selected["selection_reason"] = reason
        for row in local:
            if selected is None:
                row["selection_reason"] = "requires-structure-resolution"
            elif row["candidate_id"] != selected["candidate_id"]:
                row["selection_reason"] = "not-selected-after-deterministic-resolution"
            row["candidate_sha256"] = _candidate_hash(row)
        resolution = {
            "schema_version": HEADING_RESOLUTION_SCHEMA,
            "resolution_id": f"hgr-{sha256_json([source_sha256, segment_id])[:20]}",
            "source_sha256": source_sha256,
            "segment_id": segment_id,
            "requested_title": title,
            "title_correction": (
                {
                    "before_title": title_correction.get("before_title"),
                    "after_title": title,
                    "before_title_sha256": sha256_text(str(title_correction.get("before_title", "")).strip()),
                    "override_sha256": title_correction.get("override_sha256"),
                }
                if isinstance(title_correction, dict)
                else None
            ),
            "candidate_ids": [row["candidate_id"] for row in local],
            "candidate_hashes": {row["candidate_id"]: row["candidate_sha256"] for row in local},
            "status": status,
            "selection_policy": policy,
            "selected_candidate_id": selected["candidate_id"] if selected is not None else None,
            "selected_candidate_sha256": selected["candidate_sha256"] if selected is not None else None,
            "score_margin": margin,
            "deterministic_margin": DETERMINISTIC_MARGIN,
            "failure_code": failure_code,
            "selection_reason": reason,
            "structure_order": int(segment.get("structure_order", segment_index)),
            "physical_page": selected["physical_page"] if selected is not None else (int(segment["physical_pages"][0]) if segment.get("physical_pages") else None),
            "char_span": selected["char_span"] if selected is not None else None,
            "review_required": selected is None,
        }
        resolution["resolution_sha256"] = _resolution_hash(resolution)
        resolutions.append(resolution)
        candidates.extend(local)

    if overrides:
        apply_locator_overrides(candidates, resolutions, overrides, source_sha256)
    candidates.sort(key=lambda row: (int(row["physical_page"]), int(row["char_span"][0]), row["segment_id"], row["candidate_id"]))
    resolutions.sort(key=lambda row: (int(row.get("structure_order", 0)), row["segment_id"]))
    return candidates, resolutions, list(overrides or [])


def apply_locator_overrides(
    candidates: list[dict[str, Any]],
    resolutions: list[dict[str, Any]],
    overrides: list[dict[str, Any]],
    source_sha256: str,
) -> None:
    by_candidate = {row.get("candidate_id"): row for row in candidates if isinstance(row, dict)}
    by_resolution = {row.get("resolution_id"): row for row in resolutions if isinstance(row, dict)}
    for index, override in enumerate(overrides):
        if not isinstance(override, dict) or override.get("schema_version") != HEADING_LOCATOR_OVERRIDE_SCHEMA:
            raise HeadingLocatorError(f"heading_locator_override_invalid:{index + 1}")
        candidate_id = override.get("candidate_id")
        resolution_id = override.get("resolution_id")
        before_candidate = override.get("before_candidate_sha256")
        before_resolution = override.get("before_resolution_sha256")
        reviewer = override.get("reviewer_instance")
        rationale = override.get("rationale")
        if (
            not isinstance(candidate_id, str)
            or not isinstance(resolution_id, str)
            or not isinstance(before_candidate, str)
            or not SHA256_RE.fullmatch(before_candidate)
            or not isinstance(before_resolution, str)
            or not SHA256_RE.fullmatch(before_resolution)
            or not isinstance(reviewer, str)
            or not reviewer
            or not isinstance(rationale, str)
            or not rationale
        ):
            raise HeadingLocatorError(f"heading_locator_override_invalid:{index + 1}")
        candidate = by_candidate.get(candidate_id)
        resolution = by_resolution.get(resolution_id)
        if candidate is None or resolution is None or candidate_id not in resolution.get("candidate_ids", []):
            raise HeadingLocatorError(f"heading_locator_override_unbound:{index + 1}")
        if candidate.get("source_sha256") != source_sha256 or resolution.get("source_sha256") != source_sha256:
            raise HeadingLocatorError(f"heading_locator_override_source_mismatch:{index + 1}")
        if candidate.get("candidate_sha256") != before_candidate or resolution.get("resolution_sha256") != before_resolution:
            raise HeadingLocatorError(f"heading_locator_override_before_hash_mismatch:{index + 1}")
        if resolution.get("locator_override") is not None:
            raise HeadingLocatorError(f"heading_locator_override_duplicate:{index + 1}")
        resolution["status"] = "resolved"
        resolution["selection_policy"] = "bounded-locator-override"
        resolution["selected_candidate_id"] = candidate_id
        resolution["selected_candidate_sha256"] = candidate.get("candidate_sha256")
        resolution["failure_code"] = None
        resolution["review_required"] = False
        resolution["selection_reason"] = "Bounded reviewer-selected occurrence override."
        resolution["locator_override"] = {
            "schema_version": HEADING_LOCATOR_OVERRIDE_SCHEMA,
            "candidate_id": candidate_id,
            "before_candidate_sha256": before_candidate,
            "before_resolution_sha256": before_resolution,
            "reviewer_instance": reviewer,
            "rationale": rationale,
            "override_sha256": _override_hash(override),
        }
        candidate["selection_reason"] = "Selected by bounded locator override."
        for row in candidates:
            if row.get("segment_id") == candidate.get("segment_id") and row.get("candidate_id") != candidate_id:
                row["selection_reason"] = "not-selected-by-bounded-locator-override"
            if row.get("segment_id") == candidate.get("segment_id"):
                row["candidate_sha256"] = _candidate_hash(row)
        resolution["candidate_hashes"] = {
            row["candidate_id"]: row["candidate_sha256"]
            for row in candidates
            if row.get("segment_id") == candidate.get("segment_id")
        }
        resolution["selected_candidate_sha256"] = candidate.get("candidate_sha256")
        resolution["resolution_sha256"] = _resolution_hash(resolution)


def load_locator_overrides(path: Any) -> list[dict[str, Any]]:
    if path is None:
        return []
    path = path.expanduser().resolve()
    if not path.is_file():
        raise HeadingLocatorError(f"heading_locator_overrides_missing:{path}")
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise HeadingLocatorError(f"heading_locator_override_invalid:{index}") from error
        if not isinstance(row, dict):
            raise HeadingLocatorError(f"heading_locator_override_invalid:{index}")
        rows.append(row)
    return rows


def locator_bundle_sha256(candidates: list[dict[str, Any]], resolutions: list[dict[str, Any]]) -> str:
    return sha256_json({"candidates": candidates, "resolutions": resolutions, "protocol": HEADING_LOCATOR_PROTOCOL})


__all__ = [
    "DETERMINISTIC_MARGIN",
    "HEADING_CANDIDATE_SCHEMA",
    "HEADING_LOCATOR_OVERRIDE_SCHEMA",
    "HEADING_LOCATOR_PROTOCOL",
    "HEADING_RESOLUTION_SCHEMA",
    "HeadingLocatorError",
    "apply_locator_overrides",
    "build_heading_locator_records",
    "compact_heading_match",
    "load_locator_overrides",
    "locator_bundle_sha256",
    "normalize_heading_match",
    "sha256_json",
    "sha256_text",
]
