"""Read-only physical-page intake, route accounting and bounded batch advice."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from compiler_version import INTAKE_COMPILER_VERSION, INTAKE_REPORT_SCHEMA, RELEASE_VERSION
from environment_preflight import inspect_native_source
from ocr_availability import inspect_renderer
from native_blank_pages import native_ranges, summarize_page_diagnostics


def build_identity() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted([*root.joinpath("scripts").glob("*.py"), root / "SKILL.md"]):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return {"active_skill_root": str(root), "release_version": RELEASE_VERSION,
            "intake_version": INTAKE_COMPILER_VERSION, "build_fingerprint": digest.hexdigest(),
            "capabilities": {"automatic_page_intake": True, "opaque_white_vector_blank_proof": True,
                             "scoped_native_children": True, "ocr_backends": ["paddleocr-ppocrv6", "paddleocr-ppstructure-v3"],
                             "ocr_during_inspection": False, "network_fallback": False}}


def inspect_source(source: Path, *, renderer: str = "pdftoppm", minimum_characters: int = 40, start_page: int = 1, end_page: int | None = None, page_dispositions=None) -> dict[str, Any]:
    report = inspect_native_source(str(source), minimum_characters, renderer, page_dispositions)
    if not report.get("page_count") or "page_summary" not in report:
        return {"schema_version": INTAKE_REPORT_SCHEMA, "status": "rejected", "code": report["code"],
                "source_check": report, "identity": build_identity()}
    rows = report["page_diagnostics"]
    summary = report["page_summary"]
    end_page = report["page_count"] if end_page is None else end_page
    if type(start_page) is not int or type(end_page) is not int or not 1 <= start_page <= end_page <= report["page_count"]:
        raise ValueError("intake_selected_scope_invalid")
    # These existing book-planner patterns only label navigation/context hints;
    # the explicit scope/operator receipt remains the exclusion authority.
    from book_planner import NON_KNOWLEDGE_RE, FRONT_MATTER_RE
    from pdf_structure import flatten_outline
    from pypdf import PdfReader
    outline=sorted(flatten_outline(PdfReader(source)),key=lambda node:node.start_page)
    for index,node in enumerate(outline):
        hint = "frontmatter" if FRONT_MATTER_RE.search(node.title) else "backmatter-or-navigation" if NON_KNOWLEDGE_RE.search(node.title) else None
        if hint:
            stop=outline[index+1].start_page if index+1<len(outline) else len(rows)+1
            for row in rows:
                if node.start_page <= row["physical_page"] < stop:
                    row["outline_context_hint"]={"role":hint,"outline_page":node.start_page,"advisory_only":True}
    for row in rows:
        row["selection_state"]=("outside-scope" if not start_page <= row["physical_page"] <= end_page else
                                "selected-nonknowledge" if row["classification"]=="confirmed_blank" or row.get("disposition_proof") else
                                "selected-knowledge-candidate" if row["classification"]=="native" else "selected-unresolved")
    selected = [row for row in rows if start_page <= row["physical_page"] <= end_page]
    selected_summary = summarize_page_diagnostics(selected)
    ranges = native_ranges(selected)
    included = {page for start, end in ranges for page in range(start, end + 1)}
    omitted = [{"physical_page": row["physical_page"], "classification": row["classification"],
                "reason_codes": row["reason_codes"] if row["physical_page"] not in included else []}
               for row in rows if row["physical_page"] not in included]
    ocr_pages = selected_summary["pages"]["needs_ocr"]
    batch_size = 5 if len(ocr_pages) <= 20 else 20
    batches = [ocr_pages[index:index + batch_size] for index in range(0, len(ocr_pages), batch_size)]
    boundaries = [start_page, *[batch[0] for batch in batches[1:]], end_page + 1]
    physical_scopes = [[boundaries[index], boundaries[index + 1] - 1] for index in range(len(batches))]
    title_hint = None
    try:
        from pypdf import PdfReader
        text = PdfReader(source).pages[0].extract_text() or ""
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if 2 <= len(first_line) <= 160:
            title_hint = first_line
    except (OSError, ValueError, IndexError):
        pass
    return {
        "schema_version": INTAKE_REPORT_SCHEMA, "status": "inspected", "code": "intake_inspected",
        "identity": build_identity(), "source": {"path": report["path"], "sha256": report["sha256"],
                                                   "page_count": report["page_count"]},
        "renderer": {"requested": renderer, **inspect_renderer(renderer)},
        "source_hints": {"cover_title": title_hint, "origin": "native_cover_first_line" if title_hint else "unknown",
                         "untrusted": True, "advisory_only": True, "backend_selection_authority": False},
        "selection": {"physical_pages":[start_page,end_page], "page_summary":selected_summary,
                      "outside_pages":[row["physical_page"] for row in rows if not start_page <= row["physical_page"] <= end_page],
                      "knowledge_candidate_pages":[row["physical_page"] for row in selected if row["classification"]=="native" and not row.get("disposition_proof")],
                      "non_knowledge_pages":[row["physical_page"] for row in selected if row["classification"]=="confirmed_blank" or row.get("disposition_proof")],
                      "native_selected_scope": ranges==[[start_page,end_page]]},
        "minimum_native_characters": minimum_characters, "page_ledger": rows,
        "page_summary": summary, "confirmed_blank_pages": summary["pages"]["confirmed_blank"],
        "native_ranges": ranges, "omitted_pages_for_native_only": omitted,
        "native_whole_source": bool(report["passed"]),
        "model_selection": {"selected_backend": None, "reason": "explicit_configuration_required_for_ocr",
                            "title_decides_backend": False},
        "batch_advice": {"ocr_page_count": len(ocr_pages), "suggested_batch_size": batch_size,
                         "batches": batches, "physical_scopes": physical_scopes,
                         "basis": "only_pages_classified_needs_ocr", "serial_workers": True,
                         "checkpoint": "reuse_only_complete_source_config_tool_model_bound_receipts"},
        "source_content_is_untrusted": True, "whole_book_complete": False,
    }
