#!/usr/bin/env python3
"""Phase 7D.5B private visual benchmark gate and frozen evaluator.

This module is deliberately an offline orchestration boundary.  It can import
only externally authored, independently attested Gold fragments, validate the
two current private runtime-qualification plans, freeze calibration-derived
configuration, and evaluate one supplied hash-bound output bundle per visual
mode.  It never calls a model, MCP, network, OCR backend, or reviewer.  A
missing gate produces a machine-readable pause and never an accuracy value.

The output directory is a private candidate work area.  It contains no source
PDFs and is never a release input.  Imported Gold and model outputs remain
private and are excluded from release by policy.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import platform
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from compiler_version import (
    PORTABLE_VISUAL_REVIEW_WORKPACK_MANIFEST_SCHEMA,
    VISUAL_BENCHMARK_ATTESTATION_SCHEMA,
    VISUAL_BENCHMARK_COMPILER_VERSION,
    VISUAL_BENCHMARK_COST_LEDGER_SCHEMA,
    VISUAL_BENCHMARK_EVALUATION_SCHEMA,
    VISUAL_BENCHMARK_GOLD_FRAGMENT_SCHEMA,
    VISUAL_BENCHMARK_PREDICTION_SCHEMA,
    VISUAL_BENCHMARK_PLAN_SCHEMA,
    VISUAL_BENCHMARK_PROTOCOL,
    VISUAL_BENCHMARK_STATE_SCHEMA,
    VISUAL_BENCHMARK_THRESHOLD_CONFIG_SCHEMA,
    VISUAL_REVIEW_ADJUDICATION_COMPILER_VERSION,
    VISUAL_REVIEW_ADJUDICATION_PROTOCOL,
    VISUAL_REVIEW_ADJUDICATION_RECEIPT_SCHEMA,
)
from expert_skill_contract import sha256_file
from incremental_build_dag import (
    IncrementalDagError,
    build_dag,
    compare_snapshots,
    sha256_json,
    validate_dag_directory,
)
from paddle_runtime_qualification import (
    PaddleRuntimeQualificationError,
    validate_qualification_plan,
    validate_runtime_qualification,
)


# The PP-OCRv6 path is an additive extension of the table/chart qualification
# protocol.  Preserve the frozen 7D.5A plans whose tool hash was recorded
# before that extension; any other hash still fails closed as stale.
LEGACY_ADDITIVE_QUALIFICATION_TOOL_SHA256 = "d1d9b23090e8a99d7104b8e4b978330c22eaddbb5d8f964fc14f66c8bb21d842"


MODES = ("off", "auto", "full")
TARGET_MODEL_ID = "GPT-5.6 Luna"
TARGET_THINKING = "max"
QUALIFICATION_TOOL_NAME = "paddle_runtime_qualification.py"
QUALIFICATION_TARGETS = {
    "paddleocr-ppstructure-v3": "technical-table-v1",
    "paddleocr-chart-parsing": "technical-chart-v2",
}
REQUIRED_REVIEW_CHECKS = (
    "crop_opened",
    "render_opened",
    "source_identity_checked",
    "item_hash_checked",
    "geometry_checked",
    "split_checked",
)
METRIC_IDS = (
    "object_detection_precision",
    "object_detection_recall",
    "object_detection_f1",
    "bbox_iou",
    "localization_coverage",
    "table_row_topology_accuracy",
    "table_column_topology_accuracy",
    "table_span_exactness",
    "table_cell_exactness",
    "numeric_exactness",
    "unit_exactness",
    "hallucinated_numeric_count",
    "chart_axis_candidate_recall",
    "chart_tick_candidate_exactness",
    "chart_legend_candidate_exactness",
    "chart_series_candidate_exactness",
    "chart_value_candidate_exactness",
    "figure_presence_precision",
    "figure_localization_coverage",
    "equation_presence_precision",
    "equation_localization_coverage",
    "elapsed_ms",
    "cold_start_ms",
    "warm_repeat_ms",
    "rss_peak_bytes",
    "model_load_calls",
    "render_count",
    "crop_bytes",
    "output_bytes",
    "visual_mode_coverage_cost_off_auto_full",
)
ACCURACY_METRICS = set(METRIC_IDS[:21])
COST_FIELDS = (
    "elapsed_ms",
    "cold_start_ms",
    "warm_repeat_ms",
    "rss_peak_bytes",
    "model_load_calls",
    "render_count",
    "crop_bytes",
    "output_bytes",
    "model_invocations",
)
HASH_FIELDS = (
    "source_sha256",
    "render_sha256",
    "crop_sha256",
    "bbox_sha256",
)
HASH_RE = set("0123456789abcdef")


class VisualBenchmarkError(RuntimeError):
    """Stable fail-closed Phase 7D.5B error."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= HASH_RE


def _hash_without(value: Mapping[str, Any], key: str) -> str:
    return sha256_json({name: nested for name, nested in value.items() if name != key})


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VisualBenchmarkError(f"json_invalid:{path.name}") from error
    if not isinstance(value, dict):
        raise VisualBenchmarkError(f"json_object_required:{path.name}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise VisualBenchmarkError(f"jsonl_read_failed:{path.name}") from error
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise VisualBenchmarkError(f"jsonl_invalid:{path.name}:{line_number}") from error
        if not isinstance(value, dict):
            raise VisualBenchmarkError(f"jsonl_object_required:{path.name}:{line_number}")
        rows.append(value)
    return rows


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
    os.chmod(path, 0o600)


def _secure_directory(path: Path, *, must_be_empty: bool = False) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.exists():
        if resolved.is_symlink() or not resolved.is_dir():
            raise VisualBenchmarkError("output_directory_invalid")
        if must_be_empty and any(resolved.iterdir()):
            raise VisualBenchmarkError("output_not_empty")
    else:
        resolved.mkdir(parents=True)
    os.chmod(resolved, 0o700)
    return resolved


def _secure_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.is_symlink() or not resolved.is_file():
        raise VisualBenchmarkError(f"{label}_missing_or_symlink")
    return resolved


def _relative_file(root: Path, relative: Any, label: str) -> tuple[Path, str]:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise VisualBenchmarkError(f"{label}_path_not_relative")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path.is_symlink() or not path.is_file():
        raise VisualBenchmarkError(f"{label}_path_invalid")
    return path, relative


def _assert_empty(value: Any, code: str) -> None:
    if value is None:
        return
    if isinstance(value, list) and not value:
        return
    if isinstance(value, dict):
        for nested in value.values():
            _assert_empty(nested, code)
        return
    raise VisualBenchmarkError(code)


def _file_hash_from_manifest(manifest: Mapping[str, Any], relative: str) -> str:
    for row in manifest.get("files", []) if isinstance(manifest.get("files"), list) else []:
        if isinstance(row, Mapping) and row.get("path") == relative:
            value = row.get("sha256")
            if _is_hash(value):
                return str(value)
    raise VisualBenchmarkError(f"workpack_manifest_file_missing:{relative}")


def _candidate_item_hash(item: Mapping[str, Any]) -> str:
    return sha256_json({key: value for key, value in item.items() if key != "item_sha256"})


def _task_hash(task: Mapping[str, Any]) -> str:
    return sha256_json({key: value for key, value in task.items() if key != "task_sha256"})


def _manifest_hash(manifest: Mapping[str, Any]) -> str:
    return sha256_json({key: value for key, value in manifest.items() if key != "manifest_sha256"})


def _split_hash(split: Mapping[str, Any]) -> str:
    return sha256_json({key: value for key, value in split.items() if key != "commitment_sha256"})


def _review_plan_hash(plan: Mapping[str, Any]) -> str:
    return sha256_json({key: value for key, value in plan.items() if key != "review_plan_sha256"})


def _validate_empty_gold_template(fragments: Sequence[Mapping[str, Any]], candidate_ids: set[str]) -> None:
    if {str(row.get("item_id")) for row in fragments} != candidate_ids:
        raise VisualBenchmarkError("empty_gold_item_set_mismatch")
    for row in fragments:
        if row.get("schema_version") != "tkc.phase-7d5a-empty-gold-fragment/v0.1":
            raise VisualBenchmarkError("empty_gold_schema_invalid")
        if row.get("item_sha256") is None or row.get("reviewer_instance") is not None or row.get("attestation_id") is not None or row.get("verdict") is not None:
            raise VisualBenchmarkError("gold_template_not_empty")
        _assert_empty(row.get("gold_payload"), "gold_template_answer_present")
        if row.get("verified_gold") is not False or row.get("independent_real_gold") is not False or row.get("promotion_allowed") is not False:
            raise VisualBenchmarkError("gold_template_policy_invalid")


def load_phase7d5a_workpack(
    workpack: Path,
    *,
    allow_test_fixture: bool = False,
    allow_portable_review: bool = False,
) -> dict[str, Any]:
    """Load and replay the frozen 7D.5A workpack without modifying it."""

    root = _secure_directory(workpack)
    manifest_path = _secure_file(root / "manifest.json", "workpack_manifest")
    manifest = _read_json(manifest_path)
    legacy_workpack = manifest.get("schema_version") == "tkc.phase-7d5a-workpack-manifest/v0.1"
    portable_review = manifest.get("schema_version") == PORTABLE_VISUAL_REVIEW_WORKPACK_MANIFEST_SCHEMA
    if not legacy_workpack and not (allow_portable_review and portable_review):
        raise VisualBenchmarkError("workpack_manifest_policy_invalid")
    if manifest.get("release_included") is not False:
        raise VisualBenchmarkError("workpack_manifest_policy_invalid")
    if manifest.get("manifest_sha256") != _manifest_hash(manifest):
        raise VisualBenchmarkError("workpack_manifest_hash_mismatch")
    candidate_path = _secure_file(root / "candidate-items.jsonl", "candidate_items")
    candidate_rows = _read_jsonl(candidate_path)
    expected_count = int(manifest.get("candidate_item_count", 0) or 0)
    if legacy_workpack and not allow_test_fixture and expected_count != 31:
        raise VisualBenchmarkError("phase7d5a_candidate_count_must_be_31")
    if portable_review and expected_count < 2:
        raise VisualBenchmarkError("portable_review_candidate_count_too_small")
    if expected_count != len(candidate_rows) or not candidate_rows:
        raise VisualBenchmarkError("candidate_item_count_mismatch")
    candidate_by_id: dict[str, dict[str, Any]] = {}
    source_object_ids: set[str] = set()
    for row in candidate_rows:
        item_id = row.get("item_id")
        if not isinstance(item_id, str) or not item_id or item_id in candidate_by_id:
            raise VisualBenchmarkError("candidate_item_duplicate_or_missing")
        if row.get("item_sha256") != _candidate_item_hash(row):
            raise VisualBenchmarkError(f"candidate_item_hash_mismatch:{item_id}")
        if row.get("candidate_status") != "candidate-benchmark" or row.get("review_required") is not True or row.get("verified_gold") is not False or row.get("independent_real_gold") is not False or row.get("promotion_allowed") is not False or row.get("metrics_status") != "not-run":
            raise VisualBenchmarkError(f"candidate_policy_invalid:{item_id}")
        source = row.get("source") if isinstance(row.get("source"), Mapping) else {}
        source_object_id = source.get("source_visual_object_id")
        if not isinstance(source_object_id, str) or not source_object_id or source_object_id in source_object_ids:
            raise VisualBenchmarkError(f"candidate_source_object_duplicate:{item_id}")
        source_object_ids.add(source_object_id)
        if not _is_hash(source.get("source_sha256")) or not isinstance(source.get("physical_page"), int):
            raise VisualBenchmarkError(f"candidate_source_binding_invalid:{item_id}")
        render = row.get("render") if isinstance(row.get("render"), Mapping) else {}
        crop = row.get("crop") if isinstance(row.get("crop"), Mapping) else {}
        render_path, render_relative = _relative_file(root, render.get("path"), f"render:{item_id}")
        crop_path, crop_relative = _relative_file(root, crop.get("path"), f"crop:{item_id}")
        if sha256_file(render_path) != render.get("sha256") or sha256_file(crop_path) != crop.get("sha256"):
            raise VisualBenchmarkError(f"candidate_render_crop_hash_mismatch:{item_id}")
        if render.get("dpi") != 120 or render.get("rotation") != 0:
            raise VisualBenchmarkError(f"candidate_render_policy_invalid:{item_id}")
        candidate_by_id[item_id] = row

    split_path = _secure_file(root / "split-commitment.json", "split_commitment")
    split = _read_json(split_path)
    if split.get("commitment_sha256") != _split_hash(split):
        raise VisualBenchmarkError("split_commitment_hash_mismatch")
    candidate_ids = set(candidate_by_id)
    calibration = set(str(value) for value in split.get("calibration_item_ids", []) if isinstance(value, str))
    test = set(str(value) for value in split.get("test_item_ids", []) if isinstance(value, str))
    if calibration & test or calibration | test != candidate_ids or not calibration or not test:
        raise VisualBenchmarkError("split_commitment_item_set_invalid")
    if split.get("expected_answers_present") is not False or split.get("verified_gold") is not False:
        raise VisualBenchmarkError("split_commitment_gold_policy_invalid")
    leakage_policy = split.get("split_leakage_policy") if isinstance(split.get("split_leakage_policy"), Mapping) else {}
    if any(leakage_policy.get(key) is not True for key in ("same_crop_sha256_cannot_cross_split", "same_source_page_crop_cannot_cross_split", "same_source_visual_object_cannot_cross_split", "test_answers_created_after_freeze")):
        raise VisualBenchmarkError("split_leakage_policy_invalid")

    review_plan_path = _secure_file(root / "independent-review-plan.json", "review_plan")
    review_plan = _read_json(review_plan_path)
    if review_plan.get("review_plan_sha256") != _review_plan_hash(review_plan) or review_plan.get("task_count") != len(candidate_rows):
        raise VisualBenchmarkError("review_plan_hash_or_count_invalid")
    if review_plan.get("reviewer_registry") != [] or review_plan.get("attestation_authoring_allowed") is not False or review_plan.get("promotion_allowed") is not False or review_plan.get("reviewer_independence_required") is not True:
        raise VisualBenchmarkError("review_plan_independence_policy_invalid")
    proposer_instances = [str(value) for value in review_plan.get("proposer_instances", []) if isinstance(value, str)]
    if not proposer_instances:
        raise VisualBenchmarkError("review_plan_proposer_missing")

    tasks_path = _secure_file(root / "independent-review-tasks.jsonl", "review_tasks")
    tasks = _read_jsonl(tasks_path)
    task_by_item: dict[str, dict[str, Any]] = {}
    task_ids: set[str] = set()
    for task in tasks:
        item_id = task.get("item_id")
        if not isinstance(item_id, str) or item_id not in candidate_by_id or item_id in task_by_item or task.get("review_task_id") in task_ids:
            raise VisualBenchmarkError("review_task_set_invalid")
        if task.get("task_sha256") != _task_hash(task):
            raise VisualBenchmarkError(f"review_task_hash_mismatch:{item_id}")
        item = candidate_by_id[item_id]
        if task.get("item_sha256") != item.get("item_sha256") or task.get("render_sha256") != (item.get("render") or {}).get("sha256") or task.get("crop_sha256") != (item.get("crop") or {}).get("sha256") or task.get("source_sha256") != (item.get("source") or {}).get("source_sha256"):
            raise VisualBenchmarkError(f"review_task_input_binding_invalid:{item_id}")
        expected_reviewers = 2 if task.get("kind_candidate") in {"table", "chart"} else 1
        if task.get("required_independent_reviewers") != expected_reviewers or task.get("compiler_authored_attestation_allowed") is not False or task.get("status") != "pending-independent-review":
            raise VisualBenchmarkError(f"review_task_policy_invalid:{item_id}")
        task_by_item[item_id] = task
        task_ids.add(str(task.get("review_task_id")))
    if set(task_by_item) != candidate_ids:
        raise VisualBenchmarkError("review_task_item_set_mismatch")

    empty_gold_path = _secure_file(root / "gold-templates" / "gold-fragments.jsonl", "empty_gold")
    _validate_empty_gold_template(_read_jsonl(empty_gold_path), candidate_ids)
    metrics_path = _secure_file(root / "metrics-contract.json", "metrics_contract")
    metrics_contract = _read_json(metrics_path)
    if metrics_contract.get("verified_gold") is not False or metrics_contract.get("independent_real_gold") is not False or metrics_contract.get("accuracy_claim") is not False:
        raise VisualBenchmarkError("metrics_contract_policy_invalid")
    for metric in metrics_contract.get("metrics", []) if isinstance(metrics_contract.get("metrics"), list) else []:
        if not isinstance(metric, Mapping) or metric.get("status") != "not-run" or metric.get("value") is not None or metric.get("claim") != "none":
            raise VisualBenchmarkError("metrics_contract_already_measured")

    qualification_plans: dict[str, dict[str, Any]] = {}
    qualification_targets = () if portable_review else (("table", "qualification-plans/table-plan.json"), ("chart", "qualification-plans/chart-plan.json"))
    for label, relative in qualification_targets:
        plan_path = _secure_file(root / relative, f"qualification_plan:{label}")
        plan = _read_json(plan_path)
        try:
            validate_qualification_plan(plan, replay_local=False)
        except PaddleRuntimeQualificationError as error:
            raise VisualBenchmarkError(f"qualification_plan_invalid:{label}:{error}") from error
        backend_id = str(plan.get("backend_id"))
        if QUALIFICATION_TARGETS.get(backend_id) != plan.get("profile_id") or plan.get("execution", {}).get("repetitions") != 3 or plan.get("execution", {}).get("max_parallel_models") != 1 or plan.get("execution", {}).get("serial_inference") is not True:
            raise VisualBenchmarkError(f"qualification_plan_contract_invalid:{label}")
        policy = plan.get("policy") if isinstance(plan.get("policy"), Mapping) else {}
        if any(policy.get(key) is not expected for key, expected in (("source_pdf_passed", False), ("network_enabled", False), ("model_download", False), ("mcp_http_dependency", False), ("candidate_only", True), ("promotion", False), ("verified_gold", False), ("accuracy_claim", False))):
            raise VisualBenchmarkError(f"qualification_plan_policy_invalid:{label}")
        contract = plan.get("runtime_contract") if isinstance(plan.get("runtime_contract"), Mapping) else {}
        binding = {
            "plan_sha256": plan.get("plan_sha256"),
            "file_sha256": sha256_file(plan_path),
            "backend_id": backend_id,
            "profile_id": plan.get("profile_id"),
            "qualification_tool_sha256": (plan.get("qualification_tool") or {}).get("script_sha256"),
            "runtime_contract_sha256": contract.get("contract_sha256"),
            "runtime_inventory_sha256": (contract.get("runtime_inventory") or {}).get("inventory_sha256"),
            "model_identity_sha256": (contract.get("model_status") or {}).get("stable_identity_sha256"),
            "configuration_sha256": contract.get("configuration_sha256"),
            "worker_sha256": contract.get("worker_sha256"),
            "profile_sha256": sha256_json(contract.get("runtime_profile")),
        }
        if not all(_is_hash(value) for value in binding.values() if value != binding["backend_id"] and value != binding["profile_id"]):
            raise VisualBenchmarkError(f"qualification_plan_binding_missing:{label}")
        qualification_plans[label] = {"relative_path": relative, "plan": plan, "binding": binding}

    return {
        "root": root,
        "manifest": manifest,
        "manifest_sha256": str(manifest["manifest_sha256"]),
        "candidate_items_sha256": sha256_file(candidate_path),
        "candidate_items": candidate_rows,
        "candidate_by_id": candidate_by_id,
        "candidate_ids": candidate_ids,
        "split": split,
        "split_sha256": sha256_file(split_path),
        "calibration_ids": calibration,
        "test_ids": test,
        "review_plan": review_plan,
        "review_plan_sha256": str(review_plan["review_plan_sha256"]),
        "task_by_item": task_by_item,
        "metrics_contract": metrics_contract,
        "qualification_plans": qualification_plans,
        "portable_review": portable_review,
    }


def validate_qualification_gate(facts: Mapping[str, Any], receipts: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Validate both current private plans and optional host receipts.

    This function only validates receipts.  It never starts a worker, touches
    MCP, probes localhost, or interprets an absent receipt as a failed model.
    """

    current_tool_sha = sha256_file(Path(__file__).with_name(QUALIFICATION_TOOL_NAME))
    receipts = receipts or {}
    rows: dict[str, Any] = {}
    issues: list[str] = []
    for label in ("table", "chart"):
        entry = (facts.get("qualification_plans") or {}).get(label) if isinstance(facts.get("qualification_plans"), Mapping) else None
        if not isinstance(entry, Mapping):
            issues.append(f"qualification_plan_missing:{label}")
            continue
        plan = entry.get("plan")
        binding = entry.get("binding") if isinstance(entry.get("binding"), Mapping) else {}
        if binding.get("qualification_tool_sha256") not in {current_tool_sha, LEGACY_ADDITIVE_QUALIFICATION_TOOL_SHA256}:
            issues.append(f"qualification_plan_tool_stale:{label}")
        backend = str((plan or {}).get("backend_id"))
        receipt = receipts.get(backend) or receipts.get(label)
        row = {"backend_id": backend, "profile_id": (plan or {}).get("profile_id"), "plan_sha256": (plan or {}).get("plan_sha256"), "qualification_tool_sha256": binding.get("qualification_tool_sha256"), "status": "paused-resource-handoff", "issue_codes": []}
        if receipt is None:
            row["issue_codes"] = ["qualification_receipt_missing", "resource_handoff_required"]
            issues.extend(f"{code}:{label}" for code in row["issue_codes"])
        else:
            try:
                validate_runtime_qualification(
                    receipt,
                    contract=(plan or {}).get("runtime_contract"),
                    plan=plan,
                    backend_id=backend,
                    profile_id=(plan or {}).get("profile_id"),
                    require_qualified=True,
                )
            except PaddleRuntimeQualificationError as error:
                row["issue_codes"] = [str(error)]
                issues.append(f"qualification_receipt_invalid:{label}:{error}")
            else:
                row["status"] = "qualified"
                row["receipt_sha256"] = receipt.get("receipt_sha256")
        rows[label] = row
    if any(row.get("status") != "qualified" for row in rows.values()) or issues:
        status = "rejected" if any("stale" in issue or "invalid" in issue for issue in issues) else "paused-resource-handoff"
    else:
        status = "qualified"
    return {
        "status": status,
        "routes": rows,
        "issues": sorted(set(issues)),
        "current_qualification_tool_sha256": current_tool_sha,
        "mcp_used": False,
        "network_used": False,
        "model_invocations": 0,
    }


def _gold_input_hashes(item: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, str]:
    # 7D.5A already committed the exact geometry hash in each review task.
    # The importer must bind to that commitment rather than re-deriving a
    # subtly different floating-point representation.
    bbox_hash = task.get("bbox_sha256")
    if not _is_hash(bbox_hash):
        raise VisualBenchmarkError("review_task_bbox_hash_invalid")
    return {
        "source_sha256": str((item.get("source") or {}).get("source_sha256")),
        "render_sha256": str((item.get("render") or {}).get("sha256")),
        "crop_sha256": str((item.get("crop") or {}).get("sha256")),
        "bbox_sha256": bbox_hash,
    }


def _attestation_hash(attestation: Mapping[str, Any]) -> str:
    return sha256_json({key: value for key, value in attestation.items() if key != "attestation_sha256"})


def _fragment_hash(fragment: Mapping[str, Any]) -> str:
    return sha256_json({key: value for key, value in fragment.items() if key != "fragment_sha256"})


def _validate_adjudication_gold_provenance(row: Mapping[str, Any], facts: Mapping[str, Any], payload: Mapping[str, Any], registry_ids: Sequence[str], attestations: Sequence[Mapping[str, Any]], proposer_instances: set[str]) -> None:
    item_id = str(row["item_id"])
    if payload.get("reviewer_agreement") != "adjudicated":
        return
    provenance = row.get("fragment_provenance")
    adjudication = provenance.get("adjudication") if isinstance(provenance, Mapping) else None
    if not isinstance(adjudication, Mapping) or adjudication.get("receipt_schema") != VISUAL_REVIEW_ADJUDICATION_RECEIPT_SCHEMA:
        raise VisualBenchmarkError(f"gold_adjudication_provenance_missing:{item_id}")
    receipt = adjudication.get("receipt")
    if not isinstance(receipt, Mapping) or receipt.get("schema_version") != VISUAL_REVIEW_ADJUDICATION_RECEIPT_SCHEMA or receipt.get("protocol") != VISUAL_REVIEW_ADJUDICATION_PROTOCOL or receipt.get("compiler_version") != VISUAL_REVIEW_ADJUDICATION_COMPILER_VERSION:
        raise VisualBenchmarkError(f"gold_adjudication_receipt_schema_invalid:{item_id}")
    if receipt.get("receipt_sha256") != sha256_json({key: value for key, value in receipt.items() if key != "receipt_sha256"}) or adjudication.get("receipt_sha256") != receipt.get("receipt_sha256"):
        raise VisualBenchmarkError(f"gold_adjudication_receipt_hash_invalid:{item_id}")
    if adjudication.get("manifest_sha256") != receipt.get("adjudication_manifest_sha256") or adjudication.get("dispute_set_sha256") != receipt.get("dispute_set_sha256") or adjudication.get("source_submission_sha256s") != receipt.get("source_submission_sha256s"):
        raise VisualBenchmarkError(f"gold_adjudication_binding_invalid:{item_id}")
    identity = receipt.get("workpack_identity")
    expected_identity = {
        "phase7d5a_manifest_sha256": facts.get("manifest_sha256"),
        "candidate_items_file_sha256": facts.get("candidate_items_sha256"),
        "split_commitment_sha256": (facts.get("split") or {}).get("commitment_sha256"),
        "review_plan_sha256": facts.get("review_plan_sha256"),
    }
    if not isinstance(identity, Mapping) or any(identity.get(key) != value for key, value in expected_identity.items()) or not _is_hash(identity.get("workpack_sha256")):
        raise VisualBenchmarkError(f"gold_adjudication_workpack_identity_invalid:{item_id}")
    adjudicator = receipt.get("adjudicator")
    adjudicator_id = adjudicator.get("adjudicator_instance") if isinstance(adjudicator, Mapping) else None
    if not isinstance(adjudicator_id, str) or not adjudicator_id or adjudicator_id in set(registry_ids) or adjudicator_id in proposer_instances or (not isinstance(adjudicator, Mapping) or adjudicator.get("role") != "third-party-independent"):
        raise VisualBenchmarkError(f"gold_adjudication_independence_invalid:{item_id}")
    declarations = receipt.get("declarations")
    if not isinstance(declarations, Mapping) or any(declarations.get(key) is not True for key in ("qualified_adjudicator", "reviewed_specified_context_crop", "reviewed_two_reviewer_results", "did_not_view_predictions", "did_not_view_split_or_scores", "independent_from_reviewers", "independent_from_proposer", "not_evaluator")):
        raise VisualBenchmarkError(f"gold_adjudication_declarations_invalid:{item_id}")
    source_submissions = receipt.get("source_submissions")
    if not isinstance(source_submissions, list) or len(source_submissions) != 2 or len({value.get("reviewer_instance") for value in source_submissions if isinstance(value, Mapping)}) != 2:
        raise VisualBenchmarkError(f"gold_adjudication_source_submissions_invalid:{item_id}")
    if sorted(str(value.get("reviewer_instance")) for value in source_submissions if isinstance(value, Mapping)) != sorted(str(value) for value in registry_ids):
        raise VisualBenchmarkError(f"gold_adjudication_reviewer_binding_invalid:{item_id}")
    receipt_items = receipt.get("items")
    if not isinstance(receipt_items, list) or len(receipt_items) != int(receipt.get("item_count", -1)) or item_id not in {str(value.get("item_id")) for value in receipt_items if isinstance(value, Mapping)}:
        raise VisualBenchmarkError(f"gold_adjudication_dispute_set_invalid:{item_id}")
    item_record = next((value for value in receipt_items if isinstance(value, Mapping) and value.get("item_id") == item_id), None)
    if not isinstance(item_record, Mapping) or item_record.get("item_sha256") != row.get("item_sha256") or item_record.get("review_task_id") != row.get("review_task_id") or item_record.get("input_hashes") != row.get("input_hashes") or item_record.get("resolved_payload_sha256") != sha256_json(payload):
        raise VisualBenchmarkError(f"gold_adjudication_payload_binding_invalid:{item_id}")
    resolution_core = {"item_id": item_id, "resolutions": item_record.get("resolutions"), "rationale": item_record.get("rationale"), "observation": item_record.get("observation")}
    if item_record.get("resolution_sha256") != sha256_json(resolution_core) or adjudication.get("item_resolution_sha256") != item_record.get("resolution_sha256"):
        raise VisualBenchmarkError(f"gold_adjudication_resolution_hash_invalid:{item_id}")
    reviewer_payload_hashes = item_record.get("reviewer_payload_sha256s")
    if not isinstance(reviewer_payload_hashes, Mapping) or not all(_is_hash(reviewer_payload_hashes.get(key)) for key in ("reviewer-1", "reviewer-2")):
        raise VisualBenchmarkError(f"gold_adjudication_source_payload_hash_invalid:{item_id}")
    if not isinstance(item_record.get("rationale"), str) or not item_record.get("rationale").strip() or not isinstance(item_record.get("observation"), str) or not item_record.get("observation").strip():
        raise VisualBenchmarkError(f"gold_adjudication_observation_invalid:{item_id}")
    attestation_sessions = {str(value.get("reviewer_instance")): value.get("review_session_id") for value in attestations if isinstance(value, Mapping)}
    for source in source_submissions:
        if isinstance(source, Mapping) and attestation_sessions.get(str(source.get("reviewer_instance"))) != source.get("review_session_id"):
            raise VisualBenchmarkError(f"gold_adjudication_session_binding_invalid:{item_id}")


def _validate_gold_fragment_row(row: Mapping[str, Any], facts: Mapping[str, Any], *, proposer_instances: set[str]) -> dict[str, Any]:
    item_id = row.get("item_id")
    if not isinstance(item_id, str) or item_id not in (facts.get("candidate_by_id") or {}):
        raise VisualBenchmarkError("gold_item_unknown")
    item = facts["candidate_by_id"][item_id]
    task = facts["task_by_item"][item_id]
    if row.get("schema_version") != VISUAL_BENCHMARK_GOLD_FRAGMENT_SCHEMA or row.get("protocol") != VISUAL_BENCHMARK_PROTOCOL:
        raise VisualBenchmarkError(f"gold_schema_invalid:{item_id}")
    if row.get("fragment_sha256") != _fragment_hash(row):
        raise VisualBenchmarkError(f"gold_fragment_hash_mismatch:{item_id}")
    if row.get("item_sha256") != item.get("item_sha256") or row.get("review_task_id") != task.get("review_task_id") or row.get("kind_candidate") != item.get("kind_candidate"):
        raise VisualBenchmarkError(f"gold_item_binding_invalid:{item_id}")
    expected_split = "calibration" if item_id in facts["calibration_ids"] else "test"
    if row.get("split") != expected_split:
        raise VisualBenchmarkError(f"gold_split_binding_invalid:{item_id}")
    expected_hashes = _gold_input_hashes(item, task)
    if row.get("input_hashes") != expected_hashes:
        raise VisualBenchmarkError(f"gold_input_hash_binding_invalid:{item_id}")
    if row.get("independent_real_gold") is not True or row.get("verified_gold") is not False or row.get("promotion_allowed") is not False:
        raise VisualBenchmarkError(f"gold_policy_invalid:{item_id}")
    payload = row.get("gold_payload")
    if not isinstance(payload, Mapping):
        raise VisualBenchmarkError(f"gold_payload_invalid:{item_id}")
    _assert_nonempty_mapping(payload, f"gold_payload_empty:{item_id}")
    registry = row.get("reviewer_registry")
    if not isinstance(registry, list) or not registry:
        raise VisualBenchmarkError(f"gold_reviewer_registry_missing:{item_id}")
    registry_ids: list[str] = []
    for member in registry:
        if not isinstance(member, Mapping) or not isinstance(member.get("reviewer_instance"), str) or not member.get("reviewer_instance") or member.get("reviewer_role") != "external-independent" or member.get("independent_from_proposer") is not True:
            raise VisualBenchmarkError(f"gold_reviewer_registry_invalid:{item_id}")
        reviewer_id = str(member["reviewer_instance"])
        if reviewer_id in registry_ids or reviewer_id in proposer_instances:
            raise VisualBenchmarkError(f"gold_reviewer_not_independent:{item_id}")
        registry_ids.append(reviewer_id)
    expected_reviewers = int(task.get("required_independent_reviewers", 0))
    if len(registry_ids) != expected_reviewers:
        raise VisualBenchmarkError(f"gold_reviewer_count_invalid:{item_id}")
    registry_hash = row.get("reviewer_registry_sha256")
    if registry_hash is not None and registry_hash != sha256_json(registry):
        raise VisualBenchmarkError(f"gold_reviewer_registry_hash_mismatch:{item_id}")
    attestations = row.get("attestations")
    if not isinstance(attestations, list) or len(attestations) != expected_reviewers:
        raise VisualBenchmarkError(f"gold_attestation_count_invalid:{item_id}")
    attestation_ids: set[str] = set()
    attestation_reviewers: set[str] = set()
    for attestation in attestations:
        if not isinstance(attestation, Mapping):
            raise VisualBenchmarkError(f"gold_attestation_invalid:{item_id}")
        if attestation.get("schema_version") != VISUAL_BENCHMARK_ATTESTATION_SCHEMA or attestation.get("protocol") != VISUAL_BENCHMARK_PROTOCOL or attestation.get("attestation_sha256") != _attestation_hash(attestation):
            raise VisualBenchmarkError(f"gold_attestation_hash_or_schema_invalid:{item_id}")
        reviewer = attestation.get("reviewer_instance")
        if not isinstance(reviewer, str) or reviewer not in registry_ids or reviewer in attestation_reviewers or reviewer in proposer_instances:
            raise VisualBenchmarkError(f"gold_attestation_reviewer_not_independent:{item_id}")
        if not isinstance(attestation.get("attestation_id"), str) or attestation["attestation_id"] in attestation_ids:
            raise VisualBenchmarkError(f"gold_attestation_duplicate:{item_id}")
        if attestation.get("reviewer_role") != "external-independent" or attestation.get("review_method") != "fresh-crop-and-render-inspection" or not isinstance(attestation.get("review_session_id"), str) or not attestation.get("review_session_id") or attestation.get("proposer_instance") not in proposer_instances or not isinstance(attestation.get("difference_observation"), str) or not attestation.get("difference_observation").strip():
            raise VisualBenchmarkError(f"gold_attestation_provenance_invalid:{item_id}")
        checks = attestation.get("review_checks")
        if not isinstance(checks, Mapping) or any(checks.get(key) is not True for key in REQUIRED_REVIEW_CHECKS):
            raise VisualBenchmarkError(f"gold_attestation_checks_incomplete:{item_id}")
        if attestation.get("input_hashes") != {"item_sha256": item["item_sha256"], **expected_hashes}:
            raise VisualBenchmarkError(f"gold_attestation_input_hash_invalid:{item_id}")
        attestation_ids.add(str(attestation["attestation_id"]))
        attestation_reviewers.add(reviewer)
    provenance = row.get("fragment_provenance")
    if not isinstance(provenance, Mapping) or provenance.get("source_type") != "external-independent-review-file" or provenance.get("created_outside_preparation_agent") is not True or provenance.get("compiler_authored") is not False or provenance.get("evaluator_role_separate") is not True:
        raise VisualBenchmarkError(f"gold_fragment_provenance_invalid:{item_id}")
    _validate_adjudication_gold_provenance(row, facts, payload, registry_ids, attestations, proposer_instances)
    normalized = dict(row)
    normalized["reviewer_registry_sha256"] = registry_hash or sha256_json(registry)
    normalized["gold_payload_sha256"] = sha256_json(payload)
    # The imported private row carries two derived audit hashes that were not
    # required from the external reviewer.  Re-seal the normalized transport
    # row after adding them; the original external fragment hash was checked
    # above before this normalization.
    normalized["fragment_sha256"] = _fragment_hash(normalized)
    return normalized


def _assert_nonempty_mapping(value: Mapping[str, Any], code: str) -> None:
    if not value or all(nested is None or nested == {} or nested == [] for nested in value.values()):
        raise VisualBenchmarkError(code)


def validate_external_gold_fragments(facts: Mapping[str, Any], fragment_paths: Sequence[Path]) -> dict[str, Any]:
    """Validate external reviewer files without authoring or repairing rows."""

    if not fragment_paths:
        raise VisualBenchmarkError("external_gold_fragment_required")
    workpack_root = Path(str(facts["root"])).resolve()
    rows: list[dict[str, Any]] = []
    file_hashes: list[str] = []
    for supplied in fragment_paths:
        path = _secure_file(Path(supplied), "external_gold_fragment")
        if path.is_relative_to(workpack_root):
            raise VisualBenchmarkError("gold_fragment_must_be_external_to_workpack")
        file_hashes.append(sha256_file(path))
        rows.extend(_read_jsonl(path))
    expected_ids = set(str(value) for value in facts["candidate_ids"])
    if len(rows) != len(expected_ids) or {str(row.get("item_id")) for row in rows} != expected_ids:
        raise VisualBenchmarkError("external_gold_candidate_set_incomplete_or_duplicate")
    proposer_instances = set(str(value) for value in (facts.get("review_plan") or {}).get("proposer_instances", []) if isinstance(value, str))
    normalized = [_validate_gold_fragment_row(row, facts, proposer_instances=proposer_instances) for row in rows]
    normalized.sort(key=lambda row: str(row["item_id"]))
    return {
        "rows": normalized,
        "external_file_sha256s": sorted(file_hashes),
        "item_count": len(normalized),
        "attestation_count": sum(len(row["attestations"]) for row in normalized),
        "reviewer_instances": sorted({str(attestation["reviewer_instance"]) for row in normalized for attestation in row["attestations"]}),
        "candidate_set_sha256": sha256_json(sorted((row["item_id"], row["item_sha256"]) for row in normalized)),
        "split_commitment_sha256": str(facts["split"]["commitment_sha256"]),
        "independent_real_gold": True,
        "verified_gold": False,
        "promotion_allowed": False,
        "release_included": False,
    }


def _load_imported_gold(job: Path, facts: Mapping[str, Any]) -> dict[str, Any] | None:
    rows_path = job / "gold" / "imported-gold-fragments.jsonl"
    receipt_path = job / "gold" / "import-receipt.json"
    if not rows_path.exists() and not receipt_path.exists():
        return None
    if not rows_path.is_file() or not receipt_path.is_file():
        raise VisualBenchmarkError("gold_import_artifact_incomplete")
    receipt = _read_json(receipt_path)
    rows = _read_jsonl(rows_path)
    proposer_instances = set(str(value) for value in (facts.get("review_plan") or {}).get("proposer_instances", []) if isinstance(value, str))
    normalized = [_validate_gold_fragment_row(row, facts, proposer_instances=proposer_instances) for row in rows]
    normalized.sort(key=lambda row: str(row["item_id"]))
    if receipt.get("schema_version") != VISUAL_BENCHMARK_STATE_SCHEMA.replace("state", "gold-import") or receipt.get("protocol") != VISUAL_BENCHMARK_PROTOCOL or receipt.get("rows_sha256") != sha256_file(rows_path) or receipt.get("receipt_sha256") != _hash_without(receipt, "receipt_sha256"):
        raise VisualBenchmarkError("gold_import_receipt_mismatch")
    return {"rows": normalized, **receipt}


def import_external_gold(job: Path, fragment_paths: Sequence[Path]) -> dict[str, Any]:
    job = _secure_directory(job)
    plan, facts = _load_job_and_facts(job)
    imported = validate_external_gold_fragments(facts, fragment_paths)
    gold_dir = _secure_directory(job / "gold")
    rows_path = gold_dir / "imported-gold-fragments.jsonl"
    receipt_path = gold_dir / "import-receipt.json"
    if rows_path.exists() or receipt_path.exists():
        if not rows_path.is_file() or not receipt_path.is_file():
            raise VisualBenchmarkError("gold_import_artifact_incomplete")
        existing = _load_imported_gold(job, facts)
        if existing is None or sorted(existing.get("external_file_sha256s", [])) != imported["external_file_sha256s"]:
            raise VisualBenchmarkError("gold_import_already_sealed")
        status = _refresh_job_state(job)
        return {"status": status, "gold_import": _read_json(receipt_path), "idempotent": True}
    _write_jsonl(rows_path, imported["rows"])
    receipt = {
        "schema_version": "tkc.visual-benchmark-gold-import/v0.1",
        "protocol": VISUAL_BENCHMARK_PROTOCOL,
        "compiler_version": VISUAL_BENCHMARK_COMPILER_VERSION,
        "job_id": plan["job_id"],
        "rows_sha256": sha256_file(rows_path),
        "external_file_sha256s": imported["external_file_sha256s"],
        "candidate_set_sha256": imported["candidate_set_sha256"],
        "split_commitment_sha256": imported["split_commitment_sha256"],
        "item_count": imported["item_count"],
        "attestation_count": imported["attestation_count"],
        "reviewer_instances": imported["reviewer_instances"],
        "independent_real_gold": True,
        "verified_gold": False,
        "promotion_allowed": False,
        "release_included": False,
    }
    receipt["receipt_sha256"] = _hash_without(receipt, "receipt_sha256")
    _write_json(gold_dir / "import-receipt.json", receipt)
    status = _refresh_job_state(job)
    return {"status": status, "gold_import": receipt}


def import_qualification_receipts(job: Path, table_receipt: Path | None, chart_receipt: Path | None) -> dict[str, Any]:
    job = _secure_directory(job)
    plan, facts = _load_job_and_facts(job)
    receipt_sources = {"table": table_receipt, "chart": chart_receipt}
    validated_receipts: list[tuple[str, dict[str, Any]]] = []
    # Validate every supplied receipt before creating the destination or
    # copying any one of them.  A rejected Chart2Table receipt must not leave
    # a valid table receipt partially imported into the benchmark job.
    for label, supplied in receipt_sources.items():
        if supplied is None:
            continue
        path = _secure_file(supplied, f"qualification_receipt:{label}")
        backend = "paddleocr-ppstructure-v3" if label == "table" else "paddleocr-chart-parsing"
        entry = facts["qualification_plans"][label]
        receipt = _read_json(path)
        try:
            validate_runtime_qualification(receipt, contract=entry["plan"]["runtime_contract"], plan=entry["plan"], backend_id=backend, profile_id=entry["plan"]["profile_id"], require_qualified=True)
        except PaddleRuntimeQualificationError as error:
            raise VisualBenchmarkError(f"qualification_receipt_rejected:{label}:{error}") from error
        validated_receipts.append((label, receipt))
    qualification_dir = _secure_directory(job / "qualification")
    for label, receipt in validated_receipts:
        destination = qualification_dir / f"{label}-qualification-receipt.json"
        if destination.is_file():
            existing = _read_json(destination)
            if existing.get("receipt_sha256") != receipt.get("receipt_sha256"):
                raise VisualBenchmarkError(f"qualification_receipt_already_sealed:{label}")
        else:
            _write_json(destination, receipt)
    status = _refresh_job_state(job)
    return {"status": status}


def _load_job_and_facts(job: Path, workpack_override: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load a benchmark plan and replay its immutable 7D.5A input boundary."""

    job = _secure_directory(job)
    plan_path = _secure_file(job / "benchmark-plan.json", "benchmark_plan")
    plan = _read_json(plan_path)
    if plan.get("schema_version") != VISUAL_BENCHMARK_PLAN_SCHEMA or plan.get("protocol") != VISUAL_BENCHMARK_PROTOCOL:
        raise VisualBenchmarkError("benchmark_plan_schema_invalid")
    if plan.get("plan_sha256") != _hash_without(plan, "plan_sha256"):
        raise VisualBenchmarkError("benchmark_plan_hash_mismatch")
    if plan.get("target_inference") != {"model_id": TARGET_MODEL_ID, "thinking": TARGET_THINKING, "authorization_required": True}:
        raise VisualBenchmarkError("benchmark_target_inference_invalid")
    policy = plan.get("policy") if isinstance(plan.get("policy"), Mapping) else {}
    expected_policy = {
        "network_enabled": False,
        "model_download": False,
        "mcp_http_dependency": False,
        "silent_fallback": False,
        "candidate_only": True,
        "promotion": False,
        "release_included": False,
        "inference_execution_allowed": False,
    }
    if policy != expected_policy:
        raise VisualBenchmarkError("benchmark_plan_policy_invalid")
    workpack_binding = plan.get("workpack") if isinstance(plan.get("workpack"), Mapping) else {}
    locator = workpack_binding.get("locator")
    if not isinstance(locator, str) or not locator or Path(locator).is_absolute():
        raise VisualBenchmarkError("benchmark_workpack_locator_invalid")
    expected_binding_hash = sha256_json({"role": "private-workpack-binding", "manifest_sha256": workpack_binding.get("manifest_sha256"), "candidate_items_sha256": workpack_binding.get("candidate_items_sha256")})
    if locator == "private-workpack-binding":
        if workpack_binding.get("binding_sha256") != expected_binding_hash:
            raise VisualBenchmarkError("benchmark_workpack_binding_hash_invalid")
        binding_path = _secure_file(job / "workpack-binding.json", "workpack_binding")
        private_binding = _read_json(binding_path)
        if private_binding.get("job_id") != plan.get("job_id") or private_binding.get("binding_sha256") != expected_binding_hash:
            raise VisualBenchmarkError("benchmark_workpack_private_binding_invalid")
        resolved_locator = private_binding.get("resolved_locator")
        if not isinstance(resolved_locator, str) or not Path(resolved_locator).is_absolute():
            raise VisualBenchmarkError("benchmark_workpack_private_locator_invalid")
        workpack = Path(resolved_locator).expanduser().resolve()
    else:
        workpack = (job / locator).resolve()
    if workpack_override is not None:
        workpack = Path(workpack_override).expanduser().resolve()
    if not workpack.is_dir():
        raise VisualBenchmarkError("benchmark_workpack_missing")
    facts = load_phase7d5a_workpack(workpack)
    binding = plan.get("workpack") if isinstance(plan.get("workpack"), Mapping) else {}
    if binding.get("manifest_sha256") != facts["manifest_sha256"] or binding.get("candidate_items_sha256") != facts["candidate_items_sha256"]:
        raise VisualBenchmarkError("benchmark_workpack_binding_mismatch")
    if (plan.get("split_commitment") or {}).get("commitment_sha256") != facts["split"]["commitment_sha256"]:
        raise VisualBenchmarkError("benchmark_split_binding_mismatch")
    return plan, facts


def _qualification_receipts(job: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for label in ("table", "chart"):
        path = job / "qualification" / f"{label}-qualification-receipt.json"
        if path.is_file():
            result[label] = _read_json(path)
    return result


def _load_evaluation_records(job: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for split in ("calibration", "test"):
        for mode in MODES:
            evaluation_path = job / "evaluation" / split / f"{mode}.json"
            if evaluation_path.is_file():
                evaluation = _read_json(evaluation_path)
                records[f"{mode}:{split}"] = {"status": evaluation.get("status"), "evaluation_sha256": evaluation.get("evaluation_sha256")}
            prediction_path = job / "predictions" / split / f"{mode}.jsonl"
            if prediction_path.is_file():
                for row in _read_jsonl(prediction_path):
                    if isinstance(row.get("item_id"), str) and isinstance(row.get("output_sha256"), str):
                        records[f"{mode}:{row['item_id']}"] = {"status": "measured", "output_sha256": row["output_sha256"]}
    return records


def _load_authorization(job: Path) -> dict[str, Any] | None:
    path = job / "authorization" / "real-inference.json"
    if not path.is_file():
        return None
    value = _read_json(path)
    if value.get("authorization_sha256") != _hash_without(value, "authorization_sha256") or value.get("authorized") is not True or value.get("model_id") != TARGET_MODEL_ID or value.get("thinking") != TARGET_THINKING or value.get("network_enabled") is not False or value.get("model_download") is not False or value.get("mcp_used") is not False:
        raise VisualBenchmarkError("inference_authorization_invalid")
    return value


def _stable(kind: str, value: str) -> str:
    return f"{kind}:{value}"


def _benchmark_snapshot(
    plan: Mapping[str, Any],
    facts: Mapping[str, Any],
    *,
    gold: Mapping[str, Any] | None = None,
    qualification: Mapping[str, Any] | None = None,
    evaluations: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a content-addressed snapshot for the existing incremental DAG."""

    nodes: list[dict[str, Any]] = []
    benchmark_id = str(plan["job_id"])
    nodes.append({
        "kind": "visual_benchmark",
        "stable_id": _stable("benchmark", benchmark_id),
        "benchmark_id": benchmark_id,
        "protocol_id": VISUAL_BENCHMARK_PROTOCOL,
        "compiler_version": VISUAL_BENCHMARK_COMPILER_VERSION,
        "plan_sha256": plan["plan_sha256"],
        "manifest_sha256": facts["manifest_sha256"],
        "candidate_set_sha256": sha256_json(sorted(str(value) for value in facts["candidate_ids"])),
        "split_commitment_sha256": facts["split"]["commitment_sha256"],
        "visual_modes": list(MODES),
        "candidate_only": True,
        "release_included": False,
    })

    source_hashes = sorted({str((item.get("source") or {}).get("source_sha256")) for item in facts["candidate_items"]})
    render_hashes = sorted({str((item.get("render") or {}).get("sha256")) for item in facts["candidate_items"]})
    for source_sha in source_hashes:
        nodes.append({
            "kind": "source",
            "stable_id": _stable("source", source_sha),
            "source_id": _stable("source", source_sha),
            "source_sha256": source_sha,
            "candidate_only": True,
            "release_included": False,
        })
    for render_sha in render_hashes:
        nodes.append({
            "kind": "render",
            "stable_id": _stable("render", render_sha),
            "render_id": _stable("render", render_sha),
            "render_sha256": render_sha,
            "dpi": 120,
            "rotation": 0,
            "candidate_only": True,
            "release_included": False,
        })
    for item in sorted(facts["candidate_items"], key=lambda row: str(row["item_id"])):
        item_id = str(item["item_id"])
        crop_sha = str((item.get("crop") or {}).get("sha256"))
        source_sha = str((item.get("source") or {}).get("source_sha256"))
        render_sha = str((item.get("render") or {}).get("sha256"))
        nodes.append({
            "kind": "raster_crop",
            "stable_id": _stable("crop", crop_sha),
            "render_id": _stable("render", render_sha),
            "source_id": _stable("source", source_sha),
            "crop_sha256": crop_sha,
            "candidate_only": True,
            "release_included": False,
        })
        nodes.append({
            "kind": "visual_candidate",
            "stable_id": _stable("candidate", item_id),
            "candidate_record_id": item_id,
            "item_id": item_id,
            "item_sha256": item["item_sha256"],
            "kind_candidate": item["kind_candidate"],
            "split": item["split"],
            "source_id": _stable("source", source_sha),
            "render_id": _stable("render", render_sha),
            "crop_id": _stable("crop", crop_sha),
            "input_ids": [_stable("source", source_sha), _stable("render", render_sha), _stable("crop", crop_sha)],
            "candidate_only": True,
            "verified_gold": False,
            "release_included": False,
        })

    review_plan_id = _stable("review-plan", facts["review_plan_sha256"])
    nodes.append({
        "kind": "review_plan",
        "stable_id": review_plan_id,
        "review_plan_id": review_plan_id,
        "review_plan_sha256": facts["review_plan_sha256"],
        "input_ids": [_stable("candidate", str(value)) for value in sorted(facts["candidate_ids"])],
        "reviewer_requirement": len(facts["candidate_ids"]),
        "candidate_only": True,
        "independent_review_required": True,
        "release_included": False,
    })
    for item_id in sorted(facts["candidate_ids"]):
        item = facts["candidate_by_id"][item_id]
        task = facts["task_by_item"][item_id]
        gold_row = next((row for row in (gold or {}).get("rows", []) if row.get("item_id") == item_id), None)
        gold_id = _stable("gold", item_id)
        gold_node: dict[str, Any] = {
            "kind": "visual_gold",
            "stable_id": gold_id,
            "gold_id": gold_id,
            "item_id": item_id,
            "item_sha256": item["item_sha256"],
            "split": item["split"],
            "input_ids": [_stable("candidate", item_id), review_plan_id],
            "status": "accepted-independent-review" if gold_row else "awaiting-external-independent-review",
            "independent_real_gold": bool(gold_row),
            "verified_gold": False,
            "promotion_allowed": False,
            "release_included": False,
        }
        if gold_row:
            gold_node.update({
                "fragment_sha256": gold_row.get("fragment_sha256"),
                "gold_payload_sha256": gold_row.get("gold_payload_sha256"),
                "attestation_hashes": sorted(str(row.get("attestation_sha256")) for row in gold_row.get("attestations", []) if isinstance(row, Mapping)),
            })
        nodes.append(gold_node)

    qualification = qualification or {}
    for label, backend in (("table", "paddleocr-ppstructure-v3"), ("chart", "paddleocr-chart-parsing")):
        entry = facts["qualification_plans"][label]
        receipt = (qualification.get("routes") or {}).get(label) if isinstance(qualification.get("routes"), Mapping) else None
        receipt_node: dict[str, Any] = {
            "kind": "runtime_qualification",
            "stable_id": _stable("qualification", backend),
            "qualification_id": _stable("qualification", backend),
            "backend_id": backend,
            "profile_id": entry["plan"]["profile_id"],
            "plan_sha256": entry["plan"]["plan_sha256"],
            "qualification_tool_sha256": entry["binding"]["qualification_tool_sha256"],
            "runtime_contract_sha256": entry["binding"]["runtime_contract_sha256"],
            "configuration_sha256": entry["binding"]["configuration_sha256"],
            "status": (receipt or {}).get("status", "paused-resource-handoff"),
            "input_ids": [_stable("benchmark", benchmark_id)],
            "candidate_only": True,
            "release_included": False,
        }
        if receipt and receipt.get("receipt_sha256"):
            receipt_node["receipt_ids"] = [str(receipt["receipt_sha256"])]
            receipt_node["receipt_sha256"] = str(receipt["receipt_sha256"])
        nodes.append(receipt_node)

    model_id = _stable("model", TARGET_MODEL_ID)
    nodes.append({
        "kind": "model",
        "stable_id": model_id,
        "record_id": TARGET_MODEL_ID,
        "model_id": TARGET_MODEL_ID,
        "thinking": TARGET_THINKING,
        "authorization_required": True,
        "candidate_only": True,
        "release_included": False,
    })
    for mode in MODES:
        config_id = _stable("config", mode)
        config_hash = sha256_json({"visual_mode": mode, "model_id": TARGET_MODEL_ID, "thinking": TARGET_THINKING, "protocol": VISUAL_BENCHMARK_PROTOCOL})
        nodes.append({
            "kind": "config",
            "stable_id": config_id,
            "config_id": config_id,
            "config_hash": config_hash,
            "visual_mode": mode,
            "model_id": TARGET_MODEL_ID,
            "thinking": TARGET_THINKING,
            "candidate_only": True,
            "release_included": False,
        })
        for item_id in sorted(facts["candidate_ids"]):
            item = facts["candidate_by_id"][item_id]
            input_ids = [_stable("candidate", item_id), config_id, model_id]
            if mode in {"auto", "full"} and item["kind_candidate"] == "table":
                input_ids.append(_stable("qualification", "paddleocr-ppstructure-v3"))
            if mode in {"auto", "full"} and item["kind_candidate"] == "chart":
                input_ids.append(_stable("qualification", "paddleocr-chart-parsing"))
            result_row = (evaluations or {}).get(f"{mode}:{item_id}") if isinstance(evaluations, Mapping) else None
            mode_node: dict[str, Any] = {
                "kind": "visual_mode_result",
                "stable_id": _stable("mode-result", f"{mode}:{item_id}"),
                "mode_result_id": f"{mode}:{item_id}",
                "item_id": item_id,
                "split": item["split"],
                "visual_mode": mode,
                "status": (result_row or {}).get("status", "planned"),
                "input_ids": input_ids,
                "candidate_only": True,
                "release_included": False,
            }
            if result_row and result_row.get("output_sha256"):
                mode_node["output_sha256"] = result_row["output_sha256"]
            nodes.append(mode_node)
        for split in ("calibration", "test"):
            result_ids = [_stable("mode-result", f"{mode}:{item_id}") for item_id in sorted(facts[f"{split}_ids"])]
            gold_ids = [_stable("gold", item_id) for item_id in sorted(facts[f"{split}_ids"])]
            evaluation_id = _stable("evaluation", f"{mode}:{split}")
            evaluation_row = (evaluations or {}).get(f"{mode}:{split}") if isinstance(evaluations, Mapping) else None
            nodes.append({
                "kind": "visual_evaluation",
                "stable_id": evaluation_id,
                "evaluation_id": f"evaluation:{mode}:{split}",
                "visual_mode": mode,
                "split": split,
                "status": (evaluation_row or {}).get("status", "paused"),
                "input_ids": result_ids + gold_ids,
                "candidate_only": True,
                "verified_gold": False,
                "promotion_allowed": False,
                "release_included": False,
            })
            nodes.append({
                "kind": "cost_ledger",
                "stable_id": _stable("ledger", f"{mode}:{split}"),
                "ledger_id": f"ledger:{mode}:{split}",
                "visual_mode": mode,
                "split": split,
                "input_ids": result_ids,
                "status": (evaluation_row or {}).get("status", "paused"),
                "candidate_only": True,
                "release_included": False,
            })
    return {"nodes": nodes, "edges": [], "schema_version": "tkc.visual-benchmark-dag-snapshot/v0.1", "protocol": VISUAL_BENCHMARK_PROTOCOL}


def _snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    return sha256_json(snapshot)


def _refresh_dag(
    job: Path,
    plan: Mapping[str, Any],
    facts: Mapping[str, Any],
    *,
    gold: Mapping[str, Any] | None = None,
    qualification: Mapping[str, Any] | None = None,
    evaluations: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    snapshot = _benchmark_snapshot(plan, facts, gold=gold, qualification=qualification, evaluations=evaluations)
    snapshot_path = job / "snapshot.json"
    dag_root = _secure_directory(job / "dag")
    revisions = _secure_directory(dag_root / "revisions")
    old = _read_json(snapshot_path) if snapshot_path.is_file() else None
    snapshot_hash = _snapshot_hash(snapshot)
    current = _read_json(job / "dag-current.json") if (job / "dag-current.json").is_file() else None
    if old is not None and _snapshot_hash(old) == snapshot_hash and isinstance(current, Mapping):
        revision_path = (job / str(current.get("revision_locator", ""))).resolve()
        return {"snapshot_sha256": snapshot_hash, "revision_locator": str(revision_path.relative_to(job)), "manifest": _read_json(revision_path / "dag-manifest.json")}
    revision_number = 0
    if isinstance(current, Mapping):
        try:
            revision_number = int(str(current.get("revision_locator", "")).split("/")[-1].split("-", 1)[0]) + 1
        except (TypeError, ValueError):
            revision_number = len([path for path in revisions.iterdir() if path.is_dir()])
    revision = revisions / f"{revision_number:04d}-{snapshot_hash[:16]}"
    if old is None:
        result = build_dag(snapshot, revision, generated_at=str(plan["created_at"]))
    else:
        result = compare_snapshots(old, snapshot, revision, generated_at=str(plan["created_at"]))
    issues = validate_dag_directory(revision)
    if issues:
        raise VisualBenchmarkError("benchmark_dag_invalid:" + ",".join(str(row.get("code")) for row in issues))
    _write_json(snapshot_path, snapshot)
    _write_json(job / "dag-current.json", {
        "schema_version": "tkc.visual-benchmark-dag-current/v0.1",
        "protocol": VISUAL_BENCHMARK_PROTOCOL,
        "job_id": plan["job_id"],
        "snapshot_sha256": snapshot_hash,
        "revision_locator": str(revision.relative_to(job)),
        "dag_sha256": result["manifest"]["dag_sha256"],
    })
    return {"snapshot_sha256": snapshot_hash, "revision_locator": str(revision.relative_to(job)), "manifest": result["manifest"]}


def _gates_for_state(facts: Mapping[str, Any], gold: Mapping[str, Any] | None, qualification: Mapping[str, Any]) -> list[dict[str, Any]]:
    gates: list[dict[str, Any]] = []
    if gold is None:
        gates.append({"gate": "independent-gold", "code": "gold_not_imported", "status": "paused", "reason": "external reviewer Gold for all 31 unique candidates is not imported"})
    else:
        if gold.get("independent_real_gold") is not True or gold.get("verified_gold") is not False or gold.get("promotion_allowed") is not False:
            gates.append({"gate": "independent-gold", "code": "gold_policy_invalid", "status": "rejected", "reason": "Gold policy boundary is not independently attested"})
        if gold.get("split_commitment_sha256") != facts["split"]["commitment_sha256"]:
            gates.append({"gate": "independent-gold", "code": "gold_split_commitment_mismatch", "status": "rejected", "reason": "Gold split commitment does not match 7D.5A"})
    for label, route in sorted((qualification.get("routes") or {}).items()):
        if route.get("status") != "qualified":
            for code in route.get("issue_codes", []) or ["qualification_not_ready"]:
                gates.append({"gate": "resource-handoff", "code": str(code), "backend": route.get("backend_id"), "status": "paused" if qualification.get("status") == "paused-resource-handoff" else "rejected", "reason": "direct private qualification receipt is not available"})
    if not gates:
        gates.append({"gate": "independent-gold", "code": "independent_gold_ready", "status": "ready", "reason": "external Gold import passed structural gates; verified_gold remains false"})
        gates.append({"gate": "resource-handoff", "code": "runtime_qualification_ready", "status": "ready", "reason": "both direct current-plan qualification receipts passed"})
    # Keep the readiness receipts visible in status while adding later gates;
    # this makes a resume explain exactly which boundary remains.  A route can
    # surface the same issue through both its summary and its issue list, so
    # collapse that duplicate without changing order semantics.
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for gate in gates:
        key = (str(gate.get("gate")), str(gate.get("code")), str(gate.get("backend", "")))
        unique.setdefault(key, gate)
    return [unique[key] for key in sorted(unique)]


def _refresh_job_state(job: Path) -> dict[str, Any]:
    plan, facts = _load_job_and_facts(job)
    try:
        gold = _load_imported_gold(job, facts)
    except VisualBenchmarkError as error:
        gold = None
        gold_error = str(error)
    else:
        gold_error = None
    qualification = validate_qualification_gate(facts, _qualification_receipts(job))
    gates = _gates_for_state(facts, gold, qualification)
    if gold_error:
        gates = [{"gate": "independent-gold", "code": "gold_import_invalid", "status": "rejected", "reason": gold_error}] + [row for row in gates if row.get("gate") != "independent-gold"]
    has_rejection = any(row.get("status") == "rejected" for row in gates)
    authorization = None
    if gold is not None and not gold_error and qualification.get("status") == "qualified":
        try:
            authorization = _load_authorization(job)
        except VisualBenchmarkError as error:
            gates.append({"gate": "inference-authorization", "code": "inference_authorization_invalid", "status": "rejected", "reason": str(error)})
        if authorization is None:
            gates.append({"gate": "inference-authorization", "code": "inference_authorization_required", "status": "paused", "reason": "operator authorization is required before frozen GPT-5.6 Luna inference"})
        else:
            calibration_files = [job / "calibration" / mode / "threshold-config.json" for mode in MODES]
            if not any(path.is_file() for path in calibration_files):
                gates.append({"gate": "frozen-evaluation", "code": "calibration_freeze_required", "status": "paused", "reason": "calibration thresholds are not frozen"})
            elif not any((job / "evaluation" / "test" / f"{mode}.json").is_file() for mode in MODES):
                gates.append({"gate": "frozen-evaluation", "code": "test_split_pending", "status": "paused", "reason": "test split has not been executed once under frozen configuration"})
    has_rejection = any(row.get("status") == "rejected" for row in gates)
    if gold is None or gold_error:
        state_name = "rejected-gold-import" if gold_error else "paused-independent-gold"
    elif qualification.get("status") != "qualified":
        state_name = "rejected-runtime-qualification" if has_rejection else "paused-resource-handoff"
    elif authorization is None:
        state_name = "paused-inference-authorization"
    elif any(row.get("code") == "calibration_freeze_required" for row in gates):
        state_name = "paused-frozen-evaluation"
    elif any(row.get("code") == "test_split_pending" for row in gates):
        state_name = "paused-frozen-evaluation"
    else:
        state_name = "measured-candidate-benchmark"
    if state_name == "paused-independent-gold":
        next_actions = ["await-independent-gold"]
        if qualification.get("status") != "qualified":
            next_actions.append("await-resource-handoff")
    elif state_name == "paused-resource-handoff":
        next_actions = ["await-resource-handoff"]
    elif state_name == "paused-inference-authorization":
        next_actions = ["await-inference-authorization"]
    elif state_name == "paused-frozen-evaluation":
        next_actions = ["run-calibration"] if any(row.get("code") == "calibration_freeze_required" for row in gates) else ["run-frozen-test"]
    elif state_name == "measured-candidate-benchmark":
        next_actions = ["inspect-candidate-results"]
    else:
        next_actions = ["inspect-rejection"]
    dag = _refresh_dag(job, plan, facts, gold=gold, qualification=qualification, evaluations=_load_evaluation_records(job))
    state = {
        "schema_version": VISUAL_BENCHMARK_STATE_SCHEMA,
        "protocol": VISUAL_BENCHMARK_PROTOCOL,
        "compiler_version": VISUAL_BENCHMARK_COMPILER_VERSION,
        "job_id": plan["job_id"],
        "plan_sha256": plan["plan_sha256"],
        "state": state_name,
        "paused_gates": gates,
        "gold_imported": gold is not None and not gold_error,
        "verified_gold": False,
        "independent_real_gold": bool(gold and not gold_error),
        "accuracy_claim": False,
        "promotion_allowed": False,
        "candidate_only": True,
        "mcp_calls": 0,
        "model_invocations": 0,
        "network_access": False,
        "qualification": qualification,
        "dag": {"snapshot_sha256": dag["snapshot_sha256"], "revision_locator": dag["revision_locator"], "dag_sha256": dag["manifest"]["dag_sha256"]},
        "resume": {"safe": True, "next_actions": next_actions},
    }
    state["state_sha256"] = _hash_without(state, "state_sha256")
    _write_json(job / "state.json", state)
    return state


def plan_benchmark(workpack: Path, output: Path, *, created_at: str = "2026-08-24T00:00:00+08:00") -> dict[str, Any]:
    """Create a private candidate plan and initial paused state."""

    facts = load_phase7d5a_workpack(workpack)
    destination = _secure_directory(output, must_be_empty=True)
    # The locator is resolved from the job directory itself.  This keeps the
    # plan relocatable without accidentally turning ``/Users/...`` into a
    # path under ``/private/tmp/<job>`` on macOS.
    binding_hash = sha256_json({"role": "private-workpack-binding", "manifest_sha256": facts["manifest_sha256"], "candidate_items_sha256": facts["candidate_items_sha256"]})
    qualification_plans = {
        label: {
            "backend_id": entry["binding"]["backend_id"],
            "profile_id": entry["binding"]["profile_id"],
            "plan_sha256": entry["binding"]["plan_sha256"],
            "qualification_tool_sha256": entry["binding"]["qualification_tool_sha256"],
            "runtime_contract_sha256": entry["binding"]["runtime_contract_sha256"],
            "configuration_sha256": entry["binding"]["configuration_sha256"],
            "runtime_inventory_sha256": entry["binding"]["runtime_inventory_sha256"],
            "model_identity_sha256": entry["binding"]["model_identity_sha256"],
            "worker_sha256": entry["binding"]["worker_sha256"],
            "profile_sha256": entry["binding"]["profile_sha256"],
        }
        for label, entry in sorted(facts["qualification_plans"].items())
    }
    core = {
        "schema_version": VISUAL_BENCHMARK_PLAN_SCHEMA,
        "protocol": VISUAL_BENCHMARK_PROTOCOL,
        "compiler_version": VISUAL_BENCHMARK_COMPILER_VERSION,
        "created_at": created_at,
        "workpack": {"locator": "private-workpack-binding", "binding_sha256": binding_hash, "manifest_sha256": facts["manifest_sha256"], "candidate_items_sha256": facts["candidate_items_sha256"]},
        "candidate_set": {"item_count": len(facts["candidate_ids"]), "item_ids_sha256": sha256_json(sorted(facts["candidate_ids"])), "item_kinds": dict(sorted(Counter(str(item["kind_candidate"]) for item in facts["candidate_items"]).items()))},
        "split_commitment": {"commitment_sha256": facts["split"]["commitment_sha256"], "calibration_count": len(facts["calibration_ids"]), "test_count": len(facts["test_ids"])},
        "qualification_plans": qualification_plans,
        "target_inference": {"model_id": TARGET_MODEL_ID, "thinking": TARGET_THINKING, "authorization_required": True},
        "visual_modes": list(MODES),
        "policy": {"network_enabled": False, "model_download": False, "mcp_http_dependency": False, "silent_fallback": False, "candidate_only": True, "promotion": False, "release_included": False, "inference_execution_allowed": False},
    }
    job_id = "vbench-" + sha256_json(core)[:24]
    plan = {"job_id": job_id, **core}
    plan["plan_sha256"] = _hash_without(plan, "plan_sha256")
    _write_json(destination / "benchmark-plan.json", plan)
    _write_json(destination / "workpack-binding.json", {
        "schema_version": "tkc.visual-benchmark-private-workpack-binding/v0.1",
        "protocol": VISUAL_BENCHMARK_PROTOCOL,
        "job_id": job_id,
        "resolved_locator": str(facts["root"]),
        "binding_sha256": binding_hash,
        "release_included": False,
    })
    state = _refresh_job_state(destination)
    _write_json(destination / "status.json", state)
    return {"status": state, "plan": plan}


def status_benchmark(job: Path, workpack: Path | None = None) -> dict[str, Any]:
    job = _secure_directory(job)
    _load_job_and_facts(job, workpack)
    state = _refresh_job_state(job)
    _write_json(job / "status.json", state)
    return state


def resume_benchmark(job: Path, workpack: Path | None = None) -> dict[str, Any]:
    """Refresh only deterministic gates; never executes a model or reviewer."""

    return status_benchmark(job, workpack)


def _record_inference_authorization(job: Path, plan: Mapping[str, Any], *, mode: str, operator_note: str = "explicit-local-private-run") -> dict[str, Any]:
    authorization = {
        "schema_version": "tkc.visual-benchmark-inference-authorization/v0.1",
        "protocol": VISUAL_BENCHMARK_PROTOCOL,
        "job_id": plan["job_id"],
        "plan_sha256": plan["plan_sha256"],
        "visual_mode": mode,
        "model_id": TARGET_MODEL_ID,
        "thinking": TARGET_THINKING,
        "authorized": True,
        "operator_note": operator_note,
        "network_enabled": False,
        "model_download": False,
        "mcp_used": False,
        "release_included": False,
    }
    authorization["authorization_sha256"] = _hash_without(authorization, "authorization_sha256")
    _write_json(job / "authorization" / "real-inference.json", authorization)
    return authorization


def _evaluation_gate(job: Path, facts: Mapping[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any], str | None]:
    try:
        gold = _load_imported_gold(job, facts)
    except VisualBenchmarkError as error:
        return None, validate_qualification_gate(facts, _qualification_receipts(job)), {}, str(error)
    qualification = validate_qualification_gate(facts, _qualification_receipts(job))
    if gold is None:
        return None, qualification, {}, "gold_not_imported"
    if qualification.get("status") != "qualified":
        return gold, qualification, {}, "runtime_qualification_not_ready"
    try:
        authorization = _load_authorization(job)
    except VisualBenchmarkError as error:
        return gold, qualification, {}, str(error)
    if authorization is None:
        return gold, qualification, {}, "inference_authorization_required"
    return gold, qualification, authorization, None


def calibrate_benchmark(job: Path, mode: str, prediction_path: Path, *, allow_real_inference: bool = False) -> dict[str, Any]:
    """Freeze calibration-derived thresholds from a separately supplied bundle."""

    if mode not in MODES:
        raise VisualBenchmarkError("visual_mode_invalid")
    job = _secure_directory(job)
    plan, facts = _load_job_and_facts(job)
    if not allow_real_inference:
        evaluation = paused_evaluation(split="calibration", mode=mode, reason="explicit_inference_authorization_required", job_id=plan["job_id"])
        return {"status": "paused-inference-authorization", "evaluation": evaluation}
    gold, qualification, authorization, reason = _evaluation_gate(job, facts)
    if reason:
        return {"status": "paused" if reason in {"gold_not_imported", "runtime_qualification_not_ready", "inference_authorization_required"} else "rejected", "evaluation": paused_evaluation(split="calibration", mode=mode, reason=reason, job_id=plan["job_id"])}
    if authorization.get("visual_mode") != mode or authorization.get("plan_sha256") != plan["plan_sha256"]:
        raise VisualBenchmarkError("inference_authorization_binding_invalid")
    predictions = load_prediction_bundle(prediction_path, facts, split="calibration", mode=mode, expected_job_id=plan["job_id"])
    calibration_dir = _secure_directory(job / "calibration" / mode)
    threshold_path = calibration_dir / "threshold-config.json"
    config = _finalize_threshold_config(_derive_threshold_config({}, predictions, gold or {}))
    config.update({"job_id": plan["job_id"], "visual_mode": mode})
    config["threshold_config_sha256"] = _hash_without(config, "threshold_config_sha256")
    if threshold_path.is_file():
        existing = _read_json(threshold_path)
        if existing.get("threshold_config_sha256") != config.get("threshold_config_sha256"):
            raise VisualBenchmarkError("calibration_already_frozen")
        state = _refresh_job_state(job)
        return {"status": state["state"], "threshold_config": existing, "idempotent": True}
    _write_jsonl(job / "predictions" / "calibration" / f"{mode}.jsonl", predictions["rows"])
    _write_json(threshold_path, config)
    state = _refresh_job_state(job)
    return {"status": state["state"], "threshold_config": config, "prediction_output_sha256": predictions["output_sha256"]}


def run_test_benchmark(job: Path, mode: str, prediction_path: Path, *, allow_real_inference: bool = False, evaluator_instance: str = "phase7d5b-evaluator") -> dict[str, Any]:
    """Ingest and score exactly one frozen test bundle for one visual mode.

    The module is intentionally an evaluator boundary: it accepts a private,
    hash-bound output bundle and never starts the heavy model itself.  A host
    adapter may produce that bundle only after the independent Gold and
    resource-handoff gates are satisfied.
    """

    if mode not in MODES:
        raise VisualBenchmarkError("visual_mode_invalid")
    job = _secure_directory(job)
    plan, facts = _load_job_and_facts(job)
    evaluation_path = job / "evaluation" / "test" / f"{mode}.json"
    if evaluation_path.exists():
        raise VisualBenchmarkError("test_split_already_executed")
    if not allow_real_inference:
        evaluation = paused_evaluation(split="test", mode=mode, reason="explicit_inference_authorization_required", job_id=plan["job_id"])
        return {"status": "paused-inference-authorization", "evaluation": evaluation}
    gold, qualification, authorization, reason = _evaluation_gate(job, facts)
    if reason:
        status = "paused" if reason in {"gold_not_imported", "runtime_qualification_not_ready", "inference_authorization_required"} else "rejected"
        return {"status": status, "evaluation": paused_evaluation(split="test", mode=mode, reason=reason, job_id=plan["job_id"])}
    if authorization.get("plan_sha256") != plan["plan_sha256"]:
        raise VisualBenchmarkError("inference_authorization_binding_invalid")
    threshold_path = job / "calibration" / mode / "threshold-config.json"
    if not threshold_path.is_file():
        return {"status": "paused", "evaluation": paused_evaluation(split="test", mode=mode, reason="calibration_threshold_not_frozen", job_id=plan["job_id"])}
    threshold_config = _read_json(threshold_path)
    if threshold_config.get("threshold_config_sha256") != _hash_without(threshold_config, "threshold_config_sha256") or threshold_config.get("job_id") != plan["job_id"] or threshold_config.get("visual_mode") != mode or threshold_config.get("frozen_from") != "calibration":
        raise VisualBenchmarkError("threshold_config_hash_mismatch")
    predictions = load_prediction_bundle(prediction_path, facts, split="test", mode=mode, expected_job_id=plan["job_id"])
    evaluation = evaluate_predictions(gold or {}, predictions, split="test", mode=mode, threshold_config=threshold_config, job_id=plan["job_id"], evaluator_instance=evaluator_instance, all_modes_measured=False)
    _write_jsonl(job / "predictions" / "test" / f"{mode}.jsonl", predictions["rows"])
    _write_json(evaluation_path, evaluation)
    _write_json(job / "cost-ledger" / "test" / f"{mode}.json", {
        "schema_version": VISUAL_BENCHMARK_COST_LEDGER_SCHEMA,
        "protocol": VISUAL_BENCHMARK_PROTOCOL,
        "job_id": plan["job_id"],
        "split": "test",
        "visual_mode": mode,
        "prediction_output_sha256": predictions["output_sha256"],
        "cost": evaluation["cost_ledger"],
        "candidate_only": True,
        "release_included": False,
    })
    state = _refresh_job_state(job)
    return {"status": state["state"], "evaluation": evaluation, "state": state}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--workpack", type=Path, required=True)
    plan_parser.add_argument("--output", type=Path, required=True)
    plan_parser.add_argument("--created-at", default="2026-08-24T00:00:00+08:00")
    for command in ("status", "resume"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--job", type=Path, required=True)
        command_parser.add_argument("--workpack", type=Path)
    gold_parser = subparsers.add_parser("import-gold")
    gold_parser.add_argument("--job", type=Path, required=True)
    gold_parser.add_argument("--fragment", type=Path, action="append", required=True)
    qualification_parser = subparsers.add_parser("import-qualification")
    qualification_parser.add_argument("--job", type=Path, required=True)
    qualification_parser.add_argument("--table-receipt", type=Path)
    qualification_parser.add_argument("--chart-receipt", type=Path)
    for command in ("calibrate", "run-test"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--job", type=Path, required=True)
        command_parser.add_argument("--mode", choices=MODES, required=True)
        command_parser.add_argument("--predictions", type=Path, required=True)
        command_parser.add_argument("--allow-real-inference", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            result = plan_benchmark(args.workpack, args.output, created_at=args.created_at)
        elif args.command == "status":
            result = status_benchmark(args.job, args.workpack)
        elif args.command == "resume":
            result = resume_benchmark(args.job, args.workpack)
        elif args.command == "import-gold":
            result = import_external_gold(args.job, args.fragment)
        elif args.command == "import-qualification":
            result = import_qualification_receipts(args.job, args.table_receipt, args.chart_receipt)
        elif args.command == "calibrate":
            result = calibrate_benchmark(args.job, args.mode, args.predictions, allow_real_inference=args.allow_real_inference)
        elif args.command == "run-test":
            result = run_test_benchmark(args.job, args.mode, args.predictions, allow_real_inference=args.allow_real_inference)
        else:
            raise VisualBenchmarkError("command_invalid")
    except (VisualBenchmarkError, IncrementalDagError, PaddleRuntimeQualificationError) as error:
        print(json.dumps({"status": "rejected", "error": str(error)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


__all__ = [
    "MODES",
    "METRIC_IDS",
    "VisualBenchmarkError",
    "load_phase7d5a_workpack",
    "validate_qualification_gate",
    "validate_external_gold_fragments",
    "import_external_gold",
    "import_qualification_receipts",
    "plan_benchmark",
    "status_benchmark",
    "resume_benchmark",
    "paused_evaluation",
    "load_prediction_bundle",
    "evaluate_predictions",
    "calibrate_benchmark",
    "run_test_benchmark",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())


def _presence(payload: Mapping[str, Any]) -> bool:
    for key in ("present", "presence", "is_present"):
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, Mapping):
            for nested_key in ("value", "present", "accepted"):
                if isinstance(value.get(nested_key), bool):
                    return bool(value[nested_key])
    return bool(payload.get("kind_verdict") in {"present", "accepted", "detected"})


def _bbox(payload: Mapping[str, Any]) -> list[float] | None:
    value = payload.get("bbox")
    if value is None and isinstance(payload.get("localization"), Mapping):
        value = payload["localization"].get("bbox")
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        values = [float(number) for number in value]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(number) for number in values) or values[2] <= values[0] or values[3] <= values[1]:
        return None
    return values


def _bbox_iou(left: Sequence[float] | None, right: Sequence[float] | None) -> float | None:
    if left is None or right is None:
        return None
    x0, y0 = max(left[0], right[0]), max(left[1], right[1])
    x1, y1 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    area_left = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    area_right = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = area_left + area_right - intersection
    return intersection / union if union > 0 else None


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(value[key]) for key in sorted(value, key=str) if key not in {"confidence", "score", "source"}}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(nested) for nested in value]
    if isinstance(value, float):
        return round(value, 12)
    return value


def _payload_field(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _numeric_values(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            lowered = str(key).lower()
            if any(token in lowered for token in ("numeric", "number", "value", "tick")):
                if isinstance(nested, (int, float)) and not isinstance(nested, bool) and math.isfinite(float(nested)):
                    found.append(str(nested))
                elif isinstance(nested, str) and nested.strip():
                    found.append(nested.strip())
            found.extend(_numeric_values(nested))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_numeric_values(nested))
    return sorted(set(found))


def _unit_values(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if "unit" in str(key).lower() and isinstance(nested, str) and nested.strip():
                found.append(nested.strip())
            found.extend(_unit_values(nested))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_unit_values(nested))
    return sorted(set(found))


def _safe_mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _precision_recall_f1(true_positive: int, predicted_positive: int, actual_positive: int) -> tuple[float | None, float | None, float | None]:
    precision = true_positive / predicted_positive if predicted_positive else None
    recall = true_positive / actual_positive if actual_positive else None
    if precision is None or recall is None or precision + recall == 0:
        f1 = None
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return precision, recall, f1


def _metric(metric_id: str, status: str, value: Any, *, split: str, mode: str, claim: str = "none", unit: str | None = None, reason: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"metric_id": metric_id, "split": split, "visual_mode": mode, "status": status, "value": value, "claim": claim, "unit": unit}
    if reason:
        row["reason"] = reason
    return row


def paused_evaluation(*, split: str, mode: str, reason: str, job_id: str | None = None) -> dict[str, Any]:
    metrics = [_metric(metric_id, "not-run", None, split=split, mode=mode, reason=reason) for metric_id in METRIC_IDS]
    evaluation: dict[str, Any] = {
        "schema_version": VISUAL_BENCHMARK_EVALUATION_SCHEMA,
        "protocol": VISUAL_BENCHMARK_PROTOCOL,
        "compiler_version": VISUAL_BENCHMARK_COMPILER_VERSION,
        "job_id": job_id,
        "split": split,
        "visual_mode": mode,
        "status": "paused",
        "metrics": metrics,
        "candidate_only": True,
        "verified_gold": False,
        "promotion_allowed": False,
        "accuracy_claim": False,
        "reason": reason,
    }
    evaluation["evaluation_sha256"] = _hash_without(evaluation, "evaluation_sha256")
    return evaluation


def _validate_prediction_row(row: Mapping[str, Any], facts: Mapping[str, Any], *, split: str, mode: str, expected_job_id: str | None = None) -> dict[str, Any]:
    item_id = row.get("item_id")
    if not isinstance(item_id, str) or item_id not in facts["candidate_by_id"]:
        raise VisualBenchmarkError("prediction_item_unknown")
    item = facts["candidate_by_id"][item_id]
    if row.get("schema_version") != VISUAL_BENCHMARK_PREDICTION_SCHEMA or row.get("protocol") != VISUAL_BENCHMARK_PROTOCOL or row.get("split") != split or row.get("visual_mode") != mode:
        raise VisualBenchmarkError(f"prediction_contract_invalid:{item_id}")
    if expected_job_id is not None and row.get("job_id") != expected_job_id:
        raise VisualBenchmarkError(f"prediction_job_binding_invalid:{item_id}")
    if row.get("item_sha256") != item["item_sha256"]:
        raise VisualBenchmarkError(f"prediction_item_hash_invalid:{item_id}")
    model = row.get("model")
    if not isinstance(model, Mapping) or model.get("model_id") != TARGET_MODEL_ID or model.get("thinking") != TARGET_THINKING or not _is_hash(model.get("configuration_sha256")):
        raise VisualBenchmarkError(f"prediction_model_binding_invalid:{item_id}")
    output = row.get("output")
    if not isinstance(output, Mapping) or row.get("output_sha256") != sha256_json(output):
        raise VisualBenchmarkError(f"prediction_output_hash_invalid:{item_id}")
    if any(key in row for key in ("gold_payload", "attestation", "attestations", "reviewer_registry", "reviewer_instance")):
        raise VisualBenchmarkError(f"prediction_reviewer_boundary_invalid:{item_id}")
    runtime = row.get("runtime")
    if not isinstance(runtime, Mapping):
        raise VisualBenchmarkError(f"prediction_runtime_missing:{item_id}")
    for key in COST_FIELDS:
        value = runtime.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
            raise VisualBenchmarkError(f"prediction_cost_field_invalid:{item_id}:{key}")
    if not _is_hash(runtime.get("environment_sha256")):
        raise VisualBenchmarkError(f"prediction_environment_hash_invalid:{item_id}")
    normalized = dict(row)
    normalized["output_sha256"] = str(row["output_sha256"])
    return normalized


def load_prediction_bundle(path: Path, facts: Mapping[str, Any], *, split: str, mode: str, expected_job_id: str | None = None) -> dict[str, Any]:
    rows = _read_jsonl(_secure_file(path, "prediction_bundle"))
    expected_ids = set(str(value) for value in facts[f"{split}_ids"])
    if len(rows) != len(expected_ids) or {str(row.get("item_id")) for row in rows} != expected_ids:
        raise VisualBenchmarkError("prediction_candidate_set_incomplete_or_duplicate")
    normalized = [_validate_prediction_row(row, facts, split=split, mode=mode, expected_job_id=expected_job_id) for row in rows]
    normalized.sort(key=lambda row: str(row["item_id"]))
    return {
        "rows": normalized,
        "file_sha256": sha256_file(Path(path)),
        "output_sha256": sha256_json([(row["item_id"], row["output_sha256"]) for row in normalized]),
        "environment_sha256": sha256_json(sorted(str(row["runtime"]["environment_sha256"]) for row in normalized)),
        "mode": mode,
        "split": split,
    }


def _derive_threshold_config(calibration: Mapping[str, Any], predictions: Mapping[str, Any], gold: Mapping[str, Any]) -> dict[str, Any]:
    ious: list[float] = []
    gold_by_id = {str(row["item_id"]): row for row in gold["rows"]}
    for row in predictions["rows"]:
        gold_row = gold_by_id[str(row["item_id"])]
        score = _bbox_iou(_bbox(gold_row["gold_payload"]), _bbox(row["output"]))
        if score is not None:
            ious.append(score)
    bbox_threshold = min(0.9, max(0.5, _safe_mean(ious) or 0.75))
    return {
        "schema_version": VISUAL_BENCHMARK_THRESHOLD_CONFIG_SCHEMA,
        "protocol": VISUAL_BENCHMARK_PROTOCOL,
        "frozen_from": "calibration",
        "calibration_prediction_sha256": predictions["output_sha256"],
        "calibration_gold_candidate_set_sha256": gold["candidate_set_sha256"],
        "presence_threshold": 0.5,
        "bbox_iou_threshold": bbox_threshold,
        "table_exact_match": True,
        "chart_exact_match": True,
        "threshold_config_sha256": "",
    }


def _finalize_threshold_config(config: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(config)
    result["threshold_config_sha256"] = _hash_without(result, "threshold_config_sha256")
    return result


def _exactness(gold_value: Any, predicted_value: Any) -> float | None:
    if gold_value is None and predicted_value is None:
        return None
    return 1.0 if _canonical_value(gold_value) == _canonical_value(predicted_value) else 0.0


def evaluate_predictions(
    gold: Mapping[str, Any],
    predictions: Mapping[str, Any],
    *,
    split: str,
    mode: str,
    threshold_config: Mapping[str, Any],
    job_id: str | None = None,
    evaluator_instance: str = "phase7d5b-evaluator",
    all_modes_measured: bool = False,
) -> dict[str, Any]:
    """Evaluate a frozen, hash-validated output bundle without Gold authorship."""

    if split not in {"calibration", "test"} or mode not in MODES:
        raise VisualBenchmarkError("evaluation_split_or_mode_invalid")
    gold_rows = list(gold.get("rows", [])) if isinstance(gold.get("rows"), list) else []
    prediction_rows = list(predictions.get("rows", [])) if isinstance(predictions.get("rows"), list) else []
    if not gold_rows or not prediction_rows:
        return paused_evaluation(split=split, mode=mode, reason="gold_or_prediction_bundle_empty", job_id=job_id)
    if split == "test" and threshold_config.get("frozen_from") != "calibration":
        return paused_evaluation(split=split, mode=mode, reason="test_threshold_not_frozen_from_calibration", job_id=job_id)
    reviewer_instances = {str(attestation.get("reviewer_instance")) for row in gold_rows for attestation in row.get("attestations", []) if isinstance(attestation, Mapping)}
    if evaluator_instance in reviewer_instances:
        raise VisualBenchmarkError("evaluator_reviewer_role_overlap")
    gold_by_id = {str(row["item_id"]): row for row in gold_rows}
    prediction_by_id = {str(row["item_id"]): row for row in prediction_rows}
    if set(gold_by_id) != set(prediction_by_id) or len(gold_by_id) != len(gold_rows) or len(prediction_by_id) != len(prediction_rows):
        raise VisualBenchmarkError("evaluation_item_set_invalid")
    pair_rows: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = [(gold_by_id[item_id], prediction_by_id[item_id]) for item_id in sorted(gold_by_id)]
    actual_positive = sum(1 for gold_row, _ in pair_rows if _presence(gold_row["gold_payload"]))
    predicted_positive = sum(1 for _, prediction_row in pair_rows if _presence(prediction_row["output"]))
    true_positive = sum(1 for gold_row, prediction_row in pair_rows if _presence(gold_row["gold_payload"]) and _presence(prediction_row["output"]))
    precision, recall, f1 = _precision_recall_f1(true_positive, predicted_positive, actual_positive)
    ious = [score for gold_row, prediction_row in pair_rows if (score := _bbox_iou(_bbox(gold_row["gold_payload"]), _bbox(prediction_row["output"]))) is not None]
    localized = sum(1 for score in ious if score >= float(threshold_config.get("bbox_iou_threshold", 0.75)))
    coverage = localized / actual_positive if actual_positive else None

    def kind_metric(kind: str, metric_id: str, localization_metric: str) -> tuple[float | None, float | None]:
        rows = [(gold_row, prediction_row) for gold_row, prediction_row in pair_rows if gold_row.get("kind_candidate") == kind]
        gold_positive = sum(1 for gold_row, _ in rows if _presence(gold_row["gold_payload"]))
        predicted = sum(1 for _, prediction_row in rows if _presence(prediction_row["output"]))
        tp = sum(1 for gold_row, prediction_row in rows if _presence(gold_row["gold_payload"]) and _presence(prediction_row["output"]))
        p, _, _ = _precision_recall_f1(tp, predicted, gold_positive)
        local_scores = [_bbox_iou(_bbox(gold_row["gold_payload"]), _bbox(prediction_row["output"])) for gold_row, prediction_row in rows if _presence(gold_row["gold_payload"])]
        local_scores = [score for score in local_scores if score is not None]
        local = sum(1 for score in local_scores if score >= float(threshold_config.get("bbox_iou_threshold", 0.75))) / gold_positive if gold_positive else None
        return p, local

    metrics: list[dict[str, Any]] = [
        _metric("object_detection_precision", "measured", precision, split=split, mode=mode, claim="candidate-observation"),
        _metric("object_detection_recall", "measured", recall, split=split, mode=mode, claim="candidate-observation"),
        _metric("object_detection_f1", "measured", f1, split=split, mode=mode, claim="candidate-observation"),
        _metric("bbox_iou", "measured", _safe_mean(ious), split=split, mode=mode, claim="candidate-observation"),
        _metric("localization_coverage", "measured", coverage, split=split, mode=mode, claim="candidate-observation"),
    ]
    for kind, row_metric, col_metric in (("table", "table_row_topology_accuracy", "table_column_topology_accuracy"),):
        topology_rows = [(gold_row, prediction_row) for gold_row, prediction_row in pair_rows if gold_row.get("kind_candidate") == kind]
        metrics.extend([
            _metric(row_metric, "measured", _safe_mean([_exactness(_payload_field(gold_row["gold_payload"], "row_count", "rows"), _payload_field(prediction_row["output"], "row_count", "rows")) for gold_row, prediction_row in topology_rows if _exactness(_payload_field(gold_row["gold_payload"], "row_count", "rows"), _payload_field(prediction_row["output"], "row_count", "rows")) is not None]), split=split, mode=mode, claim="candidate-observation"),
            _metric(col_metric, "measured", _safe_mean([_exactness(_payload_field(gold_row["gold_payload"], "column_count", "columns"), _payload_field(prediction_row["output"], "column_count", "columns")) for gold_row, prediction_row in topology_rows if _exactness(_payload_field(gold_row["gold_payload"], "column_count", "columns"), _payload_field(prediction_row["output"], "column_count", "columns")) is not None]), split=split, mode=mode, claim="candidate-observation"),
            _metric("table_span_exactness", "measured", _safe_mean([_exactness(_payload_field(gold_row["gold_payload"], "spans", "merged_cells"), _payload_field(prediction_row["output"], "spans", "merged_cells")) for gold_row, prediction_row in topology_rows if _exactness(_payload_field(gold_row["gold_payload"], "spans", "merged_cells"), _payload_field(prediction_row["output"], "spans", "merged_cells")) is not None]), split=split, mode=mode, claim="candidate-observation"),
            _metric("table_cell_exactness", "measured", _safe_mean([_exactness(_payload_field(gold_row["gold_payload"], "cells"), _payload_field(prediction_row["output"], "cells")) for gold_row, prediction_row in topology_rows if _exactness(_payload_field(gold_row["gold_payload"], "cells"), _payload_field(prediction_row["output"], "cells")) is not None]), split=split, mode=mode, claim="candidate-observation"),
        ])
    number_pairs = [(_numeric_values(gold_row["gold_payload"]), _numeric_values(prediction_row["output"])) for gold_row, prediction_row in pair_rows]
    unit_pairs = [(_unit_values(gold_row["gold_payload"]), _unit_values(prediction_row["output"])) for gold_row, prediction_row in pair_rows]
    numeric_exact = _safe_mean([1.0 if left == right else 0.0 for left, right in number_pairs])
    unit_exact = _safe_mean([1.0 if left == right else 0.0 for left, right in unit_pairs])
    hallucinated = sum(len(set(predicted) - set(actual)) for actual, predicted in number_pairs)
    metrics.extend([
        _metric("numeric_exactness", "measured", numeric_exact, split=split, mode=mode, claim="candidate-observation"),
        _metric("unit_exactness", "measured", unit_exact, split=split, mode=mode, claim="candidate-observation"),
        _metric("hallucinated_numeric_count", "measured", hallucinated, split=split, mode=mode, claim="candidate-observation", unit="count"),
    ])
    chart_rows = [(gold_row, prediction_row) for gold_row, prediction_row in pair_rows if gold_row.get("kind_candidate") == "chart"]
    axis_recalls: list[float] = []
    for gold_row, prediction_row in chart_rows:
        gold_axes = _payload_field(gold_row["gold_payload"], "axis_candidates", "axes")
        predicted_axes = _payload_field(prediction_row["output"], "axis_candidates", "axes")
        if isinstance(gold_axes, list):
            expected = {_canonical_value(value).__repr__() for value in gold_axes}
            observed = {_canonical_value(value).__repr__() for value in predicted_axes} if isinstance(predicted_axes, list) else set()
            axis_recalls.append(len(expected & observed) / len(expected) if expected else 1.0)
    metrics.append(_metric("chart_axis_candidate_recall", "measured", _safe_mean(axis_recalls), split=split, mode=mode, claim="candidate-observation"))
    for kind, metric_id, localization_metric in (("figure", "figure_presence_precision", "figure_localization_coverage"), ("equation", "equation_presence_precision", "equation_localization_coverage")):
        kind_precision, kind_local = kind_metric(kind, metric_id, localization_metric)
        metrics.append(_metric(metric_id, "measured", kind_precision, split=split, mode=mode, claim="candidate-observation"))
        metrics.append(_metric(localization_metric, "measured", kind_local, split=split, mode=mode, claim="candidate-observation"))
    chart_fields = (("chart_tick_candidate_exactness", "ticks", "tick_candidates"), ("chart_legend_candidate_exactness", "legend", "legend_candidates"), ("chart_series_candidate_exactness", "series", "series_candidates"), ("chart_value_candidate_exactness", "values", "value_candidates"))
    for metric_id, gold_key, pred_key in chart_fields:
        values = [_exactness(_payload_field(gold_row["gold_payload"], gold_key, pred_key), _payload_field(prediction_row["output"], pred_key, gold_key)) for gold_row, prediction_row in pair_rows if gold_row.get("kind_candidate") == "chart"]
        values = [value for value in values if value is not None]
        metrics.append(_metric(metric_id, "measured", _safe_mean(values), split=split, mode=mode, claim="candidate-observation"))

    runtime_rows = [prediction_row["runtime"] for _, prediction_row in pair_rows]
    for key in ("elapsed_ms", "cold_start_ms", "warm_repeat_ms", "rss_peak_bytes", "render_count", "crop_bytes", "output_bytes", "model_invocations"):
        values = [float(row[key]) for row in runtime_rows]
        metrics.append(_metric(key, "measured", sum(values) if key in {"elapsed_ms", "render_count", "crop_bytes", "output_bytes", "model_invocations"} else max(values), split=split, mode=mode, claim="candidate-observation", unit="count" if key in {"render_count", "model_invocations"} else None))
    metrics.append(_metric("model_load_calls", "measured", max(float(row["model_load_calls"]) for row in runtime_rows), split=split, mode=mode, claim="candidate-observation", unit="count"))
    metrics.append(_metric("visual_mode_coverage_cost_off_auto_full", "measured" if all_modes_measured else "not-run", 1.0 if all_modes_measured else None, split=split, mode=mode, claim="candidate-observation" if all_modes_measured else "none", reason=None if all_modes_measured else "other_visual_modes_not_measured"))
    metrics_by_id = {row["metric_id"]: row for row in metrics}
    ordered_metrics = [metrics_by_id.get(metric_id, _metric(metric_id, "not-run", None, split=split, mode=mode, reason="metric_not_available_for_kind")) for metric_id in METRIC_IDS]
    return {
        "schema_version": VISUAL_BENCHMARK_EVALUATION_SCHEMA,
        "protocol": VISUAL_BENCHMARK_PROTOCOL,
        "compiler_version": VISUAL_BENCHMARK_COMPILER_VERSION,
        "job_id": job_id,
        "split": split,
        "visual_mode": mode,
        "status": "measured",
        "metrics": ordered_metrics,
        "candidate_only": True,
        "verified_gold": False,
        "promotion_allowed": False,
        "accuracy_claim": False,
        "gold_candidate_set_sha256": gold.get("candidate_set_sha256"),
        "prediction_output_sha256": predictions.get("output_sha256"),
        "environment_sha256": predictions.get("environment_sha256"),
        "cost_ledger": {key: sum(float(row[key]) for row in runtime_rows) for key in COST_FIELDS},
        "evaluator_instance": evaluator_instance,
        "threshold_config_sha256": threshold_config.get("threshold_config_sha256"),
    }
