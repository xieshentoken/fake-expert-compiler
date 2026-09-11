#!/usr/bin/env python3
"""Offline candidate-correctness benchmark for portable visual review workpacks.

This evaluator is intentionally narrower than the frozen Phase 7D.5B model
benchmark.  It scores only candidates that were already emitted by a bound
native parser run before Gold existed.  It cannot measure pages/objects that
the parser never proposed, so whole-range detection recall always remains
``not-measured`` and no promotion or release claim is produced.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from compiler_version import (
    PORTABLE_VISUAL_BENCHMARK_COMPILER_VERSION,
    PORTABLE_VISUAL_BENCHMARK_EVALUATION_SCHEMA,
    PORTABLE_VISUAL_BENCHMARK_MANIFEST_SCHEMA,
    PORTABLE_VISUAL_BENCHMARK_PLAN_SCHEMA,
    PORTABLE_VISUAL_BENCHMARK_PROTOCOL,
    PORTABLE_VISUAL_CALIBRATION_REPLAY_COMPILER_VERSION,
)
from expert_skill_contract import sha256_file
from incremental_build_dag import sha256_json
from visual_benchmark import load_phase7d5a_workpack, validate_external_gold_fragments
from visual_parser_orchestrator import validate_visual_parser_job


ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
COORDINATE_SPACE = "pdf-page-top-left-points-v1"


class PortableVisualBenchmarkError(RuntimeError):
    """Stable fail-closed error for the portable candidate benchmark."""


def _hash_without(value: Mapping[str, Any], key: str) -> str:
    return sha256_json({name: nested for name, nested in value.items() if name != key})


def _read_json(path: Path, label: str) -> dict[str, Any]:
    path = _secure_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PortableVisualBenchmarkError(f"{label}_json_invalid") from error
    if not isinstance(value, dict):
        raise PortableVisualBenchmarkError(f"{label}_object_required")
    return value


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    path = _secure_file(path, label)
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise PortableVisualBenchmarkError(f"{label}_read_failed") from error
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise PortableVisualBenchmarkError(f"{label}_jsonl_invalid:{line_number}") from error
        if not isinstance(value, dict):
            raise PortableVisualBenchmarkError(f"{label}_row_object_required:{line_number}")
        rows.append(value)
    return rows


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _secure_file(path: Path, label: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if resolved.is_symlink() or not resolved.is_file():
        raise PortableVisualBenchmarkError(f"{label}_missing_or_symlink")
    return resolved


def _secure_directory(path: Path, *, must_be_empty: bool = False) -> Path:
    resolved = Path(path).expanduser().resolve()
    if resolved.exists():
        if resolved.is_symlink() or not resolved.is_dir():
            raise PortableVisualBenchmarkError("directory_invalid")
        if must_be_empty and any(resolved.iterdir()):
            raise PortableVisualBenchmarkError("output_not_empty")
    else:
        resolved.mkdir(parents=True)
    os.chmod(resolved, 0o700)
    return resolved


def _bbox_iou(left: Any, right: Any) -> float | None:
    if not (
        isinstance(left, list)
        and isinstance(right, list)
        and len(left) == len(right) == 4
        and all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) for value in left + right)
    ):
        return None
    ax0, ay0, ax1, ay1 = (float(value) for value in left)
    bx0, by0, bx1, by1 = (float(value) for value in right)
    intersection = max(0.0, min(ax1, bx1) - max(ax0, bx0)) * max(0.0, min(ay1, by1) - max(ay0, by0))
    union = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0) + max(0.0, bx1 - bx0) * max(0.0, by1 - by0) - intersection
    return intersection / union if union > 0 else None


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _metric(metric_id: str, status: str, value: Any, *, reason: str | None = None, unit: str | None = None) -> dict[str, Any]:
    row = {"metric_id": metric_id, "status": status, "value": value, "scope": "reviewed-emitted-candidates-only"}
    if reason is not None:
        row["reason"] = reason
    if unit is not None:
        row["unit"] = unit
    return row


def _role_ids(gold_rows: Sequence[Mapping[str, Any]], facts: Mapping[str, Any]) -> dict[str, list[str]]:
    reviewers: set[str] = set()
    curators: set[str] = set()
    proposers = {str(value) for value in (facts.get("review_plan") or {}).get("proposer_instances", []) if isinstance(value, str)}
    for row in gold_rows:
        for attestation in row.get("attestations", []) if isinstance(row.get("attestations"), list) else []:
            if isinstance(attestation, Mapping):
                if isinstance(attestation.get("reviewer_instance"), str):
                    reviewers.add(str(attestation["reviewer_instance"]))
                if isinstance(attestation.get("proposer_instance"), str):
                    proposers.add(str(attestation["proposer_instance"]))
        provenance = row.get("fragment_provenance") if isinstance(row.get("fragment_provenance"), Mapping) else {}
        adjudication = provenance.get("adjudication") if isinstance(provenance.get("adjudication"), Mapping) else {}
        receipt = adjudication.get("receipt") if isinstance(adjudication.get("receipt"), Mapping) else {}
        curator = receipt.get("adjudicator") if isinstance(receipt.get("adjudicator"), Mapping) else {}
        if isinstance(curator.get("adjudicator_instance"), str):
            curators.add(str(curator["adjudicator_instance"]))
    return {"reviewers": sorted(reviewers), "curators": sorted(curators), "proposers": sorted(proposers)}


def _backend_binding(facts: Mapping[str, Any]) -> dict[str, Any]:
    bindings: list[dict[str, Any]] = []
    for item in facts["candidate_items"]:
        provenance = item.get("source_provenance") if isinstance(item.get("source_provenance"), Mapping) else {}
        binding = {
            "parser": deepcopy(provenance.get("parser")),
            "backend": deepcopy(provenance.get("backend")),
            "config": deepcopy(provenance.get("config")),
            "model": deepcopy(provenance.get("model")),
        }
        if binding not in bindings:
            bindings.append(binding)
    if len(bindings) != 1:
        raise PortableVisualBenchmarkError("candidate_backend_binding_not_unique")
    binding = bindings[0]
    parser = binding.get("parser") if isinstance(binding.get("parser"), Mapping) else {}
    backend = binding.get("backend") if isinstance(binding.get("backend"), Mapping) else {}
    config = binding.get("config") if isinstance(binding.get("config"), Mapping) else {}
    model = binding.get("model") if isinstance(binding.get("model"), Mapping) else {}
    if not isinstance(parser.get("id"), str) or not isinstance(parser.get("version"), str) or not isinstance(backend.get("id"), str) or not isinstance(config.get("id"), str):
        raise PortableVisualBenchmarkError("candidate_backend_binding_incomplete")
    if model.get("id") != "none" or model.get("available") is not False or model.get("download") is not False:
        raise PortableVisualBenchmarkError("portable_benchmark_model_route_forbidden")
    binding["binding_sha256"] = sha256_json(binding)
    return binding


def _load_gold_revision(revision: Path, facts: Mapping[str, Any]) -> dict[str, Any]:
    root = _secure_directory(revision)
    manifest = _read_json(root / "gold-review-revision.json", "gold_revision_manifest")
    if manifest.get("status") != "accepted-for-import" or manifest.get("import_compatible") is not True:
        raise PortableVisualBenchmarkError("gold_revision_not_import_compatible")
    if manifest.get("verified_gold") is not False or manifest.get("promotion_allowed") is not False or manifest.get("release_included") is not False or manifest.get("immutable") is not True:
        raise PortableVisualBenchmarkError("gold_revision_policy_invalid")
    if manifest.get("manifest_sha256") != _hash_without(manifest, "manifest_sha256"):
        raise PortableVisualBenchmarkError("gold_revision_manifest_hash_mismatch")
    gold_path = _secure_file(root / "gold-fragments.jsonl", "gold_fragments")
    expected_hash = (manifest.get("file_hashes") or {}).get("gold-fragments.jsonl")
    if expected_hash != sha256_file(gold_path):
        raise PortableVisualBenchmarkError("gold_revision_file_hash_mismatch")
    try:
        gold = validate_external_gold_fragments(facts, [gold_path])
    except Exception as error:  # keep the public error surface stable here
        raise PortableVisualBenchmarkError(f"gold_import_invalid:{error}") from error
    return {"root": root, "manifest": manifest, "path": gold_path, **gold}


def _load_parser_run(job_path: Path, facts: Mapping[str, Any]) -> dict[str, Any]:
    job_path = _secure_file(job_path, "visual_parser_job")
    validation = validate_visual_parser_job(job_path)
    if validation.get("passed") is not True:
        raise PortableVisualBenchmarkError("visual_parser_job_validation_failed")
    job = _read_json(job_path, "visual_parser_job")
    output = _secure_directory(Path(str(job.get("output_root", ""))))
    manifest = _read_json(output / "manifest.json", "visual_parser_manifest")
    bundle_path = _secure_file(output / "visual-bundle.json", "visual_bundle")
    budget_path = _secure_file(output / "visual-budget-receipt.json", "visual_budget_receipt")
    objects_path = _secure_file(output / "visual" / "visual-objects.jsonl", "visual_objects")
    grids_path = _secure_file(output / "visual" / "table-grids.jsonl", "table_grids")
    workpack_manifest = facts["manifest"]
    source_sha = str(workpack_manifest.get("source_sha256"))
    if job.get("source", {}).get("sha256") != source_sha or manifest.get("source_sha256") != source_sha:
        raise PortableVisualBenchmarkError("parser_source_binding_mismatch")
    if workpack_manifest.get("visual_bundle_sha256") != sha256_file(bundle_path):
        raise PortableVisualBenchmarkError("parser_visual_bundle_binding_mismatch")
    for item in facts["candidate_items"]:
        source = item.get("source") if isinstance(item.get("source"), Mapping) else {}
        if source.get("source_visual_bundle_sha256") != workpack_manifest.get("visual_bundle_sha256"):
            raise PortableVisualBenchmarkError(f"candidate_visual_bundle_binding_mismatch:{item.get('item_id')}")
    budget = _read_json(budget_path, "visual_budget_receipt")
    if budget.get("status") != "completed" or budget.get("source_sha256") != source_sha or budget.get("candidate_only") is not True or budget.get("promotion") is not False:
        raise PortableVisualBenchmarkError("visual_budget_receipt_policy_invalid")
    if budget.get("budget_receipt_sha256") != _hash_without(budget, "budget_receipt_sha256"):
        raise PortableVisualBenchmarkError("visual_budget_receipt_hash_mismatch")
    return {
        "job_path": job_path,
        "job": job,
        "root": output,
        "manifest": manifest,
        "bundle_path": bundle_path,
        "budget_path": budget_path,
        "budget": budget,
        "objects": _read_jsonl(objects_path, "visual_objects"),
        "grids": _read_jsonl(grids_path, "table_grids"),
        "file_hashes": {
            "job": sha256_file(job_path),
            "manifest": sha256_file(output / "manifest.json"),
            "visual_bundle": sha256_file(bundle_path),
            "visual_budget_receipt": sha256_file(budget_path),
            "visual_objects": sha256_file(objects_path),
            "table_grids": sha256_file(grids_path),
        },
        "validation": validation,
    }


def _table_details(item: Mapping[str, Any], grids: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    source_id = str((item.get("source") or {}).get("source_visual_object_id"))
    grid = next((row for row in grids if row.get("table_visual_object_id") == source_id), None)
    if not isinstance(grid, Mapping):
        return {"row_count": None, "column_count": None, "cell_fragments": [], "table_grid_status": "missing"}
    row_indexes = {str(row.get("row_id")): row.get("index") for row in grid.get("rows", []) if isinstance(row, Mapping)}
    column_indexes = {str(row.get("column_id")): row.get("index") for row in grid.get("columns", []) if isinstance(row, Mapping)}
    cells = []
    for cell in grid.get("cells", []) if isinstance(grid.get("cells"), list) else []:
        if not isinstance(cell, Mapping):
            continue
        cells.append({
            "row": row_indexes.get(str(cell.get("row_id"))),
            "column": column_indexes.get(str(cell.get("column_id"))),
            "text": cell.get("text_candidate"),
        })
    cells.sort(key=lambda row: (int(row["row"]) if isinstance(row.get("row"), int) else 10**9, int(row["column"]) if isinstance(row.get("column"), int) else 10**9, str(row.get("text"))))
    return {
        "row_count": len(row_indexes),
        "column_count": len(column_indexes),
        "cell_fragments": cells,
        "table_grid_status": grid.get("status"),
        "table_grid_sha256": grid.get("grid_sha256"),
    }


def derive_predictions(facts: Mapping[str, Any], parser: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Derive predictions from parser-bound candidate evidence only; no Gold."""

    objects = {str(row.get("visual_object_id")): row for row in parser["objects"] if isinstance(row.get("visual_object_id"), str)}
    predictions: list[dict[str, Any]] = []
    for item in sorted(facts["candidate_items"], key=lambda row: str(row["item_id"])):
        source = item.get("source") if isinstance(item.get("source"), Mapping) else {}
        source_id = str(source.get("source_visual_object_id"))
        source_object = objects.get(source_id, {})
        kind = str(item["kind_candidate"])
        details: dict[str, Any] = {
            "source_object_id": source_id,
            "source_object_type": source.get("source_object_type"),
            "source_label_candidate": source.get("source_object_label_candidate"),
        }
        if kind == "table":
            details.update(_table_details(item, parser["grids"]))
        elif isinstance(source_object, Mapping):
            details["source_attributes"] = deepcopy(source_object.get("attributes", {}))
        output = {
            "presence": "present",
            "kind": kind,
            "bbox": deepcopy((item.get("geometry") or {}).get("candidate_bbox_pdf")),
            "bbox_coordinate_space": COORDINATE_SPACE,
            "details": details,
        }
        row = {
            "item_id": item["item_id"],
            "item_sha256": item["item_sha256"],
            "split": item["split"],
            "output": output,
            "output_sha256": sha256_json(output),
        }
        predictions.append(row)
    return predictions


def _presence(payload: Mapping[str, Any]) -> bool:
    return payload.get("presence") == "present"


def _evaluate_split(split: str, gold_rows: Sequence[Mapping[str, Any]], predictions: Sequence[Mapping[str, Any]], threshold: float, *, job_id: str, evaluator_id: str) -> dict[str, Any]:
    gold = {str(row["item_id"]): row for row in gold_rows if row.get("split") == split}
    predicted = {str(row["item_id"]): row for row in predictions if row.get("split") == split}
    if not gold or set(gold) != set(predicted):
        raise PortableVisualBenchmarkError(f"evaluation_item_set_invalid:{split}")
    pairs = [(gold[item_id], predicted[item_id]) for item_id in sorted(gold)]
    actual_positive = sum(1 for gold_row, _ in pairs if _presence(gold_row["gold_payload"]))
    predicted_positive = sum(1 for _, prediction in pairs if _presence(prediction["output"]))
    true_positive = sum(1 for gold_row, prediction in pairs if _presence(gold_row["gold_payload"]) and _presence(prediction["output"]))
    precision = true_positive / predicted_positive if predicted_positive else None
    recall = true_positive / actual_positive if actual_positive else None
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else None
    type_correct = sum(1 for gold_row, prediction in pairs if _presence(gold_row["gold_payload"]) and gold_row["gold_payload"].get("kind") == prediction["output"].get("kind"))
    ious = [score for gold_row, prediction in pairs if _presence(gold_row["gold_payload"]) and (score := _bbox_iou(gold_row["gold_payload"].get("bbox"), prediction["output"].get("bbox"))) is not None]
    table_pairs = [(gold_row, prediction) for gold_row, prediction in pairs if gold_row.get("kind_candidate") == "table" and _presence(gold_row["gold_payload"])]
    figure_pairs = [(gold_row, prediction) for gold_row, prediction in pairs if gold_row.get("kind_candidate") == "figure"]
    chart_pairs = [(gold_row, prediction) for gold_row, prediction in pairs if gold_row.get("kind_candidate") == "chart"]
    exact = lambda left, right: 1.0 if left == right else 0.0
    table_row = [exact((gold_row["gold_payload"].get("details") or {}).get("row_count"), (prediction["output"].get("details") or {}).get("row_count")) for gold_row, prediction in table_pairs]
    table_column = [exact((gold_row["gold_payload"].get("details") or {}).get("column_count"), (prediction["output"].get("details") or {}).get("column_count")) for gold_row, prediction in table_pairs]
    table_cells = [exact((gold_row["gold_payload"].get("details") or {}).get("cells"), (prediction["output"].get("details") or {}).get("cell_fragments")) for gold_row, prediction in table_pairs]
    figure_predicted = sum(1 for _, prediction in figure_pairs if _presence(prediction["output"]))
    figure_true = sum(1 for gold_row, prediction in figure_pairs if _presence(gold_row["gold_payload"]) and _presence(prediction["output"]))
    chart_predicted = sum(1 for _, prediction in chart_pairs if _presence(prediction["output"]))
    chart_true = sum(1 for gold_row, prediction in chart_pairs if _presence(gold_row["gold_payload"]) and _presence(prediction["output"]))
    metrics = [
        _metric("reviewed_candidate_count", "measured", len(pairs), unit="count"),
        _metric("reviewed_candidate_presence_precision", "measured", precision),
        _metric("reviewed_candidate_presence_recall", "measured", recall, reason="denominator_contains_only_gold-positive_emitted_candidates"),
        _metric("reviewed_candidate_presence_f1", "measured", f1),
        _metric("candidate_type_accuracy", "measured", type_correct / len(pairs)),
        _metric("false_positive_count", "measured", predicted_positive - true_positive, unit="count"),
        _metric("bbox_mean_iou", "measured", _mean(ious)),
        _metric("localization_coverage_at_frozen_threshold", "measured", sum(1 for value in ious if value >= threshold) / actual_positive if actual_positive else None),
        _metric("figure_presence_precision", "measured" if figure_pairs else "not-applicable", figure_true / figure_predicted if figure_predicted else None),
        _metric("chart_presence_precision", "measured" if chart_pairs else "not-applicable", chart_true / chart_predicted if chart_predicted else None),
        _metric("table_row_count_exactness", "measured" if table_pairs else "not-applicable", _mean(table_row)),
        _metric("table_column_count_exactness", "measured" if table_pairs else "not-applicable", _mean(table_column)),
        _metric("table_cell_exactness", "measured" if table_pairs else "not-applicable", _mean(table_cells)),
        _metric("whole_range_detection_recall", "not-measured", None, reason="workpack_contains_emitted_candidates_only"),
    ]
    evaluation = {
        "schema_version": PORTABLE_VISUAL_BENCHMARK_EVALUATION_SCHEMA,
        "protocol": PORTABLE_VISUAL_BENCHMARK_PROTOCOL,
        "compiler_version": PORTABLE_VISUAL_BENCHMARK_COMPILER_VERSION,
        "job_id": job_id,
        "split": split,
        "status": "measured-candidate-observation",
        "evaluator_instance": evaluator_id,
        "bbox_iou_threshold": threshold,
        "metrics": metrics,
        "prediction_set_sha256": sha256_json([(row["item_id"], row["output_sha256"]) for row in sorted(predicted.values(), key=lambda value: str(value["item_id"]))]),
        "gold_item_set_sha256": sha256_json(sorted(gold)),
        "candidate_only": True,
        "whole_range_recall_claimed": False,
        "accuracy_claim": False,
        "verified_gold": False,
        "promotion_allowed": False,
        "release_included": False,
    }
    evaluation["evaluation_sha256"] = _hash_without(evaluation, "evaluation_sha256")
    return evaluation


def _threshold(gold_rows: Sequence[Mapping[str, Any]], predictions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    gold = {str(row["item_id"]): row for row in gold_rows if row.get("split") == "calibration"}
    predicted = {str(row["item_id"]): row for row in predictions if row.get("split") == "calibration"}
    ious = [score for item_id, gold_row in gold.items() if _presence(gold_row["gold_payload"]) and item_id in predicted and (score := _bbox_iou(gold_row["gold_payload"].get("bbox"), predicted[item_id]["output"].get("bbox"))) is not None]
    threshold = min(0.9, max(0.5, _mean(ious) or 0.75))
    value = {
        "schema_version": "tkc.portable-visual-benchmark-threshold/v0.1",
        "protocol": PORTABLE_VISUAL_BENCHMARK_PROTOCOL,
        "frozen_from": "calibration",
        "derivation": "min-0.9-max-0.5-calibration-mean-bbox-iou",
        "bbox_iou_threshold": threshold,
        "calibration_item_ids_sha256": sha256_json(sorted(gold)),
        "test_data_used": False,
    }
    value["threshold_sha256"] = _hash_without(value, "threshold_sha256")
    return value


def _load_inputs(workpack: Path, gold_revision: Path, visual_parser_job: Path, evaluator_id: str) -> dict[str, Any]:
    if not isinstance(evaluator_id, str) or not ID_RE.fullmatch(evaluator_id):
        raise PortableVisualBenchmarkError("evaluator_id_invalid")
    try:
        facts = load_phase7d5a_workpack(Path(workpack), allow_portable_review=True)
    except Exception as error:
        raise PortableVisualBenchmarkError(f"portable_workpack_invalid:{error}") from error
    if facts.get("portable_review") is not True or facts["manifest"].get("candidate_correctness_review_only") is not True or facts["manifest"].get("whole_range_recall_claimed") is not False:
        raise PortableVisualBenchmarkError("portable_workpack_scope_invalid")
    parser = _load_parser_run(visual_parser_job, facts)
    gold = _load_gold_revision(gold_revision, facts)
    roles = _role_ids(gold["rows"], facts)
    if evaluator_id in set(roles["reviewers"] + roles["curators"] + roles["proposers"]):
        raise PortableVisualBenchmarkError("evaluator_role_overlap")
    return {"facts": facts, "parser": parser, "gold": gold, "roles": roles, "backend_binding": _backend_binding(facts), "evaluator_id": evaluator_id}


def _plan(inputs: Mapping[str, Any], evaluated_at: str) -> dict[str, Any]:
    facts = inputs["facts"]
    parser = inputs["parser"]
    gold = inputs["gold"]
    core = {
        "schema_version": PORTABLE_VISUAL_BENCHMARK_PLAN_SCHEMA,
        "protocol": PORTABLE_VISUAL_BENCHMARK_PROTOCOL,
        "compiler_version": PORTABLE_VISUAL_BENCHMARK_COMPILER_VERSION,
        "evaluated_at": evaluated_at,
        "workpack_binding": {
            "manifest_sha256": facts["manifest_sha256"],
            "candidate_items_sha256": facts["candidate_items_sha256"],
            "candidate_count": len(facts["candidate_ids"]),
            "split_commitment_sha256": facts["split"]["commitment_sha256"],
        },
        "gold_binding": {
            "revision_manifest_sha256": gold["manifest"]["manifest_sha256"],
            "gold_fragments_file_sha256": sha256_file(gold["path"]),
            "item_count": gold["item_count"],
        },
        "parser_run_binding": {
            "job_sha256": parser["job"].get("job_sha256"),
            "manifest_sha256": parser["manifest"].get("manifest_sha256"),
            "file_hashes": parser["file_hashes"],
            "backend": inputs["backend_binding"],
        },
        "split": {
            "calibration_count": len(facts["calibration_ids"]),
            "test_count": len(facts["test_ids"]),
            "test_answers_cannot_change_thresholds": True,
        },
        "evaluator": {"instance": inputs["evaluator_id"], "role_separate_from": inputs["roles"]},
        "policy": {
            "candidate_only": True,
            "whole_range_recall_claimed": False,
            "accuracy_claim": False,
            "network_access": False,
            "model_invocations": 0,
            "mcp_calls": 0,
            "promotion_allowed": False,
            "release_included": False,
        },
    }
    job_id = "pvbench-" + sha256_json(core)[:24]
    value = {"job_id": job_id, **core}
    value["plan_sha256"] = _hash_without(value, "plan_sha256")
    return value


def _components(inputs: Mapping[str, Any], evaluated_at: str) -> dict[str, Any]:
    plan = _plan(inputs, evaluated_at)
    predictions = derive_predictions(inputs["facts"], inputs["parser"])
    threshold = _threshold(inputs["gold"]["rows"], predictions)
    calibration = _evaluate_split("calibration", inputs["gold"]["rows"], predictions, float(threshold["bbox_iou_threshold"]), job_id=plan["job_id"], evaluator_id=inputs["evaluator_id"])
    test = _evaluate_split("test", inputs["gold"]["rows"], predictions, float(threshold["bbox_iou_threshold"]), job_id=plan["job_id"], evaluator_id=inputs["evaluator_id"])
    import_receipt = {
        "schema_version": "tkc.portable-visual-benchmark-gold-import/v0.1",
        "protocol": PORTABLE_VISUAL_BENCHMARK_PROTOCOL,
        "job_id": plan["job_id"],
        "gold_fragments_file_sha256": sha256_file(inputs["gold"]["path"]),
        "item_count": inputs["gold"]["item_count"],
        "attestation_count": inputs["gold"]["attestation_count"],
        "reviewer_instances": inputs["roles"]["reviewers"],
        "independent_real_gold": True,
        "verified_gold": False,
        "promotion_allowed": False,
        "release_included": False,
    }
    import_receipt["receipt_sha256"] = _hash_without(import_receipt, "receipt_sha256")
    budget = inputs["parser"]["budget"]
    runtime = {
        "schema_version": "tkc.portable-visual-benchmark-runtime/v0.1",
        "protocol": PORTABLE_VISUAL_BENCHMARK_PROTOCOL,
        "job_id": plan["job_id"],
        "measurement_source": "frozen-original-parser-budget-receipt",
        "source_budget_receipt_sha256": budget["budget_receipt_sha256"],
        "source_budget_receipt_file_sha256": inputs["parser"]["file_hashes"]["visual_budget_receipt"],
        "elapsed_seconds": budget.get("elapsed_seconds"),
        "pages": budget.get("pages"),
        "regions": budget.get("regions"),
        "calls": budget.get("calls"),
        "objects": budget.get("objects"),
        "bytes": budget.get("bytes"),
        "rss_peak_bytes": {"status": "not-measured", "value": None},
        "cold_start_seconds": {"status": "not-measured", "value": None},
        "warm_repeat_seconds": {"status": "not-measured", "value": None},
        "model_invocations": 0,
        "network_access": False,
        "mcp_calls": 0,
        "candidate_only": True,
        "release_included": False,
    }
    runtime["runtime_sha256"] = _hash_without(runtime, "runtime_sha256")
    return {"plan": plan, "predictions": predictions, "threshold": threshold, "calibration": calibration, "test": test, "import_receipt": import_receipt, "runtime": runtime}


def evaluate_portable_candidates(workpack: Path, gold_revision: Path, visual_parser_job: Path, output: Path, *, evaluator_id: str, evaluated_at: str) -> dict[str, Any]:
    inputs = _load_inputs(workpack, gold_revision, visual_parser_job, evaluator_id)
    components = _components(inputs, evaluated_at)
    destination = _secure_directory(output, must_be_empty=True)
    _write_json(destination / "benchmark-plan.json", components["plan"])
    _write_json(destination / "gold-import-receipt.json", components["import_receipt"])
    _write_jsonl(destination / "predictions.jsonl", components["predictions"])
    _write_json(destination / "threshold-config.json", components["threshold"])
    _write_json(destination / "evaluation-calibration.json", components["calibration"])
    _write_json(destination / "evaluation-test.json", components["test"])
    _write_json(destination / "runtime-receipt.json", components["runtime"])
    output_files = [
        "benchmark-plan.json",
        "gold-import-receipt.json",
        "predictions.jsonl",
        "threshold-config.json",
        "evaluation-calibration.json",
        "evaluation-test.json",
        "runtime-receipt.json",
    ]
    manifest = {
        "schema_version": PORTABLE_VISUAL_BENCHMARK_MANIFEST_SCHEMA,
        "protocol": PORTABLE_VISUAL_BENCHMARK_PROTOCOL,
        "compiler_version": PORTABLE_VISUAL_BENCHMARK_COMPILER_VERSION,
        "job_id": components["plan"]["job_id"],
        "status": "measured-candidate-benchmark",
        "file_hashes": {name: sha256_file(destination / name) for name in output_files},
        "candidate_count": len(inputs["facts"]["candidate_ids"]),
        "calibration_count": len(inputs["facts"]["calibration_ids"]),
        "test_count": len(inputs["facts"]["test_ids"]),
        "gold_imported": True,
        "candidate_observation_metrics": True,
        "whole_range_recall_claimed": False,
        "accuracy_claim": False,
        "verified_gold": False,
        "promotion_allowed": False,
        "release_included": False,
        "immutable": True,
        "network_access": False,
        "model_invocations": 0,
        "mcp_calls": 0,
    }
    manifest["manifest_sha256"] = _hash_without(manifest, "manifest_sha256")
    _write_json(destination / "benchmark-manifest.json", manifest)
    return {
        "status": manifest["status"],
        "benchmark": str(destination),
        "job_id": manifest["job_id"],
        "candidate_count": manifest["candidate_count"],
        "calibration_evaluation_sha256": components["calibration"]["evaluation_sha256"],
        "test_evaluation_sha256": components["test"]["evaluation_sha256"],
        "runtime_sha256": components["runtime"]["runtime_sha256"],
        "whole_range_recall_claimed": False,
        "accuracy_claim": False,
        "verified_gold": False,
        "promotion_allowed": False,
        "release_included": False,
    }


def validate_portable_benchmark(benchmark: Path, workpack: Path, gold_revision: Path, visual_parser_job: Path) -> dict[str, Any]:
    root = _secure_directory(benchmark)
    manifest = _read_json(root / "benchmark-manifest.json", "benchmark_manifest")
    if manifest.get("schema_version") != PORTABLE_VISUAL_BENCHMARK_MANIFEST_SCHEMA or manifest.get("protocol") != PORTABLE_VISUAL_BENCHMARK_PROTOCOL or manifest.get("compiler_version") != PORTABLE_VISUAL_BENCHMARK_COMPILER_VERSION:
        raise PortableVisualBenchmarkError("benchmark_manifest_schema_invalid")
    if manifest.get("manifest_sha256") != _hash_without(manifest, "manifest_sha256"):
        raise PortableVisualBenchmarkError("benchmark_manifest_hash_mismatch")
    if manifest.get("status") != "measured-candidate-benchmark" or manifest.get("gold_imported") is not True or manifest.get("candidate_observation_metrics") is not True:
        raise PortableVisualBenchmarkError("benchmark_manifest_status_invalid")
    if manifest.get("whole_range_recall_claimed") is not False or manifest.get("accuracy_claim") is not False or manifest.get("verified_gold") is not False or manifest.get("promotion_allowed") is not False or manifest.get("release_included") is not False or manifest.get("immutable") is not True:
        raise PortableVisualBenchmarkError("benchmark_manifest_policy_invalid")
    for relative, expected in (manifest.get("file_hashes") or {}).items():
        path = (root / str(relative)).resolve()
        if not path.is_relative_to(root) or path.is_symlink() or not path.is_file() or sha256_file(path) != expected:
            raise PortableVisualBenchmarkError(f"benchmark_file_hash_mismatch:{relative}")
    plan = _read_json(root / "benchmark-plan.json", "benchmark_plan")
    inputs = _load_inputs(workpack, gold_revision, visual_parser_job, str((plan.get("evaluator") or {}).get("instance")))
    expected = _components(inputs, str(plan.get("evaluated_at")))
    checks = {
        "benchmark-plan.json": expected["plan"],
        "gold-import-receipt.json": expected["import_receipt"],
        "threshold-config.json": expected["threshold"],
        "evaluation-calibration.json": expected["calibration"],
        "evaluation-test.json": expected["test"],
        "runtime-receipt.json": expected["runtime"],
    }
    for relative, expected_value in checks.items():
        if _read_json(root / relative, relative) != expected_value:
            raise PortableVisualBenchmarkError(f"benchmark_replay_mismatch:{relative}")
    if _read_jsonl(root / "predictions.jsonl", "predictions") != expected["predictions"]:
        raise PortableVisualBenchmarkError("benchmark_replay_mismatch:predictions.jsonl")
    if any(path.is_symlink() or path.suffix.casefold() in {".pdf", ".png", ".jpg", ".jpeg"} for path in root.rglob("*")):
        raise PortableVisualBenchmarkError("benchmark_private_source_or_asset_leak")
    return {
        "status": "verified",
        "benchmark": str(root),
        "job_id": manifest["job_id"],
        "candidate_count": manifest["candidate_count"],
        "calibration_count": manifest["calibration_count"],
        "test_count": manifest["test_count"],
        "whole_range_recall_claimed": False,
        "accuracy_claim": False,
        "verified_gold": False,
        "promotion_allowed": False,
        "release_included": False,
        "network_access": False,
        "model_invocations": 0,
        "mcp_calls": 0,
    }


def _load_frozen_benchmark(benchmark: Path, facts: Mapping[str, Any], gold: Mapping[str, Any]) -> dict[str, Any]:
    root = _secure_directory(benchmark)
    manifest = _read_json(root / "benchmark-manifest.json", "frozen_benchmark_manifest")
    if manifest.get("status") != "measured-candidate-benchmark" or manifest.get("immutable") is not True:
        raise PortableVisualBenchmarkError("frozen_benchmark_status_invalid")
    if manifest.get("manifest_sha256") != _hash_without(manifest, "manifest_sha256"):
        raise PortableVisualBenchmarkError("frozen_benchmark_manifest_hash_mismatch")
    for relative, expected in (manifest.get("file_hashes") or {}).items():
        path = (root / str(relative)).resolve()
        if not path.is_relative_to(root) or path.is_symlink() or not path.is_file() or sha256_file(path) != expected:
            raise PortableVisualBenchmarkError(f"frozen_benchmark_file_hash_mismatch:{relative}")
    plan = _read_json(root / "benchmark-plan.json", "frozen_benchmark_plan")
    workpack_binding = plan.get("workpack_binding") if isinstance(plan.get("workpack_binding"), Mapping) else {}
    gold_binding = plan.get("gold_binding") if isinstance(plan.get("gold_binding"), Mapping) else {}
    if workpack_binding.get("manifest_sha256") != facts.get("manifest_sha256") or workpack_binding.get("candidate_items_sha256") != facts.get("candidate_items_sha256"):
        raise PortableVisualBenchmarkError("frozen_benchmark_workpack_binding_mismatch")
    if gold_binding.get("revision_manifest_sha256") != gold["manifest"].get("manifest_sha256") or gold_binding.get("gold_fragments_file_sha256") != sha256_file(gold["path"]):
        raise PortableVisualBenchmarkError("frozen_benchmark_gold_binding_mismatch")
    threshold = _read_json(root / "threshold-config.json", "frozen_threshold")
    if threshold.get("frozen_from") != "calibration" or threshold.get("test_data_used") is not False or threshold.get("threshold_sha256") != _hash_without(threshold, "threshold_sha256"):
        raise PortableVisualBenchmarkError("frozen_threshold_invalid")
    return {"root": root, "manifest": manifest, "plan": plan, "threshold": threshold}


def _load_bounded_recall_revision(revision: Path, source_sha256: str) -> dict[str, Any]:
    root = _secure_directory(revision)
    manifest = _read_json(root / "recall-revision.json", "bounded_recall_revision")
    if manifest.get("status") != "accepted-incremental-bounded-recall" or manifest.get("immutable") is not True or manifest.get("source_sha256") != source_sha256:
        raise PortableVisualBenchmarkError("bounded_recall_revision_status_invalid")
    if manifest.get("verified_gold") is not False or manifest.get("promotion_allowed") is not False or manifest.get("release_included") is not False:
        raise PortableVisualBenchmarkError("bounded_recall_revision_policy_invalid")
    if manifest.get("revision_sha256") != _hash_without(manifest, "revision_sha256"):
        raise PortableVisualBenchmarkError("bounded_recall_revision_hash_mismatch")
    for relative, expected in (manifest.get("file_hashes") or {}).items():
        path = (root / str(relative)).resolve()
        if not path.is_relative_to(root) or path.is_symlink() or not path.is_file() or sha256_file(path) != expected:
            raise PortableVisualBenchmarkError(f"bounded_recall_file_hash_mismatch:{relative}")
    objects_path = _secure_file(root / "gold-objects.jsonl", "bounded_recall_gold_objects")
    objects = _read_jsonl(objects_path, "bounded_recall_gold_objects")
    if len(objects) != manifest.get("object_count") or any(row.get("coordinate_space") != COORDINATE_SPACE for row in objects):
        raise PortableVisualBenchmarkError("bounded_recall_gold_objects_invalid")
    return {"root": root, "manifest": manifest, "objects": objects, "objects_path": objects_path}


def derive_replay_predictions(frozen_facts: Mapping[str, Any], candidate_facts: Mapping[str, Any], parser: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Project a changed candidate set onto the immutable original split."""

    current = {str(row["item_id"]): row for row in derive_predictions(candidate_facts, parser)}
    predictions: list[dict[str, Any]] = []
    frozen_ids = {str(row["item_id"]) for row in frozen_facts["candidate_items"]}
    for item in sorted(frozen_facts["candidate_items"], key=lambda row: str(row["item_id"])):
        item_id = str(item["item_id"])
        if item_id in current:
            output = deepcopy(current[item_id]["output"])
        else:
            output = {
                "presence": "absent",
                "kind": str(item["kind_candidate"]),
                "bbox": None,
                "bbox_coordinate_space": COORDINATE_SPACE,
                "details": {"reason": "not-emitted-by-replayed-candidate-parser"},
            }
        predictions.append({
            "item_id": item_id,
            "item_sha256": item["item_sha256"],
            "split": item["split"],
            "output": output,
            "output_sha256": sha256_json(output),
        })
    extra_ids = sorted(str(row["item_id"]) for row in candidate_facts["candidate_items"] if str(row["item_id"]) not in frozen_ids)
    return predictions, extra_ids


def _load_replay_inputs(frozen_workpack: Path, candidate_workpack: Path, gold_revision: Path, frozen_benchmark: Path, bounded_recall_revision: Path, visual_parser_job: Path, evaluator_id: str) -> dict[str, Any]:
    if not isinstance(evaluator_id, str) or not ID_RE.fullmatch(evaluator_id):
        raise PortableVisualBenchmarkError("evaluator_id_invalid")
    try:
        frozen_facts = load_phase7d5a_workpack(Path(frozen_workpack), allow_portable_review=True)
        candidate_facts = load_phase7d5a_workpack(Path(candidate_workpack), allow_portable_review=True)
    except Exception as error:
        raise PortableVisualBenchmarkError(f"portable_workpack_invalid:{error}") from error
    source_sha = str(frozen_facts["manifest"].get("source_sha256"))
    if candidate_facts["manifest"].get("source_sha256") != source_sha:
        raise PortableVisualBenchmarkError("replay_workpack_source_mismatch")
    parser = _load_parser_run(visual_parser_job, candidate_facts)
    gold = _load_gold_revision(gold_revision, frozen_facts)
    frozen = _load_frozen_benchmark(frozen_benchmark, frozen_facts, gold)
    recall = _load_bounded_recall_revision(bounded_recall_revision, source_sha)
    roles = _role_ids(gold["rows"], frozen_facts)
    recall_reviewer = (recall["manifest"].get("reviewer") or {}).get("reviewer_instance")
    if evaluator_id in set(roles["reviewers"] + roles["curators"] + roles["proposers"] + ([str(recall_reviewer)] if recall_reviewer else [])):
        raise PortableVisualBenchmarkError("evaluator_role_overlap")
    return {"frozen_facts": frozen_facts, "candidate_facts": candidate_facts, "parser": parser, "gold": gold, "frozen": frozen, "recall": recall, "roles": roles, "evaluator_id": evaluator_id}


def _replay_components(inputs: Mapping[str, Any], evaluated_at: str) -> dict[str, Any]:
    predictions, extra_ids = derive_replay_predictions(inputs["frozen_facts"], inputs["candidate_facts"], inputs["parser"])
    threshold = float(inputs["frozen"]["threshold"]["bbox_iou_threshold"])
    core = {
        "compiler_version": PORTABLE_VISUAL_CALIBRATION_REPLAY_COMPILER_VERSION,
        "evaluated_at": evaluated_at,
        "source_sha256": inputs["frozen_facts"]["manifest"]["source_sha256"],
        "frozen_benchmark_manifest_sha256": inputs["frozen"]["manifest"]["manifest_sha256"],
        "frozen_threshold_sha256": inputs["frozen"]["threshold"]["threshold_sha256"],
        "frozen_workpack_manifest_sha256": inputs["frozen_facts"]["manifest_sha256"],
        "candidate_workpack_manifest_sha256": inputs["candidate_facts"]["manifest_sha256"],
        "candidate_parser_job_sha256": inputs["parser"]["job"].get("job_sha256"),
        "candidate_parser_manifest_sha256": inputs["parser"]["manifest"].get("manifest_sha256"),
        "bounded_recall_revision_sha256": inputs["recall"]["manifest"]["revision_sha256"],
        "evaluator_instance": inputs["evaluator_id"],
    }
    replay_id = "pvreplay-" + sha256_json(core)[:24]
    calibration = _evaluate_split("calibration", inputs["gold"]["rows"], predictions, threshold, job_id=replay_id, evaluator_id=inputs["evaluator_id"])
    test = _evaluate_split("test", inputs["gold"]["rows"], predictions, threshold, job_id=replay_id, evaluator_id=inputs["evaluator_id"])
    from portable_visual_recall_review import _match
    candidates = [{
        "candidate_id": str(item["item_id"]),
        "physical_page": int((item.get("source") or {}).get("physical_page")),
        "kind": str(item["kind_candidate"]),
        "bbox": deepcopy((item.get("geometry") or {}).get("candidate_bbox_pdf")),
    } for item in inputs["candidate_facts"]["candidate_items"]]
    matches, metrics = _match(inputs["recall"]["objects"], candidates)
    bounded = {
        "status": "measured-bounded-recall-replay",
        "matching_policy": {"same_page": True, "same_kind": True, "one_to_one": True, "bbox_iou_threshold": 0.5},
        "metrics": metrics,
        "matches": matches,
        "target": {"metric": "f1", "minimum": 0.9, "met": metrics.get("f1") is not None and float(metrics["f1"]) >= 0.9},
        "assurance": inputs["recall"]["manifest"].get("assurance"),
        "whole_book_or_general_accuracy": False,
    }
    receipt = {
        "schema_version": "tkc.portable-visual-calibration-replay/v0.1",
        "protocol": "portable-visual-calibration-replay-v0.1",
        "replay_id": replay_id,
        **core,
        "frozen_split_preserved": True,
        "test_answers_changed_rule_or_threshold": False,
        "candidate_count_before": len(inputs["frozen_facts"]["candidate_ids"]),
        "candidate_count_after": len(inputs["candidate_facts"]["candidate_ids"]),
        "new_candidate_ids_outside_frozen_split": extra_ids,
        "calibration_evaluation_sha256": calibration["evaluation_sha256"],
        "test_evaluation_sha256": test["evaluation_sha256"],
        "bounded_metrics": metrics,
        "bounded_target_met": bounded["target"]["met"],
        "verified_gold": False,
        "promotion_allowed": False,
        "release_included": False,
        "mvp_acceptance": False,
        "immutable": True,
    }
    receipt["receipt_sha256"] = _hash_without(receipt, "receipt_sha256")
    return {"receipt": receipt, "predictions": predictions, "calibration": calibration, "test": test, "bounded": bounded}


def evaluate_calibration_replay(frozen_workpack: Path, candidate_workpack: Path, gold_revision: Path, frozen_benchmark: Path, bounded_recall_revision: Path, visual_parser_job: Path, output: Path, *, evaluator_id: str, evaluated_at: str) -> dict[str, Any]:
    inputs = _load_replay_inputs(frozen_workpack, candidate_workpack, gold_revision, frozen_benchmark, bounded_recall_revision, visual_parser_job, evaluator_id)
    components = _replay_components(inputs, evaluated_at)
    destination = _secure_directory(output, must_be_empty=True)
    _write_json(destination / "replay-receipt.json", components["receipt"])
    _write_jsonl(destination / "replay-predictions.jsonl", components["predictions"])
    _write_json(destination / "evaluation-calibration.json", components["calibration"])
    _write_json(destination / "evaluation-test.json", components["test"])
    _write_json(destination / "bounded-recall-replay.json", components["bounded"])
    return {"status": "measured-candidate-replay", "replay": str(destination), "replay_id": components["receipt"]["replay_id"], "bounded_metrics": components["bounded"]["metrics"], "bounded_target_met": components["bounded"]["target"]["met"], "mvp_acceptance": False, "promotion_allowed": False, "release_included": False}


def validate_calibration_replay(replay: Path, frozen_workpack: Path, candidate_workpack: Path, gold_revision: Path, frozen_benchmark: Path, bounded_recall_revision: Path, visual_parser_job: Path) -> dict[str, Any]:
    root = _secure_directory(replay)
    receipt = _read_json(root / "replay-receipt.json", "replay_receipt")
    inputs = _load_replay_inputs(frozen_workpack, candidate_workpack, gold_revision, frozen_benchmark, bounded_recall_revision, visual_parser_job, str(receipt.get("evaluator_instance")))
    expected = _replay_components(inputs, str(receipt.get("evaluated_at")))
    if receipt != expected["receipt"] or _read_jsonl(root / "replay-predictions.jsonl", "replay_predictions") != expected["predictions"] or _read_json(root / "evaluation-calibration.json", "replay_calibration") != expected["calibration"] or _read_json(root / "evaluation-test.json", "replay_test") != expected["test"] or _read_json(root / "bounded-recall-replay.json", "bounded_recall_replay") != expected["bounded"]:
        raise PortableVisualBenchmarkError("calibration_replay_mismatch")
    return {"status": "verified", "replay": str(root), "replay_id": receipt["replay_id"], "bounded_metrics": receipt["bounded_metrics"], "bounded_target_met": receipt["bounded_target_met"], "mvp_acceptance": False, "promotion_allowed": False, "release_included": False}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--workpack", type=Path, required=True)
    evaluate.add_argument("--gold-revision", type=Path, required=True)
    evaluate.add_argument("--visual-parser-job", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    evaluate.add_argument("--evaluator-id", required=True)
    evaluate.add_argument("--evaluated-at", default="1970-01-01T00:00:00Z")
    validate = subparsers.add_parser("validate")
    validate.add_argument("--benchmark", type=Path, required=True)
    validate.add_argument("--workpack", type=Path, required=True)
    validate.add_argument("--gold-revision", type=Path, required=True)
    validate.add_argument("--visual-parser-job", type=Path, required=True)
    replay = subparsers.add_parser("evaluate-replay")
    replay.add_argument("--frozen-workpack", type=Path, required=True)
    replay.add_argument("--candidate-workpack", type=Path, required=True)
    replay.add_argument("--gold-revision", type=Path, required=True)
    replay.add_argument("--frozen-benchmark", type=Path, required=True)
    replay.add_argument("--bounded-recall-revision", type=Path, required=True)
    replay.add_argument("--visual-parser-job", type=Path, required=True)
    replay.add_argument("--output", type=Path, required=True)
    replay.add_argument("--evaluator-id", required=True)
    replay.add_argument("--evaluated-at", default="1970-01-01T00:00:00Z")
    replay_validate = subparsers.add_parser("validate-replay")
    replay_validate.add_argument("--replay", type=Path, required=True)
    replay_validate.add_argument("--frozen-workpack", type=Path, required=True)
    replay_validate.add_argument("--candidate-workpack", type=Path, required=True)
    replay_validate.add_argument("--gold-revision", type=Path, required=True)
    replay_validate.add_argument("--frozen-benchmark", type=Path, required=True)
    replay_validate.add_argument("--bounded-recall-revision", type=Path, required=True)
    replay_validate.add_argument("--visual-parser-job", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "evaluate":
            result = evaluate_portable_candidates(args.workpack, args.gold_revision, args.visual_parser_job, args.output, evaluator_id=args.evaluator_id, evaluated_at=args.evaluated_at)
        elif args.command == "validate":
            result = validate_portable_benchmark(args.benchmark, args.workpack, args.gold_revision, args.visual_parser_job)
        elif args.command == "evaluate-replay":
            result = evaluate_calibration_replay(args.frozen_workpack, args.candidate_workpack, args.gold_revision, args.frozen_benchmark, args.bounded_recall_revision, args.visual_parser_job, args.output, evaluator_id=args.evaluator_id, evaluated_at=args.evaluated_at)
        else:
            result = validate_calibration_replay(args.replay, args.frozen_workpack, args.candidate_workpack, args.gold_revision, args.frozen_benchmark, args.bounded_recall_revision, args.visual_parser_job)
    except (PortableVisualBenchmarkError, OSError, ValueError) as error:
        print(json.dumps({"status": "rejected", "error": str(error)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
