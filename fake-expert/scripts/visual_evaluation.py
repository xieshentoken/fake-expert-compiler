#!/usr/bin/env python3
"""Closed-world evaluation contracts for visual candidates.

This module deliberately evaluates only a supplied Gold object.  It never
creates a Gold label from a PDF, never calls an OCR/parser backend, and never
turns a candidate benchmark into a verified semantic or visual result.  A
real-page run without an independently authored Gold set is therefore reported
as ``not-run`` rather than as an accuracy score.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from compiler_version import VISUAL_EVALUATION_CONTRACT_SCHEMA
from visual_semantics import sha256_json


NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


class VisualEvaluationError(RuntimeError):
    """Stable fail-closed evaluation error."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _box(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        result = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in result) or result[2] <= result[0] or result[3] <= result[1]:
        return None
    return result


def bbox_iou(first: Any, second: Any) -> float:
    """Return axis-aligned IoU for two canonical top-left bboxes."""

    left = _box(first)
    right = _box(second)
    if left is None or right is None:
        return 0.0
    x0 = max(left[0], right[0])
    y0 = max(left[1], right[1])
    x1 = min(left[2], right[2])
    y1 = min(left[3], right[3])
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    area_left = (left[2] - left[0]) * (left[3] - left[1])
    area_right = (right[2] - right[0]) * (right[3] - right[1])
    union = area_left + area_right - intersection
    return intersection / union if union > 0 else 0.0


def _normal_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _objects(bundle: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(bundle, Mapping):
        rows = bundle.get("objects", [])
    else:
        rows = bundle
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _object_compatible(predicted: Mapping[str, Any], gold: Mapping[str, Any]) -> bool:
    if predicted.get("type") != gold.get("type"):
        return False
    if predicted.get("physical_page") != gold.get("physical_page"):
        return False
    gold_label = _normal_text(gold.get("label"))
    predicted_label = _normal_text(predicted.get("label"))
    return not gold_label or not predicted_label or gold_label == predicted_label


def _match_objects(predicted: Sequence[Mapping[str, Any]], gold: Sequence[Mapping[str, Any]], iou_threshold: float) -> tuple[list[dict[str, Any]], set[int], set[int]]:
    matches: list[dict[str, Any]] = []
    used_predicted: set[int] = set()
    used_gold: set[int] = set()
    candidates: list[tuple[float, int, int]] = []
    for predicted_index, predicted_row in enumerate(predicted):
        for gold_index, gold_row in enumerate(gold):
            if _object_compatible(predicted_row, gold_row):
                candidates.append((bbox_iou(predicted_row.get("bbox"), gold_row.get("bbox")), predicted_index, gold_index))
    for iou, predicted_index, gold_index in sorted(candidates, key=lambda item: (-item[0], item[1], item[2])):
        if predicted_index in used_predicted or gold_index in used_gold:
            continue
        used_predicted.add(predicted_index)
        used_gold.add(gold_index)
        matches.append({"predicted_index": predicted_index, "gold_index": gold_index, "iou": round(iou, 6), "matched": iou >= iou_threshold})
    return matches, used_predicted, used_gold


def _object_metrics(predicted: Sequence[Mapping[str, Any]], gold: Sequence[Mapping[str, Any]], iou_threshold: float) -> dict[str, Any]:
    matches, used_predicted, used_gold = _match_objects(predicted, gold, iou_threshold)
    true_positive = sum(1 for row in matches if row["matched"])
    false_positive = len(predicted) - true_positive
    false_negative = len(gold) - true_positive
    precision = true_positive / len(predicted) if predicted else (1.0 if not gold else 0.0)
    recall = true_positive / len(gold) if gold else (1.0 if not predicted else 0.0)
    f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
    ious = [row["iou"] for row in matches]
    return {
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "bbox_iou_mean": round(sum(ious) / len(ious), 6) if ious else 0.0,
        "bbox_iou_ge_threshold": round(sum(1 for value in ious if value >= iou_threshold) / len(ious), 6) if ious else 0.0,
        "iou_threshold": iou_threshold,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "matches": matches,
        "unmatched_predicted": sorted(set(range(len(predicted))) - used_predicted),
        "unmatched_gold": sorted(set(range(len(gold))) - used_gold),
    }


def _grid_rows(bundle: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(bundle, Mapping):
        return []
    return [dict(row) for row in bundle.get("table_grids", []) if isinstance(row, Mapping)]


def _cell_signature(cell: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(cell.get("row_id")),
        str(cell.get("column_id")),
        int(cell.get("row_span", 1)),
        int(cell.get("column_span", 1)),
        _normal_text(cell.get("text_candidate")),
        _normal_text(cell.get("value_candidate")),
        _normal_text(cell.get("unit_candidate")),
        _normal_text(cell.get("symbol_candidate")),
    )


def _grid_key(grid: Mapping[str, Any]) -> tuple[Any, ...]:
    return (grid.get("physical_page"), grid.get("table_visual_object_id"))


def _table_metrics(predicted: Sequence[Mapping[str, Any]], gold: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    predicted_by_key = {_grid_key(row): row for row in predicted}
    gold_by_key = {_grid_key(row): row for row in gold}
    keys = sorted(set(predicted_by_key) | set(gold_by_key), key=lambda value: repr(value))
    exact = 0
    topology_matches = 0
    cell_total = 0
    cell_exact = 0
    details: list[dict[str, Any]] = []
    for key in keys:
        predicted_grid = predicted_by_key.get(key)
        gold_grid = gold_by_key.get(key)
        if predicted_grid is None or gold_grid is None:
            details.append({"key": list(key), "topology_exact": False, "cell_exact": 0, "cell_total": len((gold_grid or {}).get("cells", []))})
            cell_total += len((gold_grid or {}).get("cells", []))
            continue
        predicted_cells = {_cell_signature(cell) for cell in predicted_grid.get("cells", []) if isinstance(cell, Mapping)}
        gold_cells = {_cell_signature(cell) for cell in gold_grid.get("cells", []) if isinstance(cell, Mapping)}
        row_count_equal = len(predicted_grid.get("rows", [])) == len(gold_grid.get("rows", []))
        column_count_equal = len(predicted_grid.get("columns", [])) == len(gold_grid.get("columns", []))
        topology_exact = row_count_equal and column_count_equal and predicted_cells == gold_cells
        if topology_exact:
            exact += 1
        topology_matches += int(row_count_equal and column_count_equal)
        cell_exact += len(predicted_cells & gold_cells)
        cell_total += len(gold_cells)
        details.append({"key": list(key), "topology_exact": topology_exact, "cell_exact": len(predicted_cells & gold_cells), "cell_total": len(gold_cells)})
    return {
        "table_count_predicted": len(predicted),
        "table_count_gold": len(gold),
        "structure_exact": round(exact / len(gold), 6) if gold else (1.0 if not predicted else 0.0),
        "row_column_topology_exact": round(topology_matches / len(gold), 6) if gold else (1.0 if not predicted else 0.0),
        "cell_exactness": round(cell_exact / cell_total, 6) if cell_total else (1.0 if not predicted else 0.0),
        "details": details,
    }


def _typed_tokens(bundle: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> Counter[str]:
    tokens: Counter[str] = Counter()
    for grid in _grid_rows(bundle):
        for cell in grid.get("cells", []):
            if not isinstance(cell, Mapping):
                continue
            for field in ("value_candidate", "unit_candidate", "symbol_candidate"):
                value = _normal_text(cell.get(field))
                if value:
                    tokens[f"{field}:{value}"] += 1
            text = _normal_text(cell.get("text_candidate"))
            for number in NUMBER_RE.findall(text):
                tokens[f"number:{number}"] += 1
    return tokens


def _numeric_unit_metrics(predicted: Mapping[str, Any], gold: Mapping[str, Any]) -> dict[str, Any]:
    predicted_tokens = _typed_tokens(predicted)
    gold_tokens = _typed_tokens(gold)
    exact = sum((predicted_tokens & gold_tokens).values())
    total = sum(gold_tokens.values())
    return {"exact": exact, "gold_total": total, "exactness": round(exact / total, 6) if total else (1.0 if not predicted_tokens else 0.0), "predicted_tokens": dict(sorted(predicted_tokens.items())), "gold_tokens": dict(sorted(gold_tokens.items()))}


def _chart_rows(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in bundle.get("chart_candidates", []) if isinstance(row, Mapping)]


def _chart_cells(candidate: Mapping[str, Any]) -> list[tuple[Any, ...]]:
    chart = candidate.get("chart") if isinstance(candidate.get("chart"), Mapping) else {}
    rows = chart.get("value_candidates", []) if isinstance(chart, Mapping) else []
    cells: list[tuple[Any, ...]] = []
    for row_index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, Mapping):
            continue
        for column_index, cell in enumerate(row.get("cells", []) if isinstance(row.get("cells"), list) else []):
            if not isinstance(cell, Mapping):
                continue
            cells.append((
                int(row_index),
                int(column_index),
                _normal_text(cell.get("column")),
                _normal_text(cell.get("text_candidate")),
                _normal_text(cell.get("value_candidate")),
                _normal_text(cell.get("unit_candidate")),
            ))
    return cells


def _chart_metrics(predicted: Mapping[str, Any], gold: Mapping[str, Any]) -> dict[str, Any]:
    predicted_rows = _chart_rows(predicted)
    gold_rows = _chart_rows(gold)
    predicted_by_page = {int(row.get("physical_page", index + 1)): row for index, row in enumerate(predicted_rows)}
    gold_by_page = {int(row.get("physical_page", index + 1)): row for index, row in enumerate(gold_rows)}
    pages = sorted(set(predicted_by_page) | set(gold_by_page))
    topology_exact = 0
    cell_exact = 0
    cell_total = 0
    numeric_exact = 0
    numeric_total = 0
    hallucinated_numeric = 0
    details: list[dict[str, Any]] = []
    for page in pages:
        predicted_candidate = predicted_by_page.get(page, {})
        gold_candidate = gold_by_page.get(page, {})
        predicted_cells = Counter(_chart_cells(predicted_candidate))
        gold_cells = Counter(_chart_cells(gold_candidate))
        predicted_shape = (
            len({cell[0] for cell in predicted_cells}),
            max((cell[1] for cell in predicted_cells), default=-1) + 1,
        )
        gold_shape = (
            len({cell[0] for cell in gold_cells}),
            max((cell[1] for cell in gold_cells), default=-1) + 1,
        )
        page_topology_exact = predicted_shape == gold_shape
        topology_exact += int(page_topology_exact)
        intersection = predicted_cells & gold_cells
        page_cell_exact = sum(intersection.values())
        page_cell_total = sum(gold_cells.values())
        cell_exact += page_cell_exact
        cell_total += page_cell_total
        predicted_numeric = Counter(cell[4] for cell in predicted_cells.elements() if cell[4])
        gold_numeric = Counter(cell[4] for cell in gold_cells.elements() if cell[4])
        numeric_exact += sum((predicted_numeric & gold_numeric).values())
        numeric_total += sum(gold_numeric.values())
        if not gold_numeric:
            hallucinated_numeric += sum(predicted_numeric.values())
        details.append({"physical_page": page, "topology_exact": page_topology_exact, "predicted_shape": list(predicted_shape), "gold_shape": list(gold_shape), "cell_exact": page_cell_exact, "cell_total": page_cell_total})
    denominator = len(gold_rows)
    return {
        "chart_count_predicted": len(predicted_rows),
        "chart_count_gold": len(gold_rows),
        "row_column_topology_exact": round(topology_exact / denominator, 6) if denominator else (1.0 if not predicted_rows else 0.0),
        "cell_exactness": round(cell_exact / cell_total, 6) if cell_total else (1.0 if not predicted_rows else 0.0),
        "numeric_exactness": round(numeric_exact / numeric_total, 6) if numeric_total else (1.0 if hallucinated_numeric == 0 else 0.0),
        "hallucinated_numeric_count": hallucinated_numeric,
        "details": details,
    }


def _conflict_metrics(predicted: Mapping[str, Any], gold: Mapping[str, Any]) -> dict[str, Any]:
    predicted_conflicts = [row for row in predicted.get("conflicts", []) if isinstance(row, Mapping)]
    gold_conflicts = [row for row in gold.get("conflicts", []) if isinstance(row, Mapping)]
    predicted_negative = [row for row in predicted.get("negative_cases", []) if isinstance(row, Mapping)]
    false_positive = sum(1 for row in predicted_negative if row.get("created_object") is True)
    return {"conflict_count_predicted": len(predicted_conflicts), "conflict_count_gold": len(gold_conflicts), "conflict_type_counts": dict(sorted(Counter(str(row.get("conflict_type")) for row in predicted_conflicts).items())), "negative_false_positives": false_positive, "negative_case_count": len(predicted_negative)}


def repeat_build_determinism(digests: Iterable[str]) -> dict[str, Any]:
    values = [str(value) for value in digests]
    return {"checked": len(values), "deterministic": bool(values) and len(set(values)) == 1, "digests": values}


def build_evaluation_contract(*, predicted: Mapping[str, Any] | Sequence[Mapping[str, Any]], gold: Mapping[str, Any] | None = None, source_kind: str = "synthetic-fixture", independently_reviewed: bool = False, repeat_digests: Iterable[str] = (), iou_threshold: float = 0.8, reason: str | None = None) -> dict[str, Any]:
    """Return an evaluation contract without overstating evidence.

    ``independently_reviewed`` is accepted only to make the boundary explicit;
    the compiler cannot author or verify reviewer independence, so this API
    rejects a claim of verified Gold.
    """

    if independently_reviewed:
        raise VisualEvaluationError("compiler_cannot_author_independent_gold")
    predicted_bundle = predicted if isinstance(predicted, Mapping) else {"objects": list(predicted)}
    if gold is None:
        return {
            "schema_version": VISUAL_EVALUATION_CONTRACT_SCHEMA,
            "contract_kind": "candidate-benchmark",
            "source_kind": source_kind,
            "status": "not-run",
            "verified_gold": False,
            "synthetic_contract_truth": False,
            "independent_real_gold": False,
            "reason": reason or "independent_gold_not_supplied",
            "metrics": {},
            "recommended_targets": {"object_f1": 0.90, "bbox_iou_ge_0_80": 0.90, "same_page_table_structure": 0.90},
            "candidate_only": True,
        }
    if not isinstance(gold, Mapping):
        raise VisualEvaluationError("gold_contract_invalid")
    predicted_objects = _objects(predicted_bundle)
    gold_objects = _objects(gold)
    metrics = {
        "objects": _object_metrics(predicted_objects, gold_objects, float(iou_threshold)),
        "tables": _table_metrics(_grid_rows(predicted_bundle), _grid_rows(gold)),
        "charts": _chart_metrics(predicted_bundle, gold),
        "numeric_unit_symbol": _numeric_unit_metrics(predicted_bundle, gold),
        "conflicts_and_negative_cases": _conflict_metrics(predicted_bundle, gold),
        "repeat_build": repeat_build_determinism(repeat_digests),
    }
    return {
        "schema_version": VISUAL_EVALUATION_CONTRACT_SCHEMA,
        "contract_kind": "synthetic-gold" if source_kind.startswith("synthetic") else "candidate-benchmark",
        "source_kind": source_kind,
        "status": "measured",
        "verified_gold": False,
        "synthetic_contract_truth": source_kind.startswith("synthetic"),
        "independent_real_gold": False,
        "reason": "synthetic_gold_is_deterministic_but_not_independent_semantic_review" if source_kind.startswith("synthetic") else "independent_gold_attestation_required",
        "metrics": metrics,
        "recommended_targets": {"object_f1": 0.90, "bbox_iou_ge_0_80": 0.90, "same_page_table_structure": 0.90},
        "candidate_only": True,
        "evaluation_sha256": sha256_json({"source_kind": source_kind, "metrics": metrics, "verified_gold": False}),
    }


def evaluate_visual_candidates(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return build_evaluation_contract(*args, **kwargs)


def evaluate_visual_bundle(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return build_evaluation_contract(*args, **kwargs)


__all__ = [
    "VisualEvaluationError",
    "bbox_iou",
    "repeat_build_determinism",
    "build_evaluation_contract",
    "evaluate_visual_candidates",
    "evaluate_visual_bundle",
]
