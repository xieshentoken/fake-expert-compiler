#!/usr/bin/env python3
"""Plan, inspect, and safely resume a local fake-expert compilation job.

The orchestrator may run deterministic compiler stages.  It always pauses for
semantic proposal, independent review, competency evaluation, sealing, and
execution authorization; it never authors evidence or review records itself.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from expert_skill_contract import load_json, sha256_file
from book_planner import (
    BookPlannerError,
    build_book_plan,
    load_book_plan,
    summarize_book_plan,
)
from semantic_assurance import canonical_json, sha256_json, stable_id
from semantic_promotion import (
    SemanticPromotionError,
    build_semantic_workpack,
    prepare_review_plan,
    promote_reviewed_workpack,
    validate_reviewed_workpack,
)
from incremental_build_dag import (
    INCREMENTAL_BUILD_DAG_COMPILER_VERSION,
    INCREMENTAL_BUILD_DAG_PROTOCOL,
    IncrementalDagError,
    _load_snapshot_input,
    _snapshot_fingerprint,
    compare_snapshots,
    resume_plan,
    validate_dag_directory,
)


JOB_SCHEMA = "tkc.pipeline-job/v0.1"
RFC3339_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)
STAGE_NAMES = (
    "workpack",
    "semantic-proposal",
    "review-plan",
    "independent-review",
    "promotion",
    "competency",
    "seal",
    "execution",
)
BOOK_JOB_SCHEMA = "tkc.book-pipeline-job/v0.1"
BOOK_STAGE_NAMES = (
    "book-plan",
    "structure-resolution",
    "ocr-candidate",
    "semantic-review",
    "visual-review",
    "competency",
    "seal",
    "execution",
)
INCREMENTAL_JOB_SCHEMA = "tkc.incremental-build-job/v0.1"


class PipelineJobError(RuntimeError):
    """Stable local orchestration failure."""


def _book_job_hash(job: dict[str, Any]) -> str:
    return sha256_json({key: value for key, value in job.items() if key != "job_sha256"})


def _write_book_job(path: Path, job: dict[str, Any]) -> None:
    job = dict(job)
    job["job_sha256"] = _book_job_hash(job)
    path = path.expanduser().resolve()
    if path.exists() and path.is_dir():
        raise PipelineJobError("book_job_path_is_directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(job, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == payload:
        return
    path.write_text(payload, encoding="utf-8")


def _load_book_job(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    try:
        job = load_json(path)
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineJobError("book_job_invalid") from error
    if not isinstance(job, dict) or job.get("schema_version") != BOOK_JOB_SCHEMA:
        raise PipelineJobError("book_job_schema_invalid")
    if job.get("job_sha256") != _book_job_hash(job):
        raise PipelineJobError("book_job_hash_mismatch")
    return job


def _book_stage(name: str, status: str = "pending", evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "status": status, "evidence": evidence or {}}


def _job_hash(job: dict[str, Any]) -> str:
    return sha256_json({key: value for key, value in job.items() if key != "job_sha256"})


def _write_job(path: Path, job: dict[str, Any]) -> None:
    job = dict(job)
    job["job_sha256"] = _job_hash(job)
    path = path.expanduser().resolve()
    if path.exists() and path.is_dir():
        raise PipelineJobError("job_path_is_directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(job, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == payload:
        return
    path.write_text(payload, encoding="utf-8")


def _load_job(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    try:
        job = load_json(path)
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineJobError("job_invalid") from error
    if not isinstance(job, dict) or job.get("schema_version") != JOB_SCHEMA:
        raise PipelineJobError("job_schema_invalid")
    if job.get("job_sha256") != _job_hash(job):
        raise PipelineJobError("job_hash_mismatch")
    return job


def _directory_fingerprint(root: Path) -> str:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise PipelineJobError(f"input_directory_missing:{root}")
    rows = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return sha256_json(rows)


def _stage(name: str, status: str = "pending", evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "status": status, "evidence": evidence or {}}


def create_job(
    *,
    job_path: Path,
    source: Path,
    ir: Path,
    workpack: Path,
    output_skill: Path,
    name: str,
    display_name: str,
    domain: str,
    reviewer_instances: list[str],
    created_at: str,
) -> dict[str, Any]:
    if not RFC3339_RE.fullmatch(created_at):
        raise PipelineJobError("created_at_invalid")
    source = source.expanduser().resolve()
    ir = ir.expanduser().resolve()
    workpack = workpack.expanduser().resolve()
    output_skill = output_skill.expanduser().resolve()
    if not source.is_file():
        raise PipelineJobError("source_missing")
    reviewers = sorted(set(reviewer_instances))
    if len(reviewers) < 2:
        raise PipelineJobError("reviewer_coverage_insufficient")
    if workpack == output_skill or workpack in output_skill.parents or output_skill in workpack.parents:
        raise PipelineJobError("output_paths_overlap")
    identity = {
        "source_sha256": sha256_file(source),
        "ir_sha256": _directory_fingerprint(ir),
        "workpack": str(workpack),
        "output_skill": str(output_skill),
        "name": name,
        "domain": domain,
        "created_at": created_at,
    }
    job = {
        "schema_version": JOB_SCHEMA,
        "job_id": stable_id("job", identity),
        "created_at": created_at,
        "inputs": {
            "source": str(source),
            "source_sha256": identity["source_sha256"],
            "pdf_ir": str(ir),
            "pdf_ir_sha256": identity["ir_sha256"],
        },
        "outputs": {"workpack": str(workpack), "skill": str(output_skill)},
        "package": {"name": name, "display_name": display_name, "domain": domain},
        "reviewer_instances": reviewers,
        "stages": [_stage(name) for name in STAGE_NAMES],
        "next_action": "resume",
        "policy": {
            "network_allowed": False,
            "model_launch_allowed": False,
            "review_authoring_allowed": False,
            "capability_escalation_allowed": False,
            "auto_seal_allowed": False,
            "auto_execution_allowed": False,
        },
    }
    if job_path.expanduser().resolve().exists():
        raise PipelineJobError("job_already_exists")
    _write_job(job_path, job)
    return job


def _set_stage(job: dict[str, Any], name: str, status: str, evidence: dict[str, Any] | None = None) -> None:
    stages = job.get("stages")
    if not isinstance(stages, list):
        raise PipelineJobError("job_stages_invalid")
    row = next((item for item in stages if isinstance(item, dict) and item.get("name") == name), None)
    if row is None:
        raise PipelineJobError("job_stages_invalid")
    row["status"] = status
    row["evidence"] = evidence or {}


def _draft_coverage(workpack: Path) -> tuple[int, int]:
    manifest = load_json(workpack / "workpack.json")
    units = manifest.get("units", []) if isinstance(manifest, dict) else []
    expected = {
        row.get("unit_id")
        for row in units
        if isinstance(row, dict) and isinstance(row.get("unit_id"), str)
    }
    drafts = workpack / str(manifest.get("drafts_directory", "drafts"))
    present: set[str] = set()
    if drafts.is_dir():
        for path in drafts.glob("*.json"):
            try:
                row = load_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(row, dict) and isinstance(row.get("unit_id"), str):
                present.add(row["unit_id"])
    return len(present & expected), len(expected)


def inspect_job(job: dict[str, Any]) -> dict[str, Any]:
    source = Path(job["inputs"]["source"])
    ir = Path(job["inputs"]["pdf_ir"])
    workpack = Path(job["outputs"]["workpack"])
    output_skill = Path(job["outputs"]["skill"])
    if not source.is_file() or sha256_file(source) != job["inputs"]["source_sha256"]:
        return {"state": "blocked", "next_action": "restore_source", "code": "source_hash_mismatch"}
    if _directory_fingerprint(ir) != job["inputs"]["pdf_ir_sha256"]:
        return {"state": "blocked", "next_action": "restore_pdf_ir", "code": "pdf_ir_hash_mismatch"}
    if not (workpack / "workpack.json").is_file():
        return {"state": "ready", "next_action": "resume", "code": "workpack_pending"}
    present, expected = _draft_coverage(workpack)
    if present != expected:
        return {
            "state": "waiting",
            "next_action": "author_semantic_proposals",
            "code": "semantic_proposal_required",
            "drafts": {"present": present, "expected": expected},
        }
    if not (workpack / "review-plan.json").is_file():
        return {"state": "ready", "next_action": "resume", "code": "review_plan_pending"}
    try:
        workpack_manifest = load_json(workpack / "workpack.json")
    except (OSError, json.JSONDecodeError):
        workpack_manifest = {}
    visual_manifest = workpack_manifest.get("visual_semantics") if isinstance(workpack_manifest, dict) else None
    if isinstance(visual_manifest, dict) and visual_manifest.get("review_attestation_required") is True:
        visual_attestations = workpack / "visual-review-attestations.jsonl"
        if not visual_attestations.is_file() or not visual_attestations.read_text(encoding="utf-8").strip():
            return {
                "state": "waiting",
                "next_action": "obtain_independent_visual_reviews",
                "code": "visual_review_required",
                "visual_review_protocol": visual_manifest.get("review_protocol", "visual-review-attestation-v0.2"),
                "visual_candidate_paths": {
                    "objects": visual_manifest.get("objects"),
                    "relations": visual_manifest.get("relations"),
                    "table_grids": visual_manifest.get("table_grids"),
                    "conflicts": visual_manifest.get("conflicts"),
                },
            }
    if not (workpack / "review-attestations.jsonl").is_file() or not (workpack / "review-attestations.jsonl").read_text(encoding="utf-8").strip():
        return {"state": "waiting", "next_action": "obtain_independent_reviews", "code": "independent_review_required"}
    issues, summary = validate_reviewed_workpack(source, workpack)
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        return {
            "state": "blocked",
            "next_action": "repair_review_material",
            "code": errors[0].code,
            "path": errors[0].path,
        }
    if not (output_skill / "manifest.json").is_file():
        return {"state": "ready", "next_action": "resume", "code": "promotion_pending", "reviewed": summary.get("reviewed")}
    return {
        "state": "waiting",
        "next_action": "run_independent_competency_then_seal",
        "code": "competency_required",
    }


def resume_job(job_path: Path) -> dict[str, Any]:
    job = _load_job(job_path)
    state = inspect_job(job)
    workpack = Path(job["outputs"]["workpack"])
    output_skill = Path(job["outputs"]["skill"])
    source = Path(job["inputs"]["source"])
    ir = Path(job["inputs"]["pdf_ir"])
    code = state["code"]
    if code == "workpack_pending":
        manifest = build_semantic_workpack(source, ir, workpack, semantic_assurance=True)
        _set_stage(job, "workpack", "completed", {"workpack_id": manifest["workpack_id"]})
        _set_stage(job, "semantic-proposal", "waiting")
        job["next_action"] = "author_semantic_proposals"
    elif code == "review_plan_pending":
        plan = prepare_review_plan(source, workpack, job["reviewer_instances"])
        _set_stage(job, "semantic-proposal", "completed")
        _set_stage(
            job,
            "review-plan",
            "completed",
            {
                "bundle_sha256": plan["bundle_sha256"],
                "visual_review_protocol": plan.get("visual_review_protocol"),
                "visual_review_authoring_allowed": False,
            },
        )
        _set_stage(job, "independent-review", "waiting")
        job["next_action"] = "obtain_independent_reviews"
    elif code == "promotion_pending":
        package = job["package"]
        result = promote_reviewed_workpack(
            source,
            workpack,
            output_skill,
            name=package["name"],
            display_name=package["display_name"],
            domain=package["domain"],
        )
        _set_stage(job, "independent-review", "completed")
        _set_stage(job, "promotion", "completed", {"status": result["status"]})
        _set_stage(job, "competency", "waiting")
        job["next_action"] = "run_independent_competency_then_seal"
    elif state["state"] in {"waiting", "blocked"}:
        job["next_action"] = state["next_action"]
    else:
        raise PipelineJobError(f"resume_state_invalid:{code}")
    _write_job(job_path, job)
    return {"job": job, "status": inspect_job(job)}


def create_book_job(
    *,
    job_path: Path,
    source: Path,
    plan_output: Path,
    created_at: str,
    minimum_native_characters: int = 40,
) -> dict[str, Any]:
    if not RFC3339_RE.fullmatch(created_at):
        raise PipelineJobError("created_at_invalid")
    source = source.expanduser().resolve()
    plan_output = plan_output.expanduser().resolve()
    if not source.is_file():
        raise PipelineJobError("source_missing")
    if plan_output == source or plan_output in source.parents:
        raise PipelineJobError("book_plan_output_overlaps_source")
    if minimum_native_characters < 1:
        raise PipelineJobError("minimum_native_characters_invalid")
    if job_path.expanduser().resolve().exists():
        raise PipelineJobError("book_job_already_exists")
    source_hash = sha256_file(source)
    job = {
        "schema_version": BOOK_JOB_SCHEMA,
        "job_id": stable_id("bjob", source_hash, str(plan_output)),
        "created_at": created_at,
        "inputs": {
            "source": str(source),
            "source_sha256": source_hash,
        },
        "outputs": {"book_plan": str(plan_output)},
        "planner": {
            "compiler": "0.6.0-whole-book-structure-coverage",
            "protocol": "whole-book-structure-coverage-v0.2",
            "minimum_native_characters": minimum_native_characters,
        },
        "stages": [_book_stage(name) for name in BOOK_STAGE_NAMES],
        "next_action": "build_book_plan",
        "policy": {
            "network_allowed": False,
            "model_launch_allowed": False,
            "ocr_invocation_allowed": False,
            "review_authoring_allowed": False,
            "visual_review_authoring_allowed": False,
            "competency_authoring_allowed": False,
            "capability_escalation_allowed": False,
            "auto_seal_allowed": False,
            "auto_execution_allowed": False,
            "whole_book_complete_claim_allowed": False,
        },
    }
    _write_book_job(job_path, job)
    return job


def _set_book_stage(job: dict[str, Any], name: str, status: str, evidence: dict[str, Any] | None = None) -> None:
    stages = job.get("stages")
    if not isinstance(stages, list):
        raise PipelineJobError("book_job_stages_invalid")
    row = next((item for item in stages if isinstance(item, dict) and item.get("name") == name), None)
    if row is None:
        raise PipelineJobError("book_job_stages_invalid")
    row["status"] = status
    row["evidence"] = evidence or {}


def inspect_book_job(job: dict[str, Any]) -> dict[str, Any]:
    source = Path(job["inputs"]["source"])
    plan_output = Path(job["outputs"]["book_plan"])
    if not source.is_file() or sha256_file(source) != job["inputs"]["source_sha256"]:
        return {"state": "blocked", "next_action": "restore_source", "code": "source_hash_mismatch"}
    if not (plan_output / "book-plan.json").is_file():
        return {"state": "ready", "next_action": "build_book_plan", "code": "book_plan_pending"}
    try:
        status = summarize_book_plan(plan_output, source=source)
    except (BookPlannerError, OSError, ValueError, json.JSONDecodeError) as error:
        return {
            "state": "blocked",
            "next_action": "repair_book_plan",
            "code": str(error),
        }
    return {
        **status,
        "code": status["next_action"],
        "state": "waiting",
    }


def _sync_book_stages(job: dict[str, Any], status: dict[str, Any]) -> None:
    if status.get("code") == "book_plan_pending":
        return
    _set_book_stage(job, "book-plan", "completed", {"book_plan_id": status.get("book_plan_id")})
    structure_status = status.get("structure_status")
    if structure_status == "heading_resolution_required":
        _set_book_stage(job, "structure-resolution", "waiting")
    else:
        _set_book_stage(job, "structure-resolution", "completed")
    if status.get("planning_status_counts", {}).get("ocr-candidate", 0):
        _set_book_stage(job, "ocr-candidate", "waiting")
    else:
        _set_book_stage(job, "ocr-candidate", "not-required")
    if status.get("planning_status_counts", {}).get("visual-review", 0):
        _set_book_stage(job, "visual-review", "waiting")
    else:
        _set_book_stage(job, "visual-review", "not-required")
    _set_book_stage(job, "semantic-review", "waiting")
    _set_book_stage(job, "competency", "waiting")
    _set_book_stage(job, "seal", "waiting")
    _set_book_stage(job, "execution", "forbidden")
    job["next_action"] = status.get("next_action", "independent_semantic_review_required")


def resume_book_job(job_path: Path) -> dict[str, Any]:
    job = _load_book_job(job_path)
    state = inspect_book_job(job)
    source = Path(job["inputs"]["source"])
    plan_output = Path(job["outputs"]["book_plan"])
    if state.get("code") == "book_plan_pending":
        plan = build_book_plan(
            source,
            plan_output,
            minimum_native_characters=int(job["planner"].get("minimum_native_characters", 40)),
        )
        state = summarize_book_plan(plan_output, source=source)
        state["code"] = state["next_action"]
        state["state"] = "waiting"
        _sync_book_stages(job, state)
        _set_book_stage(job, "book-plan", "completed", {"book_plan_id": plan["book_plan_id"]})
    elif state.get("state") in {"waiting", "blocked"}:
        if state.get("state") == "waiting":
            _sync_book_stages(job, state)
        job["next_action"] = state.get("next_action", job.get("next_action"))
    else:
        raise PipelineJobError(f"book_resume_state_invalid:{state.get('code')}")
    _write_book_job(job_path, job)
    return {"job": job, "status": inspect_book_job(job)}


def _incremental_job_hash(job: dict[str, Any]) -> str:
    return sha256_json({key: value for key, value in job.items() if key != "job_sha256"})


def _write_incremental_job(path: Path, job: dict[str, Any]) -> None:
    job = dict(job)
    job["job_sha256"] = _incremental_job_hash(job)
    path = path.expanduser().resolve()
    if path.exists() and path.is_dir():
        raise PipelineJobError("incremental_job_path_is_directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(job, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == payload:
        return
    path.write_text(payload, encoding="utf-8")


def _load_incremental_job(path: Path) -> dict[str, Any]:
    try:
        job = load_json(path.expanduser().resolve())
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineJobError("incremental_job_invalid") from error
    if not isinstance(job, dict) or job.get("schema_version") != INCREMENTAL_JOB_SCHEMA:
        raise PipelineJobError("incremental_job_schema_invalid")
    if job.get("job_sha256") != _incremental_job_hash(job):
        raise PipelineJobError("incremental_job_hash_mismatch")
    return job


def create_incremental_job(
    *,
    job_path: Path,
    old_input: Path,
    new_input: Path,
    output: Path,
    generated_at: str,
) -> dict[str, Any]:
    if not RFC3339_RE.fullmatch(generated_at):
        raise PipelineJobError("created_at_invalid")
    old_input = old_input.expanduser().resolve()
    new_input = new_input.expanduser().resolve()
    output = output.expanduser().resolve()
    job_resolved = job_path.expanduser().resolve()
    if not old_input.exists() or not new_input.exists():
        raise PipelineJobError("incremental_input_missing")
    def overlaps(first: Path, second: Path) -> bool:
        return first == second or first in second.parents or second in first.parents

    if overlaps(output, old_input) or overlaps(output, new_input):
        raise PipelineJobError("incremental_output_overlaps_input")
    if overlaps(job_resolved, old_input) or overlaps(job_resolved, new_input) or overlaps(job_resolved, output):
        raise PipelineJobError("incremental_job_overlaps_input_or_output")
    if job_resolved.exists():
        raise PipelineJobError("incremental_job_already_exists")
    try:
        old_graph = _load_snapshot_input(old_input)
        new_graph = _load_snapshot_input(new_input)
        comparison = compare_snapshots(old_input, new_input, output, generated_at=generated_at)
    except IncrementalDagError as error:
        raise PipelineJobError(str(error)) from error
    job = {
        "schema_version": INCREMENTAL_JOB_SCHEMA,
        "compiler_version": INCREMENTAL_BUILD_DAG_COMPILER_VERSION,
        "protocol": INCREMENTAL_BUILD_DAG_PROTOCOL,
        "job_id": stable_id("ibjob", _snapshot_fingerprint(old_graph), _snapshot_fingerprint(new_graph), str(output)),
        "created_at": generated_at,
        "inputs": {
            "old": str(old_input),
            "old_dag_sha256": comparison["changes"]["old_dag_sha256"],
            "new": str(new_input),
            "new_dag_sha256": comparison["changes"]["new_dag_sha256"],
        },
        "output": str(output),
        "plan_sha256": sha256_json(comparison["plan"]),
        "receipt_ids": [],
        "receipts_sha256": sha256_json([]),
        "state": "paused" if comparison["plan"]["required_actions"] or comparison["plan"]["fail_closed"] else "no-op",
        "pause_only": True,
        "policy": comparison["plan"]["policy"],
    }
    _write_incremental_job(job_path, job)
    return job


def inspect_incremental_job(job: dict[str, Any]) -> dict[str, Any]:
    try:
        old_graph = _load_snapshot_input(Path(job["inputs"]["old"]))
        new_graph = _load_snapshot_input(Path(job["inputs"]["new"]))
    except (IncrementalDagError, OSError, json.JSONDecodeError, KeyError) as error:
        return {"state": "blocked", "next_action": "restore_incremental_input", "code": str(error)}
    if _snapshot_fingerprint(old_graph) != job["inputs"].get("old_dag_sha256"):
        return {"state": "blocked", "next_action": "restore_incremental_input", "code": "old_dag_binding_mismatch"}
    if _snapshot_fingerprint(new_graph) != job["inputs"].get("new_dag_sha256"):
        return {"state": "blocked", "next_action": "restore_incremental_input", "code": "new_dag_binding_mismatch"}
    output = Path(job["output"])
    # Check the persisted state binding against the frozen new-input DAG before
    # validating the output directory.  A caller may recompute a tampered
    # manifest/count/hash consistently while leaving the state receipt binding
    # unchanged; report that first frozen-boundary violation rather than
    # allowing a structural validator error to obscure the immutable input
    # mismatch.
    try:
        persisted_state = load_json(output / "state.json")
    except (OSError, json.JSONDecodeError):
        persisted_state = None
    if isinstance(persisted_state, dict) and persisted_state.get("dag_sha256") != job["inputs"].get("new_dag_sha256"):
        return {"state": "blocked", "next_action": "repair_incremental_plan", "code": "state_dag_mismatch"}
    issues = validate_dag_directory(output)
    if issues:
        return {"state": "blocked", "next_action": "repair_incremental_plan", "code": issues[0]["code"]}
    try:
        plan = load_json(output / "invalidation-plan.json")
        state = load_json(output / "state.json")
    except (OSError, json.JSONDecodeError):
        return {"state": "blocked", "next_action": "repair_incremental_plan", "code": "incremental_plan_missing"}
    if sha256_json(plan) != job.get("plan_sha256"):
        return {"state": "blocked", "next_action": "repair_incremental_plan", "code": "incremental_plan_binding_mismatch"}
    if state.get("receipt_ids") != job.get("receipt_ids", []) or state.get("receipts_sha256") != job.get("receipts_sha256", sha256_json([])):
        return {"state": "blocked", "next_action": "repair_incremental_plan", "code": "receipt_binding_mismatch"}
    if plan.get("fail_closed"):
        return {
            "state": "paused",
            "next_action": "resolve_safety_violation",
            "code": "resolve_safety_violation",
            "fail_closed": True,
            "completed_actions": state.get("completed_actions", []),
            "paused_gates": ["safety-violation"],
        }
    return {
        "state": state.get("state", "paused"),
        "next_action": "resume" if state.get("state") in {"paused", "built"} else "inspect_gate",
        "code": state.get("pause_reason") or ("fail_closed" if plan.get("fail_closed") else "incremental_ready"),
        "fail_closed": plan.get("fail_closed", False),
        "completed_actions": state.get("completed_actions", []),
        "paused_gates": state.get("paused_gates", []),
    }


def resume_incremental_job(job_path: Path) -> dict[str, Any]:
    job = _load_incremental_job(job_path)
    status = inspect_incremental_job(job)
    if status["state"] == "blocked":
        _write_incremental_job(job_path, job)
        return {"job": job, "status": status}
    try:
        resumed = resume_plan(Path(job["output"]))
    except IncrementalDagError as error:
        raise PipelineJobError(str(error)) from error
    job["state"] = resumed["state"]
    job["completed_actions"] = resumed["completed_actions"]
    job["pause_reason"] = resumed.get("pause_reason")
    try:
        output_state = load_json(Path(job["output"]) / "state.json")
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineJobError("incremental_state_missing") from error
    job["receipt_ids"] = output_state.get("receipt_ids", [])
    job["receipts_sha256"] = output_state.get("receipts_sha256", sha256_json([]))
    _write_incremental_job(job_path, job)
    return {"job": job, "status": {**inspect_incremental_job(job), **resumed}}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--job", required=True, type=Path)
    plan.add_argument("--source", required=True, type=Path)
    plan.add_argument("--ir", required=True, type=Path)
    plan.add_argument("--workpack", required=True, type=Path)
    plan.add_argument("--output-skill", required=True, type=Path)
    plan.add_argument("--name", required=True)
    plan.add_argument("--display-name", required=True)
    plan.add_argument("--domain", required=True)
    plan.add_argument("--reviewer-instance", action="append", required=True)
    plan.add_argument("--created-at", required=True)
    for name in ("status", "resume"):
        command = sub.add_parser(name)
        command.add_argument("--job", required=True, type=Path)
    book_plan = sub.add_parser("book-plan")
    book_plan.add_argument("--job", required=True, type=Path)
    book_plan.add_argument("--source", required=True, type=Path)
    book_plan.add_argument("--plan-output", required=True, type=Path)
    book_plan.add_argument("--created-at", required=True)
    book_plan.add_argument("--minimum-native-characters", type=int, default=40)
    for name in ("book-status", "book-resume"):
        command = sub.add_parser(name)
        command.add_argument("--job", required=True, type=Path)
    incremental_plan = sub.add_parser("incremental-plan")
    incremental_plan.add_argument("--job", required=True, type=Path)
    incremental_plan.add_argument("--old", required=True, type=Path)
    incremental_plan.add_argument("--new", required=True, type=Path)
    incremental_plan.add_argument("--output", required=True, type=Path)
    incremental_plan.add_argument("--created-at", required=True)
    for name in ("incremental-status", "incremental-resume"):
        command = sub.add_parser(name)
        command.add_argument("--job", required=True, type=Path)
    for command in sub.choices.values():
        command.add_argument("--json", action="store_true", dest="json_output", default=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "plan":
            job = create_job(
                job_path=args.job,
                source=args.source,
                ir=args.ir,
                workpack=args.workpack,
                output_skill=args.output_skill,
                name=args.name,
                display_name=args.display_name,
                domain=args.domain,
                reviewer_instances=args.reviewer_instance,
                created_at=args.created_at,
            )
            payload: Any = {"job": job, "status": inspect_job(job)}
        elif args.command == "status":
            job = _load_job(args.job)
            payload = {"job_id": job["job_id"], "status": inspect_job(job)}
        elif args.command == "resume":
            payload = resume_job(args.job)
        elif args.command == "book-plan":
            create_book_job(
                job_path=args.job,
                source=args.source,
                plan_output=args.plan_output,
                created_at=args.created_at,
                minimum_native_characters=args.minimum_native_characters,
            )
            payload = resume_book_job(args.job)
        elif args.command == "book-status":
            job = _load_book_job(args.job)
            payload = {"job_id": job["job_id"], "status": inspect_book_job(job)}
        elif args.command == "book-resume":
            payload = resume_book_job(args.job)
        elif args.command == "incremental-plan":
            job = create_incremental_job(
                job_path=args.job,
                old_input=args.old,
                new_input=args.new,
                output=args.output,
                generated_at=args.created_at,
            )
            payload = {"job": job, "status": inspect_incremental_job(job)}
        elif args.command == "incremental-status":
            job = _load_incremental_job(args.job)
            payload = {"job_id": job["job_id"], "status": inspect_incremental_job(job)}
        else:
            payload = resume_incremental_job(args.job)
    except (PipelineJobError, BookPlannerError, SemanticPromotionError, IncrementalDagError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        status = payload["status"]
        print(f"PASS state={status['state']} next_action={status['next_action']} code={status['code']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
