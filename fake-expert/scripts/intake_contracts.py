"""Closed intake wire shapes and physical-page accounting invariants."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from native_blank_pages import native_ranges, summarize_page_diagnostics
from scanned_pdf_ocr import validate_local_json_schema

SCHEMAS = Path(__file__).resolve().parents[1] / "assets/schemas"


def validate_definition(value: Any, definition: str | None = None) -> None:
    schema = json.loads((SCHEMAS / "intake-workflow.schema.json").read_text())
    selected = {"$ref": f"#/$defs/{definition}", "$defs": schema["$defs"]} if definition else schema
    try:
        validate_local_json_schema(value, selected)
    except RuntimeError as error:
        raise ValueError(str(error).replace("scan_ir_schema_invalid", "intake_workflow_schema_invalid", 1)) from error


def validate_ocr_config(value: Any) -> None:
    schema = json.loads((SCHEMAS / "ocr-local-config.schema.json").read_text())
    try:
        validate_local_json_schema(value, schema)
    except RuntimeError as error:
        raise ValueError(str(error).replace("scan_ir_schema_invalid", "ocr_config_schema_invalid", 1)) from error


def validate_workflow(value: dict[str, Any]) -> None:
    validate_definition(value)
    source, intake = value["source"], value["intake"]
    rows = intake["page_ledger"]
    if source != intake["source"] or [row["physical_page"] for row in rows] != list(range(1, source["page_count"] + 1)):
        raise ValueError("intake_page_ledger_coverage_invalid")
    for row in rows:
        proof = row["proof"]
        if row["source_sha256"] != source["sha256"] or (proof is not None) != (row["classification"] == "confirmed_blank"):
            raise ValueError("intake_page_diagnostic_binding_invalid")
        if proof and (proof["source_sha256"] != source["sha256"] or proof["physical_page"] != row["physical_page"]):
            raise ValueError("intake_blank_proof_binding_invalid")
    summary = summarize_page_diagnostics(rows)
    ranges = native_ranges(rows)
    if intake["page_summary"] != summary or intake["confirmed_blank_pages"] != summary["pages"]["confirmed_blank"] or intake["native_ranges"] != ranges:
        raise ValueError("intake_page_summary_mismatch")
    included = {page for start, end in ranges for page in range(start, end + 1)}
    omitted = [{key: row[key] for key in ("physical_page", "classification", "reason_codes")}
               for row in rows if row["physical_page"] not in included]
    if intake["omitted_pages_for_native_only"] != omitted:
        raise ValueError("intake_omitted_page_ledger_mismatch")
    advice = intake["batch_advice"]
    ocr_pages = summary["pages"]["needs_ocr"]
    budget = 5 if len(ocr_pages) <= 20 else 20
    if advice["ocr_page_count"] != len(ocr_pages) or advice["suggested_batch_size"] != budget or advice["batches"] != [ocr_pages[index:index + budget] for index in range(0, len(ocr_pages), budget)]:
        raise ValueError("intake_ocr_batch_accounting_invalid")
    boundaries = [1, *[batch[0] for batch in advice["batches"][1:]], source["page_count"] + 1]
    if advice["physical_scopes"] != [[boundaries[index], boundaries[index + 1] - 1] for index in range(len(advice["batches"]))]:
        raise ValueError("intake_ocr_scope_accounting_invalid")
    if value["bindings"]["base"]["source_sha256"] != source["sha256"]:
        raise ValueError("intake_source_binding_invalid")
    if value["route"] == "native_ranges" and value["choice"] != "native_ranges":
        raise ValueError("intake_partial_choice_missing")
    if value["route"] == "native" and not intake["native_whole_source"]:
        raise ValueError("intake_native_whole_source_invalid")
    if value["route"] == "ocr" and (not ocr_pages or not value["ocr_config"] or not value["availability"]["runnable"]):
        raise ValueError("intake_ocr_route_invalid")
    authorization = value["authorization"]
    if authorization and (authorization["source_sha256"] != source["sha256"] or value["ocr_config"] is None
                          or authorization["ocr_config_sha256"] != value["ocr_config"]["sha256"]
                          or authorization["backend"] != value["availability"]["selected_backend"]):
        raise ValueError("intake_ocr_authorization_binding_invalid")
    workspace = Path(value["workspace"]).resolve()
    scopes = []
    for child in value["state"]["children"]:
        if child["physical_pages"] not in ranges or child["physical_pages"] in scopes:
            raise ValueError("intake_native_child_scope_invalid")
        scopes.append(child["physical_pages"])
        for field in ("child", "workpack", "package", "audit_path"):
            if child.get(field) and not Path(child[field]).resolve().is_relative_to(workspace):
                raise ValueError("intake_native_child_path_escape")
    for attempt in value["state"]["scan_attempts"]:
        if attempt["batch_index"] >= len(advice["physical_scopes"]) or attempt["scope"] != advice["physical_scopes"][attempt["batch_index"]]:
            raise ValueError("intake_scan_attempt_scope_invalid")
        if not set(attempt["completed_pages"]).issubset(advice["batches"][attempt["batch_index"]]):
            raise ValueError("intake_scan_completed_pages_invalid")
        if not Path(attempt["path"]).resolve().is_relative_to(workspace / "scan-attempts"):
            raise ValueError("intake_scan_attempt_path_escape")
        if any(Path(name).is_absolute() or ".." in Path(name).parts for name in attempt["files"]):
            raise ValueError("intake_scan_inventory_path_escape")
