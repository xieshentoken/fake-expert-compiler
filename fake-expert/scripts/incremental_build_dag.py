#!/usr/bin/env python3
"""Content-addressed, fail-closed incremental build DAG.

The Phase 5 differ remains the compatibility report for package updates.  This
module is the Phase 7B layer around it: it normalizes immutable snapshots into a
typed content-addressed graph, compares graph identities, computes a complete
downstream invalidation closure, and records a pause-only resumable plan.  It
never interprets source text, creates review conclusions, seals an artifact, or
enables execution.

The implementation intentionally uses only the standard library.  Snapshot
inputs may be an explicit ``{"nodes": ..., "edges": ...}`` JSON fixture, a
previous DAG directory, a Phase 6C book-plan directory, a workpack directory,
or a source-free Expert Skill package directory.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import re
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable, Mapping

from compiler_version import (
    INCREMENTAL_BUILD_DAG_COMPILER_VERSION,
    INCREMENTAL_BUILD_DAG_PROTOCOL,
    INCREMENTAL_CHANGE_SET_SCHEMA,
    INCREMENTAL_BUILD_PLAN_SCHEMA,
    INCREMENTAL_BUILD_RECEIPT_SCHEMA,
    INCREMENTAL_BUILD_STATE_SCHEMA,
    INCREMENTAL_DAG_EDGE_SCHEMA,
    INCREMENTAL_DAG_MANIFEST_SCHEMA,
    INCREMENTAL_DAG_NODE_SCHEMA,
    VISUAL_PARSER_MANIFEST_SCHEMA,
    VISUAL_CENSUS_SCHEMA,
    VISUAL_ROUTING_SCHEMA,
    VISUAL_BUDGET_RECEIPT_SCHEMA,
    VISUAL_INDEX_SCHEMA,
    VISUAL_COVERAGE_SCHEMA,
    SELECTIVE_VISUAL_PROTOCOL,
)
from expert_skill_contract import load_json, load_jsonl, sha256_file
from paddle_runtime_qualification import (
    PaddleRuntimeQualificationError,
    validate_runtime_qualification,
)


DEFAULT_TIMESTAMP = "1970-01-01T00:00:00+00:00"
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
NODE_ID_RE = re.compile(r"^node-[0-9a-f]{32}$")

NODE_KINDS = {
    "source",
    "page",
    "segment",
    "module",
    "render",
    "parser",
    "model",
    "config",
    "locator",
    "visual_object",
    "visual_relation",
    "table_grid",
    "semantic_assertion",
    "support",
    "review_plan",
    "review_session",
    "review_attestation",
    "composer",
    "alignment_candidate",
    "semantic_difference",
    "composer_conflict",
    "unit_map",
    "composer_review_plan",
    "composer_review",
    "composer_receipt",
    "formula_ast",
    "formula_test",
    "test",
    "runtime",
    "runtime_qualification",
    "runtime_result",
    "worker",
    "raster_crop",
    "ocr_observation",
    "ocr_transcript",
    "scan_structure",
    "scan_review",
    "scan_transform",
    "scan_backend_receipt",
    "visual_candidate",
    "visual_gap",
    "visual_conflict",
    "table_cell",
    "review_overlay",
    "visual_policy",
    "visual_census",
    "visual_routing",
    "visual_budget",
    "visual_index",
    "visual_coverage",
    "visual_chart_candidate",
    # Phase 7D.5B benchmark nodes.  They use the same content-addressed
    # node/edge protocol as the existing visual/runtime graph so Gold, mode
    # outputs, evaluations, and private cost ledgers invalidate the smallest
    # downstream closure.
    "visual_benchmark",
    "visual_gold",
    "visual_mode_result",
    "visual_evaluation",
    "cost_ledger",
    "competency",
    "seal",
    "host_certificate",
    "composite",
    "package",
    "artifact",
    "receipt",
    "unknown",
}

EDGE_TYPES = {
    "contains",
    "extracts",
    "renders",
    "parses",
    "configures",
    "locates",
    "supports",
    "reviews",
    "attests",
    "uses",
    "feeds",
    "tests",
    "evaluates",
    "seals",
    "certifies",
    "composes",
    "depends_on",
    "projects",
    "invalidates",
    "inspects",
    "routes",
    "budgets",
    "indexes",
    "candidates",
}

ACTION_ORDER = (
    "re-extract",
    "rebuild",
    "re-review",
    "retest",
    "repackage",
    "reseal",
    "recertify",
    "recompose",
)

PAUSE_ACTIONS = {
    "re-review",
    "retest",
    "repackage",
    "reseal",
    "recertify",
    "recompose",
}
PAUSE_GATES = {
    "structure-resolution",
    "ocr-candidate",
    "visual-review",
    "independent-visual-review",
    "independent-semantic-review",
    "review-plan-freeze",
    "independent-review",
    "formula-review-and-execution-gates",
    "independent-formula-test",
    "competency",
    "seal",
    "host-certification",
    "composer-boundary",
    "execution",
    "signing",
}
DETERMINISTIC_ACTIONS = {"re-extract", "rebuild"}

KIND_ACTIONS: dict[str, tuple[str, ...]] = {
    "source": ("re-extract", "rebuild"),
    "page": ("re-extract", "rebuild"),
    "segment": ("re-extract", "rebuild"),
    "parser": ("re-extract", "rebuild"),
    "model": ("re-extract", "rebuild", "re-review", "recompose", "repackage"),
    "config": ("re-extract", "rebuild", "re-review", "recompose", "repackage"),
    "render": ("re-extract", "rebuild", "re-review", "recompose", "repackage"),
    "locator": ("re-extract", "rebuild", "re-review"),
    "visual_object": ("re-review", "rebuild"),
    "visual_relation": ("re-review", "rebuild"),
    "table_grid": ("re-review", "rebuild"),
    "semantic_assertion": ("re-review", "rebuild"),
    "support": ("re-review", "rebuild"),
    "review_plan": ("re-review", "rebuild"),
    "review_session": ("re-review", "rebuild"),
    "review_attestation": ("re-review", "rebuild"),
    "composer": ("recompose", "rebuild"),
    "alignment_candidate": ("recompose", "re-align", "re-review"),
    "semantic_difference": ("recompose", "re-review"),
    "composer_conflict": ("recompose", "re-review"),
    "unit_map": ("recompose", "re-review"),
    "composer_review_plan": ("re-review", "recompose"),
    "composer_review": ("re-review", "recompose"),
    "composer_receipt": ("rebuild", "recompose"),
    "formula_ast": ("re-review", "rebuild", "retest"),
    "formula_test": ("retest", "rebuild"),
    "test": ("retest", "rebuild"),
    "competency": ("retest", "rebuild"),
    "runtime": ("re-extract", "rebuild", "re-review", "recompose", "repackage"),
    "runtime_qualification": ("re-extract", "rebuild", "re-review", "recompose", "repackage"),
    "runtime_result": ("re-extract", "rebuild", "re-review", "recompose", "repackage"),
    "worker": ("re-extract", "rebuild", "re-review", "recompose", "repackage"),
    "raster_crop": ("re-extract", "rebuild", "re-review", "recompose", "repackage"),
    "ocr_observation": ("re-extract", "rebuild", "re-review", "recompose", "repackage"),
    "ocr_transcript": ("re-extract", "rebuild", "re-review", "recompose", "repackage"),
    "scan_structure": ("re-extract", "rebuild", "re-review", "recompose", "repackage"),
    "scan_review": ("re-review", "rebuild", "recompose", "repackage"),
    "scan_transform": ("re-extract", "rebuild", "re-review", "recompose", "repackage"),
    "scan_backend_receipt": ("re-extract", "rebuild", "re-review", "recompose", "repackage"),
    "visual_candidate": ("re-review", "rebuild", "recompose", "repackage"),
    "visual_gap": ("re-review", "rebuild", "recompose", "repackage"),
    "visual_conflict": ("re-review", "rebuild", "recompose", "repackage"),
    "table_cell": ("re-review", "rebuild"),
    "review_overlay": ("re-review", "rebuild", "repackage"),
    "visual_policy": ("rebuild", "re-review", "recompose", "repackage"),
    "visual_census": ("re-extract", "rebuild", "re-review", "recompose", "repackage"),
    "visual_routing": ("re-extract", "rebuild", "re-review", "recompose", "repackage"),
    "visual_budget": ("re-extract", "rebuild", "re-review", "recompose", "repackage"),
    "visual_index": ("rebuild", "re-review", "recompose", "repackage"),
    "visual_coverage": ("rebuild", "re-review", "recompose", "repackage"),
    "visual_chart_candidate": ("re-review", "rebuild", "recompose", "repackage"),
    "visual_benchmark": ("rebuild", "re-review", "repackage"),
    "visual_gold": ("re-review", "rebuild"),
    "visual_mode_result": ("re-extract", "rebuild", "re-review", "repackage"),
    "visual_evaluation": ("rebuild", "re-review"),
    "cost_ledger": ("rebuild",),
    "seal": ("reseal",),
    "host_certificate": ("recertify",),
    "composite": ("recompose",),
    "module": ("rebuild", "repackage"),
    "package": ("repackage",),
    "artifact": ("rebuild",),
    "receipt": ("rebuild",),
    "unknown": ("rebuild",),
}

KIND_GATES: dict[str, str] = {
    "source": "source-identity",
    "page": "structure-resolution",
    "segment": "structure-resolution",
    "parser": "parser-contract",
    "model": "model-contract",
    "config": "configuration-contract",
    "render": "visual-review",
    "locator": "structure-resolution",
    "visual_object": "independent-visual-review",
    "visual_relation": "independent-visual-review",
    "table_grid": "independent-visual-review",
    "semantic_assertion": "independent-semantic-review",
    "support": "independent-semantic-review",
    "review_plan": "review-plan-freeze",
    "review_session": "independent-review",
    "review_attestation": "independent-review",
    "composer": "composer-boundary",
    "alignment_candidate": "independent-semantic-review",
    "semantic_difference": "independent-semantic-review",
    "composer_conflict": "independent-review",
    "unit_map": "independent-review",
    "composer_review_plan": "review-plan-freeze",
    "composer_review": "independent-review",
    "composer_receipt": "composer-boundary",
    "formula_ast": "formula-review-and-execution-gates",
    "formula_test": "independent-formula-test",
    "test": "competency",
    "competency": "competency",
    "runtime": "runtime-rebuild",
    "runtime_qualification": "runtime-qualification",
    "runtime_result": "runtime-rebuild",
    "worker": "runtime-rebuild",
    "raster_crop": "visual-review",
    "ocr_observation": "ocr-candidate",
    "ocr_transcript": "ocr-candidate",
    "scan_structure": "structure-resolution",
    "scan_review": "independent-review",
    "scan_transform": "visual-review",
    "scan_backend_receipt": "receipt-validation",
    "visual_candidate": "independent-visual-review",
    "visual_gap": "independent-visual-review",
    "visual_conflict": "independent-visual-review",
    "table_cell": "independent-visual-review",
    "review_overlay": "independent-visual-review",
    "visual_policy": "visual-policy-selection",
    "visual_census": "visual-census-routing",
    "visual_routing": "visual-census-routing",
    "visual_budget": "visual-budget",
    "visual_index": "independent-visual-review",
    "visual_coverage": "visual-budget",
    "visual_chart_candidate": "independent-visual-review",
    "visual_benchmark": "benchmark-orchestration",
    "visual_gold": "independent-review",
    "visual_mode_result": "runtime-rebuild",
    "visual_evaluation": "independent-review",
    "cost_ledger": "runtime-rebuild",
    "seal": "seal",
    "host_certificate": "host-certification",
    "composite": "composer-boundary",
    "module": "module-rebuild",
    "package": "package-integrity",
    "artifact": "artifact-rebuild",
    "receipt": "receipt-validation",
    "unknown": "unknown-node",
}


class IncrementalDagError(RuntimeError):
    """Stable fail-closed DAG error."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _timestamp(value: str | None) -> str:
    result = value or DEFAULT_TIMESTAMP
    try:
        parsed = dt.datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as error:
        raise IncrementalDagError("generated_at_invalid") from error
    if parsed.tzinfo is None:
        raise IncrementalDagError("generated_at_invalid")
    return result


def _hash_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and HASH_RE.fullmatch(value) else None


def _clean_for_hash(value: Any) -> Any:
    """Drop transport metadata and paths, retaining semantic/evidence fields."""

    if isinstance(value, dict):
        dropped = {
            "node_id",
            "content_hash",
            "declared_content_hash",
            "record_hash",
            "row_hash",
            "node_hash",
            "path",
            "absolute_path",
            "output_path",
            "generated_at",
            "created_at",
        }
        return {
            key: _clean_for_hash(item)
            for key, item in value.items()
            if key not in dropped
        }
    if isinstance(value, list):
        return [_clean_for_hash(item) for item in value]
    return value


def _identity_projection(record: dict[str, Any]) -> dict[str, Any]:
    explicit = record.get("identity")
    if isinstance(explicit, dict):
        return _clean_for_hash(explicit)
    fields = {
        key: record.get(key)
        for key in (
            "id",
            "stable_id",
            "source_id",
            "source_sha256",
            "physical_page",
            "page",
            "page_number",
            "segment_id",
            "module_id",
            "object_id",
            "assertion_id",
            "support_id",
            "review_plan_id",
            "review_session_id",
            "formula_id",
            "test_id",
            "artifact_path",
            "relative_path",
            "record_id",
            "route_id",
            "page_record_id",
            "segment_record_id",
            "kind",
            "type",
        )
        if record.get(key) is not None
    }
    return _clean_for_hash(fields)


def _evidence_projection(record: dict[str, Any]) -> dict[str, Any]:
    explicit = record.get("evidence_identity")
    if isinstance(explicit, dict):
        return _clean_for_hash(explicit)
    keys = (
        "source_id",
        "source_sha256",
        "source_hash",
        "artifact_hash",
        "physical_page",
        "page",
        "page_number",
        "printed_page_label",
        "segment_id",
        "locator",
        "evidence_ids",
        "evidence_hashes",
        "render_sha256",
        "render_hash",
        "crop_sha256",
        "bbox",
        "geometry",
        "relation_ids",
        "grid_ids",
        "support_ids",
        "relation_type",
        "relation_ids",
        "endpoint_ids",
        "table_cell_id",
        "series_id",
        "grid_id",
        "input_hashes",
    )
    return _clean_for_hash({key: record[key] for key in keys if key in record})


def _stable_key(kind: str, record: dict[str, Any], ordinal: int | None = None) -> str:
    for key in (
        "stable_key",
        "stable_id",
        "node_id",
        "id",
        "source_id",
        "page_id",
        "segment_id",
        "module_id",
        "object_id",
        "assertion_id",
        "support_id",
        "review_plan_id",
        "review_session_id",
        "formula_id",
        "test_id",
        "record_id",
        "route_id",
        "page_record_id",
        "segment_record_id",
        "artifact_path",
        "relative_path",
        "record_id",
        "route_id",
        "page_record_id",
        "segment_record_id",
    ):
        value = record.get(key)
        if isinstance(value, (str, int)) and str(value):
            return f"{kind}:{value}"
    # If a source gives no explicit stable identity, a content-addressed identity
    # is safer than an array position.  It becomes add/remove on edits rather than
    # silently reusing a different semantic object.
    return f"{kind}:anonymous:{sha256_json(_identity_projection(record))}"


def _node_id(kind: str, stable_key: str) -> str:
    return f"node-{sha256_json([kind, stable_key])[:32]}"


def _protocol_id(record: dict[str, Any]) -> str:
    for key in ("protocol_id", "protocol", "schema_version", "schema", "compiler_version"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return INCREMENTAL_BUILD_DAG_PROTOCOL


def _kind_from_record(record: dict[str, Any], default: str = "unknown") -> str:
    raw = record.get("kind", record.get("node_kind", record.get("type", default)))
    if not isinstance(raw, str):
        return default
    value = raw.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "source_pdf": "source",
        "physical_page": "page",
        "book_page": "page",
        "semantic_segment": "segment",
        "visual": "visual_object",
        "visualobject": "visual_object",
        "visualrelation": "visual_relation",
        "tablegrid": "table_grid",
        "semantic_assertion_row": "semantic_assertion",
        "support_matrix": "support",
        "review": "review_attestation",
        "review_attestation_row": "review_attestation",
        "alignment": "alignment_candidate",
        "alignment_candidate_row": "alignment_candidate",
        "difference": "semantic_difference",
        "conflict": "composer_conflict",
        "unit": "unit_map",
        "composer_workpack": "composer",
        "composer_review_plan": "composer_review_plan",
        "composer_attestation": "composer_review",
        "formula": "formula_ast",
        "evaluation": "test",
        "runtime_catalog": "runtime",
        "certificate": "host_certificate",
        "composite_skill": "composite",
        "scan_structure_review": "scan_structure",
        "structure_review": "scan_structure",
        "scan_review_receipt": "scan_review",
    }
    value = aliases.get(value, value)
    return value if value in NODE_KINDS else default


def _node_from_record(
    kind: str,
    record: dict[str, Any],
    *,
    ordinal: int | None = None,
    artifact_path: str | None = None,
) -> dict[str, Any]:
    source = dict(record)
    if source.get("schema_version") == INCREMENTAL_DAG_NODE_SCHEMA:
        # A serialized DAG node already contains the source payload hash.  Do
        # not hash the normalized transport row a second time (that would make
        # every reload look tampered); the manifest protects the row bytes and
        # the content hash protects the original snapshot payload.
        required = ("node_id", "kind", "stable_key", "content_hash", "protocol_id")
        if any(not isinstance(source.get(key), str) or not source.get(key) for key in required):
            raise IncrementalDagError("dag_node_invalid")
        if source.get("kind") not in NODE_KINDS or source.get("node_id") != _node_id(source["kind"], source["stable_key"]):
            raise IncrementalDagError("dag_node_identity_invalid")
        for key in ("content_hash", "semantic_identity_hash", "evidence_identity_hash"):
            if not HASH_RE.fullmatch(str(source.get(key, ""))):
                raise IncrementalDagError(f"dag_node_{key}_invalid")
        return {
            "schema_version": INCREMENTAL_DAG_NODE_SCHEMA,
            "node_id": source["node_id"],
            "kind": source["kind"],
            "stable_key": source["stable_key"],
            "protocol_id": source["protocol_id"],
            "content_hash": source["content_hash"],
            "semantic_identity_hash": source["semantic_identity_hash"],
            "evidence_identity_hash": source["evidence_identity_hash"],
            "input_hashes": sorted(str(value) for value in source.get("input_hashes", []) if isinstance(value, (str, int))),
            "receipt_ids": sorted(str(value) for value in source.get("receipt_ids", []) if isinstance(value, (str, int))),
            "required_receipt_ids": sorted(str(value) for value in source.get("required_receipt_ids", []) if isinstance(value, (str, int))),
            "reviewer_requirement": source.get("reviewer_requirement") if isinstance(source.get("reviewer_requirement"), int) else None,
            "capabilities": copy.deepcopy(source.get("capabilities", {})) if isinstance(source.get("capabilities", {}), dict) else {},
            "attributes": copy.deepcopy(source.get("attributes", {})) if isinstance(source.get("attributes", {}), dict) else {},
        }
    if artifact_path and not source.get("artifact_path"):
        source["artifact_path"] = artifact_path
    stable_key = _stable_key(kind, source, ordinal)
    node_id = _node_id(kind, stable_key)
    identity = _identity_projection(source)
    evidence_identity = _evidence_projection(source)
    content_hash = sha256_json(_clean_for_hash(source))
    declared = source.get("content_hash", source.get("declared_content_hash"))
    if declared is not None and declared != content_hash:
        raise IncrementalDagError(f"tampered_content_hash:{stable_key}")
    input_values = source.get("input_hashes", source.get("inputs", []))
    if isinstance(input_values, dict):
        input_values = list(input_values.values())
    if isinstance(input_values, str):
        input_values = [input_values]
    if not isinstance(input_values, list):
        input_values = []
    input_hashes = sorted(str(value) for value in input_values if isinstance(value, (str, int)))
    receipt_values = source.get("receipt_ids", source.get("receipts", []))
    if isinstance(receipt_values, str):
        receipt_values = [receipt_values]
    if not isinstance(receipt_values, list):
        receipt_values = []
    required_receipts = source.get("required_receipt_ids", source.get("required_receipts", []))
    if isinstance(required_receipts, str):
        required_receipts = [required_receipts]
    if not isinstance(required_receipts, list):
        required_receipts = []
    reviewer_requirement = source.get(
        "reviewer_requirement",
        source.get("required_reviewer_count", source.get("required_reviewers")),
    )
    if isinstance(reviewer_requirement, list):
        reviewer_requirement = len(reviewer_requirement)
    if not isinstance(reviewer_requirement, int):
        reviewer_requirement = None
    semantic_fields = {
        key: source.get(key)
        for key in (
            "type",
            "title",
            "statement",
            "normalized_statement",
            "claim",
            "assertion",
            "formula",
            "expression",
            "normalized_expression",
            "relation_type",
            "endpoints",
            "values",
        )
        if key in source
    }
    semantic_identity_hash = _hash_or_none(source.get("semantic_identity_hash")) or sha256_json(
        {"stable_identity": identity, "semantic": _clean_for_hash(semantic_fields)}
    )
    evidence_identity_hash = _hash_or_none(source.get("evidence_identity_hash")) or sha256_json(evidence_identity)
    capabilities = source.get("capabilities")
    if not isinstance(capabilities, dict):
        capabilities = {}
    attributes: dict[str, Any] = {}
    for key in (
        "change_kind",
        "change_tag",
        "change_tags",
        "source_sha256",
        "source_hash",
        "render_sha256",
        "render_hash",
        "parser_version",
        "model_id",
        "backend_id",
        "config_hash",
        "locator_hash",
        "page_text_hash",
        "text_hash",
        "relation_hash",
        "grid_hash",
        "assertion_hash",
        "support_hash",
        "formula_hash",
        "test_hash",
        "qualification_id",
        "receipt_qualification_id",
        "receipt_sha256",
        "runtime_contract_sha256",
        "runtime_inventory_sha256",
        "model_identity_sha256",
        "configuration_sha256",
        "worker_sha256",
        "profile_sha256",
        "artifact_path",
        "file_path",
        "sealed",
        "status",
    ):
        if key in source:
            attributes[key] = copy.deepcopy(source[key])
    return {
        "schema_version": INCREMENTAL_DAG_NODE_SCHEMA,
        "node_id": node_id,
        "kind": kind,
        "stable_key": stable_key,
        "protocol_id": _protocol_id(source),
        "content_hash": content_hash,
        "semantic_identity_hash": semantic_identity_hash,
        "evidence_identity_hash": evidence_identity_hash,
        "input_hashes": input_hashes,
        "receipt_ids": sorted(str(value) for value in receipt_values if isinstance(value, (str, int))),
        "required_receipt_ids": sorted(str(value) for value in required_receipts if isinstance(value, (str, int))),
        "reviewer_requirement": reviewer_requirement,
        "capabilities": copy.deepcopy(capabilities),
        "attributes": attributes,
    }


def _edge_type(value: Any) -> str:
    if not isinstance(value, str):
        return "depends_on"
    value = value.strip().lower().replace("-", "_")
    return value if value in EDGE_TYPES else "depends_on"


def _edge_id(edge_type: str, source_id: str, target_id: str) -> str:
    return f"edge-{sha256_json([edge_type, source_id, target_id])[:32]}"


def _edge_from_record(
    edge: dict[str, Any], aliases: dict[str, str], nodes: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    source = edge.get("from", edge.get("source", edge.get("from_node_id", edge.get("source_id"))))
    target = edge.get("to", edge.get("target", edge.get("to_node_id", edge.get("target_id"))))
    if not isinstance(source, str) or not isinstance(target, str):
        raise IncrementalDagError("edge_endpoint_invalid")
    source_id = aliases.get(source, source)
    target_id = aliases.get(target, target)
    if source_id not in nodes or target_id not in nodes:
        raise IncrementalDagError(f"dangling_edge:{source}->{target}")
    edge_type = _edge_type(edge.get("type", edge.get("edge_type")))
    output = {
        "schema_version": INCREMENTAL_DAG_EDGE_SCHEMA,
        "edge_id": _edge_id(edge_type, source_id, target_id),
        "type": edge_type,
        "from": source_id,
        "to": target_id,
        "required": bool(edge.get("required", True)),
    }
    declared = edge.get("edge_id")
    if declared is not None and declared != output["edge_id"]:
        raise IncrementalDagError("tampered_edge_id")
    return output


def _add_auto_edges(
    records: list[tuple[str, dict[str, Any], dict[str, Any]]],
    aliases: dict[str, str],
    nodes: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for kind, source, node in records:
        target = node["node_id"]
        refs: list[tuple[str, str]] = []
        for key in (
            "input_ids",
            "dependency_ids",
            "depends_on",
            "input_node_ids",
            "parent_id",
            "page_ids",
            "segment_ids",
            "scope_ids",
            "assertion_ids",
            "support_ids",
            "evidence_ids",
            "visual_task_ids",
            "visual_ids",
            "relation_ids",
            "grid_ids",
            "formula_ids",
            "test_ids",
            "review_ids",
        ):
            values = source.get(key, [])
            if isinstance(values, str):
                values = [values]
            if isinstance(values, list):
                refs.extend((str(value), "depends_on") for value in values if isinstance(value, (str, int)))
        for key, edge_type in (
            ("source_node_id", "extracts"),
            ("source_id", "extracts"),
            ("parser_id", "parses"),
            ("config_id", "configures"),
            ("model_id", "uses"),
            ("page_id", "depends_on"),
            ("render_id", "renders"),
            ("source_visual_object_id", "depends_on"),
            ("target_visual_object_id", "depends_on"),
            ("table_visual_object_id", "depends_on"),
            ("locator_id", "locates"),
            ("support_id", "supports"),
            ("assertion_id", "supports"),
            ("review_plan_id", "reviews"),
            ("review_session_id", "reviews"),
            ("formula_id", "tests"),
            ("runtime_id", "evaluates"),
            ("seal_id", "seals"),
            ("certificate_id", "certifies"),
        ):
            if isinstance(source.get(key), (str, int)):
                refs.append((str(source[key]), edge_type))
        for key, edge_type in (("plan_id", "projects"), ("parent_plan_id", "contains"), ("package_id", "projects")):
            parent = source.get(key)
            if isinstance(parent, (str, int)):
                parent_id = aliases.get(str(parent), str(parent))
                if parent_id in nodes and parent_id != target:
                    edges.append(
                        {
                            "schema_version": INCREMENTAL_DAG_EDGE_SCHEMA,
                            "edge_id": _edge_id(edge_type, target, parent_id),
                            "type": edge_type,
                            "from": target,
                            "to": parent_id,
                            "required": True,
                        }
                    )
                elif parent_id != target:
                    raise IncrementalDagError(f"dangling_reference:{parent}")
        for key, edge_type in (("module_id", "depends_on"), ("route_ids", "projects"), ("module_ids", "depends_on")):
            values = source.get(key, [])
            if isinstance(values, str):
                values = [values]
            if isinstance(values, list):
                for value in values:
                    if not isinstance(value, (str, int)):
                        continue
                    downstream_id = aliases.get(str(value), str(value))
                    if downstream_id in nodes and downstream_id != target:
                        edges.append(
                            {
                                "schema_version": INCREMENTAL_DAG_EDGE_SCHEMA,
                                "edge_id": _edge_id(edge_type, target, downstream_id),
                                "type": edge_type,
                                "from": target,
                                "to": downstream_id,
                                "required": True,
                            }
                        )
                    elif downstream_id != target:
                        raise IncrementalDagError(f"dangling_reference:{value}")
        for value in source.get("evidence_ids", []) if isinstance(source.get("evidence_ids", []), list) else []:
            if isinstance(value, str):
                refs.append((value, "supports"))
        # Review and evaluation products often use plural protocol fields
        # rather than the generic input_ids field.  Keep the prerequisite
        # direction explicit and deterministic: evidence/assertions/supports /
        # visual objects -> review, formula AST -> test, and tests -> runtime.
        if kind in {"review_plan", "review_session", "review_attestation"}:
            for key in ("assertion_ids", "support_ids", "visual_task_ids", "visual_ids", "relation_ids", "grid_ids"):
                values = source.get(key, [])
                if isinstance(values, str):
                    values = [values]
                if isinstance(values, list):
                    refs.extend((str(value), "reviews") for value in values if isinstance(value, (str, int)))
        if kind in {"formula_test", "test", "competency"}:
            for key in ("formula_ids", "formula_id", "ast_ids", "ast_id"):
                values = source.get(key, [])
                if isinstance(values, str):
                    values = [values]
                if isinstance(values, list):
                    refs.extend((str(value), "tests") for value in values if isinstance(value, (str, int)))
        if kind in {"runtime", "seal", "host_certificate", "composite"}:
            for key in ("formula_ids", "test_ids", "runtime_ids", "seal_ids", "certificate_ids"):
                values = source.get(key, [])
                if isinstance(values, str):
                    values = [values]
                if isinstance(values, list):
                    refs.extend((str(value), "evaluates") for value in values if isinstance(value, (str, int)))
        for ref, edge_type in refs:
            ref_id = aliases.get(ref, ref)
            if ref_id in nodes and ref_id != target:
                edges.append(
                    {
                        "schema_version": INCREMENTAL_DAG_EDGE_SCHEMA,
                        "edge_id": _edge_id(edge_type, ref_id, target),
                        "type": edge_type,
                        "from": ref_id,
                        "to": target,
                        "required": True,
                    }
                )
            elif ref_id != target:
                raise IncrementalDagError(f"dangling_reference:{ref}")
    return edges


def _validate_graph(nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    adjacency: dict[str, list[str]] = defaultdict(list)
    indegree: dict[str, int] = {node_id: 0 for node_id in nodes}
    for edge in edges:
        edge_id = edge.get("edge_id")
        source = edge.get("from")
        target = edge.get("to")
        if not isinstance(edge_id, str) or edge_id in seen:
            raise IncrementalDagError("duplicate_edge")
        seen.add(edge_id)
        if source not in nodes or target not in nodes:
            raise IncrementalDagError("dangling_edge")
        if edge_id != _edge_id(str(edge.get("type")), str(source), str(target)):
            raise IncrementalDagError("edge_hash_mismatch")
        adjacency[str(source)].append(str(target))
        indegree[str(target)] += 1
    queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for target in sorted(adjacency.get(current, [])):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if visited != len(nodes):
        raise IncrementalDagError("cyclic_edge")


def _explicit_snapshot(value: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw_nodes = value.get("nodes", [])
    raw_edges = value.get("edges", [])
    if isinstance(raw_nodes, dict):
        raw_nodes = list(raw_nodes.values())
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise IncrementalDagError("snapshot_nodes_edges_invalid")
    nodes: dict[str, dict[str, Any]] = {}
    aliases: dict[str, str] = {}
    records: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for ordinal, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            raise IncrementalDagError("snapshot_node_invalid")
        kind = _kind_from_record(raw)
        node = _node_from_record(kind, raw, ordinal=ordinal)
        if node["node_id"] in nodes:
            raise IncrementalDagError("duplicate_node_identity")
        nodes[node["node_id"]] = node
        records.append((kind, raw, node))
        identity_keys = ["node_id", "id", "stable_id", "stable_key"]
        kind_identity_key = {
            "source": "source_id",
            "page": "page_id",
            "segment": "segment_id",
            "module": "module_id",
            "visual_object": "object_id",
            "visual_relation": "relation_id",
            "table_grid": "grid_id",
            "runtime_result": "result_id",
            "ocr_transcript": "transcript_id",
            "scan_structure": "structure_review_id",
            "scan_review": "review_id",
            "visual_candidate": "candidate_record_id",
            "visual_gap": "gap_id",
            "visual_conflict": "conflict_id",
            "table_cell": "cell_id",
        "review_overlay": "overlay_id",
            "visual_benchmark": "benchmark_id",
            "visual_gold": "gold_id",
            "visual_mode_result": "mode_result_id",
            "visual_evaluation": "evaluation_id",
            "cost_ledger": "ledger_id",
            "semantic_assertion": "assertion_id",
            "support": "support_id",
            "review_plan": "review_plan_id",
            "review_session": "review_session_id",
            "review_attestation": "attestation_id",
            "formula_ast": "formula_id",
            "formula_test": "test_id",
            "test": "test_id",
            "competency": "test_id",
            # ``artifact_path`` is a file/container locator, not a logical
            # identity.  A JSONL artifact commonly contains many typed rows
            # with the same path; using it as the artifact alias would make a
            # valid content-addressed composer snapshot fail with an
            # alias_conflict.  Records still bind through stable_id/id (or the
            # content-addressed fallback in _stable_key).
            "render": "render_id",
            "locator": "locator_id",
            "receipt": "receipt_id",
            "host_certificate": "certificate_id",
        }
        identity_keys.append(kind_identity_key.get(kind, "record_id"))
        if kind in {"page", "segment"}:
            identity_keys.append("record_id")
        if kind == "segment":
            identity_keys.append("unit_id")
        if kind == "support":
            identity_keys.extend(("evidence_id", "anchor_id"))
        elif kind in {"review_plan", "review_session", "review_attestation"}:
            identity_keys.append("route_id")
        for key in identity_keys:
            alias = raw.get(key)
            if isinstance(alias, (str, int)):
                alias_text = str(alias)
                previous = aliases.get(alias_text)
                if previous is not None and previous != node["node_id"]:
                    raise IncrementalDagError(f"alias_conflict:{alias_text}")
                aliases[alias_text] = node["node_id"]
        # The normalized stable_key is the canonical identity.  Register it
        # explicitly because transport rows carry a kind-prefixed stable_key
        # (for example ``test:file:path``) while the canonical key is
        # ``file:path``; without this alias a reload cannot rebind source
        # receipt IDs to their file node.
        stable_alias = node["stable_key"]
        previous = aliases.get(stable_alias)
        if previous is not None and previous != node["node_id"]:
            raise IncrementalDagError(f"alias_conflict:{stable_alias}")
        aliases[stable_alias] = node["node_id"]
        # Normalized DAG rows keep source locators in bounded attributes, but
        # artifact_path is a transport/provenance field and is commonly shared
        # by many rows (for example a workpack JSONL). Do not promote it to an
        # identity alias; only explicit identities and file:<path> aliases
        # below may resolve references.
        attributes = raw.get("attributes")
        file_path = None
        if isinstance(attributes, dict) and isinstance(attributes.get("file_path"), str):
            file_path = attributes["file_path"]
        elif isinstance(raw.get("file_path"), str):
            file_path = raw["file_path"]
        if file_path is not None:
            alias_text = f"file:{file_path}"
            previous = aliases.get(alias_text)
            if previous is not None and previous != node["node_id"]:
                raise IncrementalDagError(f"alias_conflict:{alias_text}")
            aliases[alias_text] = node["node_id"]
    # Source-package receipt_ids name source evidence files, not Phase 7B
    # runtime receipts. Bind those names to the corresponding file node
    # after all nodes have registered their stable file:<path> aliases. This
    # preserves the binding across normalized DAG serialization while keeping
    # the namespaces distinct and fail-closed for missing source receipts.
    for _kind, _raw, node in records:
        for receipt_id in node.get("receipt_ids", []):
            receipt_text = str(receipt_id)
            file_node_id = aliases.get(f"file:{receipt_text}")
            if file_node_id is not None:
                previous = aliases.get(receipt_text)
                if previous is not None and previous != file_node_id:
                    raise IncrementalDagError(f"alias_conflict:{receipt_text}")
                aliases[receipt_text] = file_node_id
        # Benchmark/runtime receipts are content-addressed by their own
        # receipt_sha256 rather than by a source-package file path.  Register
        # that hash against its owning node so a copied receipt is resolvable
        # during the same DAG replay that first imports it.
        receipt_hash = node.get("receipt_sha256")
        if receipt_hash is None and isinstance(node.get("attributes"), Mapping):
            receipt_hash = node["attributes"].get("receipt_sha256")
        if isinstance(receipt_hash, str) and receipt_hash:
            previous = aliases.get(receipt_hash)
            if previous is not None and previous != node["node_id"]:
                raise IncrementalDagError(f"alias_conflict:{receipt_hash}")
            aliases[receipt_hash] = node["node_id"]
    edges: list[dict[str, Any]] = []
    for raw in raw_edges:
        if not isinstance(raw, dict):
            raise IncrementalDagError("snapshot_edge_invalid")
        edges.append(_edge_from_record(raw, aliases, nodes))
    # Explicit inventory edges are only part of the contract.  Package and
    # workpack inventories also carry protocol-level references in rows; those
    # inferred prerequisite edges must be merged even when an inventory has a
    # few explicit root/containment edges.  Otherwise an explicit edge would
    # accidentally suppress semantic/support/review/formula fan-out.
    edges.extend(_add_auto_edges(records, aliases, nodes))
    unique = {edge["edge_id"]: edge for edge in edges}
    edges = [unique[key] for key in sorted(unique)]
    for node in nodes.values():
        for receipt_id in node.get("required_receipt_ids", []):
            if receipt_id not in aliases and receipt_id not in nodes:
                raise IncrementalDagError(f"missing_receipt:{receipt_id}")
        for receipt_id in node.get("receipt_ids", []):
            if receipt_id not in aliases and receipt_id not in nodes:
                raise IncrementalDagError(f"missing_receipt:{receipt_id}")
    _validate_graph(nodes, edges)
    return {"nodes": nodes, "edges": edges, "aliases": aliases}, list(raw_edges)


def _inventory_package(package: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Convert a source-free Expert Skill package into an explicit snapshot."""

    manifest_path = package / "manifest.json"
    if not manifest_path.is_file():
        raise IncrementalDagError("package_manifest_missing")
    manifest = load_json(manifest_path)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    package_schema = manifest.get("schema_version")
    root_kind = "composite" if package_schema == "tkc.composite-skill/v0.1" else "package"
    root = dict(manifest)
    root["kind"] = root_kind
    root["stable_id"] = str(manifest.get("package", {}).get("id", package.name))
    root["protocol_id"] = str(package_schema or INCREMENTAL_BUILD_DAG_PROTOCOL)
    nodes.append(root)
    root_external = root["stable_id"]
    for source in manifest.get("sources", []):
        if isinstance(source, dict):
            row = dict(source)
            row["kind"] = "source"
            nodes.append(row)
            edges.append({"type": "feeds", "from": row.get("source_id"), "to": root_external})
    for module in manifest.get("modules", []):
        if isinstance(module, dict):
            row = dict(module)
            row["kind"] = "module"
            row["stable_id"] = row.get("module_id")
            nodes.append(row)
            edges.append({"type": "contains", "from": row.get("module_id"), "to": root_external})
    try:
        index = load_json(package / "references/knowledge/index.json")
    except (OSError, json.JSONDecodeError):
        index = {}
    object_rows: list[dict[str, Any]] = []
    for entry in index.get("objects", []) if isinstance(index, dict) else []:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            try:
                object_row = load_json(package / entry["path"])
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(object_row, dict):
                object_rows.append(object_row)
    for row in object_rows:
        record = dict(row)
        record["kind"] = "semantic_assertion" if row.get("type") in {"Claim", "Equation", "Definition", "Concept", "Assumption", "Exception", "Method", "Procedure", "DecisionRule"} else "artifact"
        nodes.append(record)
        for evidence_id in row.get("evidence_ids", []) if isinstance(row.get("evidence_ids", []), list) else []:
            edges.append({"type": "supports", "from": evidence_id, "to": row.get("id")})
    jsonl_map = {
        "references/evidence/anchors.jsonl": "support",
        "references/knowledge/relations.jsonl": "visual_relation",
        "references/conflicts.jsonl": "support",
        "references/knowledge-gaps.jsonl": "support",
        "references/competency-tests.jsonl": "competency",
        "references/semantic/semantic-assertions.jsonl": "semantic_assertion",
        "references/semantic/support-matrix.jsonl": "support",
        "references/semantic/coverage-ledger.jsonl": "support",
        "references/semantic/review-attestations.jsonl": "review_attestation",
        "references/visual/visual-objects.jsonl": "visual_object",
        "references/visual/visual-relations.jsonl": "visual_relation",
        "references/visual/table-grids.jsonl": "table_grid",
        "references/visual/visual-review-attestations.jsonl": "review_attestation",
    }
    for relative, kind in jsonl_map.items():
        path = package / relative
        if not path.is_file():
            continue
        for row in load_jsonl(path):
            if isinstance(row, dict):
                record = dict(row)
                record["kind"] = kind
                nodes.append(record)
    for relative, kind, field in (
        ("references/runtime/catalog.json", "runtime", None),
        ("references/evaluations/evaluation-plan.json", "competency", None),
        ("references/reviews/review-plan.json", "review_plan", None),
        ("references/reviews/review-session.json", "review_session", None),
        ("references/procedures/index.json", "artifact", "procedures"),
        ("references/decisions/index.json", "artifact", "decisions"),
    ):
        path = package / relative
        if path.is_file():
            try:
                row = load_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            row = dict(row)
            row["kind"] = kind
            row["artifact_path"] = relative
            nodes.append(row)
    # Every file is represented by a source-free artifact node.  This catches
    # parser/model/config/locator/render/runtime/seal changes even when a future
    # package schema does not yet expose a typed record for that file.
    for path in sorted(candidate for candidate in package.rglob("*") if candidate.is_file() and not candidate.is_symlink()):
        relative = path.relative_to(package).as_posix()
        if relative == "manifest.json":
            continue
        kind = "artifact"
        lowered = relative.lower()
        if "render" in lowered:
            kind = "render"
        elif "parser" in lowered:
            kind = "parser"
        elif "model" in lowered:
            kind = "model"
        elif "config" in lowered:
            kind = "config"
        elif "locator" in lowered or "heading" in lowered:
            kind = "locator"
        elif "formula" in lowered or "ast" in lowered:
            kind = "formula_ast"
        elif "test" in lowered or "competenc" in lowered:
            kind = "test"
        elif "seal" in lowered or "integrity" in lowered:
            kind = "seal"
        elif "runtime" in lowered or "catalog" in lowered:
            kind = "runtime"
        elif "review" in lowered or "attestation" in lowered:
            kind = "review_attestation"
        row = {
            "kind": kind,
            "stable_id": f"file:{relative}",
            # Keep file-byte identity separate from a logical record's
            # artifact_path alias.  A package may expose the same path both as
            # a typed logical product and as a raw file; conflating aliases
            # would silently choose one node and make byte tamper reusable.
            "file_path": relative,
            "artifact_hash": sha256_file(path),
            "protocol_id": package_schema or INCREMENTAL_BUILD_DAG_PROTOCOL,
        }
        nodes.append(row)
        edges.append({"type": "projects", "from": f"file:{relative}", "to": root_external})
    # Bind every product to the package root as a downstream aggregate edge.
    # A product change therefore forces package repackage/reseal; changing the
    # root policy never pretends to rewrite or silently revalidate products.
    for row in nodes[1:]:
        row.setdefault("package_id", root_external)
    return _explicit_snapshot({"nodes": nodes, "edges": edges})


def _load_snapshot_input(value: Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        from compiler_version import SCIENCE_SNAPSHOT_SCHEMA
        if value.get("schema_version") == SCIENCE_SNAPSHOT_SCHEMA:
            from pipeline_orchestrator import validate_science_snapshot
            validate_science_snapshot(value)
        graph, _ = _explicit_snapshot(value)
        return graph
    path = value.expanduser().resolve()
    if path.is_dir():
        if (path / "dag-manifest.json").is_file() and (path / "nodes.jsonl").is_file():
            return _load_dag_directory(path)
        if (path / "manifest.json").is_file():
            try:
                manifest_probe = load_json(path / "manifest.json")
            except (OSError, json.JSONDecodeError):
                manifest_probe = {}
            if isinstance(manifest_probe, dict) and manifest_probe.get("schema_version") == VISUAL_PARSER_MANIFEST_SCHEMA:
                return _load_visual_parser_snapshot(path, manifest_probe)
            return _inventory_package(path)[0]
        if (path / "nodes.jsonl").is_file():
            rows = load_jsonl(path / "nodes.jsonl")
            edges = load_jsonl(path / "edges.jsonl") if (path / "edges.jsonl").is_file() else []
            return _explicit_snapshot({"nodes": rows, "edges": edges})[0]
        if (path / "book-plan.json").is_file():
            return _load_book_plan_snapshot(path)
        if (path / "workpack.json").is_file():
            return _load_workpack_snapshot(path)
        raise IncrementalDagError("snapshot_directory_unrecognized")
    if not path.is_file():
        raise IncrementalDagError("snapshot_input_missing")
    try:
        data = load_json(path)
    except (OSError, json.JSONDecodeError) as error:
        raise IncrementalDagError("snapshot_json_invalid") from error
    if isinstance(data, dict) and data.get("schema_version") == INCREMENTAL_DAG_MANIFEST_SCHEMA:
        return _load_dag_directory(path.parent)
    if isinstance(data, dict):
        return _load_snapshot_input(data)
    raise IncrementalDagError("snapshot_input_invalid")


def _load_visual_parser_snapshot(path: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Project a Phase 7D visual bundle into the typed DAG.

    Synthetic fake rows are deliberately rejected at this downstream boundary;
    they remain valid only inside their explicitly marked test workpack and can
    never become a real parser/visual/review/composer prerequisite.
    """

    if manifest.get("synthetic_only") is True:
        raise IncrementalDagError("synthetic_visual_rows_not_real_downstream")
    if manifest.get("manifest_sha256") != sha256_json({key: value for key, value in manifest.items() if key != "manifest_sha256"}):
        raise IncrementalDagError("visual_parser_manifest_hash_mismatch")
    components = manifest.get("components")
    if not isinstance(components, dict):
        raise IncrementalDagError("visual_parser_manifest_components_missing")
    required_components = {"job.json", "visual-bundle.json"}
    if not required_components.issubset(components):
        missing = sorted(required_components - set(components))
        raise IncrementalDagError(f"visual_parser_manifest_component_missing:{missing[0]}")
    snapshot_root = path.expanduser().resolve()
    for relative, expected_hash in sorted(components.items()):
        if not isinstance(relative, str) or not isinstance(expected_hash, str):
            raise IncrementalDagError("visual_parser_manifest_component_invalid")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise IncrementalDagError("visual_parser_manifest_component_path_invalid")
        component_path = path / relative_path
        try:
            resolved_component = component_path.resolve()
        except OSError as error:
            raise IncrementalDagError(f"visual_parser_manifest_component_path_invalid:{relative}") from error
        if component_path.is_symlink() or not resolved_component.is_relative_to(snapshot_root):
            raise IncrementalDagError(f"visual_parser_manifest_component_path_invalid:{relative}")
        if not resolved_component.is_file() or sha256_file(resolved_component) != expected_hash:
            raise IncrementalDagError(f"visual_parser_component_hash_mismatch:{relative}")
    bundle = load_json(path / "visual-bundle.json")
    job = load_json(path / "job.json")
    scan_ir: dict[str, Any] = {}
    scan_observations: list[dict[str, Any]] = []
    scan_backend_receipts: list[dict[str, Any]] = []
    scan_workers: list[dict[str, Any]] = []
    scan_transforms: list[dict[str, Any]] = []
    scan_visual_candidates: list[dict[str, Any]] = []
    scan_visual_gaps: list[dict[str, Any]] = []
    scan_visual_conflicts: list[dict[str, Any]] = []
    scan_result_adapters: list[dict[str, Any]] = []
    scan_root = path / "scan-ir"
    if (scan_root / "scan-ir.json").is_file():
        scan_ir = load_json(scan_root / "scan-ir.json")
        for filename, target in (
            ("ocr-observations.jsonl", scan_observations),
            ("ocr-backend-receipts.jsonl", scan_backend_receipts),
            ("ocr-worker-responses.jsonl", scan_workers),
            ("coordinate-transforms.jsonl", scan_transforms),
            ("visual-candidates.jsonl", scan_visual_candidates),
            ("visual-gaps.jsonl", scan_visual_gaps),
            ("visual-conflicts.jsonl", scan_visual_conflicts),
            ("paddle-result-adapters.jsonl", scan_result_adapters),
        ):
            if (scan_root / filename).is_file():
                target.extend(row for row in load_jsonl(scan_root / filename) if isinstance(row, dict))
    # Older workspaces may have carried the projected rows only in scan-ir.json.
    # Prefer the dedicated JSONL receipts when present, but retain the same
    # typed DAG projection for a validated v0.12 scan that was materialized
    # before the sidecar files were split out.
    if not scan_visual_candidates and isinstance(scan_ir.get("visual_candidates"), list):
        scan_visual_candidates.extend(row for row in scan_ir["visual_candidates"] if isinstance(row, dict))
    if not scan_visual_gaps and isinstance(scan_ir.get("visual_gaps"), list):
        scan_visual_gaps.extend(row for row in scan_ir["visual_gaps"] if isinstance(row, dict))
    if not scan_visual_conflicts and isinstance(scan_ir.get("visual_conflicts"), list):
        scan_visual_conflicts.extend(row for row in scan_ir["visual_conflicts"] if isinstance(row, dict))
    if not scan_result_adapters and isinstance(scan_ir.get("result_adapters"), list):
        scan_result_adapters.extend(row for row in scan_ir["result_adapters"] if isinstance(row, dict))
    if not isinstance(bundle, dict) or not isinstance(job, dict) or bundle.get("synthetic_only") is True:
        raise IncrementalDagError("visual_parser_synthetic_or_invalid_downstream")
    try:
        from visual_parser_orchestrator import validate_visual_parser_job_schema

        validate_visual_parser_job_schema(job)
    except IncrementalDagError:
        raise
    except Exception as error:
        raise IncrementalDagError("visual_parser_job_schema_invalid") from error
    if job.get("job_sha256") != sha256_json({key: value for key, value in job.items() if key != "job_sha256"}):
        raise IncrementalDagError("visual_parser_job_hash_mismatch")
    if manifest.get("job_sha256") != job.get("job_sha256"):
        raise IncrementalDagError("visual_parser_manifest_job_mismatch")
    source_sha256 = str(manifest.get("source_sha256"))
    source = job.get("source") if isinstance(job.get("source"), dict) else {}
    source_path = Path(str(source.get("path", ""))).expanduser().resolve()
    if source.get("sha256") != source_sha256 or not source_path.is_file() or sha256_file(source_path) != source.get("sha256"):
        raise IncrementalDagError("visual_parser_source_hash_mismatch")
    if bundle.get("source_sha256") != source_sha256 or bundle.get("candidate_only") is not True or bundle.get("whole_book_completeness_claimed") is not False:
        raise IncrementalDagError("visual_parser_bundle_contract_invalid")
    try:
        from pypdf import PdfReader
        from visual_semantics import validate_visual_bundle, validate_visual_review_overlay_manifest
        from visual_parser_orchestrator import _collect_receipt_hashes

        reader = PdfReader(str(source_path))
        page_sizes = {
            int(index): (float(page.mediabox.width), float(page.mediabox.height))
            for index, page in enumerate(reader.pages, start=1)
        }
        visual_issues = validate_visual_bundle(bundle, source_sha256=source_sha256, page_sizes=page_sizes, require_candidate_only=True)
    except Exception as error:
        if isinstance(error, IncrementalDagError):
            raise
        raise IncrementalDagError("visual_parser_bundle_validation_failed") from error
    if visual_issues:
        raise IncrementalDagError(f"visual_parser_bundle_invalid:{visual_issues[0].code}")
    overlay_path = path / "visual-review-overlay.json"
    overlay_payload: dict[str, Any] | None = None
    if overlay_path.is_file():
        try:
            overlay = load_json(overlay_path)
            overlay_payload = overlay
            receipt_material = {"scan_ir": {key: scan_ir.get(key) for key in ("raster_regions", "runtime_contracts", "table_grids", "table_grid_bindings", "visual_candidates", "visual_gaps", "visual_conflicts", "result_adapters")}, "backend_receipts": scan_backend_receipts, "transforms": scan_transforms, "workers": scan_workers, "visual_candidates": scan_visual_candidates, "visual_gaps": scan_visual_gaps, "visual_conflicts": scan_visual_conflicts, "result_adapters": scan_result_adapters}
            overlay_issues = validate_visual_review_overlay_manifest(overlay, source_sha256=source_sha256, objects=bundle.get("objects", []), grids=bundle.get("table_grids", []), relations=bundle.get("relations", []), synthetic_only=False, receipt_hashes=_collect_receipt_hashes(receipt_material))
        except Exception as error:
            raise IncrementalDagError("visual_parser_overlay_validation_failed") from error
        if overlay_issues:
            raise IncrementalDagError(f"visual_parser_overlay_invalid:{overlay_issues[0].code}")
    root_id = f"visual-parser:{manifest.get('job_sha256', manifest.get('manifest_sha256', 'unknown'))}"
    source_id = f"source:{source_sha256}"
    nodes: list[dict[str, Any]] = [{"kind": "package", "stable_id": root_id, "package_id": root_id, "source_sha256": source_sha256, "protocol": manifest.get("protocol")}, {"kind": "source", "stable_id": source_id, "source_id": source_id, "source_sha256": source_sha256, "package_id": root_id}]
    qualification_payloads: dict[str, dict[str, Any]] = {}
    qualification_ids: dict[str, str] = {}
    backend_config = job.get("backend_config") if isinstance(job.get("backend_config"), Mapping) else {}
    qualifications = backend_config.get("runtime_qualifications") if isinstance(backend_config, Mapping) else None
    if isinstance(qualifications, Mapping) and qualifications.get("schema_version") == "tkc.paddle-runtime-qualification/v0.1":
        qualifications = {str(qualifications.get("backend_id")): qualifications}
    if isinstance(qualifications, Mapping):
        for backend_id, receipt in sorted(qualifications.items()):
            if not isinstance(receipt, Mapping):
                raise IncrementalDagError(f"visual_parser_runtime_qualification_invalid:{backend_id}")
            receipt_hash = str(receipt.get("receipt_sha256", ""))
            if not HASH_RE.fullmatch(receipt_hash) or receipt_hash != sha256_json({key: value for key, value in receipt.items() if key != "receipt_sha256"}):
                raise IncrementalDagError(f"visual_parser_runtime_qualification_invalid:{backend_id}")
            try:
                validate_runtime_qualification(
                    receipt,
                    backend_id=str(backend_id),
                    require_qualified=True,
                )
            except PaddleRuntimeQualificationError as error:
                raise IncrementalDagError(
                    f"visual_parser_runtime_qualification_invalid:{backend_id}:{error}"
                ) from error
            qualification_id = f"runtime-qualification:{backend_id}:{receipt_hash}"
            qualification_payloads[str(backend_id)] = dict(receipt)
            qualification_ids[str(backend_id)] = qualification_id
    # Phase 7D.3 policy/census/routing/budget/index artifacts are explicit DAG
    # inputs.  A change in scheduling policy therefore invalidates only the
    # selective visual/review/composition closure; native text nodes remain
    # reusable.  v0.3 manifests simply omit this subtree and stay read-only.
    selective_sidecars = (
        ("visual_policy", "visual_policy", "policy_sha256", "visual-policy"),
        ("visual_census", "visual_census", "census_sha256", "visual-census"),
        ("visual_routing", "visual_routing", "routing_sha256", "visual-routing"),
        ("visual_budget_receipt", "visual_budget", "budget_receipt_sha256", "visual-budget"),
        ("visual_index", "visual_index", "index_sha256", "visual-index"),
        ("visual_coverage", "visual_coverage", "coverage_sha256", "visual-coverage"),
    )
    selective_ids: dict[str, str] = {}
    for manifest_key, kind, hash_key, prefix in selective_sidecars:
        relative = manifest.get(manifest_key)
        if manifest_key == "visual_policy" and isinstance(relative, Mapping):
            payload = dict(relative)
        else:
            if not isinstance(relative, str):
                continue
            sidecar_path = path / relative
            if not sidecar_path.is_file():
                raise IncrementalDagError(f"visual_parser_manifest_component_missing:{relative}")
            payload = load_json(sidecar_path)
        payload_hash = str(payload.get(hash_key, "")) if isinstance(payload, Mapping) else ""
        if not HASH_RE.fullmatch(payload_hash):
            raise IncrementalDagError(f"visual_parser_selective_identity_invalid:{kind}")
        node_id = f"{prefix}:{payload_hash}"
        inputs = [source_id]
        if kind != "visual_policy" and "visual_policy" in selective_ids:
            inputs.append(selective_ids["visual_policy"])
        if kind in {"visual_routing", "visual_budget", "visual_coverage"} and "visual_census" in selective_ids:
            inputs.append(selective_ids["visual_census"])
        if kind in {"visual_index", "visual_coverage"} and "visual_routing" in selective_ids:
            inputs.append(selective_ids["visual_routing"])
        node = {**dict(payload), "kind": kind, "stable_id": node_id, "id": node_id, "input_ids": sorted(set(inputs)), "package_id": root_id, "source_sha256": source_sha256, "candidate_only": True, "promotion": False}
        nodes.append(node)
        selective_ids[kind] = node_id
    chart_candidate_ids: list[str] = []
    chart_relative = manifest.get("chart_candidates")
    if isinstance(chart_relative, str):
        chart_path = path / chart_relative
        if not chart_path.is_file():
            raise IncrementalDagError(f"visual_parser_manifest_component_missing:{chart_relative}")
        chart_rows = load_json(chart_path)
        if not isinstance(chart_rows, list):
            raise IncrementalDagError("visual_parser_chart_candidates_invalid")
        for chart in chart_rows:
            if not isinstance(chart, Mapping):
                raise IncrementalDagError("visual_parser_chart_candidate_invalid")
            candidate_hash = str(chart.get("candidate_sha256") or sha256_json({key: value for key, value in chart.items() if key != "candidate_sha256"}))
            if not HASH_RE.fullmatch(candidate_hash):
                raise IncrementalDagError("visual_parser_chart_candidate_identity_invalid")
            chart_id = f"chart-candidate:{candidate_hash}"
            chart_inputs = [source_id]
            if selective_ids.get("visual_routing"):
                chart_inputs.append(selective_ids["visual_routing"])
            if qualification_ids.get("paddleocr-chart-parsing"):
                chart_inputs.append(qualification_ids["paddleocr-chart-parsing"])
            nodes.append({**dict(chart), "kind": "visual_chart_candidate", "stable_id": chart_id, "id": chart_id, "input_ids": sorted(set(chart_inputs)), "package_id": root_id, "source_sha256": source_sha256, "candidate_only": True, "promotion": False, "executable": False})
            chart_candidate_ids.append(chart_id)
    parser_id = f"parser:{job.get('compiler_version', manifest.get('compiler_version'))}"
    nodes.append({"kind": "parser", "stable_id": parser_id, "parser_id": parser_id, "parser_version": job.get("compiler_version"), "source_id": source_id, "package_id": root_id})
    config_hash = job.get("backend_config_sha256")
    config_id: str | None = None
    if isinstance(config_hash, str):
        config_id = f"config:{config_hash}"
        # Config is an upstream input to visual objects.  Do not put
        # parser_id on this node: _add_auto_edges interprets parser_id as
        # parser -> derived-node and that would create a misleading reverse
        # parser -> config edge rather than config -> visual object.
        nodes.append({"kind": "config", "stable_id": config_id, "config_id": config_id, "config_hash": config_hash, "package_id": root_id})
    runtime_ids: dict[str, str] = {}
    worker_ids: dict[str, str] = {}
    runtime_contracts = job.get("external_runtime") if isinstance(job.get("external_runtime"), Mapping) else {}
    for backend_id, contract in sorted(runtime_contracts.items()) if isinstance(runtime_contracts, Mapping) else []:
        if not isinstance(contract, Mapping):
            continue
        contract_hash = str(contract.get("contract_sha256", ""))
        if not HASH_RE.fullmatch(contract_hash):
            raise IncrementalDagError(f"visual_parser_runtime_contract_invalid:{backend_id}")
        runtime_id = f"runtime:{backend_id}:{contract_hash}"
        runtime_ids[str(backend_id)] = runtime_id
        runtime_inputs = [config_id] if config_id is not None else []
        nodes.append({"kind": "runtime", "stable_id": runtime_id, "runtime_id": runtime_id, "backend_id": str(backend_id), "contract_sha256": contract_hash, "worker_sha256": contract.get("worker_sha256"), "model_identity_sha256": (contract.get("model") or {}).get("identity_sha256"), "runtime_inventory_sha256": (contract.get("runtime_inventory") or {}).get("inventory_sha256"), "input_ids": runtime_inputs, "package_id": root_id})
        worker_hash = str(contract.get("worker_sha256", ""))
        if HASH_RE.fullmatch(worker_hash):
            worker_id = f"worker:{worker_hash}"
            worker_ids[str(backend_id)] = worker_id
            nodes.append({"kind": "worker", "stable_id": worker_id, "worker_id": worker_id, "backend_id": str(backend_id), "worker_sha256": worker_hash, "runtime_id": runtime_id, "input_ids": [runtime_id], "package_id": root_id})
    for backend_id, receipt in sorted(qualification_payloads.items()):
        qualification_id = qualification_ids[backend_id]
        inputs = []
        if backend_id in runtime_ids:
            inputs.append(runtime_ids[backend_id])
        elif config_id is not None:
            inputs.append(config_id)
        nodes.append({
            **receipt,
            "kind": "runtime_qualification",
            "stable_id": qualification_id,
            "receipt_qualification_id": receipt.get("qualification_id"),
            "qualification_id": qualification_id,
            "backend_id": backend_id,
            "input_ids": inputs,
            "package_id": root_id,
            "candidate_only": True,
            "promotion": False,
        })
    model_ids: dict[str, str] = {}
    for backend in job.get("backend_receipts", []) if isinstance(job.get("backend_receipts"), list) else []:
        if not isinstance(backend, dict):
            continue
        backend_id = str(backend.get("backend_id", "backend"))
        model = backend.get("model") if isinstance(backend.get("model"), dict) else {}
        model_hash = model.get("identity_sha256")
        if isinstance(model_hash, str):
            model_id = f"model:{backend_id}:{model_hash}"
            # The parser consumes the model only as an extraction input; the
            # model must invalidate visual objects directly, not become a
            # parser child through the generic parser_id edge rule.
            model_inputs = [runtime_ids[backend_id]] if backend_id in runtime_ids else []
            nodes.append({"kind": "model", "stable_id": model_id, "model_id": model_id, "model_hash": model_hash, "input_ids": model_inputs, "package_id": root_id})
            model_ids[backend_id] = model_id
    for page in bundle.get("pages", []) if isinstance(bundle.get("pages"), list) else []:
        page_id = f"physical-page-{int(page)}"
        nodes.append({"kind": "page", "stable_id": page_id, "page_id": page_id, "physical_page": int(page), "source_id": source_id, "source_sha256": source_sha256, "package_id": root_id})
    for render in bundle.get("render_receipts", []) if isinstance(bundle.get("render_receipts"), list) else []:
        if not isinstance(render, dict):
            continue
        render_hash = str(render.get("render_sha256"))
        render_id = f"render:{render_hash}"
        page_id = f"physical-page-{int(render.get('physical_page'))}"
        nodes.append({"kind": "render", "stable_id": render_id, "render_id": render_id, "render_sha256": render_hash, "physical_page": int(render.get("physical_page")), "page_id": page_id, "source_id": source_id, "package_id": root_id})
    crop_ids: dict[str, str] = {}
    for region in scan_ir.get("raster_regions", []) if isinstance(scan_ir.get("raster_regions"), list) else []:
        if not isinstance(region, Mapping):
            continue
        crop_hash = str(region.get("crop_sha256", ""))
        if not HASH_RE.fullmatch(crop_hash):
            raise IncrementalDagError("visual_parser_crop_receipt_invalid")
        crop_id = f"raster-crop:{crop_hash}"
        crop_ids[str(region.get("region_id", crop_hash))] = crop_id
        page_id = f"physical-page-{int(region.get('physical_page'))}"
        crop_inputs = [page_id]
        crop_inputs.extend(runtime_ids.values())
        nodes.append({"kind": "raster_crop", "stable_id": crop_id, "crop_id": crop_id, "crop_sha256": crop_hash, "render_sha256": region.get("render_sha256"), "physical_page": int(region.get("physical_page")), "input_ids": sorted(set(crop_inputs)), "package_id": root_id})
    receipt_ids: dict[str, str] = {}
    for receipt in scan_backend_receipts:
        if not isinstance(receipt, Mapping) or not receipt.get("id"):
            continue
        receipt_id = f"receipt:{receipt['id']}"
        receipt_ids[str(receipt["id"])] = receipt_id
        backend_id = str(receipt.get("backend_id", ""))
        receipt_inputs = [runtime_ids[backend_id]] if backend_id in runtime_ids else []
        if backend_id in qualification_ids:
            receipt_inputs.append(qualification_ids[backend_id])
        worker_id = worker_ids.get(backend_id)
        if worker_id:
            receipt_inputs.append(worker_id)
        if receipt.get("crop_sha256"):
            crop_id = next((value for value in crop_ids.values() if value.endswith(str(receipt.get("crop_sha256")))), None)
            if crop_id:
                receipt_inputs.append(crop_id)
        nodes.append({"kind": "receipt", "stable_id": receipt_id, "receipt_id": receipt_id, "backend_id": backend_id, "receipt_sha256": receipt.get("runtime_contract_sha256") or receipt.get("worker_response_sha256"), "input_ids": sorted(set(receipt_inputs)), "package_id": root_id})
    for adapter in scan_result_adapters:
        if not isinstance(adapter, Mapping) or not adapter.get("id"):
            continue
        adapter_id = str(adapter["id"])
        backend_id = str(adapter.get("backend_id", ""))
        result_id = f"runtime-result:{adapter_id}"
        result_inputs = [runtime_ids[backend_id]] if backend_id in runtime_ids else []
        if backend_id in qualification_ids:
            result_inputs.append(qualification_ids[backend_id])
        if backend_id in worker_ids:
            result_inputs.append(worker_ids[backend_id])
        receipt_id = receipt_ids.get(str(adapter.get("backend_receipt_id")))
        if receipt_id:
            result_inputs.append(receipt_id)
        crop_id = next((value for value in crop_ids.values() if value.endswith(str(adapter.get("crop_sha256", "")))), None)
        if crop_id:
            result_inputs.append(crop_id)
        nodes.append({
            "kind": "runtime_result",
            "stable_id": result_id,
            "result_id": result_id,
            "adapter_id": adapter_id,
            "backend_id": backend_id,
            "physical_page": int(adapter.get("physical_page", 0)),
            "raw_result_sha256": adapter.get("raw_result_sha256"),
            "normalized_result_sha256": adapter.get("normalized_result_sha256"),
            "runtime_contract_sha256": adapter.get("runtime_contract_sha256"),
            "candidate_only": True,
            "promotion": False,
            "verified_gold": False,
            "input_ids": sorted(set(value for value in result_inputs if value)),
            "package_id": root_id,
        })
    for observation in scan_observations:
        observation_id = str(observation.get("id", ""))
        if not observation_id:
            continue
        backend_id = str((observation.get("backend") or {}).get("id", ""))
        observation_node_id = f"ocr-observation:{observation_id}"
        observation_inputs = [f"physical-page-{int(observation.get('physical_page'))}"]
        if backend_id in runtime_ids:
            observation_inputs.extend([runtime_ids[backend_id], worker_ids.get(backend_id, "")])
        receipt_node = receipt_ids.get(str(observation.get("backend_receipt_id")))
        if receipt_node:
            observation_inputs.append(receipt_node)
        raw_region = (observation.get("raw_observation") or {}).get("raster_region") if isinstance(observation.get("raw_observation"), Mapping) else None
        if isinstance(raw_region, Mapping) and str(raw_region.get("region_id")) in crop_ids:
            observation_inputs.append(crop_ids[str(raw_region.get("region_id"))])
        nodes.append({"kind": "ocr_observation", "stable_id": observation_node_id, "observation_id": observation_id, "backend_id": backend_id, "physical_page": int(observation.get("physical_page")), "input_ids": sorted(set(value for value in observation_inputs if value)), "input_hashes": [str(observation.get("configuration_sha256"))] if observation.get("configuration_sha256") else [], "package_id": root_id})
    adapter_candidate_to_object: dict[str, str] = {}
    for object_row in bundle.get("objects", []) if isinstance(bundle.get("objects"), list) else []:
        if not isinstance(object_row, Mapping):
            continue
        attributes = object_row.get("attributes") if isinstance(object_row.get("attributes"), Mapping) else {}
        local_id = attributes.get("adapter_local_candidate_id")
        canonical_id = object_row.get("visual_object_id")
        if isinstance(local_id, str) and local_id and isinstance(canonical_id, str) and canonical_id:
            adapter_candidate_to_object[local_id] = canonical_id
    for candidate in scan_visual_candidates:
        if not isinstance(candidate, Mapping):
            continue
        candidate_value = str(candidate.get("candidate_id") or candidate.get("adapter_local_candidate_id") or "")
        if not candidate_value:
            continue
        adapter_id = str(candidate.get("result_adapter_id", ""))
        candidate_id = f"visual-candidate:{candidate_value}:{adapter_id or candidate.get('backend_receipt_id', 'unknown')}"
        candidate_inputs = [f"physical-page-{int(candidate.get('physical_page', 0))}"]
        result_node = f"runtime-result:{adapter_id}" if adapter_id else ""
        if result_node:
            candidate_inputs.append(result_node)
        receipt_node = receipt_ids.get(str(candidate.get("backend_receipt_id")))
        if receipt_node:
            candidate_inputs.append(receipt_node)
        crop_id = crop_ids.get(str(candidate.get("region_id")))
        if crop_id:
            candidate_inputs.append(crop_id)
        canonical_object_id = adapter_candidate_to_object.get(candidate_value)
        if canonical_object_id:
            candidate_inputs.append(canonical_object_id)
        nodes.append({
            **dict(candidate),
            "kind": "visual_candidate",
            "stable_id": candidate_id,
            "candidate_record_id": candidate_id,
            "adapter_local_candidate_id": candidate_value,
            "canonical_visual_object_id": canonical_object_id,
            "input_ids": sorted(set(value for value in candidate_inputs if value)),
            "package_id": root_id,
            "candidate_only": True,
            "promotion": False,
            "verified_gold": False,
        })
    for gap in scan_visual_gaps:
        if not isinstance(gap, Mapping) or not gap.get("gap_id"):
            continue
        gap_id = f"visual-gap:{gap['gap_id']}"
        gap_inputs = [f"physical-page-{int(gap.get('physical_page', 0))}"]
        adapter_id = str(gap.get("result_adapter_id", ""))
        if adapter_id:
            gap_inputs.append(f"runtime-result:{adapter_id}")
        receipt_node = receipt_ids.get(str(gap.get("backend_receipt_id")))
        if receipt_node:
            gap_inputs.append(receipt_node)
        nodes.append({**dict(gap), "kind": "visual_gap", "stable_id": gap_id, "gap_id": gap_id, "input_ids": sorted(set(gap_inputs)), "package_id": root_id, "candidate_only": True, "promotion": False, "verified_gold": False})
    for conflict in scan_visual_conflicts:
        if not isinstance(conflict, Mapping) or not conflict.get("conflict_id"):
            continue
        conflict_id = f"visual-conflict:{conflict['conflict_id']}"
        conflict_inputs = [f"physical-page-{int(conflict.get('physical_page', 0))}"]
        adapter_id = str(conflict.get("result_adapter_id", ""))
        if adapter_id:
            conflict_inputs.append(f"runtime-result:{adapter_id}")
        receipt_node = receipt_ids.get(str(conflict.get("backend_receipt_id")))
        if receipt_node:
            conflict_inputs.append(receipt_node)
        adapter_local_object_ids = [str(value) for value in conflict.get("object_ids", []) if isinstance(value, str)]
        canonical_object_ids = [adapter_candidate_to_object.get(value, value) for value in adapter_local_object_ids]
        conflict_inputs.extend(canonical_object_ids)
        nodes.append({
            **dict(conflict),
            "kind": "visual_conflict",
            "stable_id": conflict_id,
            "conflict_id": conflict_id,
            "object_ids": sorted(set(canonical_object_ids)),
            "adapter_local_object_ids": sorted(set(adapter_local_object_ids)),
            "input_ids": sorted(set(conflict_inputs)),
            "package_id": root_id,
            "candidate_only": True,
            "promotion": False,
            "verified_gold": False,
        })
    object_ids: list[str] = []
    relation_ids: list[str] = []
    grid_ids: list[str] = []
    observation_ids: list[str] = []
    for observation in scan_observations:
        observation_id = str(observation.get("id", ""))
        if observation_id:
            observation_ids.append(f"ocr-observation:{observation_id}")
    for row in bundle.get("objects", []) if isinstance(bundle.get("objects"), list) else []:
        if not isinstance(row, dict) or row.get("synthetic_only") is True:
            raise IncrementalDagError("visual_parser_synthetic_object_downstream")
        object_id = str(row.get("visual_object_id"))
        page_id = f"physical-page-{int(row.get('physical_page'))}"
        canonical_render_sha256 = row.get("canonical_render_sha256")
        render_id = f"render:{canonical_render_sha256}" if canonical_render_sha256 else None
        backend_id = str((row.get("backend_receipt") or {}).get("backend_id", "")) if isinstance(row.get("backend_receipt"), dict) else ""
        object_inputs = [page_id, parser_id]
        object_inputs.extend(selective_ids.get(kind) for kind in ("visual_policy", "visual_census", "visual_routing", "visual_budget") if selective_ids.get(kind))
        if config_id is not None:
            object_inputs.append(config_id)
        if render_id is not None:
            object_inputs.append(render_id)
        if backend_id in model_ids:
            object_inputs.append(model_ids[backend_id])
        if backend_id in runtime_ids:
            object_inputs.append(runtime_ids[backend_id])
        if backend_id in worker_ids:
            object_inputs.append(worker_ids[backend_id])
        for observation in scan_observations:
            if int(observation.get("physical_page", 0)) == int(row.get("physical_page", 0)) and str((observation.get("backend") or {}).get("id", "")) == backend_id:
                object_inputs.append(f"ocr-observation:{observation.get('id')}")
        raw_region = (row.get("attributes") or {}).get("raster_region") if isinstance(row.get("attributes"), Mapping) else None
        if isinstance(raw_region, Mapping) and str(raw_region.get("region_id")) in crop_ids:
            object_inputs.append(crop_ids[str(raw_region.get("region_id"))])
        object_row = {**row, "kind": "visual_object", "stable_id": object_id, "object_id": object_id, "source_id": source_id, "page_id": page_id, "parser_id": parser_id, "package_id": root_id, "input_ids": object_inputs, "input_hashes": [str(row.get("input_sha256"))] if row.get("input_sha256") else []}
        nodes.append(object_row)
        object_ids.append(object_id)
    for row in bundle.get("relations", []) if isinstance(bundle.get("relations"), list) else []:
        if not isinstance(row, dict):
            continue
        relation_id = str(row.get("visual_relation_id"))
        relation_inputs = [str(value) for value in (row.get("source_visual_object_id"), row.get("target_visual_object_id")) if value]
        nodes.append({**row, "kind": "visual_relation", "stable_id": relation_id, "relation_id": relation_id, "source_sha256": source_sha256, "package_id": root_id, "input_ids": relation_inputs, "input_hashes": [str(value) for value in row.get("evidence_hashes", []) if isinstance(value, str)]})
        relation_ids.append(relation_id)
    for row in bundle.get("table_grids", []) if isinstance(bundle.get("table_grids"), list) else []:
        if not isinstance(row, dict):
            continue
        grid_id = str(row.get("table_grid_id"))
        grid_inputs = [str(row.get("table_visual_object_id"))] if row.get("table_visual_object_id") else []
        binding = next((item for item in scan_ir.get("table_grid_bindings", []) if isinstance(item, Mapping) and item.get("table_grid_id") == grid_id), None)
        if isinstance(binding, Mapping):
            receipt_node = receipt_ids.get(str(binding.get("backend_receipt_id")))
            if receipt_node:
                grid_inputs.append(receipt_node)
            crop_id = crop_ids.get(str(binding.get("region_id")))
            if crop_id:
                grid_inputs.append(crop_id)
            runtime_id = runtime_ids.get(str(binding.get("backend_id")))
            if runtime_id:
                grid_inputs.append(runtime_id)
        nodes.append({**row, "kind": "table_grid", "stable_id": grid_id, "grid_id": grid_id, "source_sha256": source_sha256, "package_id": root_id, "input_ids": grid_inputs, "input_hashes": [str(row.get("grid_sha256"))] if row.get("grid_sha256") else []})
        grid_ids.append(grid_id)
        for cell in row.get("cells", []) if isinstance(row.get("cells"), list) else []:
            if not isinstance(cell, Mapping) or not cell.get("cell_id"):
                continue
            cell_id = str(cell["cell_id"])
            cell_inputs = [grid_id, f"physical-page-{int(row.get('physical_page', 0))}"]
            if row.get("table_visual_object_id"):
                cell_inputs.append(str(row["table_visual_object_id"]))
            nodes.append({
                **dict(cell),
                "kind": "table_cell",
                "stable_id": cell_id,
                "cell_id": cell_id,
                "table_grid_id": grid_id,
                "source_sha256": source_sha256,
                "physical_page": int(row.get("physical_page", 0)),
                "input_ids": sorted(set(cell_inputs)),
                "package_id": root_id,
                "candidate_only": True,
                "promotion": False,
                "verified_gold": False,
            })
    if overlay_payload is not None:
        overlay_hash = str(overlay_payload.get("overlay_sha256", ""))
        if not HASH_RE.fullmatch(overlay_hash):
            raise IncrementalDagError("visual_parser_overlay_identity_invalid")
        overlay_id = f"review-overlay:{overlay_hash}"
        overlay_inputs = sorted(set(object_ids + relation_ids + grid_ids))
        overlay_inputs.extend(f"visual-candidate:{str(row.get('candidate_id') or row.get('adapter_local_candidate_id'))}:{str(row.get('result_adapter_id') or row.get('backend_receipt_id', 'unknown'))}" for row in scan_visual_candidates if isinstance(row, Mapping) and (row.get("candidate_id") or row.get("adapter_local_candidate_id")))
        overlay_inputs.extend(f"visual-gap:{row.get('gap_id')}" for row in scan_visual_gaps if isinstance(row, Mapping) and row.get("gap_id"))
        overlay_inputs.extend(f"visual-conflict:{row.get('conflict_id')}" for row in scan_visual_conflicts if isinstance(row, Mapping) and row.get("conflict_id"))
        nodes.append({
            "kind": "review_overlay",
            "stable_id": overlay_id,
            "overlay_id": overlay_id,
            "overlay_sha256": overlay_hash,
            "source_sha256": source_sha256,
            "candidate_only": True,
            "review_authoring": False,
            "promotion": False,
            "input_ids": sorted(set(overlay_inputs)),
            "package_id": root_id,
        })
    composer_id = f"composer-intent:{manifest.get('job_sha256', manifest.get('manifest_sha256', 'unknown'))}"
    composer_inputs = sorted(set(object_ids + relation_ids + grid_ids + observation_ids + chart_candidate_ids + list(selective_ids.values())))
    if overlay_payload is not None:
        composer_inputs.append(f"review-overlay:{overlay_payload.get('overlay_sha256')}")
    nodes.append({"kind": "composer", "stable_id": composer_id, "composer_id": composer_id, "input_ids": sorted(set(composer_inputs)), "package_id": root_id, "source_id": source_id, "candidate_only": True, "promotion": False, "paused": True})
    return _explicit_snapshot({"nodes": nodes, "edges": []})[0]


def _load_book_plan_snapshot(path: Path) -> dict[str, Any]:
    plan = load_json(path / "book-plan.json")
    root_id = str(plan.get("book_plan_id", "book-plan"))
    nodes: list[dict[str, Any]] = [{**plan, "kind": "package", "stable_id": root_id}]
    for relative, kind in (
        ("book-coverage-ledger.jsonl", "coverage"),
        ("module-plan.jsonl", "module"),
        ("review-routing.jsonl", "review_plan"),
        ("heading-candidates.jsonl", "locator"),
        ("heading-resolutions.jsonl", "locator"),
    ):
        file = path / relative
        if not file.is_file():
            continue
        for row in load_jsonl(file):
            if isinstance(row, dict):
                record = dict(row)
                if kind == "coverage":
                    scope_kind = str(record.get("scope_kind", "")).lower()
                    record["kind"] = "segment" if scope_kind in {"segment", "semantic_segment"} else "page"
                    record["stable_id"] = record.get("record_id", record.get("page_id", record.get("segment_id")))
                    physical_pages = record.get("physical_pages", [])
                    if record["kind"] == "page" and isinstance(physical_pages, list) and len(physical_pages) == 1:
                        record["page_id"] = f"physical-page-{physical_pages[0]}"
                else:
                    record["kind"] = kind
                    if kind == "review_plan":
                        record["stable_id"] = record.get("route_id", record.get("record_id", record.get("id")))
                record["artifact_path"] = relative
                if kind == "module":
                    record["input_ids"] = sorted(
                        {
                            str(value)
                            for key in ("page_ids", "segment_ids", "input_ids", "dependency_ids")
                            for value in (record.get(key, []) if isinstance(record.get(key, []), list) else [record.get(key)])
                            if isinstance(value, (str, int))
                        }
                    )
                    record["input_ids"] = sorted(
                        set(record["input_ids"])
                        | {
                            f"physical-page-{value}"
                            for value in record.get("physical_pages", [])
                            if isinstance(value, int)
                        }
                    )
                elif kind == "review_plan":
                    scope_refs = []
                    for key in ("scope_id", "page_id", "segment_id", "module_id", "record_id"):
                        value = record.get(key)
                        if isinstance(value, (str, int)):
                            scope_refs.append(str(value))
                    record["input_ids"] = sorted(set(scope_refs))
                nodes.append(record)
    # _add_auto_edges resolves page/segment -> module -> plan and review route
    # -> scope.  The plan root is a declared dependency of every top-level
    # module/route so a route or module mutation cannot disappear from closure.
    for row in nodes[1:]:
        if row.get("kind") in {"module", "review_plan", "locator"}:
            row["plan_id"] = root_id
    return _explicit_snapshot({"nodes": nodes, "edges": []})[0]


def _load_composer_workpack_snapshot(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Project a Phase 7C Composer workpack into the Phase 7B DAG.

    Only typed identities, hashes, and prerequisite links are retained in the
    DAG.  Candidate/source prose and PDF bytes never become release nodes.
    """
    root_id = str(manifest.get("workpack_id", path.name))
    nodes: list[dict[str, Any]] = [{**manifest, "kind": "composer", "id": root_id, "stable_id": root_id, "workpack_id": root_id}]
    edges: list[dict[str, Any]] = []
    inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), dict) else {}
    candidate = inputs.get("candidate_source") if isinstance(inputs.get("candidate_source"), dict) else {}
    candidate_source_id = candidate.get("source_id") or f"source:{candidate.get('sha256', root_id)}"
    nodes.append({
        "kind": "source", "id": candidate_source_id, "stable_id": candidate_source_id,
        "source_id": candidate_source_id, "source_sha256": candidate.get("sha256"),
        "package_id": root_id, "protocol_id": manifest.get("protocol"),
    })
    edges.append({"type": "feeds", "from": candidate_source_id, "to": root_id})
    for descriptor in inputs.get("packages", []) if isinstance(inputs.get("packages"), list) else []:
        if not isinstance(descriptor, dict):
            continue
        package_id = descriptor.get("package_id")
        if not isinstance(package_id, str):
            continue
        nodes.append({
            "kind": "package", "id": package_id, "stable_id": package_id,
            "package_id": package_id, "source_sha256": descriptor.get("source_sha256"),
            "artifact_hash": descriptor.get("manifest_sha256"), "protocol_id": manifest.get("protocol"),
        })
        edges.append({"type": "depends_on", "from": package_id, "to": root_id})

    def add_jsonl(relative: str, kind: str, id_key: str, input_keys: tuple[str, ...] = ()) -> list[str]:
        file = path / relative
        ids: list[str] = []
        if not file.is_file():
            return ids
        for raw in load_jsonl(file):
            if not isinstance(raw, dict) or not isinstance(raw.get(id_key), str):
                continue
            identifier = str(raw[id_key])
            input_ids: list[str] = []
            for key in input_keys:
                value = raw.get(key, [])
                values = value if isinstance(value, list) else [value]
                input_ids.extend(str(item) for item in values if isinstance(item, str))
            record = {
                "kind": kind, "id": identifier, "stable_id": identifier,
                id_key: identifier, "input_ids": sorted(set(input_ids)),
                "package_id": root_id, "protocol_id": manifest.get("protocol"),
                "source_sha256": raw.get("source_sha256"),
                "artifact_path": relative,
                "artifact_hash": sha256_json(raw),
            }
            if isinstance(raw.get("required_reviewer_count"), int):
                record["required_reviewer_count"] = raw["required_reviewer_count"]
            nodes.append(record)
            ids.append(identifier)
        return ids

    concept_ids = add_jsonl("concept-refs.jsonl", "artifact", "concept_ref_id")
    evidence_ids = add_jsonl("evidence-bindings.jsonl", "support", "binding_id")
    alignment_ids = add_jsonl("alignment-candidates.jsonl", "alignment_candidate", "alignment_candidate_id", ("left_ref", "right_ref", "difference_ids", "conflict_ids", "unit_map_ids", "evidence_binding_ids"))
    difference_ids = add_jsonl("differences.jsonl", "semantic_difference", "difference_id", ("left_ref", "right_ref", "evidence_binding_ids"))
    conflict_ids = add_jsonl("conflicts.jsonl", "composer_conflict", "conflict_id", ("difference_id", "left_ref", "right_ref", "evidence_binding_ids"))
    unit_ids = add_jsonl("unit-maps.jsonl", "unit_map", "unit_map_id", ("left_ref", "right_ref", "evidence_binding_ids"))
    plan = load_json(path / "review-plan.json") if (path / "review-plan.json").is_file() else {}
    plan_id = plan.get("review_plan_id") if isinstance(plan, dict) else None
    if isinstance(plan_id, str):
        nodes.append({
            "kind": "composer_review_plan", "id": plan_id, "stable_id": plan_id,
            "review_plan_id": plan_id, "input_ids": sorted(set(alignment_ids + difference_ids + conflict_ids + unit_ids + evidence_ids)),
            "package_id": root_id, "protocol_id": manifest.get("protocol"),
            "artifact_path": "review-plan.json", "artifact_hash": sha256_json(plan),
            "reviewer_requirement": max((int(item.get("required_reviewer_count", 1)) for item in plan.get("items", []) if isinstance(item, dict)), default=1),
        })
    attestation_ids = add_jsonl("review-attestations.jsonl", "composer_review", "attestation_id", ("candidate_id",))
    receipt_ids = add_jsonl("receipts.jsonl", "composer_receipt", "receipt_id")
    if isinstance(plan_id, str):
        for identifier in alignment_ids + difference_ids + conflict_ids + unit_ids:
            edges.append({"type": "reviews", "from": identifier, "to": plan_id})
        for identifier in attestation_ids:
            edges.append({"type": "reviews", "from": plan_id, "to": identifier})
    if isinstance(candidate.get("normalized_pdf_ir"), dict):
        ir = candidate["normalized_pdf_ir"]
        parser_id = f"pdf-ir:{ir.get('ir_sha256', root_id)}"
        locator_id = f"heading-task:{ir.get('heading_task_id', root_id)}"
        nodes.extend([
            {"kind": "parser", "id": parser_id, "stable_id": parser_id, "source_id": candidate_source_id, "parser_version": ir.get("identity_algorithm"), "artifact_hash": ir.get("ir_sha256"), "package_id": root_id},
            {"kind": "locator", "id": locator_id, "stable_id": locator_id, "source_id": candidate_source_id, "locator_hash": ir.get("heading_task_sha256"), "input_ids": [parser_id], "package_id": root_id},
        ])
        edges.append({"type": "parses", "from": parser_id, "to": root_id})
        edges.append({"type": "locates", "from": locator_id, "to": root_id})
    return _explicit_snapshot({"nodes": nodes, "edges": edges})[0]


def _load_workpack_snapshot(path: Path) -> dict[str, Any]:
    """Load a workpack as a conservative, protocol-aware prerequisite DAG.

    Workpacks predate the incremental DAG and deliberately do not carry one
    canonical edge list.  Normalize their stable IDs and bind every product
    to the exact segment/page/assertion/support/review inputs advertised by
    the workpack protocol.  Where a review artifact has no exact scope, bind
    it to the whole workpack product set (fail closed for reuse).
    """

    manifest = load_json(path / "workpack.json")
    if isinstance(manifest, dict) and manifest.get("schema_version") == "tkc.semantic-composer-workpack/v0.1":
        return _load_composer_workpack_snapshot(path, manifest)
    root_id = str(manifest.get("workpack_id", "workpack")) if isinstance(manifest, dict) else "workpack"
    root = {**manifest, "kind": "package", "stable_id": root_id, "workpack_id": root_id}
    nodes: list[dict[str, Any]] = [root]
    source_info = manifest.get("source", {}) if isinstance(manifest, dict) else {}
    source_id = str(source_info.get("source_id", f"source:{root_id}")) if isinstance(source_info, dict) else f"source:{root_id}"
    source_row = {
        **(source_info if isinstance(source_info, dict) else {}),
        "kind": "source",
        "stable_id": source_id,
        "source_id": source_id,
        "source_sha256": source_info.get("sha256") if isinstance(source_info, dict) else None,
        "workpack_id": root_id,
    }
    nodes.append(source_row)
    # Preserve parser/render/config identity from the immutable PDF-IR receipt
    # without copying any PDF or render material into the DAG output.
    pdf_ir = manifest.get("pdf_ir", {}) if isinstance(manifest, dict) else {}
    parser_id = ""
    render_id = ""
    if isinstance(pdf_ir, dict):
        parser_id = str(pdf_ir.get("layout_parser", {}).get("id", "parser")) if isinstance(pdf_ir.get("layout_parser"), dict) else "parser"
        parser_row = {"kind": "parser", "stable_id": parser_id, "parser_version": pdf_ir.get("layout_parser", {}).get("version") if isinstance(pdf_ir.get("layout_parser"), dict) else None, "source_id": source_id, "workpack_id": root_id}
        nodes.append(parser_row)
        render_hash = pdf_ir.get("render_receipts_sha256")
        if isinstance(render_hash, str):
            render_id = f"render:{render_hash}"
            nodes.append({"kind": "render", "stable_id": render_id, "render_id": render_id, "render_sha256": render_hash, "parser_id": parser_id, "workpack_id": root_id})
        config = pdf_ir.get("layout_parser", {}).get("configuration") if isinstance(pdf_ir.get("layout_parser"), dict) else None
        if isinstance(config, dict):
            nodes.append({"kind": "config", "stable_id": f"config:{sha256_json(config)}", "config_hash": sha256_json(config), "parser_id": parser_id, "workpack_id": root_id})
    units: list[dict[str, Any]] = []
    unit_pages: dict[str, set[int]] = defaultdict(set)

    def collect_pages(value: Any, result: set[int]) -> None:
        if isinstance(value, dict):
            page = value.get("page")
            if isinstance(page, int):
                result.add(page)
            for child in value.values():
                collect_pages(child, result)
        elif isinstance(value, list):
            for child in value:
                collect_pages(child, result)

    for unit in manifest.get("units", []) if isinstance(manifest, dict) else []:
        if not isinstance(unit, dict):
            continue
        row = dict(unit)
        unit_id = str(row.get("unit_id", row.get("segment_id", "")))
        if not unit_id:
            continue
        segment_id = str(row.get("segment_id", unit_id))
        row.update({"kind": "segment", "stable_id": segment_id, "segment_id": segment_id, "unit_id": unit_id})
        unit_file = path / "units" / f"{unit_id}.json"
        if unit_file.is_file():
            try:
                unit_payload = load_json(unit_file)
                pages: set[int] = set()
                collect_pages(unit_payload, pages)
                unit_pages[unit_id].update(pages)
                row["input_ids"] = [f"physical-page-{page}" for page in sorted(pages)]
            except (OSError, json.JSONDecodeError):
                pass
        units.append(row)
        nodes.append(row)

    page_rows: dict[int, dict[str, Any]] = {}
    page_index = path / "page-index.jsonl"
    if page_index.is_file():
        for raw in load_jsonl(page_index):
            if isinstance(raw, dict) and isinstance(raw.get("page"), int):
                page_rows[int(raw["page"])] = dict(raw)
    all_pages = set(page_rows)
    for pages in unit_pages.values():
        all_pages.update(pages)
    for page in sorted(all_pages):
        raw = page_rows.get(page, {})
        row = {
            **raw,
            "kind": "page",
            "stable_id": f"physical-page-{page}",
            "page_id": f"physical-page-{page}",
            "physical_page": page,
            "page": page,
            "artifact_path": "page-index.jsonl",
        }
        row.update({"source_id": source_id, "parser_id": parser_id, "render_id": render_id} if parser_id else {"source_id": source_id})
        nodes.append(row)

    # A scan workpack keeps OCR and reviewed structure as private candidate
    # sidecars.  Give those sidecars typed DAG identities so transcript,
    # structure, and review changes invalidate the existing extraction/review
    # closure without copying source payloads into the DAG.
    scan_candidate = manifest.get("scan_candidate") if isinstance(manifest, dict) else None
    scan_structure_id: str | None = None
    scan_transcript_ids: list[str] = []
    scan_observation_ids: list[str] = []
    scan_render_ids: dict[int, str] = {}
    scan_transform_ids: dict[str, str] = {}
    scan_backend_receipt_ids: dict[str, str] = {}
    scan_runtime_ids: dict[tuple[str, str], str] = {}
    scan_runtime_by_hash: dict[str, str] = {}
    scan_worker_ids: dict[tuple[str, str], str] = {}
    scan_worker_by_hash: dict[str, str] = {}
    scan_qualification_ids: dict[str, str] = {}
    if isinstance(scan_candidate, dict):
        hardened_scan = (
            scan_candidate.get("schema_version")
            == "tkc.scanned-pdf-workpack-candidate/v0.2"
        )
        structure_hash = scan_candidate.get("structure_review_sha256")
        structure_status = scan_candidate.get("structure_review_status")
        if structure_hash is not None and not HASH_RE.fullmatch(str(structure_hash)):
            raise IncrementalDagError("scan_structure_identity_invalid")
        if isinstance(structure_hash, str):
            scan_structure_id = f"scan-structure:{structure_hash}"

        def _scan_sidecar(relative: Any) -> Path | None:
            if not isinstance(relative, str) or not relative:
                return None
            relative_path = Path(relative)
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise IncrementalDagError("scan_sidecar_path_invalid")
            candidate = path / relative_path
            resolved = candidate.resolve()
            if candidate.is_symlink() or not resolved.is_relative_to(path.resolve()):
                raise IncrementalDagError("scan_sidecar_path_invalid")
            if not resolved.is_file():
                raise IncrementalDagError(f"scan_sidecar_missing:{relative}")
            return resolved

        def _scan_backend_id(row: Mapping[str, Any]) -> str | None:
            value = row.get("backend_id")
            if isinstance(value, str) and value:
                return value
            backend = row.get("backend")
            if isinstance(backend, Mapping) and isinstance(backend.get("id"), str):
                return str(backend["id"])
            return None

        def _scan_binding_hash(row: Mapping[str, Any], key: str) -> str | None:
            value = row.get(key)
            if key == "worker_sha256" and value is None:
                worker = row.get("worker")
                if isinstance(worker, Mapping):
                    value = worker.get("worker_sha256", worker.get("script_sha256"))
            if isinstance(value, str) and value:
                if not HASH_RE.fullmatch(value):
                    raise IncrementalDagError(f"scan_binding_hash_invalid:{key}")
                return value
            return None

        def _append_once(node: dict[str, Any], seen: set[str]) -> None:
            stable_id = str(node["stable_id"])
            if stable_id not in seen:
                nodes.append(node)
                seen.add(stable_id)

        scan_seen: set[str] = set()

        render_path = _scan_sidecar(scan_candidate.get("render_receipts"))
        if render_path is not None:
            for raw in load_jsonl(render_path):
                if not isinstance(raw, dict) or not isinstance(raw.get("physical_page"), int):
                    raise IncrementalDagError("scan_render_receipt_invalid")
                render_hash = raw.get("render_sha256")
                if not isinstance(render_hash, str) or not HASH_RE.fullmatch(render_hash):
                    raise IncrementalDagError("scan_render_receipt_hash_invalid")
                page = int(raw["physical_page"])
                stable_id = f"scan-render:{page}:{render_hash}"
                scan_render_ids[page] = stable_id
                _append_once(
                    {
                        **raw,
                        "kind": "render",
                        "stable_id": stable_id,
                        "render_id": stable_id,
                        "input_ids": [f"physical-page-{page}"],
                        "artifact_path": str(render_path.relative_to(path.resolve()).as_posix()),
                        "candidate_only": True,
                    },
                    scan_seen,
                )

        transform_path = _scan_sidecar(scan_candidate.get("transforms"))
        if transform_path is not None:
            for raw in load_jsonl(transform_path):
                if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                    raise IncrementalDagError("scan_transform_invalid")
                page = raw.get("physical_page")
                if not isinstance(page, int):
                    raise IncrementalDagError("scan_transform_page_invalid")
                transform_id = str(raw["id"])
                stable_id = f"scan-transform:{transform_id}"
                scan_transform_ids[transform_id] = stable_id
                inputs = [f"physical-page-{page}"]
                render_id = scan_render_ids.get(page)
                if render_id is not None:
                    inputs.append(render_id)
                _append_once(
                    {
                        **raw,
                        "kind": "scan_transform",
                        "stable_id": stable_id,
                        "transform_id": stable_id,
                        "input_ids": sorted(set(inputs)),
                        "artifact_path": str(transform_path.relative_to(path.resolve()).as_posix()),
                        "candidate_only": True,
                    },
                    scan_seen,
                )

        def _register_qualification(qualification_hash: str, backend_id: str | None, runtime_id: str | None) -> str:
            existing = scan_qualification_ids.get(qualification_hash)
            if existing is not None:
                return existing
            stable_id = f"scan-qualification:{qualification_hash}"
            inputs = [runtime_id] if runtime_id is not None else []
            _append_once(
                {
                    "kind": "runtime_qualification",
                    "stable_id": stable_id,
                    "qualification_id": stable_id,
                    "receipt_sha256": qualification_hash,
                    "qualification_receipt_sha256": qualification_hash,
                    "backend_id": backend_id,
                    "input_ids": sorted(set(inputs)),
                    "artifact_path": "scan-candidate-runtime-qualification-commitment",
                    "candidate_only": True,
                    "promotion": False,
                },
                scan_seen,
            )
            scan_qualification_ids[qualification_hash] = stable_id
            return stable_id

        def _register_runtime(
            backend_id: str,
            runtime_hash: str,
            worker_hash: str | None,
            qualification_hash: str | None,
            raw: Mapping[str, Any],
        ) -> tuple[str, str | None, str | None]:
            runtime_key = (backend_id, runtime_hash)
            runtime_id = scan_runtime_ids.get(runtime_key)
            if runtime_id is None:
                runtime_id = f"scan-runtime:{backend_id}:{runtime_hash}"
                _append_once(
                    {
                        **dict(raw),
                        "kind": "runtime",
                        "stable_id": runtime_id,
                        "runtime_id": runtime_id,
                        "backend_id": backend_id,
                        "runtime_contract_sha256": runtime_hash,
                        "worker_sha256": worker_hash,
                        "qualification_receipt_sha256": qualification_hash,
                        "artifact_path": "scan-candidate-runtime-contracts.jsonl",
                        "candidate_only": True,
                        "promotion": False,
                    },
                    scan_seen,
                )
                scan_runtime_ids[runtime_key] = runtime_id
                scan_runtime_by_hash[runtime_hash] = runtime_id
            elif worker_hash is not None:
                existing_node = next(
                    (row for row in nodes if row.get("stable_id") == runtime_id), None
                )
                if isinstance(existing_node, dict) and existing_node.get("worker_sha256") not in {None, worker_hash}:
                    raise IncrementalDagError("scan_runtime_worker_binding_conflict")
            worker_id: str | None = None
            if worker_hash is not None:
                worker_key = (backend_id, worker_hash)
                worker_id = scan_worker_ids.get(worker_key)
                if worker_id is None:
                    worker_id = f"scan-worker:{backend_id}:{worker_hash}"
                    _append_once(
                        {
                            "kind": "worker",
                            "stable_id": worker_id,
                            "worker_id": worker_id,
                            "backend_id": backend_id,
                            "worker_sha256": worker_hash,
                            "runtime_contract_sha256": runtime_hash,
                            "input_ids": [runtime_id],
                            "artifact_path": "scan-candidate-runtime-contracts.jsonl",
                            "candidate_only": True,
                            "promotion": False,
                        },
                        scan_seen,
                    )
                    scan_worker_ids[worker_key] = worker_id
                    scan_worker_by_hash[worker_hash] = worker_id
            qualification_id: str | None = None
            if qualification_hash is not None:
                qualification_id = _register_qualification(
                    qualification_hash, backend_id, runtime_id
                )
            return runtime_id, worker_id, qualification_id

        runtime_path = _scan_sidecar(scan_candidate.get("runtime_contracts"))
        if runtime_path is not None:
            for raw in load_jsonl(runtime_path):
                if not isinstance(raw, dict):
                    raise IncrementalDagError("scan_runtime_contract_invalid")
                backend_id = _scan_backend_id(raw)
                runtime_hash = raw.get("contract_sha256", raw.get("runtime_contract_sha256"))
                if not isinstance(backend_id, str) or not isinstance(runtime_hash, str) or not HASH_RE.fullmatch(runtime_hash):
                    raise IncrementalDagError("scan_runtime_contract_binding_invalid")
                worker_hash = _scan_binding_hash(raw, "worker_sha256")
                qualification_hash = _scan_binding_hash(raw, "qualification_receipt_sha256")
                _register_runtime(backend_id, runtime_hash, worker_hash, qualification_hash, raw)

        backend_receipt_path = _scan_sidecar(scan_candidate.get("backend_receipts"))
        backend_receipt_rows: list[dict[str, Any]] = []
        if backend_receipt_path is not None:
            for raw in load_jsonl(backend_receipt_path):
                if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                    raise IncrementalDagError("scan_backend_receipt_invalid")
                page = raw.get("physical_page")
                backend_id = _scan_backend_id(raw)
                runtime_hash = raw.get("runtime_contract_sha256")
                worker_hash = _scan_binding_hash(raw, "worker_sha256")
                worker = raw.get("worker")
                if isinstance(worker, Mapping) and worker_hash is None:
                    worker_hash = _scan_binding_hash(worker, "worker_sha256")
                qualification_hash = _scan_binding_hash(raw, "qualification_receipt_sha256")
                if not isinstance(page, int) or not isinstance(backend_id, str):
                    raise IncrementalDagError("scan_backend_receipt_binding_invalid")
                if hardened_scan and (not isinstance(runtime_hash, str) or not HASH_RE.fullmatch(runtime_hash)):
                    raise IncrementalDagError("scan_backend_runtime_binding_invalid")
                runtime_id: str | None = None
                worker_id: str | None = None
                qualification_id: str | None = None
                if isinstance(runtime_hash, str) and HASH_RE.fullmatch(runtime_hash):
                    runtime_id, worker_id, qualification_id = _register_runtime(
                        backend_id, runtime_hash, worker_hash, qualification_hash, raw
                    )
                stable_id = f"scan-backend-receipt:{raw['id']}"
                scan_backend_receipt_ids[str(raw["id"])] = stable_id
                inputs = [f"physical-page-{page}"]
                if (render_id := scan_render_ids.get(page)) is not None:
                    inputs.append(render_id)
                transform_id = raw.get("coordinate_transform_id")
                if isinstance(transform_id, str) and transform_id in scan_transform_ids:
                    inputs.append(scan_transform_ids[transform_id])
                elif hardened_scan and isinstance(transform_id, str):
                    raise IncrementalDagError("scan_backend_transform_binding_invalid")
                inputs.extend(value for value in (runtime_id, worker_id, qualification_id) if value is not None)
                _append_once(
                    {
                        **raw,
                        "kind": "scan_backend_receipt",
                        "stable_id": stable_id,
                        "backend_receipt_id": stable_id,
                        "input_ids": sorted(set(inputs)),
                        "artifact_path": str(backend_receipt_path.relative_to(path.resolve()).as_posix()),
                        "candidate_only": True,
                        "promotion": False,
                    },
                    scan_seen,
                )
                backend_receipt_rows.append(raw)
        backend_receipts_by_id = {
            str(row["id"]): row
            for row in backend_receipt_rows
            if isinstance(row.get("id"), str)
        }

        if scan_structure_id:
            structure_inputs = [source_id] + [
                str(row.get("segment_id")) for row in units if row.get("segment_id")
            ]
            structure_inputs.extend(scan_render_ids.values())
            structure_inputs.extend(scan_transform_ids.values())
            structure_inputs.extend(scan_backend_receipt_ids.values())
            structure_inputs.extend(scan_runtime_by_hash.values())
            structure_inputs.extend(scan_worker_by_hash.values())
            structure_inputs.extend(scan_qualification_ids.values())
            nodes.append(
                {
                    "kind": "scan_structure",
                    "stable_id": scan_structure_id,
                    "structure_review_id": scan_structure_id,
                    "source_sha256": source_info.get("sha256") if isinstance(source_info, dict) else None,
                    "structure_review_sha256": structure_hash,
                    "status": structure_status,
                    "input_ids": sorted(set(structure_inputs)),
                    "artifact_path": "scan-candidate-structure-review",
                    "candidate_only": True,
                    "promotable": False,
                }
            )

        transcript_path = _scan_sidecar(scan_candidate.get("transcripts"))
        if transcript_path is not None:
            for raw in load_jsonl(transcript_path):
                if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                    raise IncrementalDagError("scan_transcript_row_invalid")
                transcript_id = f"scan-transcript:{raw['id']}"
                transcript_inputs = [
                    f"physical-page-{raw['physical_page']}"
                    for _ in [0]
                    if isinstance(raw.get("physical_page"), int)
                ]
                if scan_structure_id:
                    transcript_inputs.append(scan_structure_id)
                if hardened_scan:
                    page = int(raw["physical_page"])
                    if page not in scan_render_ids:
                        raise IncrementalDagError("scan_transcript_render_binding_invalid")
                    transcript_inputs.append(scan_render_ids[page])
                    receipt_id = raw.get("backend_receipt_id")
                    receipt_row = backend_receipts_by_id.get(str(receipt_id)) if isinstance(receipt_id, str) else None
                    transform_id = raw.get("coordinate_transform_id")
                    if not isinstance(transform_id, str) and isinstance(receipt_row, Mapping):
                        transform_id = receipt_row.get("coordinate_transform_id")
                    if not isinstance(transform_id, str) or transform_id not in scan_transform_ids:
                        raise IncrementalDagError("scan_transcript_transform_binding_invalid")
                    transcript_inputs.append(scan_transform_ids[transform_id])
                    if not isinstance(receipt_id, str) or receipt_id not in scan_backend_receipt_ids:
                        raise IncrementalDagError("scan_transcript_receipt_binding_invalid")
                    transcript_inputs.append(scan_backend_receipt_ids[receipt_id])
                    backend_id = _scan_backend_id(raw)
                    runtime_hash = raw.get("runtime_contract_sha256")
                    if not isinstance(runtime_hash, str) and isinstance(receipt_row, Mapping):
                        runtime_hash = receipt_row.get("runtime_contract_sha256")
                    worker_hash = _scan_binding_hash(raw, "worker_sha256")
                    if worker_hash is None and isinstance(receipt_row, Mapping):
                        worker_hash = _scan_binding_hash(receipt_row, "worker_sha256")
                    if not isinstance(backend_id, str) or not isinstance(runtime_hash, str):
                        raise IncrementalDagError("scan_transcript_runtime_binding_invalid")
                    runtime_id = scan_runtime_ids.get((backend_id, runtime_hash))
                    worker_id = scan_worker_ids.get((backend_id, worker_hash)) if worker_hash else None
                    if runtime_id is None or (worker_hash is not None and worker_id is None):
                        raise IncrementalDagError("scan_transcript_runtime_worker_binding_invalid")
                    transcript_inputs.extend(value for value in (runtime_id, worker_id) if value is not None)
                    qualification_hash = raw.get("qualification_receipt_sha256")
                    if qualification_hash is None and isinstance(receipt_row, Mapping):
                        qualification_hash = receipt_row.get("qualification_receipt_sha256")
                    if isinstance(qualification_hash, str):
                        qualification_id = scan_qualification_ids.get(qualification_hash)
                        if qualification_id is None:
                            raise IncrementalDagError("scan_transcript_qualification_binding_invalid")
                        transcript_inputs.append(qualification_id)
                nodes.append(
                    {
                        **raw,
                        "kind": "ocr_transcript",
                        "stable_id": transcript_id,
                        "transcript_id": transcript_id,
                        "input_ids": sorted(set(transcript_inputs)),
                        "artifact_path": str(transcript_path.relative_to(path.resolve()).as_posix()),
                        "candidate_only": True,
                        "promotion": False,
                    }
                )
                scan_transcript_ids.append(transcript_id)

        observation_path = _scan_sidecar(scan_candidate.get("observations"))
        if observation_path is not None:
            for raw in load_jsonl(observation_path):
                if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                    raise IncrementalDagError("scan_observation_row_invalid")
                observation_id = f"scan-observation:{raw['id']}"
                observation_inputs = [
                    f"physical-page-{raw['physical_page']}"
                    for _ in [0]
                    if isinstance(raw.get("physical_page"), int)
                ]
                transcript_span_id = raw.get("transcript_span_id")
                if isinstance(transcript_span_id, str):
                    observation_inputs.append(f"scan-transcript:{transcript_span_id}")
                if scan_structure_id:
                    observation_inputs.append(scan_structure_id)
                if hardened_scan:
                    page = int(raw["physical_page"])
                    if page not in scan_render_ids:
                        raise IncrementalDagError("scan_observation_render_binding_invalid")
                    observation_inputs.append(scan_render_ids[page])
                    receipt_id = raw.get("backend_receipt_id")
                    receipt_row = backend_receipts_by_id.get(str(receipt_id)) if isinstance(receipt_id, str) else None
                    transform_id = raw.get("coordinate_transform_id")
                    if not isinstance(transform_id, str) and isinstance(receipt_row, Mapping):
                        transform_id = receipt_row.get("coordinate_transform_id")
                    if not isinstance(transform_id, str) or transform_id not in scan_transform_ids:
                        raise IncrementalDagError("scan_observation_transform_binding_invalid")
                    observation_inputs.append(scan_transform_ids[transform_id])
                    if not isinstance(receipt_id, str) or receipt_id not in scan_backend_receipt_ids:
                        raise IncrementalDagError("scan_observation_receipt_binding_invalid")
                    observation_inputs.append(scan_backend_receipt_ids[receipt_id])
                    backend_id = _scan_backend_id(raw)
                    runtime_hash = raw.get("runtime_contract_sha256")
                    if not isinstance(runtime_hash, str) and isinstance(receipt_row, Mapping):
                        runtime_hash = receipt_row.get("runtime_contract_sha256")
                    worker_hash = _scan_binding_hash(raw, "worker_sha256")
                    if worker_hash is None and isinstance(receipt_row, Mapping):
                        worker_hash = _scan_binding_hash(receipt_row, "worker_sha256")
                    if not isinstance(backend_id, str) or not isinstance(runtime_hash, str):
                        raise IncrementalDagError("scan_observation_runtime_binding_invalid")
                    runtime_id = scan_runtime_ids.get((backend_id, runtime_hash))
                    worker_id = scan_worker_ids.get((backend_id, worker_hash)) if worker_hash else None
                    if runtime_id is None or (worker_hash is not None and worker_id is None):
                        raise IncrementalDagError("scan_observation_runtime_worker_binding_invalid")
                    observation_inputs.extend(value for value in (runtime_id, worker_id) if value is not None)
                    qualification_hash = raw.get("qualification_receipt_sha256")
                    if qualification_hash is None and isinstance(receipt_row, Mapping):
                        qualification_hash = receipt_row.get("qualification_receipt_sha256")
                    if isinstance(qualification_hash, str):
                        qualification_id = scan_qualification_ids.get(qualification_hash)
                        if qualification_id is None:
                            raise IncrementalDagError("scan_observation_qualification_binding_invalid")
                        observation_inputs.append(qualification_id)
                nodes.append(
                    {
                        **raw,
                        "kind": "ocr_observation",
                        "stable_id": observation_id,
                        "observation_id": observation_id,
                        "input_ids": sorted(set(observation_inputs)),
                        "artifact_path": str(observation_path.relative_to(path.resolve()).as_posix()),
                        "candidate_only": True,
                        "promotion": False,
                    }
                )
                scan_observation_ids.append(observation_id)

        if scan_structure_id:
            review_id = f"scan-review:{structure_hash}"
            nodes.append(
                {
                    "kind": "scan_review",
                    "stable_id": review_id,
                    "review_id": review_id,
                    "source_sha256": source_info.get("sha256") if isinstance(source_info, dict) else None,
                    "structure_review_sha256": structure_hash,
                    "status": structure_status,
                    "input_ids": sorted(
                        set(
                            [scan_structure_id]
                            + scan_transcript_ids
                            + scan_observation_ids
                            + list(scan_render_ids.values())
                            + list(scan_transform_ids.values())
                            + list(scan_backend_receipt_ids.values())
                            + list(scan_runtime_by_hash.values())
                            + list(scan_worker_by_hash.values())
                            + list(scan_qualification_ids.values())
                        )
                    ),
                    "artifact_path": "scan-candidate-structure-review",
                    "reviewer_requirement": 1,
                    "candidate_only": True,
                    "promotion": False,
                }
            )

    def page_inputs(row: dict[str, Any]) -> list[str]:
        values: set[str] = set()
        spans = row.get("evidence_spans", [])
        if isinstance(spans, list):
            for span in spans:
                if isinstance(span, dict) and isinstance(span.get("page"), int):
                    values.add(f"physical-page-{span['page']}")
        if isinstance(row.get("page"), int):
            values.add(f"physical-page-{row['page']}")
        unit_id = row.get("unit_id")
        if isinstance(unit_id, str):
            values.update(f"physical-page-{page}" for page in unit_pages.get(unit_id, set()))
        return sorted(values)

    assertion_ids: list[str] = []
    support_ids: list[str] = []
    visual_ids: list[str] = []
    session_ids: list[str] = []
    plan_id = f"review-plan:{root_id}"
    review_session_id = ""

    assertions_path = path / "semantic-assertions.jsonl"
    if assertions_path.is_file():
        for raw in load_jsonl(assertions_path):
            if not isinstance(raw, dict) or not isinstance(raw.get("assertion_id"), str):
                continue
            row = dict(raw)
            assertion_id = str(row["assertion_id"])
            assertion_ids.append(assertion_id)
            segment = row.get("unit_id")
            input_ids = page_inputs(row)
            if isinstance(segment, str):
                input_ids.append(segment)
            row.update({"kind": "semantic_assertion", "stable_id": assertion_id, "assertion_id": assertion_id, "input_ids": sorted(set(input_ids)), "artifact_path": "semantic-assertions.jsonl"})
            nodes.append(row)

    supports_path = path / "support-matrix.jsonl"
    if supports_path.is_file():
        for raw in load_jsonl(supports_path):
            if not isinstance(raw, dict) or not isinstance(raw.get("assertion_id"), str):
                continue
            row = dict(raw)
            assertion_id = str(row["assertion_id"])
            support_id = str(row.get("support_id", f"support:{assertion_id}"))
            support_ids.append(support_id)
            input_ids = page_inputs(row) + [assertion_id]
            if isinstance(row.get("unit_id"), str):
                input_ids.append(str(row["unit_id"]))
            row.update({"kind": "support", "stable_id": support_id, "support_id": support_id, "input_ids": sorted(set(input_ids)), "artifact_path": "support-matrix.jsonl"})
            nodes.append(row)

    plan_path = path / "review-plan.json"
    if plan_path.is_file():
        try:
            raw_plan = load_json(plan_path)
        except (OSError, json.JSONDecodeError):
            raw_plan = {}
        if isinstance(raw_plan, dict):
            review_session_id = str(raw_plan.get("review_session_id", f"review-session:{root_id}"))
            all_inputs = sorted(set(assertion_ids + support_ids + visual_ids + [unit_id for unit_id in unit_pages]))
            session_row = {
                **raw_plan,
                "kind": "review_session",
                "stable_id": review_session_id,
                "review_session_id": review_session_id,
                "input_ids": all_inputs,
                "artifact_path": "review-plan.json",
            }
            nodes.append(session_row)
            plan_row = {
                **raw_plan,
                "kind": "review_plan",
                "stable_id": plan_id,
                "review_plan_id": plan_id,
                "review_session_id": review_session_id,
                "input_ids": all_inputs,
                "artifact_path": "review-plan.json",
            }
            nodes.append(plan_row)

    visual_path = path / "visual-tasks.jsonl"
    if visual_path.is_file():
        for raw in load_jsonl(visual_path):
            if not isinstance(raw, dict):
                continue
            visual_id = str(raw.get("id", raw.get("visual_id", "")))
            if not visual_id:
                continue
            row = dict(raw)
            input_ids = page_inputs(row)
            for ref in row.get("proposal_refs", []) if isinstance(row.get("proposal_refs"), list) else []:
                if isinstance(ref, str):
                    input_ids.append(ref.split(":", 1)[0])
            row.update({"kind": "visual_object", "stable_id": visual_id, "object_id": visual_id, "input_ids": sorted(set(input_ids)), "artifact_path": "visual-tasks.jsonl"})
            visual_ids.append(visual_id)
            nodes.append(row)

    visual_queue_path = path / "visual-review-queue.jsonl"
    if visual_queue_path.is_file():
        for raw in load_jsonl(visual_queue_path):
            if not isinstance(raw, dict) or not isinstance(raw.get("page"), int):
                continue
            row = dict(raw)
            session = f"visual-review:{raw['page']}"
            input_ids = [f"physical-page-{raw['page']}"] + [str(value) for value in raw.get("visual_task_ids", []) if isinstance(value, str)]
            for ref in raw.get("proposal_refs", []) if isinstance(raw.get("proposal_refs"), list) else []:
                if isinstance(ref, str):
                    input_ids.append(ref.split(":", 1)[0])
            row.update({"kind": "review_session", "stable_id": session, "review_session_id": session, "review_plan_id": plan_id, "input_ids": sorted(set(input_ids)), "artifact_path": "visual-review-queue.jsonl"})
            nodes.append(row)
            session_ids.append(session)

    review_queue_path = path / "review-queue.jsonl"
    if review_queue_path.is_file():
        for index, raw in enumerate(load_jsonl(review_queue_path)):
            if not isinstance(raw, dict):
                continue
            unit_id = str(raw.get("unit_id", index))
            session = f"semantic-review:{unit_id}:{index}"
            row = dict(raw)
            input_ids = [str(value) for value in raw.get("assertion_ids", []) if isinstance(value, str)]
            input_ids.extend(f"support:{value}" for value in input_ids if value in assertion_ids)
            input_ids.extend(str(value) for value in raw.get("visual_task_ids", []) if isinstance(value, str))
            input_ids.extend(unit_pages.get(unit_id, set()) and [f"physical-page-{page}" for page in sorted(unit_pages[unit_id])] or [])
            row.update({"kind": "review_session", "stable_id": session, "review_session_id": session, "review_plan_id": plan_id, "input_ids": sorted(set(input_ids)), "artifact_path": "review-queue.jsonl"})
            nodes.append(row)
            session_ids.append(session)

    for relative in ("review-attestations.jsonl", "review-records.jsonl"):
        file = path / relative
        if not file.is_file():
            continue
        for index, raw in enumerate(load_jsonl(file)):
            if not isinstance(raw, dict):
                continue
            attestation_id = str(raw.get("attestation_id", raw.get("review_id", f"{relative}:{index}")))
            input_ids = [str(value) for key in ("assertion_ids", "support_ids", "visual_task_ids") for value in (raw.get(key, []) if isinstance(raw.get(key, []), list) else [raw.get(key)]) if isinstance(value, str)]
            input_ids.extend(session_ids)
            row = {**raw, "kind": "review_attestation", "stable_id": attestation_id, "attestation_id": attestation_id, "review_plan_id": plan_id, "input_ids": sorted(set(input_ids)), "artifact_path": relative}
            nodes.append(row)

    # The review plan/session may be parsed before visual queues.  Rebind both
    # to the complete product set now that every exact visual/assertion/support
    # identity is known; this conservative fan-out is required when a legacy
    # workpack only exposes bundle hashes instead of per-row IDs.
    all_review_inputs = sorted(set(assertion_ids + support_ids + visual_ids + [unit_id for unit_id in unit_pages]))
    for row in nodes:
        if row.get("kind") in {"review_plan", "review_session", "review_attestation"}:
            row["input_ids"] = sorted(set(row.get("input_ids", [])) | set(all_review_inputs))

    # A workpack has no module node.  Product -> package binding keeps every
    # typed product in the aggregate closure and makes product changes require
    # package repackage; root policy edits do not rewrite upstream products.
    for row in nodes[1:]:
        row.setdefault("package_id", root_id)
    return _explicit_snapshot({"nodes": nodes, "edges": []})[0]


def _load_dag_directory(path: Path) -> dict[str, Any]:
    manifest = load_json(path / "dag-manifest.json")
    if manifest.get("schema_version") != INCREMENTAL_DAG_MANIFEST_SCHEMA:
        raise IncrementalDagError("dag_manifest_schema_invalid")
    nodes = load_jsonl(path / "nodes.jsonl")
    edges = load_jsonl(path / "edges.jsonl")
    actual_nodes_hash = sha256_json(nodes)
    actual_edges_hash = sha256_json(edges)
    if manifest.get("nodes_sha256") != actual_nodes_hash or manifest.get("edges_sha256") != actual_edges_hash:
        raise IncrementalDagError("dag_manifest_hash_mismatch")
    graph, _ = _explicit_snapshot({"nodes": nodes, "edges": edges})
    if manifest.get("dag_sha256") != _dag_hash(graph):
        raise IncrementalDagError("dag_hash_mismatch")
    return graph


def _dag_hash(graph: dict[str, Any]) -> str:
    return sha256_json({"nodes": sorted(graph["nodes"].values(), key=lambda row: row["node_id"]), "edges": graph["edges"]})


def _snapshot_fingerprint(graph: dict[str, Any]) -> str:
    return _dag_hash(graph)


def _change_reason(old: dict[str, Any] | None, new: dict[str, Any] | None) -> str:
    row = new or old or {}
    attrs = row.get("attributes", {}) if isinstance(row, dict) else {}
    explicit = attrs.get("change_kind", attrs.get("change_tag"))
    if isinstance(explicit, str) and explicit:
        return explicit
    kind = row.get("kind") if isinstance(row, dict) else "unknown"
    if old is None:
        return "node_added"
    if new is None:
        return "node_deleted"
    mappings = {
        "source": "source_identity_changed",
        "page": "page_text_or_structure_changed",
        "segment": "segment_structure_changed",
        "render": "render_changed",
        "parser": "parser_changed",
        "model": "model_changed",
        "config": "configuration_changed",
        "locator": "heading_locator_changed",
        "visual_object": "visual_object_changed",
        "visual_relation": "visual_relation_changed",
        "table_grid": "table_grid_changed",
        "semantic_assertion": "semantic_assertion_changed",
        "support": "support_changed",
        "review_plan": "review_plan_changed",
        "review_session": "review_session_changed",
        "review_attestation": "review_attestation_changed",
        "composer": "composer_changed",
        "alignment_candidate": "alignment_candidate_changed",
        "semantic_difference": "semantic_difference_changed",
        "composer_conflict": "composer_conflict_changed",
        "unit_map": "unit_map_changed",
        "composer_review_plan": "composer_review_plan_changed",
        "composer_review": "composer_review_changed",
        "composer_receipt": "composer_receipt_changed",
        "formula_ast": "formula_ast_changed",
        "formula_test": "formula_test_changed",
        "test": "competency_test_changed",
        "competency": "competency_changed",
        "runtime": "runtime_changed",
        "runtime_qualification": "runtime_qualification_changed",
        "runtime_result": "runtime_result_changed",
        "ocr_transcript": "ocr_transcript_changed",
        "scan_structure": "scan_structure_changed",
        "scan_review": "scan_review_changed",
        "scan_transform": "scan_transform_changed",
        "scan_backend_receipt": "scan_backend_receipt_changed",
        "visual_benchmark": "visual_benchmark_changed",
        "visual_gold": "visual_gold_changed",
        "visual_mode_result": "visual_mode_result_changed",
        "visual_evaluation": "visual_evaluation_changed",
        "cost_ledger": "cost_ledger_changed",
        "seal": "seal_changed",
        "host_certificate": "host_certificate_changed",
        "composite": "composite_changed",
        "module": "module_changed",
        "package": "package_changed",
    }
    return mappings.get(str(kind), "artifact_changed")


def _identity_violations(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    if old.get("semantic_identity_hash") != new.get("semantic_identity_hash"):
        violations.append({"code": "stable_id_semantic_drift", "node_id": new["node_id"]})
    if old.get("evidence_identity_hash") != new.get("evidence_identity_hash"):
        violations.append({"code": "stable_id_evidence_drift", "node_id": new["node_id"]})
    if old.get("protocol_id") != new.get("protocol_id"):
        violations.append({"code": "protocol_identity_changed", "node_id": new["node_id"]})
    return violations


def _reviewer_violations(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    old_count = old.get("reviewer_requirement")
    new_count = new.get("reviewer_requirement")
    if isinstance(old_count, int) and isinstance(new_count, int) and new_count < old_count:
        return [{"code": "reviewer_requirement_narrowed", "node_id": new["node_id"]}]
    return []


def _capability_violations(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    old_caps = old.get("capabilities", {})
    new_caps = new.get("capabilities", {})
    if not isinstance(old_caps, dict) or not isinstance(new_caps, dict):
        return []
    rows = []
    for key in sorted(set(old_caps) | set(new_caps)):
        if old_caps.get(key) is False and new_caps.get(key) is True:
            rows.append({"code": "capability_escalation", "node_id": new["node_id"], "capability": key})
    return rows


def _edge_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, list[str]]:
    old_ids = {edge["edge_id"] for edge in old["edges"]}
    new_ids = {edge["edge_id"] for edge in new["edges"]}
    return {
        "added": sorted(new_ids - old_ids),
        "removed": sorted(old_ids - new_ids),
        "unchanged": sorted(old_ids & new_ids),
    }


def _adjacency(graph: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    for edge in graph["edges"]:
        result[edge["from"]].append(edge["to"])
    for key in result:
        result[key] = sorted(set(result[key]))
    return result


def _paths_to_roots(
    target: str,
    roots: set[str],
    reverse: dict[str, list[str]],
) -> list[list[str]]:
    paths: list[list[str]] = []
    queue: deque[tuple[str, list[str]]] = deque([(target, [target])])
    visited: set[tuple[str, ...]] = set()
    while queue and len(paths) < 32:
        current, path = queue.popleft()
        if current in roots:
            paths.append(list(reversed(path)))
            continue
        for parent in sorted(reverse.get(current, [])):
            if parent in path:
                continue
            candidate = tuple([parent, *path])
            if candidate in visited:
                continue
            visited.add(candidate)
            queue.append((parent, [parent, *path]))
    return sorted(paths)


def _legacy_package_diff(old_input: Path | dict[str, Any], new_input: Path | dict[str, Any], generated_at: str) -> dict[str, Any] | None:
    if not isinstance(old_input, Path) or not isinstance(new_input, Path):
        return None
    if not (old_input.is_dir() and new_input.is_dir() and (old_input / "manifest.json").is_file() and (new_input / "manifest.json").is_file()):
        return None
    try:
        from diff_expert_skills import build_report

        return build_report(old_input, new_input, generated_at=generated_at)
    except Exception as error:  # compatibility report is advisory; DAG remains authoritative
        return {"schema_version": "tkc.incremental-update-report/v0.1", "error": str(error)}


def compare_graphs(
    old: dict[str, Any],
    new: dict[str, Any],
    *,
    generated_at: str = DEFAULT_TIMESTAMP,
    legacy_diff: dict[str, Any] | None = None,
) -> dict[str, Any]:
    generated_at = _timestamp(generated_at)
    _validate_graph(old["nodes"], old["edges"])
    _validate_graph(new["nodes"], new["edges"])
    old_nodes = old["nodes"]
    new_nodes = new["nodes"]
    old_ids = set(old_nodes)
    new_ids = set(new_nodes)
    common = old_ids & new_ids
    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)
    changed = sorted(
        node_id
        for node_id in common
        if old_nodes[node_id].get("content_hash") != new_nodes[node_id].get("content_hash")
        or old_nodes[node_id].get("protocol_id") != new_nodes[node_id].get("protocol_id")
        or old_nodes[node_id].get("input_hashes") != new_nodes[node_id].get("input_hashes")
        or old_nodes[node_id].get("reviewer_requirement") != new_nodes[node_id].get("reviewer_requirement")
        or old_nodes[node_id].get("capabilities") != new_nodes[node_id].get("capabilities")
    )
    unchanged = sorted(common - set(changed))
    edge_changes = _edge_diff(old, new)
    edge_by_id_old = {edge["edge_id"]: edge for edge in old["edges"]}
    edge_by_id_new = {edge["edge_id"]: edge for edge in new["edges"]}
    edge_roots: set[str] = set()
    for edge_id in edge_changes["added"] + edge_changes["removed"]:
        edge = edge_by_id_new.get(edge_id, edge_by_id_old.get(edge_id))
        if edge is None:
            continue
        # Both endpoints participate in the frozen dependency contract.  The
        # downstream endpoint drives the usual prerequisite closure, while
        # invalidating the upstream endpoint prevents a changed relationship
        # from being hidden behind an otherwise unchanged producer.
        for endpoint in (edge.get("from"), edge.get("to")):
            if endpoint in old_nodes or endpoint in new_nodes:
                edge_roots.add(str(endpoint))
    roots = set(added) | set(removed) | set(changed) | edge_roots
    root_rows: list[dict[str, Any]] = []
    safety: list[dict[str, Any]] = []
    for node_id in sorted(roots):
        old_node = old_nodes.get(node_id)
        new_node = new_nodes.get(node_id)
        row = new_node or old_node
        assert row is not None
        reason = "typed_edge_changed" if node_id in edge_roots and node_id not in added and node_id not in removed and node_id not in changed else _change_reason(old_node, new_node)
        violations: list[dict[str, Any]] = []
        if new_node is None and old_node is not None and old_node.get("kind") in {"source", "receipt"}:
            violations.append({"code": "original_node_missing", "node_id": node_id})
        if old_node is not None and new_node is not None:
            violations.extend(_identity_violations(old_node, new_node))
            violations.extend(_reviewer_violations(old_node, new_node))
            violations.extend(_capability_violations(old_node, new_node))
        for violation in violations:
            safety.append({"root_node_id": node_id, **violation, "reason": reason})
        root_rows.append(
            {
                "node_id": node_id,
                "kind": row.get("kind", "unknown"),
                "change_type": "added" if node_id in added else "removed" if node_id in removed else "changed",
                "reason": reason,
                "identity_violations": violations,
            }
        )
    for node_id in sorted(common):
        if node_id not in roots:
            safety.extend(
                {"root_node_id": node_id, **violation, "reason": "unchanged_identity_check"}
                for violation in _reviewer_violations(old_nodes[node_id], new_nodes[node_id])
            )
    old_reverse: dict[str, list[str]] = defaultdict(list)
    new_reverse: dict[str, list[str]] = defaultdict(list)
    for edge in old["edges"]:
        old_reverse[edge["to"]].append(edge["from"])
    for edge in new["edges"]:
        new_reverse[edge["to"]].append(edge["from"])
    adjacency = _adjacency(new)
    union_nodes = sorted(old_ids | new_ids)
    affected: dict[str, dict[str, Any]] = {}
    queue: deque[tuple[str, str, list[str]]] = deque((root, root, [root]) for root in sorted(roots))
    seen_paths: set[tuple[str, ...]] = set()
    while queue:
        root, current, path = queue.popleft()
        if tuple(path) in seen_paths:
            continue
        seen_paths.add(tuple(path))
        row = new_nodes.get(current, old_nodes.get(current))
        if row is None:
            continue
        entry = affected.setdefault(
            current,
            {
                "node_id": current,
                "kind": row.get("kind", "unknown"),
                "root_causes": [],
                "paths": [],
                "required_actions": set(),
                "final_gate": KIND_GATES.get(row.get("kind", "unknown"), "unknown-node"),
            },
        )
        root_reason = next((item["reason"] for item in root_rows if item["node_id"] == root), "node_changed")
        entry["root_causes"].append({"node_id": root, "reason": root_reason})
        entry["paths"].append(path)
        for action in KIND_ACTIONS.get(row.get("kind", "unknown"), ("rebuild",)):
            entry["required_actions"].add(action)
        for target in adjacency.get(current, []):
            if target not in path:
                queue.append((root, target, [*path, target]))
    # Removed nodes have no new adjacency; old downstream dependants still need
    # invalidation, so walk the old graph from every removed root as well.
    old_adjacency = _adjacency(old)
    for root in sorted(removed):
        queue = deque([(root, root, [root])])
        while queue:
            root_id, current, path = queue.popleft()
            row = old_nodes.get(current)
            if row is None:
                continue
            entry = affected.setdefault(
                current,
                {
                    "node_id": current,
                    "kind": row.get("kind", "unknown"),
                    "root_causes": [],
                    "paths": [],
                    "required_actions": set(),
                    "final_gate": KIND_GATES.get(row.get("kind", "unknown"), "unknown-node"),
                },
            )
            entry["root_causes"].append({"node_id": root_id, "reason": "node_deleted"})
            entry["paths"].append(path)
            entry["required_actions"].update(KIND_ACTIONS.get(row.get("kind", "unknown"), ("rebuild",)))
            for target in old_adjacency.get(current, []):
                if target not in path:
                    queue.append((root_id, target, [*path, target]))
    invalidations: list[dict[str, Any]] = []
    for node_id in sorted(affected):
        entry = affected[node_id]
        action_list = [action for action in ACTION_ORDER if action in entry["required_actions"]]
        causes = sorted(
            {json.dumps(item, ensure_ascii=False, sort_keys=True): item for item in entry["root_causes"]}.values(),
            key=lambda item: (item["node_id"], item["reason"]),
        )
        paths = sorted({tuple(path) for path in entry["paths"]})
        invalidations.append(
            {
                "invalidation_id": f"invalidation-{sha256_json([node_id, causes, paths, action_list])[:32]}",
                "node_id": node_id,
                "kind": entry["kind"],
                "root_causes": causes,
                "paths": [list(path) for path in paths],
                "required_actions": action_list,
                "final_action": action_list[-1] if action_list else "rebuild",
                "final_gate": entry["final_gate"],
                "status": "invalidated",
            }
        )
    affected_ids = set(affected)
    reuse: list[dict[str, Any]] = []
    for node_id in sorted(new_ids):
        row = new_nodes[node_id]
        eligible = node_id in unchanged and node_id not in affected_ids
        reasons: list[str] = []
        if node_id in affected_ids:
            reasons.append("dependency_invalidated")
        if node_id not in unchanged:
            reasons.append("node_changed")
        if any(violation.get("node_id") == node_id for violation in safety):
            eligible = False
            reasons.append("fail_closed_identity_or_policy")
        missing_receipts = sorted(set(row.get("required_receipt_ids", [])) - set(row.get("receipt_ids", [])))
        if missing_receipts:
            eligible = False
            reasons.append("required_receipt_missing")
            safety.append({"root_node_id": node_id, "node_id": node_id, "code": "required_receipt_missing", "receipt_ids": missing_receipts})
        reuse.append(
            {
                "node_id": node_id,
                "eligible": eligible,
                "reason_codes": sorted(set(reasons)) or ["inputs_protocol_dependencies_unchanged"],
                "reuse": "reuse" if eligible else "rebuild",
            }
        )
    invalidations.sort(key=lambda row: row["node_id"])
    action_nodes: dict[str, list[str]] = defaultdict(list)
    for row in invalidations:
        for action in row["required_actions"]:
            action_nodes[action].append(row["node_id"])
    action_plan: list[dict[str, Any]] = []
    for action in ACTION_ORDER:
        node_ids = sorted(set(action_nodes.get(action, [])))
        if not node_ids:
            continue
        gates = sorted({row["final_gate"] for row in invalidations if action in row["required_actions"]})
        paused = action in PAUSE_ACTIONS or any(gate in PAUSE_GATES for gate in gates)
        action_plan.append(
            {
                "action": action,
                "node_ids": node_ids,
                "gates": gates,
                "execution": "paused" if paused else "allowed-local-deterministic",
                "reason": "typed-dependency-closure",
            }
        )
    safety = sorted(
        {json.dumps(row, ensure_ascii=False, sort_keys=True): row for row in safety}.values(),
        key=lambda row: (str(row.get("code")), str(row.get("node_id", row.get("root_node_id", ""))))
    )
    return {
        "schema_version": INCREMENTAL_CHANGE_SET_SCHEMA,
        "compiler_version": INCREMENTAL_BUILD_DAG_COMPILER_VERSION,
        "protocol": INCREMENTAL_BUILD_DAG_PROTOCOL,
        "generated_at": generated_at,
        "old_dag_sha256": _dag_hash(old),
        "new_dag_sha256": _dag_hash(new),
        "nodes": {
            "added": added,
            "changed": changed,
            "removed": removed,
            "unchanged": unchanged,
        },
        "edges": edge_changes,
        "root_causes": root_rows,
        "invalidations": invalidations,
        "reuse": reuse,
        "action_plan": action_plan,
        "safety_violations": safety,
        "fail_closed": bool(safety),
        "legacy_diff_report": legacy_diff,
    }


def build_manifest(graph: dict[str, Any], *, generated_at: str = DEFAULT_TIMESTAMP, source_label: str = "snapshot") -> dict[str, Any]:
    generated_at = _timestamp(generated_at)
    nodes = sorted(graph["nodes"].values(), key=lambda row: row["node_id"])
    edges = sorted(graph["edges"], key=lambda row: row["edge_id"])
    return {
        "schema_version": INCREMENTAL_DAG_MANIFEST_SCHEMA,
        "compiler_version": INCREMENTAL_BUILD_DAG_COMPILER_VERSION,
        "protocol": INCREMENTAL_BUILD_DAG_PROTOCOL,
        "generated_at": generated_at,
        "source_label": source_label,
        "immutable_input": True,
        "hash_algorithm": "sha256-canonical-json-v1",
        "dag_sha256": _dag_hash({"nodes": {row["node_id"]: row for row in nodes}, "edges": edges}),
        "nodes_sha256": sha256_json(nodes),
        "edges_sha256": sha256_json(edges),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "node_kind_counts": {
            kind: sum(1 for row in nodes if row.get("kind") == kind)
            for kind in sorted({row.get("kind", "unknown") for row in nodes})
        },
        "policy": {
            "network_allowed": False,
            "model_launch_allowed": False,
            "ocr_invocation_allowed": False,
            "review_authoring_allowed": False,
            "competency_authoring_allowed": False,
            "capability_escalation_allowed": False,
            "sealed_input_mutation_allowed": False,
            "auto_seal_allowed": False,
            "auto_execution_allowed": False,
            "auto_signing_allowed": False,
            "whole_book_complete_claim_allowed": False,
        },
    }


def build_dag(
    snapshot: Path | dict[str, Any],
    output: Path | None = None,
    *,
    generated_at: str = DEFAULT_TIMESTAMP,
) -> dict[str, Any]:
    graph = _load_snapshot_input(snapshot)
    manifest = build_manifest(graph, generated_at=generated_at, source_label=str(snapshot) if isinstance(snapshot, Path) else "inline-snapshot")
    result = {"manifest": manifest, "nodes": sorted(graph["nodes"].values(), key=lambda row: row["node_id"]), "edges": sorted(graph["edges"], key=lambda row: row["edge_id"])}
    if output is not None:
        _write_dag_directory(output, result)
    return result


def _write_dag_directory(output: Path, result: dict[str, Any]) -> None:
    output = output.expanduser().resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise IncrementalDagError("output_not_empty")
    output.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output / "nodes.jsonl", result["nodes"])
    _write_jsonl(output / "edges.jsonl", result["edges"])
    _write_json(output / "dag-manifest.json", result["manifest"])
    state = {
        "schema_version": INCREMENTAL_BUILD_STATE_SCHEMA,
        "compiler_version": INCREMENTAL_BUILD_DAG_COMPILER_VERSION,
        "protocol": INCREMENTAL_BUILD_DAG_PROTOCOL,
        "state": "built",
        "dag_sha256": result["manifest"]["dag_sha256"],
        "plan_sha256": None,
        "completed_actions": [],
        "paused_gates": [],
        "receipt_ids": [],
        "receipts_sha256": sha256_json([]),
        "policy": result["manifest"]["policy"],
    }
    state["state_sha256"] = sha256_json({key: value for key, value in state.items() if key != "state_sha256"})
    _write_json(output / "state.json", state)
    _write_jsonl(output / "receipts.jsonl", [])


def compare_snapshots(
    old_snapshot: Path | dict[str, Any],
    new_snapshot: Path | dict[str, Any],
    output: Path | None = None,
    *,
    generated_at: str = DEFAULT_TIMESTAMP,
) -> dict[str, Any]:
    old = _load_snapshot_input(old_snapshot)
    new = _load_snapshot_input(new_snapshot)
    legacy = _legacy_package_diff(old_snapshot if isinstance(old_snapshot, Path) else None, new_snapshot if isinstance(new_snapshot, Path) else None, _timestamp(generated_at))
    changes = compare_graphs(old, new, generated_at=generated_at, legacy_diff=legacy)
    manifest = build_manifest(new, generated_at=generated_at, source_label=str(new_snapshot) if isinstance(new_snapshot, Path) else "inline-snapshot")
    plan = {
        "schema_version": INCREMENTAL_BUILD_PLAN_SCHEMA,
        "compiler_version": INCREMENTAL_BUILD_DAG_COMPILER_VERSION,
        "protocol": INCREMENTAL_BUILD_DAG_PROTOCOL,
        "generated_at": _timestamp(generated_at),
        "old_dag_sha256": changes["old_dag_sha256"],
        "new_dag_sha256": changes["new_dag_sha256"],
        "required_actions": changes["action_plan"],
        "invalidations": changes["invalidations"],
        "reuse": changes["reuse"],
        "safety_violations": changes["safety_violations"],
        "fail_closed": changes["fail_closed"],
        "pause_only": True,
        "policy": build_manifest(new, generated_at=generated_at)["policy"],
    }
    result = {
        "manifest": manifest,
        "nodes": sorted(new["nodes"].values(), key=lambda row: row["node_id"]),
        "edges": sorted(new["edges"], key=lambda row: row["edge_id"]),
        "changes": changes,
        "plan": plan,
    }
    if output is not None:
        _write_compare_directory(output, result)
    return result


def _write_compare_directory(output: Path, result: dict[str, Any]) -> None:
    output = output.expanduser().resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise IncrementalDagError("output_not_empty")
    output.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output / "nodes.jsonl", result["nodes"])
    _write_jsonl(output / "edges.jsonl", result["edges"])
    _write_json(output / "dag-manifest.json", result["manifest"])
    _write_json(output / "change-set.json", result["changes"])
    _write_json(output / "invalidation-plan.json", result["plan"])
    state = {
        "schema_version": INCREMENTAL_BUILD_STATE_SCHEMA,
        "compiler_version": INCREMENTAL_BUILD_DAG_COMPILER_VERSION,
        "protocol": INCREMENTAL_BUILD_DAG_PROTOCOL,
        "state": "paused" if result["plan"]["required_actions"] or result["plan"]["fail_closed"] else "no-op",
        "dag_sha256": result["manifest"]["dag_sha256"],
        "plan_sha256": sha256_json(result["plan"]),
        "completed_actions": [],
        "paused_gates": sorted({gate for action in result["plan"]["required_actions"] for gate in action["gates"]}),
        "receipt_ids": [],
        "receipts_sha256": sha256_json([]),
        "policy": result["plan"]["policy"],
    }
    state["state_sha256"] = sha256_json({key: value for key, value in state.items() if key != "state_sha256"})
    _write_json(output / "state.json", state)
    _write_jsonl(output / "receipts.jsonl", [])


def _state_hash(state: dict[str, Any]) -> str:
    return sha256_json({key: value for key, value in state.items() if key != "state_sha256"})


def _receipts_hash(receipts: list[dict[str, Any]]) -> str:
    return sha256_json(sorted(receipts, key=lambda row: str(row.get("receipt_id", ""))))


def validate_dag_directory(path: Path) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    path = path.expanduser().resolve()
    required = ("dag-manifest.json", "nodes.jsonl", "edges.jsonl", "state.json", "receipts.jsonl")
    if not path.is_dir():
        return [{"severity": "error", "code": "dag_directory_missing", "path": str(path)}]
    if any(not (path / item).is_file() for item in required):
        return [{"severity": "error", "code": "dag_artifact_missing", "path": str(path)}]
    try:
        manifest = load_json(path / "dag-manifest.json")
        nodes = load_jsonl(path / "nodes.jsonl")
        edges = load_jsonl(path / "edges.jsonl")
        state = load_json(path / "state.json")
        if any(
            not isinstance(row, dict)
            or row.get("schema_version") != INCREMENTAL_DAG_NODE_SCHEMA
            or not isinstance(row.get("node_id"), str)
            or not isinstance(row.get("kind"), str)
            or not isinstance(row.get("stable_key"), str)
            or not isinstance(row.get("content_hash"), str)
            or not isinstance(row.get("protocol_id"), str)
            for row in nodes
        ):
            raise IncrementalDagError("dag_node_schema_invalid")
        if any(
            not isinstance(row, dict)
            or row.get("schema_version") != INCREMENTAL_DAG_EDGE_SCHEMA
            or not isinstance(row.get("edge_id"), str)
            or not isinstance(row.get("type"), str)
            or not isinstance(row.get("from"), str)
            or not isinstance(row.get("to"), str)
            for row in edges
        ):
            raise IncrementalDagError("dag_edge_schema_invalid")
        graph, _ = _explicit_snapshot({"nodes": nodes, "edges": edges})
        if manifest.get("schema_version") != INCREMENTAL_DAG_MANIFEST_SCHEMA:
            raise IncrementalDagError("dag_manifest_schema_invalid")
        if manifest.get("compiler_version") != INCREMENTAL_BUILD_DAG_COMPILER_VERSION or manifest.get("protocol") != INCREMENTAL_BUILD_DAG_PROTOCOL:
            raise IncrementalDagError("dag_manifest_protocol_invalid")
        required_false_policy = {
            "network_allowed",
            "model_launch_allowed",
            "ocr_invocation_allowed",
            "review_authoring_allowed",
            "competency_authoring_allowed",
            "capability_escalation_allowed",
            "sealed_input_mutation_allowed",
            "auto_seal_allowed",
            "auto_execution_allowed",
            "auto_signing_allowed",
            "whole_book_complete_claim_allowed",
        }
        policy = manifest.get("policy")
        if not isinstance(policy, dict) or any(policy.get(key) is not False for key in required_false_policy):
            raise IncrementalDagError("policy_gate_weakened")
        # Check the state receipt/DAG binding before structural manifest
        # counts and row hashes.  If an attacker recomputes a shortened
        # manifest, the immutable state boundary is the precise first failure
        # (state_dag_mismatch), rather than a less-specific count mismatch.
        if state.get("state_sha256") != _state_hash(state):
            raise IncrementalDagError("state_hash_mismatch")
        if state.get("schema_version") != INCREMENTAL_BUILD_STATE_SCHEMA or state.get("compiler_version") != INCREMENTAL_BUILD_DAG_COMPILER_VERSION or state.get("protocol") != INCREMENTAL_BUILD_DAG_PROTOCOL:
            raise IncrementalDagError("state_protocol_invalid")
        if state.get("dag_sha256") != manifest.get("dag_sha256"):
            raise IncrementalDagError("state_dag_mismatch")
        if state.get("policy") != policy:
            raise IncrementalDagError("state_policy_mismatch")
        if manifest.get("node_count") != len(nodes) or manifest.get("edge_count") != len(edges):
            raise IncrementalDagError("dag_count_mismatch")
        if manifest.get("immutable_input") is not True:
            raise IncrementalDagError("immutable_input_required")
        if manifest.get("nodes_sha256") != sha256_json(sorted(nodes, key=lambda row: row.get("node_id", ""))):
            raise IncrementalDagError("nodes_hash_mismatch")
        if manifest.get("edges_sha256") != sha256_json(sorted(edges, key=lambda row: row.get("edge_id", ""))):
            raise IncrementalDagError("edges_hash_mismatch")
        if manifest.get("dag_sha256") != _dag_hash(graph):
            raise IncrementalDagError("dag_hash_mismatch")
        if (path / "change-set.json").is_file():
            changes = load_json(path / "change-set.json")
            if changes.get("schema_version") != INCREMENTAL_CHANGE_SET_SCHEMA or changes.get("compiler_version") != INCREMENTAL_BUILD_DAG_COMPILER_VERSION or changes.get("protocol") != INCREMENTAL_BUILD_DAG_PROTOCOL:
                raise IncrementalDagError("change_set_schema_invalid")
            if changes.get("new_dag_sha256") != manifest.get("dag_sha256"):
                raise IncrementalDagError("change_set_dag_mismatch")
        if (path / "invalidation-plan.json").is_file():
            plan = load_json(path / "invalidation-plan.json")
            if plan.get("schema_version") != INCREMENTAL_BUILD_PLAN_SCHEMA or plan.get("compiler_version") != INCREMENTAL_BUILD_DAG_COMPILER_VERSION or plan.get("protocol") != INCREMENTAL_BUILD_DAG_PROTOCOL:
                raise IncrementalDagError("build_plan_schema_invalid")
            if state.get("plan_sha256") != sha256_json(plan):
                raise IncrementalDagError("state_plan_mismatch")
            if plan.get("new_dag_sha256") != manifest.get("dag_sha256") or plan.get("policy") != policy:
                raise IncrementalDagError("build_plan_binding_mismatch")
        receipts = load_jsonl(path / "receipts.jsonl")
        receipt_ids: set[str] = set()
        if state.get("receipt_ids") != sorted(str(row.get("receipt_id")) for row in receipts if isinstance(row, dict)):
            raise IncrementalDagError("state_receipt_ids_mismatch")
        if state.get("receipts_sha256") != _receipts_hash([row for row in receipts if isinstance(row, dict)]):
            raise IncrementalDagError("state_receipts_binding_mismatch")
        declared_receipts = {
            str(receipt_id)
            for node in nodes
            if isinstance(node, dict)
            # receipt_ids are source-package evidence bindings and are
            # validated against artifact/file aliases by _explicit_snapshot.
            # Only required_receipt_ids name Phase 7B runtime receipts that
            # must be present in this directory's receipts.jsonl.
            for receipt_id in list(node.get("required_receipt_ids", []))
            if isinstance(receipt_id, (str, int))
        }
        if declared_receipts - receipt_ids:
            raise IncrementalDagError("required_receipt_missing")
        plan = load_json(path / "invalidation-plan.json") if (path / "invalidation-plan.json").is_file() else None
        allowed_actions: dict[str, set[str]] = {}
        allowed_nodes: set[str] = set(graph["nodes"])
        if isinstance(plan, dict):
            for action_row in plan.get("required_actions", []):
                if isinstance(action_row, dict) and isinstance(action_row.get("action"), str):
                    allowed_actions[action_row["action"]] = set(action_row.get("node_ids", []))
        for row in receipts:
            receipt_id = row.get("receipt_id") if isinstance(row, dict) else None
            if not isinstance(receipt_id, str) or receipt_id in receipt_ids:
                raise IncrementalDagError("receipt_replay_or_duplicate")
            receipt_ids.add(receipt_id)
            if row.get("schema_version") != INCREMENTAL_BUILD_RECEIPT_SCHEMA or row.get("compiler_version") != INCREMENTAL_BUILD_DAG_COMPILER_VERSION or row.get("protocol") != INCREMENTAL_BUILD_DAG_PROTOCOL:
                raise IncrementalDagError("receipt_protocol_invalid")
            action = row.get("action")
            node_ids = row.get("node_ids")
            if isinstance(plan, dict):
                if action not in allowed_actions:
                    raise IncrementalDagError("receipt_action_not_in_plan")
                if sorted(node_ids or []) != sorted(allowed_actions[action]):
                    raise IncrementalDagError("receipt_node_binding_mismatch")
                if row.get("input_dag_sha256") != plan.get("new_dag_sha256"):
                    raise IncrementalDagError("receipt_input_binding_mismatch")
            if not isinstance(node_ids, list) or any(node_id not in allowed_nodes for node_id in node_ids):
                raise IncrementalDagError("receipt_node_unknown")
            if not isinstance(row.get("output_hashes"), dict):
                raise IncrementalDagError("receipt_output_binding_invalid")
            if row.get("status") != "prepared-not-executed" or row.get("execution") is not False or row.get("review_authored") is not False or row.get("sealed") is not False or row.get("executable") is not False or row.get("output_hashes") != {}:
                raise IncrementalDagError("false_completion_receipt")
            declared = row.get("receipt_hash")
            if declared != sha256_json({key: value for key, value in row.items() if key != "receipt_hash"}):
                raise IncrementalDagError("receipt_hash_mismatch")
    except (OSError, json.JSONDecodeError, KeyError, TypeError, IncrementalDagError) as error:
        issues.append({"severity": "error", "code": str(error), "path": str(path)})
    return issues


def resume_plan(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    issues = validate_dag_directory(path)
    if issues:
        raise IncrementalDagError(issues[0]["code"])
    state = load_json(path / "state.json")
    plan = load_json(path / "invalidation-plan.json") if (path / "invalidation-plan.json").is_file() else None
    if not isinstance(plan, dict):
        return {"state": state.get("state", "built"), "completed_actions": state.get("completed_actions", []), "paused": False, "pause_reason": None}
    receipts = load_jsonl(path / "receipts.jsonl") if (path / "receipts.jsonl").is_file() else []
    if plan.get("fail_closed"):
        state["state"] = "paused"
        state["completed_actions"] = []
        state["paused_gates"] = ["safety-violation"]
        state["pause_reason"] = "resolve_safety_violation"
        state["receipt_ids"] = sorted(str(row.get("receipt_id")) for row in receipts if isinstance(row, dict))
        state["receipts_sha256"] = _receipts_hash([row for row in receipts if isinstance(row, dict)])
        state["state_sha256"] = _state_hash(state)
        _write_json(path / "state.json", state)
        return {
            "state": "paused",
            "completed_actions": [],
            "paused": True,
            "pause_reason": "resolve_safety_violation",
            "pause_only": True,
            "review_authored": False,
            "sealed": False,
            "executable": False,
        }
    completed = list(state.get("completed_actions", []))
    receipt_ids = {row.get("receipt_id") for row in receipts if isinstance(row, dict)}
    paused_reason: str | None = None
    for action_row in plan.get("required_actions", []):
        action = action_row.get("action")
        if not isinstance(action, str) or action in completed:
            continue
        if action not in DETERMINISTIC_ACTIONS:
            paused_reason = f"{action}_requires_explicit_gate"
            break
        receipt_id = f"receipt-{sha256_json([plan.get('new_dag_sha256'), action, action_row.get('node_ids', [])])[:32]}"
        if receipt_id not in receipt_ids:
            receipt = {
                "schema_version": INCREMENTAL_BUILD_RECEIPT_SCHEMA,
                "compiler_version": INCREMENTAL_BUILD_DAG_COMPILER_VERSION,
                "protocol": INCREMENTAL_BUILD_DAG_PROTOCOL,
                "receipt_id": receipt_id,
                "action": action,
                "status": "prepared-not-executed",
                "node_ids": sorted(action_row.get("node_ids", [])),
                "input_dag_sha256": plan.get("new_dag_sha256"),
                "output_hashes": {},
                "execution": False,
                "review_authored": False,
                "sealed": False,
                "executable": False,
            }
            receipt["receipt_hash"] = sha256_json(receipt)
            receipts.append(receipt)
            receipt_ids.add(receipt_id)
        # A receipt is a prepared intent only.  No action is marked completed
        # until a future executor supplies an output-bound receipt.
        paused_reason = "deterministic_executor_required"
        break
    pending_gates = sorted({gate for row in plan.get("required_actions", []) if row.get("execution") == "paused" for gate in row.get("gates", [])})
    if paused_reason:
        new_state = "paused"
    elif pending_gates:
        paused_reason = f"{pending_gates[0]}_requires_explicit_gate"
        new_state = "paused"
    elif len(completed) == len(plan.get("required_actions", [])):
        new_state = "prepared"
    else:
        new_state = "paused"
    state["state"] = new_state
    state["completed_actions"] = sorted(set(completed), key=lambda item: ACTION_ORDER.index(item) if item in ACTION_ORDER else item)
    state["paused_gates"] = pending_gates
    state["pause_reason"] = paused_reason
    state["receipt_ids"] = sorted(str(row.get("receipt_id")) for row in receipts if isinstance(row, dict))
    state["receipts_sha256"] = _receipts_hash([row for row in receipts if isinstance(row, dict)])
    state["state_sha256"] = _state_hash(state)
    _write_json(path / "state.json", state)
    _write_jsonl(path / "receipts.jsonl", sorted(receipts, key=lambda row: row.get("receipt_id", "")))
    return {
        "state": new_state,
        "completed_actions": state["completed_actions"],
        "paused": bool(paused_reason or state["paused_gates"]),
        "pause_reason": paused_reason,
        "pause_only": True,
        "review_authored": False,
        "sealed": False,
        "executable": False,
    }


def _print_payload(payload: Any, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    elif isinstance(payload, dict):
        print(f"PASS state={payload.get('state', 'ok')} fail_closed={payload.get('fail_closed', False)}")
    else:
        print(payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--input", "--snapshot", dest="snapshot", required=True, type=Path)
    build.add_argument("--output", required=True, type=Path)
    build.add_argument("--generated-at", default=DEFAULT_TIMESTAMP)
    compare = sub.add_parser("compare")
    compare.add_argument("--old", required=True, type=Path)
    compare.add_argument("--new", required=True, type=Path)
    compare.add_argument("--output", required=True, type=Path)
    compare.add_argument("--generated-at", default=DEFAULT_TIMESTAMP)
    validate = sub.add_parser("validate")
    validate.add_argument("--dag", "--input", dest="dag", required=True, type=Path)
    validate.add_argument("--json", action="store_true", dest="json_output")
    resume = sub.add_parser("resume")
    resume.add_argument("--dag", "--input", dest="dag", required=True, type=Path)
    resume.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "build":
            result = build_dag(args.snapshot, args.output, generated_at=args.generated_at)
            _print_payload(result["manifest"], True)
            return 0
        if args.command == "compare":
            result = compare_snapshots(args.old, args.new, args.output, generated_at=args.generated_at)
            _print_payload(result["changes"], True)
            return 0 if not result["changes"]["fail_closed"] else 1
        if args.command == "validate":
            issues = validate_dag_directory(args.dag)
            payload = {"valid": not issues, "issues": issues}
            _print_payload(payload, args.json_output or True)
            return 0 if not issues else 1
        result = resume_plan(args.dag)
        _print_payload(result, args.json_output or True)
        return 0
    except (IncrementalDagError, OSError, json.JSONDecodeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
