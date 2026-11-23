"""Versioned intake choices and bounded local execution, separate from v0.2 jobs."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from compiler_version import INTAKE_COMPILER_VERSION, INTAKE_WORKFLOW_PROTOCOL, INTAKE_WORKFLOW_SCHEMA
from native_intake import inspect_source
from ocr_availability import availability_summary, inspect_ocr, inspect_renderer, load_config, resolve_config_path
from pdf_parser_adapters import sha256_json
from scoped_native_workflow import run_native_child

SCRIPTS = Path(__file__).resolve().parent
WORKFLOW_FILE = "intake-workflow.json"
CHOICES = ("configure_environment", "native_ranges", "inspection_only", "review_source")
TOP_KEYS = {"schema_version", "protocol", "compiler_version", "workflow_id", "workflow_sha256", "workspace",
            "source", "intake", "ocr_config", "availability", "capability", "renderer", "minimum_native_characters",
            "package", "bindings", "choice", "route", "state", "authorization"}


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _save(path: Path, record: dict[str, Any]) -> None:
    record["workflow_sha256"] = sha256_json({key: value for key, value in record.items() if key != "workflow_sha256"})
    from intake_contracts import validate_workflow
    validate_workflow(record)
    target = path.with_name(path.name + ".tmp")
    target.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(target, 0o600)
    target.replace(path)


def _config_record(path: Path | None) -> dict[str, str] | None:
    if path is None:
        return None
    absolute = path.expanduser().absolute()
    return {"path": str(absolute), "sha256": _digest(absolute) if absolute.is_file() else None}


def _ocr_bindings(config_record: dict[str, str] | None, availability: dict[str, Any]) -> dict[str, Any]:
    files: dict[str, str] = {}
    if config_record:
        path = Path(config_record["path"])
        if path.is_file():
            files[str(path)] = _digest(path)
        try:
            config = load_config(path)
        except (OSError, ValueError):
            config = {}
        for key in ("backend_config", "qualification_plan", "qualification_receipt", "worker"):
            if config.get(key) and Path(config[key]).is_file():
                files[config[key]] = _digest(Path(config[key]))
    interpreter = availability.get("interpreter", {})
    model_checks = [row for row in availability.get("checks", []) if row["code"] == "model_available"]
    fingerprint = {"binary": interpreter.get("binary_sha256"), "launcher": interpreter.get("launcher"),
                   "launcher_link": interpreter.get("launcher_link"), "distributions": interpreter.get("distributions"),
                   "model": model_checks[0].get("details", {}).get("stable_identity_sha256") if model_checks else None,
                   "qualification": next((row.get("receipt_sha256") for row in availability.get("checks", []) if row["code"] == "qualification_valid"), None)}
    return {"files": files, "runtime_fingerprint": fingerprint, "was_runnable": availability.get("runnable", False)}


def _base_bindings(source: dict[str, Any], renderer: str) -> dict[str, Any]:
    tools = {path.name: _digest(path) for path in sorted(SCRIPTS.glob("*.py"))}
    references = [SCRIPTS.parent / "SKILL.md", *[SCRIPTS.parent / "references" / name for name in
                  ("quickstart.md", "troubleshooting.md", "pdf-intermediate-representation.md", "scanned-pdf-ocr.md")]]
    schemas = {path.name: _digest(path) for path in (SCRIPTS.parent / "assets/schemas").glob("*intake*.json")}
    schemas.update({path.name: _digest(path) for path in (SCRIPTS.parent / "assets/schemas").glob("*ocr-local-config*.json")})
    schemas.update({path.name: _digest(path) for path in (SCRIPTS.parent / "assets/schemas").glob("*scoped-native-child*.json")})
    return {"source_sha256": _digest(Path(source["path"])), "tools": tools, "schemas": schemas,
            "references": {str(path): _digest(path) for path in references}, "renderer": inspect_renderer(renderer)}


def inspect(source: Path, *, config: Path | None = None, renderer: str = "pdftoppm",
            capability: str = "text", minimum_characters: int = 40) -> dict[str, Any]:
    report = inspect_source(source, renderer=renderer, minimum_characters=minimum_characters)
    if report["status"] == "rejected":
        return report
    if report["page_summary"]["counts"]["needs_ocr"]:
        availability = inspect_ocr(config, renderer=renderer, capability=capability)
        report["model_selection"] = {"selected_backend": availability["selected_backend"],
                                     "reason": availability["selection_reason"], "title_decides_backend": False}
    else:
        availability = {"status": "not_required", "runnable": False, "checks": [], "selected_backend": None,
                        "selection_reason": "no_pages_classified_needs_ocr", "ocr_invoked": False}
    report["ocr_availability"] = availability_summary(availability)
    return report


def _options(record: dict[str, Any]) -> list[dict[str, str]]:
    counts = record["intake"]["page_summary"]["counts"]
    options = []
    if counts["needs_ocr"]:
        options.append({"choice": "configure_environment", "meaning": "Configure or repair the explicit local OCR runtime, then rebind an OCR config; no installation is automatic."})
    if counts["review_required"]:
        options.append({"choice": "review_source", "meaning": "Retain the original physical pages and inspect the stated uncertainty; no blank exemption or reviewer answer is fabricated."})
    if record["intake"]["native_ranges"]:
        options.append({"choice": "native_ranges", "meaning": "Create separate native children only for the listed continuous native+confirmed-blank ranges; omitted pages remain gaps."})
    options.append({"choice": "inspection_only", "meaning": "Keep the complete page checklist without creating any knowledge package."})
    return options


def _set_route(record: dict[str, Any]) -> None:
    counts = record["intake"]["page_summary"]["counts"]
    choice = record["choice"]
    if not counts["native"] and not counts["needs_ocr"] and not counts["review_required"]:
        route, status, code = "inspection_only", "reported", "no_content"
    elif choice == "inspection_only":
        route, status, code = "inspection_only", "reported", "inspection_only_selected"
    elif choice == "native_ranges":
        if not record["intake"]["native_ranges"]:
            raise ValueError("native_ranges_unavailable")
        route, status, code = "native_ranges", "planned", "partial_native_ranges_selected"
    elif choice == "review_source":
        route, status, code = "waiting", "awaiting_review", "page_review_required"
    elif record["intake"]["native_whole_source"]:
        route, status, code = "native", "planned", "native_intake_ready"
    elif counts["needs_ocr"] and record["availability"].get("runnable"):
        route, status, code = "ocr", "planned", "ocr_execution_authorization_required"
    elif choice == "configure_environment":
        route, status, code = "waiting", "awaiting_environment", "ocr_environment_unavailable"
    else:
        route, status, code = "waiting", "awaiting_choice", "ocr_environment_unavailable" if counts["needs_ocr"] else "page_review_required"
    record["route"] = route
    record["state"].update(status=status, code=code)


def plan(args: argparse.Namespace) -> dict[str, Any]:
    workspace = args.workspace.expanduser().absolute()
    if workspace.is_symlink() or workspace in {Path("/"), Path.home()}:
        raise ValueError("intake_workspace_invalid")
    if workspace.exists() and (not workspace.is_dir() or any(workspace.iterdir())):
        raise ValueError("intake_workspace_not_empty")
    source = args.source.expanduser().resolve()
    renderer = str(getattr(args, "pdftoppm", "pdftoppm"))
    config = resolve_config_path(getattr(args, "ocr_config", None))
    capability = getattr(args, "capability", "text")
    minimum = getattr(args, "minimum_native_characters", 40)
    report = inspect(source, config=config, renderer=renderer, capability=capability, minimum_characters=minimum)
    if report["status"] == "rejected":
        return report
    package = {"name": args.name, "display_name": args.display_name, "domain": args.domain,
               "version": args.version, "created_at": args.created_at, "figures": getattr(args, "figures", None) or "index+images"}
    config_record = _config_record(config)
    record = {"schema_version": INTAKE_WORKFLOW_SCHEMA, "protocol": INTAKE_WORKFLOW_PROTOCOL,
              "compiler_version": INTAKE_COMPILER_VERSION, "workflow_id": "itw-" + sha256_json({"source": report["source"], "workspace": str(workspace.resolve()), "package": package})[:24],
              "workflow_sha256": "", "workspace": str(workspace.resolve()), "source": report["source"], "intake": report,
              "ocr_config": config_record, "availability": report["ocr_availability"], "capability": capability,
              "renderer": renderer, "minimum_native_characters": minimum, "package": package,
              "bindings": {"base": _base_bindings(report["source"], renderer),
                           "ocr": _ocr_bindings(config_record, report["ocr_availability"])},
              "choice": None, "route": "waiting", "state": {"status": "planned", "code": "intake_planned", "children": [], "scan_attempts": []},
              "authorization": None}
    _set_route(record)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{workspace.name}-intake-", dir=workspace.parent) as temporary:
        stage = Path(temporary) / "workspace"
        stage.mkdir()
        _save(stage / WORKFLOW_FILE, record)
        stage.rename(workspace)
    return summary(record)


def load(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("intake_workflow_symlink_forbidden")
    record = json.loads(path.read_text())
    if not isinstance(record, dict) or set(record) != TOP_KEYS or record.get("schema_version") != INTAKE_WORKFLOW_SCHEMA or record.get("protocol") != INTAKE_WORKFLOW_PROTOCOL:
        raise ValueError("intake_workflow_schema_invalid")
    if record["workflow_sha256"] != sha256_json({key: value for key, value in record.items() if key != "workflow_sha256"}):
        raise ValueError("intake_workflow_hash_mismatch")
    from intake_contracts import validate_workflow
    validate_workflow(record)
    if path.resolve() != Path(record["workspace"]) / WORKFLOW_FILE:
        raise ValueError("intake_workflow_workspace_mismatch")
    if record["choice"] is not None and record["choice"] not in CHOICES:
        raise ValueError("intake_workflow_choice_invalid")
    return record


def _drift(record: dict[str, Any], *, allow_ocr_rebind: bool = False) -> list[str]:
    issues = []
    try:
        if _base_bindings(record["source"], record["renderer"]) != record["bindings"]["base"]:
            issues.append("source_tool_document_or_renderer_drift")
        if not allow_ocr_rebind and record["ocr_config"] and record["intake"]["page_summary"]["counts"]["needs_ocr"]:
            current = inspect_ocr(Path(record["ocr_config"]["path"]), renderer=record["renderer"], capability=record["capability"])
            if _ocr_bindings(record["ocr_config"], current) != record["bindings"]["ocr"]:
                issues.append("ocr_config_qualification_model_or_interpreter_drift")
        for attempt in record["state"]["scan_attempts"]:
            _check_attempt_files(attempt)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        issues.append("intake_bound_input_unavailable")
    return issues


def summary(record: dict[str, Any]) -> dict[str, Any]:
    partial = record["route"] == "native_ranges"
    return {"status": record["state"]["status"], "code": record["state"]["code"], "schema_version": record["schema_version"],
            "workflow": str(Path(record["workspace"]) / WORKFLOW_FILE), "workflow_id": record["workflow_id"],
            "source": record["source"], "route": record["route"], "choice": record["choice"],
            "page_summary": record["intake"]["page_summary"], "page_ledger": record["intake"]["page_ledger"],
            "confirmed_blank_pages": record["intake"]["confirmed_blank_pages"],
            "native_ranges": record["intake"]["native_ranges"], "partial": partial,
            "omitted_pages": record["intake"]["omitted_pages_for_native_only"] if partial else [],
            "ocr_availability": record["availability"], "batch_advice": record["intake"]["batch_advice"],
            "choice_request": {"id": record["workflow_id"], "status": "pending" if record["state"]["status"] == "awaiting_choice" else "resolved",
                               "repeat_prompt": False, "options": _options(record)},
            "children": record["state"]["children"], "scan_attempts": record["state"]["scan_attempts"],
            "ocr_progress": record["state"].get("ocr_progress"),
            "next_action": record["state"].get("next_action"),
            "failure_detail": record["state"].get("failure_detail"),
            "whole_book_complete": False, "knowledge_verified": False}


def status(path: Path) -> dict[str, Any]:
    record = load(path)
    drift = _drift(record)
    if drift:
        return {**summary(record), "status": "rejected", "code": "intake_binding_drift_replan", "drift": drift}
    return summary(record)


def choose(path: Path, choice: str, *, config: Path | None = None) -> dict[str, Any]:
    record = load(path)
    if _drift(record, allow_ocr_rebind=choice == "configure_environment"):
        raise ValueError("intake_binding_drift_replan")
    if choice not in {row["choice"] for row in _options(record)}:
        raise ValueError("intake_choice_not_available")
    if record["state"]["children"] or record["state"]["scan_attempts"]:
        raise ValueError("intake_choice_after_execution_requires_replan")
    if config is not None and choice != "configure_environment":
        raise ValueError("ocr_rebind_requires_configure_environment_choice")
    record["choice"] = choice
    if choice == "configure_environment":
        selected = config or (Path(record["ocr_config"]["path"]) if record["ocr_config"] else None)
        record["ocr_config"] = _config_record(selected)
        record["availability"] = availability_summary(inspect_ocr(selected, renderer=record["renderer"], capability=record["capability"]))
        record["bindings"]["ocr"] = _ocr_bindings(record["ocr_config"], record["availability"])
        record["authorization"] = None
    _set_route(record)
    _save(path, record)
    return summary(record)


def _check_attempt_files(attempt: dict[str, Any]) -> None:
    root = Path(attempt["path"])
    if root.is_symlink():
        raise ValueError("scan_attempt_symlink_forbidden")
    for name, digest in attempt["files"].items():
        if Path(name).is_absolute() or ".." in Path(name).parts or (root / name).is_symlink() or not (root / name).is_file() or _digest(root / name) != digest:
            raise ValueError("scan_attempt_file_drift_replan")


def _verify_attempt(record: dict[str, Any], attempt: dict[str, Any], config: dict[str, Any], *, reuse_verified: bool = False) -> dict[str, Any]:
    from verify_pdf_ir import verify_pdf_ir
    _check_attempt_files(attempt)
    if reuse_verified and attempt.get("verification", {}).get("passed") is True and attempt.get("checkpoint_status") == "complete":
        return {**attempt["verification"], "reused_verified_receipt": True}
    root = Path(attempt["path"])
    result = verify_pdf_ir(Path(record["source"]["path"]), root, pdftoppm=record["renderer"],
                           docling_allow_network=False, backend_config=config)
    # The existing verifier intentionally reports this gate for a valid but
    # incomplete checkpoint. Preserve that paused result and completed receipts;
    # never convert it to verified knowledge or tolerate any integrity failure.
    only_checkpoint_pause = set(result["failures"]) == {"scan_checkpoint_paused"} and attempt.get("checkpoint_status") != "complete"
    if not result["passed"] and not only_checkpoint_pause:
        raise ValueError("scan_ir_verification_failed:" + ",".join(result["failures"]))
    return result


def _run_ocr(record: dict[str, Any]) -> None:
    from scanned_pdf_ocr import build_scanned_pdf_ir, write_scanned_pdf_ir
    current = inspect_ocr(Path(record["ocr_config"]["path"]), renderer=record["renderer"], capability=record["capability"])
    if not current["runnable"]:
        record["availability"] = availability_summary(current)
        record["state"].update(status="awaiting_environment", code="ocr_environment_unavailable")
        return
    execution = current["execution"]
    config = copy.deepcopy(execution["backend_config"])
    config["worker_max_requests"] = record["intake"]["batch_advice"]["suggested_batch_size"]
    attempts = record["state"]["scan_attempts"]
    checkpoint = None
    advice = record["intake"]["batch_advice"]
    batch_index = 0
    if attempts:
        _verify_attempt(record, attempts[-1], config, reuse_verified=True)
        batch_index = attempts[-1]["batch_index"]
        checkpoint_path = Path(attempts[-1]["path"]) / "scan-checkpoint.json"
        if checkpoint_path.is_file():
            checkpoint = json.loads(checkpoint_path.read_text())
            if checkpoint.get("status") == "complete":
                batch_index += 1
                checkpoint = None
                if batch_index >= len(advice["batches"]):
                    record["state"].update(status="paused", code="structure_review_required", checkpoint_reused=True,
                                            next_action="use_existing_independent_scan_structure_review_route")
                    return
    scope = advice["physical_scopes"][batch_index]
    workspace = Path(record["workspace"])
    parent = workspace / "scan-attempts"
    parent.mkdir(exist_ok=True)
    target = parent / f"attempt-{len(attempts) + 1:04d}"
    if target.exists():
        raise ValueError("scan_attempt_output_not_empty")
    with tempfile.TemporaryDirectory(prefix=".scan-staging-", dir=parent) as temporary:
        # Host scratch paths are excluded by the existing engine-config projection.
        config["phase7d1_workspace"] = str(Path(temporary) / "runtime")
        result = build_scanned_pdf_ir(Path(record["source"]["path"]), scope[0], scope[1],
                                     primary_backend=execution["backend"], primary_model_root=Path(execution["model_root"]),
                                     pdftoppm=record["renderer"], minimum_native_characters=record["minimum_native_characters"],
                                     backend_config=config, checkpoint=checkpoint)
        stage = Path(temporary) / "ir"
        write_scanned_pdf_ir(stage, result)
        attempt = {"path": str(stage), "files": {path.relative_to(stage).as_posix(): _digest(path) for path in stage.rglob("*") if path.is_file()},
                   "scope": scope, "batch_index": batch_index,
                   "worker_error_codes": sorted({row.get("error_code", "worker_error") for row in result.get("ocr_worker_errors", [])}),
                   "checkpoint_status": (result.get("scan_checkpoint") or {}).get("status")}
        verification = _verify_attempt(record, attempt, config)
        attempt["verification"] = {"passed": verification["passed"], "failures": verification["failures"]}
        complete_regions = {row["region_id"] for row in result.get("scan_checkpoint", {}).get("regions", []) if row["status"] == "complete"}
        attempt["completed_pages"] = sorted({row["physical_page"] for row in result["scan_ir"].get("raster_regions", []) if row["region_id"] in complete_regions})
        stage.rename(target)
        attempt["path"] = str(target)
        attempts.append(attempt)
    completed = sorted({page for saved in attempts for page in saved["completed_pages"]})
    remaining = sorted(set(record["intake"]["page_summary"]["pages"]["needs_ocr"]) - set(completed))
    nonbudget_errors = set(attempt["worker_error_codes"]) - {"worker_batch_max_requests_exceeded"}
    new_completed = len(attempt["completed_pages"]) - sum(row["status"] == "complete" for row in (checkpoint or {}).get("regions", []))
    needs_repair = bool(nonbudget_errors) or (bool(remaining) and new_completed <= 0)
    record["state"].update(status="paused", code=("ocr_worker_repair_required" if needs_repair else "ocr_checkpoint_incomplete") if remaining else "structure_review_required",
                            checkpoint_reused=len(attempts) > 1,
                            ocr_progress={"total_pages": len(completed) + len(remaining), "completed_pages": completed, "remaining_pages": remaining,
                                          "request_budget": config["worker_max_requests"]},
                            next_action=("repair_worker_then_explicit_resume" if needs_repair else "resume_next_ocr_batch") if remaining else "use_existing_independent_scan_structure_review_route")


def run(path: Path, *, authorize_ocr: bool = False, exclusive_model: bool = False, dry_run: bool = False) -> dict[str, Any]:
    record = load(path)
    drift = _drift(record)
    if drift:
        return {**summary(record), "status": "rejected", "code": "intake_binding_drift_replan", "drift": drift}
    if dry_run:
        return {**summary(record), "dry_run": True, "ocr_invoked": False, "writes": False,
                "planned_actions": (["bounded_ocr_batch_then_checkpoint"] if record["route"] == "ocr" else
                                    ["extract_original_physical_native_ranges"] if record["route"] in {"native", "native_ranges"} else [])}
    if record["route"] in {"waiting", "inspection_only"}:
        return summary(record)
    if record["route"] == "ocr":
        bound_authorization = {"source_sha256": record["source"]["sha256"], "ocr_config_sha256": record["ocr_config"]["sha256"],
                               "backend": record["availability"]["selected_backend"], "exclusive_heavy_model": True}
        if authorize_ocr and exclusive_model:
            record["authorization"] = bound_authorization
        if record["authorization"] != bound_authorization:
            record["state"].update(status="paused", code="ocr_execution_authorization_required")
            _save(path, record)
            return summary(record)
        try:
            _run_ocr(record)
        except (OSError, ValueError, RuntimeError) as error:
            record["state"].update(status="paused", code="scan_execution_failed", failure_detail=str(error),
                                    next_action="repair_runtime_then_explicit_resume")
    else:
        ranges = [[1, record["source"]["page_count"]]] if record["route"] == "native" else record["intake"]["native_ranges"]
        children = []
        for start, end in ranges:
            try:
                child = run_native_child(Path(record["source"]["path"]), [start, end],
                                         Path(record["workspace"]) / "children" / f"pages-{start:04d}-{end:04d}",
                                         bindings=record["bindings"], package=record["package"], renderer=record["renderer"],
                                         minimum_characters=record["minimum_native_characters"])
            except (OSError, ValueError, RuntimeError) as error:
                child = {"status": "paused", "code": "native_child_requires_attention", "physical_pages": [start, end], "detail": str(error),
                         "audit_path": getattr(error, "audit_path", None),
                         "next_action": "inspect_retained_heading_tasks_and_use_existing_scoped_reconstruction_overrides"}
            child_fields = {"status", "code", "physical_pages", "child", "workpack", "package", "issues", "detail",
                            "audit_path", "next_action", "whole_book_complete", "object_count", "anchor_count"}
            children.append({key: value for key, value in child.items() if key in child_fields})
        record["state"]["children"] = children
        all_drafts = all(child["status"] == "draft" for child in children)
        record["state"].update(status="draft" if all_drafts else "paused",
                               code=("partial_native_drafts" if record["route"] == "native_ranges" else "native_draft") if all_drafts else next(child["code"] for child in children if child["status"] != "draft"))
    _save(path, record)
    return summary(record)


def add_cli(subparsers: Any) -> None:
    parser = subparsers.add_parser("intake", help="Automatic read-only intake and persistent local workflow")
    commands = parser.add_subparsers(dest="intake_command", required=True)
    for name in ("inspect", "plan"):
        command = commands.add_parser(name)
        command.add_argument("--source", required=True, type=Path)
        command.add_argument("--ocr-config", type=Path)
        command.add_argument("--capability", choices=("text", "structure"), default="text")
        command.add_argument("--pdftoppm", default="pdftoppm")
        command.add_argument("--minimum-native-characters", type=int, default=40)
        if name == "plan":
            command.add_argument("--workspace", required=True, type=Path)
            command.add_argument("--name", default="reference")
            command.add_argument("--display-name", default="Reference Draft")
            command.add_argument("--domain", default="technical-reference")
            command.add_argument("--version", default="0.1.0")
            command.add_argument("--created-at", required=True)
            command.add_argument("--figures", choices=("none", "index", "index+images"), default="index+images")
        command.add_argument("--json", action="store_true", dest="json_output")
    for name in ("status", "choose", "run", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--workflow", required=True, type=Path)
        if name == "choose":
            command.add_argument("--choice", required=True, choices=CHOICES)
            command.add_argument("--ocr-config", type=Path)
        if name in {"run", "resume"}:
            command.add_argument("--authorize-ocr", action="store_true")
            command.add_argument("--exclusive-heavy-model", action="store_true")
            command.add_argument("--dry-run", action="store_true")
        command.add_argument("--json", action="store_true", dest="json_output")


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    command = args.intake_command
    if command == "inspect":
        return inspect(args.source, config=args.ocr_config, renderer=args.pdftoppm,
                       capability=args.capability, minimum_characters=args.minimum_native_characters)
    if command == "plan":
        return plan(args)
    if command == "status":
        return status(args.workflow)
    if command == "choose":
        return choose(args.workflow, args.choice, config=args.ocr_config)
    return run(args.workflow, authorize_ocr=args.authorize_ocr, exclusive_model=args.exclusive_heavy_model, dry_run=args.dry_run)


def is_workflow(path: Path) -> bool:
    try:
        value = json.loads(path.read_text())
        return isinstance(value, dict) and value.get("schema_version") == INTAKE_WORKFLOW_SCHEMA
    except (OSError, ValueError):
        return False
