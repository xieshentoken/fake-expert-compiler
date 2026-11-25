"""Closed intake wire shapes and physical-page accounting invariants."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from native_blank_pages import native_ranges, summarize_page_diagnostics
from compiler_version import LEGACY_INTAKE_COMPILER_VERSION, LEGACY_INTAKE_WORKFLOW_SCHEMA
from scanned_pdf_ocr import validate_local_json_schema

SCHEMAS = Path(__file__).resolve().parents[1] / "assets/schemas"


def validate_historical_workflow(value: dict[str, Any]) -> None:
    if value.get("schema_version") == "tkc.intake-workflow/v0.1":
        schema = json.loads((SCHEMAS / "intake-workflow-v0.1.schema.json").read_text())
    elif value.get("schema_version") == LEGACY_INTAKE_WORKFLOW_SCHEMA:
        # v0.3 adds one closed top-level field. Validate the exact old shape
        # without migrating it or duplicating the complete schema.
        schema = json.loads((SCHEMAS / "intake-workflow.schema.json").read_text())
        schema["required"].remove("native_focus")
        schema["properties"].pop("native_focus")
        schema["$defs"]["package"]["properties"].pop("term_aliases")
        schema["properties"]["schema_version"]["const"] = LEGACY_INTAKE_WORKFLOW_SCHEMA
        schema["properties"]["protocol"]["const"] = "intake-workflow-v0.2"
        schema["properties"]["compiler_version"]["const"] = LEGACY_INTAKE_COMPILER_VERSION
        schema["$defs"]["identity"]["properties"]["intake_version"]["const"] = LEGACY_INTAKE_COMPILER_VERSION
    else:
        raise ValueError("intake_workflow_schema_invalid")
    validate_local_json_schema(value, schema)


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
    start,end=intake["selection"]["physical_pages"]
    if not 1 <= start <= end <= source["page_count"]:
        raise ValueError("intake_selected_scope_invalid")
    selected=[row for row in rows if start <= row["physical_page"] <= end]
    ranges = native_ranges(selected)
    expected_selection={"physical_pages":[start,end],"page_summary":summarize_page_diagnostics(selected),
                        "outside_pages":[row["physical_page"] for row in rows if not start <= row["physical_page"] <= end],
                        "knowledge_candidate_pages":[row["physical_page"] for row in selected if row["classification"]=="native" and not row.get("disposition_proof")],
                        "non_knowledge_pages":[row["physical_page"] for row in selected if row["classification"]=="confirmed_blank" or row.get("disposition_proof")],
                        "native_selected_scope":ranges==[[start,end]]}
    for row in rows:
        expected_state=("outside-scope" if not start<=row["physical_page"]<=end else
                        "selected-nonknowledge" if row["classification"]=="confirmed_blank" or row.get("disposition_proof") else
                        "selected-knowledge-candidate" if row["classification"]=="native" else "selected-unresolved")
        if row.get("selection_state")!=expected_state:
            raise ValueError("intake_page_selection_state_invalid")
    if intake["selection"]!=expected_selection:
        raise ValueError("intake_selection_accounting_invalid")
    aliases = value["package"].get("term_aliases")
    if aliases:
        from reference_alias_profiles import merged_aliases
        if not expected_selection["knowledge_candidate_pages"] or not Path(aliases["path"]).is_absolute():
            raise ValueError("term_aliases_binding_invalid")
        merged_aliases(aliases["extensions"])
    focus = value["native_focus"]
    if focus["structure_selections"] and (not focus["structure_selections"]["sha256"]
            or value["reused_workpacks"] or not expected_selection["native_selected_scope"]
            or not expected_selection["knowledge_candidate_pages"]):
        raise ValueError("structure_selections_require_unbound_native_scope")
    preview = focus["preview"]
    if preview and ((preview["status"] == "ready" and (not preview["unit_count"] or preview["reason"] is not None
                                                        or not preview["focus_plan_sha256"]))
                    or (preview["status"] == "review_required" and (not preview["reason"] or preview["unit_count"]
                                                                   or preview["focus_plan_sha256"] is not None))
                    or preview["split_segments"] > preview["unit_count"]):
        raise ValueError("native_focus_preview_invalid")
    if intake["page_summary"] != summary or intake["confirmed_blank_pages"] != summary["pages"]["confirmed_blank"] or intake["native_ranges"] != ranges:
        raise ValueError("intake_page_summary_mismatch")
    included = {page for start, end in ranges for page in range(start, end + 1)}
    omitted = [{key: row[key] for key in ("physical_page", "classification", "reason_codes")}
               for row in rows if row["physical_page"] not in included]
    if intake["omitted_pages_for_native_only"] != omitted:
        raise ValueError("intake_omitted_page_ledger_mismatch")
    advice = intake["batch_advice"]
    ocr_pages = expected_selection["page_summary"]["pages"]["needs_ocr"]
    budget = 5 if len(ocr_pages) <= 20 else 20
    if advice["ocr_page_count"] != len(ocr_pages) or advice["suggested_batch_size"] != budget or advice["batches"] != [ocr_pages[index:index + budget] for index in range(0, len(ocr_pages), budget)]:
        raise ValueError("intake_ocr_batch_accounting_invalid")
    boundaries = [start, *[batch[0] for batch in advice["batches"][1:]], end + 1]
    if advice["physical_scopes"] != [[boundaries[index], boundaries[index + 1] - 1] for index in range(len(advice["batches"]))]:
        raise ValueError("intake_ocr_scope_accounting_invalid")
    if value["bindings"]["base"]["source_sha256"] != source["sha256"]:
        raise ValueError("intake_source_binding_invalid")
    if value["route"] == "native_ranges" and value["choice"] != "native_ranges":
        raise ValueError("intake_partial_choice_missing")
    if value["route"] == "native" and not intake["selection"]["native_selected_scope"]:
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
        if (child["physical_pages"] not in ranges and child["physical_pages"] not in [b["physical_pages"] for b in value["reused_workpacks"]]) or child["physical_pages"] in scopes:
            raise ValueError("intake_native_child_scope_invalid")
        scopes.append(child["physical_pages"])
        for field in ("child", "workpack", "package", "audit_path"):
            if field=="workpack" and child.get(field) in [b["path"] for b in value["reused_workpacks"]]:
                continue
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
