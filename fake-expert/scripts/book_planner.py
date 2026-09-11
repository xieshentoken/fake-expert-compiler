#!/usr/bin/env python3
"""Deterministic whole-book structure, coverage, and review planning.

This module is intentionally a planner, not a compiler executor.  It reads a
local PDF through the existing pinned pypdf PDF-IR boundary, records every
physical page and reconstructed segment, and emits candidate-only routes.  It
never invokes OCR, starts a model, authors a review, promotes evidence, seals
an artifact, or enables execution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from compiler_version import (
    HEADING_LOCATOR_PROTOCOL,
    WHOLE_BOOK_PLANNER_COMPILER_VERSION,
    WHOLE_BOOK_PLANNER_PROTOCOL,
)
from heading_locator import locator_bundle_sha256
from pdf_structure import (
    PdfPipelineError,
    compile_pdf_intermediate_representation,
    sha256_file,
    stable_id,
)


BOOK_PLAN_SCHEMA = "tkc.book-plan/v0.2"
COVERAGE_LEDGER_SCHEMA = "tkc.book-coverage-ledger/v0.2"
MODULE_PLAN_SCHEMA = "tkc.module-plan/v0.2"
REVIEW_ROUTING_SCHEMA = "tkc.review-routing/v0.2"
PLANNING_STATES = (
    "compile",
    "context-only",
    "non-knowledge",
    "ocr-candidate",
    "visual-review",
    "gap",
    "quarantined",
)
PROMOTION_STATES = ("not-started", "candidate-only", "promoted", "blocked")
NON_KNOWLEDGE_RE = re.compile(
    r"\b(?:bibliography|references?|index|glossary|acknowledg(?:e)?ments?|contents|table of contents)\b",
    re.IGNORECASE,
)
FRONT_MATTER_RE = re.compile(
    r"\b(?:preface|foreword|dedication|copyright|contents|table of contents|acknowledg(?:e)?ments?)\b",
    re.IGNORECASE,
)


class BookPlannerError(RuntimeError):
    """Stable whole-book planner failure."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def _without_hash(row: dict[str, Any], field: str) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != field}


def _row_hash(row: dict[str, Any], field: str) -> str:
    return sha256_json(_without_hash(row, field))


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _jsonl_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    ).encode("utf-8")


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _ensure_empty_output(output: Path) -> Path:
    output = output.expanduser().resolve()
    if output.exists():
        if not output.is_dir():
            raise BookPlannerError(f"plan_output_not_directory:{output}")
        if any(output.iterdir()):
            raise BookPlannerError(f"plan_output_not_empty:{output}")
    output.mkdir(parents=True, exist_ok=True)
    return output


def _pypdf_version(preflight: dict[str, Any]) -> str:
    extractor = str(preflight.get("extractor", "pypdf-unknown"))
    return extractor.removeprefix("pypdf-") or "unknown"


def _page_planning_status(
    page: dict[str, Any],
    *,
    covered_by_segment: bool,
    text: str,
    first_structured_page: int | None,
) -> tuple[str, str]:
    if page.get("ocr_reasons"):
        return "ocr-candidate", "Native text is missing or below the configured threshold; OCR is a candidate route only."
    if page.get("page_disposition") == "manual-review-required":
        return "quarantined", "The page is not eligible for native compilation and has no automatic fallback."
    if page.get("visual_verification_reasons"):
        return "visual-review", "Native text is available, but visual verification is required by the page receipt."
    if not covered_by_segment:
        if first_structured_page is None or int(page["physical_page"]) < first_structured_page:
            return "context-only", "The page is outside reconstructed knowledge segments and is retained as book context."
        if FRONT_MATTER_RE.search(text):
            return "context-only", "Front matter is retained as context and is not promoted as a knowledge module."
        return "gap", "No reconstructed segment claims this page; deterministic planning records an explicit gap."
    return "compile", "Native text and structure coverage permit a candidate compilation route."


def _segment_planning_status(
    segment: dict[str, Any],
    page_by_number: dict[int, dict[str, Any]],
    resolutions_by_segment: dict[str, dict[str, Any]],
) -> tuple[str, str]:
    pages = [int(page) for page in segment.get("physical_pages", [])]
    if any(page_by_number.get(page, {}).get("ocr_reasons") for page in pages):
        return "ocr-candidate", "At least one segment page is an OCR candidate; no OCR invocation is performed."
    resolution = resolutions_by_segment.get(str(segment.get("id")))
    if not isinstance(resolution, dict) or resolution.get("status") != "resolved":
        return "gap", "Heading resolution is not complete; the segment remains a structured gap."
    if any(page_by_number.get(page, {}).get("visual_verification_reasons") for page in pages):
        return "visual-review", "The segment contains a page requiring visual verification."
    if NON_KNOWLEDGE_RE.search(str(segment.get("title", ""))):
        return "non-knowledge", "The structural title identifies reference or navigation material."
    return "compile", "The segment has a hash-bound heading resolution and native page coverage."


def _promotion_status(planning_status: str) -> str:
    if planning_status in {"ocr-candidate", "visual-review"}:
        return "candidate-only"
    if planning_status in {"gap", "quarantined"}:
        return "blocked"
    return "not-started"


def _structure_route(resolution: dict[str, Any] | None) -> str:
    if isinstance(resolution, dict) and resolution.get("status") == "resolved":
        return "resolved"
    return "heading-resolution-required"


def _routes(
    *,
    planning_status: str,
    resolution: dict[str, Any] | None,
) -> dict[str, str]:
    return {
        "structure": _structure_route(resolution),
        "semantic": "independent-review-required",
        "ocr": "candidate-only-no-invocation" if planning_status == "ocr-candidate" else "not-routed",
        "visual": "visual-review-required" if planning_status == "visual-review" else "not-routed",
        "competency": "independent-competency-required",
        "seal": "manual-seal-required",
        "execution": "forbidden-until-all-gates",
    }


def _input_hash(source_sha256: str, value: Any) -> str:
    return sha256_json({"source_sha256": source_sha256, "value": value})


def _root_segment_id(
    segment: dict[str, Any], by_id: dict[str, dict[str, Any]]
) -> str | None:
    current = segment
    seen: set[str] = set()
    while isinstance(current, dict) and isinstance(current.get("id"), str):
        current_id = str(current["id"])
        if current_id in seen:
            return current_id
        seen.add(current_id)
        parent_id = current.get("parent_id")
        if not isinstance(parent_id, str) or parent_id not in by_id:
            return current_id
        current = by_id[parent_id]
    return None


def _module_id(source_sha256: str, title: str, segment_ids: list[str], pages: list[int]) -> str:
    return stable_id("mod", source_sha256, title, segment_ids, pages)


def _build_modules(
    *,
    source_sha256: str,
    segments: list[dict[str, Any]],
    page_numbers: list[int],
) -> tuple[list[dict[str, Any]], dict[int, str], dict[str, str]]:
    by_id = {
        str(segment["id"]): segment
        for segment in segments
        if isinstance(segment, dict) and isinstance(segment.get("id"), str)
    }
    groups: dict[str, list[dict[str, Any]]] = {}
    for segment in segments:
        segment_id = segment.get("id")
        if not isinstance(segment_id, str):
            continue
        root_id = _root_segment_id(segment, by_id) or segment_id
        groups.setdefault(root_id, []).append(segment)

    module_groups: list[tuple[str, str, list[str], list[int], int]] = []
    for root_id, grouped in groups.items():
        root = by_id.get(root_id, grouped[0])
        segment_ids = sorted(
            (str(segment["id"]) for segment in grouped),
            key=lambda item: int(by_id[item].get("structure_order", 0)),
        )
        pages = sorted(
            {
                int(page)
                for segment in grouped
                for page in segment.get("physical_pages", [])
                if isinstance(page, int)
            }
        )
        if pages:
            first_page = pages[0]
        else:
            first_page = 10**9
        module_groups.append((root_id, str(root.get("title", root_id)), segment_ids, pages, first_page))

    covered_pages = {page for _, _, _, pages, _ in module_groups for page in pages}
    uncovered = sorted(set(page_numbers) - covered_pages)
    chunk_size = 50
    for offset in range(0, len(uncovered), chunk_size):
        chunk = uncovered[offset : offset + chunk_size]
        if not chunk:
            continue
        chunk_number = offset // chunk_size + 1
        module_groups.append(
            (
                f"context-{chunk_number:04d}",
                f"Unstructured book context {chunk[0]}-{chunk[-1]}",
                [],
                chunk,
                chunk[0],
            )
        )

    module_groups.sort(key=lambda row: (row[4], row[1].casefold(), row[0]))
    modules: list[dict[str, Any]] = []
    page_to_module: dict[int, str] = {}
    segment_to_module: dict[str, str] = {}
    for _, title, segment_ids, pages, _ in module_groups:
        module_id = _module_id(source_sha256, title, segment_ids, pages)
        for page in pages:
            page_to_module.setdefault(page, module_id)
        for segment_id in segment_ids:
            segment_to_module[segment_id] = module_id
        modules.append(
            {
                "schema_version": MODULE_PLAN_SCHEMA,
                "module_id": module_id,
                "book_plan_id": "",
                "title": title,
                "segment_ids": segment_ids,
                "physical_pages": pages,
                "depends_on": [],
                "routes": {},
                "input_hashes": {},
            }
        )
    if set(page_numbers) != set(page_to_module):
        missing = sorted(set(page_numbers) - set(page_to_module))
        raise BookPlannerError(f"module_page_coverage_missing:{missing[:5]}")
    return modules, page_to_module, segment_to_module


def _finalize_module_rows(
    modules: list[dict[str, Any]],
    *,
    book_plan_id: str,
    source_sha256: str,
    segment_by_id: dict[str, dict[str, Any]],
    page_by_number: dict[int, dict[str, Any]],
    resolution_by_segment: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    finalized: list[dict[str, Any]] = []
    for module in modules:
        segment_ids = [str(value) for value in module.get("segment_ids", [])]
        pages = [int(value) for value in module.get("physical_pages", [])]
        statuses: list[str] = []
        for segment_id in segment_ids:
            segment = segment_by_id[segment_id]
            status, _ = _segment_planning_status(segment, page_by_number, resolution_by_segment)
            statuses.append(status)
        page_statuses = []
        for page in pages:
            row = page_by_number.get(page, {})
            if row.get("ocr_reasons"):
                page_statuses.append("ocr-candidate")
            elif row.get("page_disposition") == "manual-review-required":
                page_statuses.append("quarantined")
            elif row.get("visual_verification_reasons"):
                page_statuses.append("visual-review")
        all_statuses = statuses + page_statuses
        planning_status = "compile"
        for candidate in ("ocr-candidate", "quarantined", "gap", "visual-review", "non-knowledge", "context-only"):
            if candidate in all_statuses:
                planning_status = candidate
                break
        resolution = next(
            (
                resolution_by_segment.get(segment_id)
                for segment_id in segment_ids
                if resolution_by_segment.get(segment_id) is not None
            ),
            None,
        )
        module["book_plan_id"] = book_plan_id
        module["routes"] = _routes(planning_status=planning_status, resolution=resolution)
        module["input_hashes"] = {
            "source_sha256": source_sha256,
            "pages_sha256": sha256_json(
                [
                    {
                        "physical_page": page,
                        "native_text_sha256": page_by_number[page].get("native_text", {}).get("sha256"),
                    }
                    for page in pages
                ]
            ),
            "segments_sha256": sha256_json(
                [segment_by_id[segment_id] for segment_id in segment_ids]
            ),
            "heading_resolution_sha256": sha256_json(
                [resolution_by_segment.get(segment_id) for segment_id in segment_ids]
            ),
        }
        module["planning_status"] = planning_status
        module["promotion_status"] = _promotion_status(planning_status)
        module["module_sha256"] = _row_hash(module, "module_sha256")
        finalized.append(module)
    return finalized


def _coverage_row(
    *,
    book_plan_id: str,
    scope_kind: str,
    record_id: str,
    pages: list[int],
    planning_status: str,
    module_id: str,
    route_ids: list[str],
    input_sha256: str,
    rationale: str,
) -> dict[str, Any]:
    row = {
        "schema_version": COVERAGE_LEDGER_SCHEMA,
        "book_plan_id": book_plan_id,
        "record_id": record_id,
        "scope_kind": scope_kind,
        "physical_pages": sorted(set(pages)),
        "planning_status": planning_status,
        "promotion_status": _promotion_status(planning_status),
        "module_id": module_id,
        "route_ids": sorted(set(route_ids)),
        "input_sha256": input_sha256,
        "rationale": rationale,
    }
    row["coverage_sha256"] = _row_hash(row, "coverage_sha256")
    return row


def _routing_row(
    *,
    book_plan_id: str,
    scope_kind: str,
    record_id: str,
    pages: list[int],
    routes: dict[str, str],
    input_sha256: str,
) -> dict[str, Any]:
    route_id = stable_id("rrt", book_plan_id, scope_kind, record_id, pages, routes)
    row = {
        "schema_version": REVIEW_ROUTING_SCHEMA,
        "route_id": route_id,
        "book_plan_id": book_plan_id,
        "scope_kind": scope_kind,
        "record_id": record_id,
        "physical_pages": sorted(set(pages)),
        "routes": routes,
        "input_sha256": input_sha256,
    }
    row["routing_sha256"] = _row_hash(row, "routing_sha256")
    return row


def _artifact_info(output: Path, relative: str, record_count: int) -> dict[str, Any]:
    path = output / relative
    return {
        "path": relative,
        "sha256": sha256_file(path),
        "record_count": record_count,
    }


def build_book_plan(
    source_path: Path,
    output: Path,
    *,
    minimum_native_characters: int = 40,
    heading_overrides: Path | None = None,
    heading_locator_overrides: Path | None = None,
) -> dict[str, Any]:
    """Build a source-free plan directory from the complete PDF page scope."""

    source_path = source_path.expanduser().resolve()
    output = _ensure_empty_output(output)
    if not source_path.is_file():
        raise BookPlannerError("source_missing")
    source_sha256 = sha256_file(source_path)
    try:
        result = compile_pdf_intermediate_representation(
            source_path,
            1,
            None,
            minimum_native_characters=minimum_native_characters,
            anchor_strategy="all",
            layout_parser="pypdf",
            pdftoppm=None,
            heading_overrides=heading_overrides,
            heading_locator_overrides=heading_locator_overrides,
        )
    except PdfPipelineError as error:
        raise BookPlannerError(str(error)) from error

    preflight = result["preflight"]
    document_map = result["document_map"]
    pages = list(preflight.get("pages", []))
    segments = [
        segment
        for segment in document_map.get("segments", [])
        if isinstance(segment, dict) and isinstance(segment.get("id"), str)
    ]
    page_numbers = [int(page["physical_page"]) for page in pages]
    page_by_number = {int(page["physical_page"]): page for page in pages}
    if page_numbers != list(range(1, len(page_numbers) + 1)):
        raise BookPlannerError("physical_page_coverage_not_contiguous")
    page_texts = {
        int(page["physical_page"]): str(page.get("native_text", {}).get("sha256", ""))
        for page in pages
    }
    first_structured_page = min(
        (int(page) for segment in segments for page in segment.get("physical_pages", [])),
        default=None,
    )
    segment_by_id = {str(segment["id"]): segment for segment in segments}
    resolution_rows = [
        row for row in result.get("heading_resolutions", []) if isinstance(row, dict)
    ]
    resolution_by_segment = {
        str(row["segment_id"]): row
        for row in resolution_rows
        if isinstance(row.get("segment_id"), str)
    }

    heading_candidate_sha256 = sha256_json(result.get("heading_candidates", []))
    heading_resolution_sha256 = sha256_json(resolution_rows)
    heading_bundle_sha256 = locator_bundle_sha256(
        result.get("heading_candidates", []), resolution_rows
    )
    structure_unresolved = [
        str(row.get("resolution_id"))
        for row in resolution_rows
        if row.get("status") != "resolved" and isinstance(row.get("resolution_id"), str)
    ]

    modules, page_to_module, segment_to_module = _build_modules(
        source_sha256=source_sha256,
        segments=segments,
        page_numbers=page_numbers,
    )
    preliminary_identity = {
        "source_sha256": source_sha256,
        "page_count": len(page_numbers),
        "segment_ids": [str(segment["id"]) for segment in segments],
        "heading_bundle_sha256": heading_bundle_sha256,
        "protocol": WHOLE_BOOK_PLANNER_PROTOCOL,
    }
    book_plan_id = stable_id("bpl", preliminary_identity)
    modules = _finalize_module_rows(
        modules,
        book_plan_id=book_plan_id,
        source_sha256=source_sha256,
        segment_by_id=segment_by_id,
        page_by_number=page_by_number,
        resolution_by_segment=resolution_by_segment,
    )
    module_by_id = {str(module["module_id"]): module for module in modules}

    page_plans: dict[int, tuple[str, str]] = {}
    for page in pages:
        physical_page = int(page["physical_page"])
        text_marker = str(page.get("native_text", {}).get("sha256", ""))
        covered = any(physical_page in segment.get("physical_pages", []) for segment in segments)
        page_plans[physical_page] = _page_planning_status(
            page,
            covered_by_segment=covered,
            text=text_marker,
            first_structured_page=first_structured_page,
        )

    routes: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    page_route_ids: dict[int, list[str]] = {}
    for page in pages:
        physical_page = int(page["physical_page"])
        planning_status, rationale = page_plans[physical_page]
        resolution = next(
            (
                row
                for segment_id, row in resolution_by_segment.items()
                if physical_page in segment_by_id.get(segment_id, {}).get("physical_pages", [])
            ),
            None,
        )
        page_routes = _routes(planning_status=planning_status, resolution=resolution)
        input_sha = _input_hash(
            source_sha256,
            {
                "physical_page": physical_page,
                "native_text_sha256": page.get("native_text", {}).get("sha256"),
                "ocr_reasons": page.get("ocr_reasons", []),
                "visual_reasons": page.get("visual_verification_reasons", []),
            },
        )
        route = _routing_row(
            book_plan_id=book_plan_id,
            scope_kind="physical_page",
            record_id=f"page-{physical_page:04d}",
            pages=[physical_page],
            routes=page_routes,
            input_sha256=input_sha,
        )
        routes.append(route)
        page_route_ids[physical_page] = [route["route_id"]]
        coverage.append(
            _coverage_row(
                book_plan_id=book_plan_id,
                scope_kind="physical_page",
                record_id=f"page-{physical_page:04d}",
                pages=[physical_page],
                planning_status=planning_status,
                module_id=page_to_module[physical_page],
                route_ids=[route["route_id"]],
                input_sha256=input_sha,
                rationale=rationale,
            )
        )

    for segment in segments:
        segment_id = str(segment["id"])
        planning_status, rationale = _segment_planning_status(
            segment, page_by_number, resolution_by_segment
        )
        resolution = resolution_by_segment.get(segment_id)
        segment_routes = _routes(planning_status=planning_status, resolution=resolution)
        input_sha = _input_hash(
            source_sha256,
            {
                "segment": segment,
                "heading_resolution": resolution,
                "heading_candidate_sha256": heading_candidate_sha256,
            },
        )
        route = _routing_row(
            book_plan_id=book_plan_id,
            scope_kind="segment",
            record_id=segment_id,
            pages=[int(page) for page in segment.get("physical_pages", [])],
            routes=segment_routes,
            input_sha256=input_sha,
        )
        routes.append(route)
        coverage.append(
            _coverage_row(
                book_plan_id=book_plan_id,
                scope_kind="segment",
                record_id=segment_id,
                pages=[int(page) for page in segment.get("physical_pages", [])],
                planning_status=planning_status,
                module_id=segment_to_module.get(segment_id, page_to_module[int(segment["physical_pages"][0])]),
                route_ids=[route["route_id"]],
                input_sha256=input_sha,
                rationale=rationale,
            )
        )

    for module in modules:
        module_pages = [int(page) for page in module["physical_pages"]]
        route = _routing_row(
            book_plan_id=book_plan_id,
            scope_kind="module",
            record_id=str(module["module_id"]),
            pages=module_pages,
            routes=dict(module["routes"]),
            input_sha256=sha256_json(module["input_hashes"]),
        )
        routes.append(route)

    coverage.sort(key=lambda row: (row["scope_kind"], row["record_id"]))
    routes.sort(key=lambda row: (row["scope_kind"], row["record_id"], row["route_id"]))
    modules.sort(key=lambda row: (min(row["physical_pages"]), row["title"].casefold(), row["module_id"]))

    coverage_path = "book-coverage-ledger.jsonl"
    modules_path = "module-plan.jsonl"
    routes_path = "review-routing.jsonl"
    _write_bytes(output / coverage_path, _jsonl_bytes(coverage))
    _write_bytes(output / modules_path, _jsonl_bytes(modules))
    _write_bytes(output / routes_path, _jsonl_bytes(routes))
    plan = {
        "schema_version": BOOK_PLAN_SCHEMA,
        "book_plan_id": book_plan_id,
        "source": {
            "source_id": str(result["source"]["source_id"]),
            "sha256": source_sha256,
            "page_count": len(page_numbers),
        },
        "compiler": {
            "planner_version": WHOLE_BOOK_PLANNER_COMPILER_VERSION,
            "protocol": WHOLE_BOOK_PLANNER_PROTOCOL,
            "heading_locator_protocol": HEADING_LOCATOR_PROTOCOL,
            "pypdf": _pypdf_version(preflight),
        },
        "structure": {
            "status": "heading_resolution_required" if structure_unresolved else "resolved",
            "segment_count": len(segments),
            "unresolved_resolution_ids": structure_unresolved,
            "heading_candidate_sha256": heading_candidate_sha256,
            "heading_resolution_sha256": heading_resolution_sha256,
            "heading_bundle_sha256": heading_bundle_sha256,
        },
        "counts": {
            "physical_pages": len(page_numbers),
            "segments": len(segments),
            "modules": len(modules),
            "coverage_rows": len(coverage),
            "review_routes": len(routes),
        },
        "planning_states": list(PLANNING_STATES),
        "artifacts": {
            coverage_path: _artifact_info(output, coverage_path, len(coverage)),
            modules_path: _artifact_info(output, modules_path, len(modules)),
            routes_path: _artifact_info(output, routes_path, len(routes)),
        },
        "policy": {
            "source_text_embedded": False,
            "ocr_invocation": False,
            "model_launch": False,
            "semantic_review_authoring": False,
            "visual_review_authoring": False,
            "competency_authoring": False,
            "auto_seal": False,
            "auto_execution": False,
            "whole_book_semantic_completeness": False,
        },
        "whole_book_complete_claimed": False,
    }
    plan["plan_sha256"] = _row_hash(plan, "plan_sha256")
    _write_bytes(output / "book-plan.json", _json_bytes(plan))
    return plan


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise BookPlannerError(f"artifact_missing:{path}") from error
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise BookPlannerError(f"artifact_json_invalid:{path}:{line_number}") from error
        if not isinstance(value, dict):
            raise BookPlannerError(f"artifact_row_invalid:{path}:{line_number}")
        rows.append(value)
    return rows


def load_book_plan(output: Path, *, verify_source: Path | None = None) -> dict[str, Any]:
    output = output.expanduser().resolve()
    try:
        plan = json.loads((output / "book-plan.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BookPlannerError("book_plan_invalid") from error
    if not isinstance(plan, dict) or plan.get("schema_version") != BOOK_PLAN_SCHEMA:
        raise BookPlannerError("book_plan_schema_invalid")
    if plan.get("plan_sha256") != _row_hash(plan, "plan_sha256"):
        raise BookPlannerError("book_plan_hash_mismatch")
    if plan.get("whole_book_complete_claimed") is not False:
        raise BookPlannerError("whole_book_completion_claim_invalid")
    if verify_source is not None:
        source = verify_source.expanduser().resolve()
        if not source.is_file() or sha256_file(source) != plan.get("source", {}).get("sha256"):
            raise BookPlannerError("book_plan_source_hash_mismatch")
    artifacts = plan.get("artifacts")
    if not isinstance(artifacts, dict):
        raise BookPlannerError("book_plan_artifacts_invalid")
    loaded: dict[str, list[dict[str, Any]]] = {}
    for relative, info in artifacts.items():
        if not isinstance(info, dict) or not isinstance(relative, str):
            raise BookPlannerError("book_plan_artifact_info_invalid")
        path = output / relative
        if not path.is_file() or sha256_file(path) != info.get("sha256"):
            raise BookPlannerError(f"book_plan_artifact_hash_mismatch:{relative}")
        rows = _load_jsonl(path)
        if len(rows) != int(info.get("record_count", -1)):
            raise BookPlannerError(f"book_plan_artifact_count_mismatch:{relative}")
        loaded[relative] = rows
    coverage = loaded.get("book-coverage-ledger.jsonl", [])
    modules = loaded.get("module-plan.jsonl", [])
    routes = loaded.get("review-routing.jsonl", [])
    counts = plan.get("counts", {})
    if len(coverage) != counts.get("coverage_rows") or len(modules) != counts.get("modules") or len(routes) != counts.get("review_routes"):
        raise BookPlannerError("book_plan_counts_mismatch")
    pages = [
        row for row in coverage if row.get("scope_kind") == "physical_page"
    ]
    expected_pages = int(plan.get("source", {}).get("page_count", 0))
    page_numbers = sorted(
        int(row["physical_pages"][0])
        for row in pages
        if isinstance(row.get("physical_pages"), list) and len(row["physical_pages"]) == 1
    )
    if page_numbers != list(range(1, expected_pages + 1)):
        raise BookPlannerError("book_plan_physical_page_coverage_invalid")
    for row in coverage:
        if row.get("coverage_sha256") != _row_hash(row, "coverage_sha256"):
            raise BookPlannerError(f"book_plan_coverage_hash_mismatch:{row.get('record_id')}")
    for row in modules:
        if row.get("module_sha256") != _row_hash(row, "module_sha256"):
            raise BookPlannerError(f"book_plan_module_hash_mismatch:{row.get('module_id')}")
    for row in routes:
        if row.get("routing_sha256") != _row_hash(row, "routing_sha256"):
            raise BookPlannerError(f"book_plan_route_hash_mismatch:{row.get('route_id')}")
    return {"plan": plan, "coverage": coverage, "modules": modules, "routes": routes}


def summarize_book_plan(output: Path, *, source: Path | None = None) -> dict[str, Any]:
    loaded = load_book_plan(output, verify_source=source)
    plan = loaded["plan"]
    coverage = loaded["coverage"]
    modules = loaded["modules"]
    unresolved = plan["structure"].get("unresolved_resolution_ids", [])
    status_counts: dict[str, int] = {}
    for row in coverage:
        status = str(row.get("planning_status"))
        status_counts[status] = status_counts.get(status, 0) + 1
    if unresolved:
        next_action = "heading_resolution_required"
    elif any(row.get("planning_status") == "ocr-candidate" for row in coverage):
        next_action = "ocr_candidate_review_required"
    elif any(row.get("planning_status") == "visual-review" for row in coverage):
        next_action = "visual_review_required"
    else:
        next_action = "independent_semantic_review_required"
    return {
        "state": "planned",
        "next_action": next_action,
        "book_plan_id": plan["book_plan_id"],
        "whole_book_complete_claimed": False,
        "structure_status": plan["structure"]["status"],
        "counts": plan["counts"],
        "planning_status_counts": status_counts,
        "module_status_counts": {
            status: sum(1 for module in modules if module.get("planning_status") == status)
            for status in PLANNING_STATES
            if any(module.get("planning_status") == status for module in modules)
        },
        "gates": {
            "ocr_invocation": "paused",
            "semantic_review": "paused",
            "visual_review": "paused",
            "competency": "paused",
            "seal": "paused",
            "execution": "forbidden",
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--heading-overrides", type=Path)
    parser.add_argument("--heading-locator-overrides", type=Path)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.status:
            payload: Any = summarize_book_plan(args.output, source=args.source)
        else:
            payload = build_book_plan(
                args.source,
                args.output,
                heading_overrides=args.heading_overrides,
                heading_locator_overrides=args.heading_locator_overrides,
            )
            payload = {"passed": True, "plan": payload, "status": summarize_book_plan(args.output, source=args.source)}
    except (BookPlannerError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        status = payload.get("status", payload)
        print(f"PASS state={status.get('state')} next_action={status.get('next_action')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

