#!/usr/bin/env python3
"""Deterministic atomic semantic assurance for fake-expert.

This module proves traceability, coverage, reviewer separation, and artifact
integrity.  It deliberately does not claim to prove natural-language truth;
that judgment remains in externally authored review attestations.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from compiler_version import (
    SEMANTIC_ASSURANCE_COMPILER_VERSION,
    SEMANTIC_ASSURANCE_PROTOCOL,
)
from formula_execution import UNIT_TABLE


ASSERTION_SCHEMA = "tkc.semantic-assertion/v0.2"
SUPPORT_SCHEMA = "tkc.support-matrix/v0.1"
ATTESTATION_SCHEMA = "tkc.review-attestation/v0.1"
COVERAGE_SCHEMA = "tkc.coverage-ledger/v0.1"
ASSURANCE_MANIFEST_SCHEMA = "tkc.semantic-assurance-manifest/v0.1"
ATTESTATION_LEVEL = "host-orchestrator-recorded-not-cryptographic"

ASSERTION_KINDS = {
    "proposition",
    "numeric",
    "unit",
    "symbol",
    "assumption",
    "applicability",
    "failure_condition",
    "derivation_step",
    "visual_fact",
}
HIGH_RISK_KINDS = {
    "applicability",
    "failure_condition",
    "derivation_step",
    "visual_fact",
}
SUPPORT_KINDS = {"explicit", "paraphrase", "derived", "visual", "contradicted"}
COVERAGE_DISPOSITIONS = {
    "promoted",
    "context-only",
    "non-knowledge",
    "rejected",
    "gap",
    "quarantined",
}
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ISSUE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
NUMBER_RE = re.compile(r"(?<![A-Za-z_])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
SYMBOL_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]{0,63}\b")
_UNIT_NAMES = sorted((name for name in UNIT_TABLE if name != "1"), key=lambda value: (-len(value), value))
UNIT_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:" + "|".join(re.escape(name) for name in _UNIT_NAMES) + r")(?:\^-?\d+)?(?![A-Za-z0-9_])"
)


@dataclass(frozen=True)
class AssuranceIssue:
    severity: str
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    return f"{prefix}-{sha256_json(list(parts))[:20]}"


def _row_hash(row: dict[str, Any], hash_field: str) -> str:
    return sha256_json({key: value for key, value in row.items() if key != hash_field})


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[Any]:
    rows: list[Any] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid_jsonl:{path}:{line_number}:{error}") from error
    return rows


def assurance_manifest_block() -> dict[str, Any]:
    """Return the canonical workpack declaration for the v0.5 protocol."""

    return {
        "protocol": SEMANTIC_ASSURANCE_PROTOCOL,
        "compiler_version": SEMANTIC_ASSURANCE_COMPILER_VERSION,
        "candidate_only": True,
        "semantic_assertions": "semantic-assertions.jsonl",
        "support_matrix": "support-matrix.jsonl",
        "coverage_ledger": "coverage-ledger.jsonl",
        "review_attestations": "review-attestations.jsonl",
    }


def is_assurance_enabled(manifest: dict[str, Any]) -> bool:
    assurance = manifest.get("semantic_assurance")
    return (
        isinstance(assurance, dict)
        and assurance.get("protocol") == SEMANTIC_ASSURANCE_PROTOCOL
        and assurance.get("compiler_version") == SEMANTIC_ASSURANCE_COMPILER_VERSION
    )


def initialize_workpack_assurance(root: Path) -> None:
    """Create empty candidate artifacts; proposals are materialized after drafts."""

    for name in (
        "semantic-assertions.jsonl",
        "support-matrix.jsonl",
        "coverage-ledger.jsonl",
        "review-attestations.jsonl",
    ):
        _write_jsonl(root / name, [])


def _span(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    page, start, end, digest = (
        value.get("page"),
        value.get("start"),
        value.get("end"),
        value.get("span_sha256"),
    )
    if (
        not isinstance(page, int)
        or not isinstance(start, int)
        or not isinstance(end, int)
        or not isinstance(digest, str)
    ):
        return None
    return {"page": page, "start": start, "end": end, "span_sha256": digest}


def _spans(value: Any) -> list[dict[str, Any]]:
    rows = [_span(row) for row in value] if isinstance(value, list) else []
    return sorted(
        (row for row in rows if row is not None),
        key=lambda row: (row["page"], row["start"], row["end"], row["span_sha256"]),
    )


def _item_rows(drafts: dict[str, dict[str, Any]]) -> list[tuple[str, str, str, dict[str, Any], str]]:
    rows: list[tuple[str, str, str, dict[str, Any], str]] = []
    for unit_id in sorted(drafts):
        draft = drafts[unit_id]
        proposer = str(draft.get("proposer_instance", ""))
        for obj in draft.get("objects", []):
            if isinstance(obj, dict) and isinstance(obj.get("local_id"), str):
                rows.append((f"{unit_id}:{obj['local_id']}", "object", unit_id, obj, proposer))
        for index, relation in enumerate(draft.get("relations", [])):
            if isinstance(relation, dict):
                rows.append((f"rel:{unit_id}:{index}", "relation", unit_id, relation, proposer))
        for index, conflict in enumerate(draft.get("conflicts", [])):
            if isinstance(conflict, dict):
                local = str(conflict.get("local_id", index))
                rows.append((f"conf:{unit_id}:{local}", "conflict", unit_id, conflict, proposer))
        for index, gap in enumerate(draft.get("gaps", [])):
            if isinstance(gap, dict):
                rows.append((f"gap:{unit_id}:{index}", "gap", unit_id, gap, proposer))
    return rows


def _risk_tier(item_kind: str, value: dict[str, Any], assertion_kind: str) -> int:
    if item_kind in {"relation", "conflict", "gap"}:
        return 2
    if value.get("type") == "Equation" or isinstance(value.get("formula"), dict):
        return 2
    if assertion_kind in HIGH_RISK_KINDS:
        return 2
    return 1


def _origin(value: dict[str, Any], default: str = "compiler-derived") -> str:
    origin = value.get("origin")
    if origin in {"source-explicit", "source-paraphrase", "compiler-derived"}:
        return str(origin)
    return default


def _assertion(
    *,
    workpack_id: str,
    parent_item_ref: str,
    item_kind: str,
    unit_id: str,
    value: dict[str, Any],
    kind: str,
    origin: str,
    payload: dict[str, Any],
    evidence_spans: list[dict[str, Any]],
    dependency_assertion_ids: Iterable[str] = (),
    formula_node_paths: Iterable[str] = (),
    visual_task_ids: Iterable[str] = (),
    visual_object_ids: Iterable[str] = (),
    table_cell_ids: Iterable[str] = (),
    series_ids: Iterable[str] = (),
    render_sha256s: Iterable[str] = (),
    crop_sha256s: Iterable[str] = (),
    bbox_sha256s: Iterable[str] = (),
) -> dict[str, Any]:
    proposal_sha256 = sha256_json(value)
    seed = {
        "parent_item_ref": parent_item_ref,
        "unit_id": unit_id,
        "kind": kind,
        "origin": origin,
        "payload": payload,
        "evidence_spans": evidence_spans,
        "dependency_assertion_ids": sorted(set(dependency_assertion_ids)),
        "formula_node_paths": sorted(set(formula_node_paths)),
        "visual_task_ids": sorted(set(visual_task_ids)),
        "visual_object_ids": sorted(set(visual_object_ids)),
        "table_cell_ids": sorted(set(table_cell_ids)),
        "series_ids": sorted(set(series_ids)),
        "render_sha256s": sorted(set(render_sha256s)),
        "crop_sha256s": sorted(set(crop_sha256s)),
        "bbox_sha256s": sorted(set(bbox_sha256s)),
        "risk_tier": _risk_tier(item_kind, value, kind),
        "proposal_sha256": proposal_sha256,
    }
    row = {
        "schema_version": ASSERTION_SCHEMA,
        "assertion_id": stable_id("sa", workpack_id, seed),
        **seed,
    }
    row["assertion_sha256"] = _row_hash(row, "assertion_sha256")
    return row


def _text_fields(value: dict[str, Any]) -> Iterable[tuple[str, str]]:
    statement = value.get("statement")
    if isinstance(statement, str):
        yield "statement", statement
    for field in ("assumptions", "valid_when", "fails_when"):
        rows = value.get(field)
        if isinstance(rows, list):
            for index, row in enumerate(rows):
                if isinstance(row, str):
                    yield f"{field}[{index}]", row
    formula = value.get("formula")
    if isinstance(formula, dict):
        for field in ("source_notation", "normalized_notation", "derived_notation"):
            row = formula.get(field)
            if isinstance(row, str):
                yield f"formula.{field}", row


def _formula_fields(value: dict[str, Any]) -> Iterable[tuple[str, str]]:
    formula = value.get("formula")
    if not isinstance(formula, dict):
        return
    for field in ("source_notation", "normalized_notation", "derived_notation"):
        row = formula.get(field)
        if isinstance(row, str):
            yield f"formula.{field}", row


def _walk_ast_nodes(node: Any, path: str = "formula.ast.expression") -> Iterable[tuple[str, dict[str, Any]]]:
    if not isinstance(node, dict):
        return
    yield path, node
    kind = node.get("kind")
    if kind in {"add", "subtract", "multiply", "divide"}:
        yield from _walk_ast_nodes(node.get("left"), f"{path}.left")
        yield from _walk_ast_nodes(node.get("right"), f"{path}.right")
    elif kind in {"negate", "sqrt"}:
        yield from _walk_ast_nodes(node.get("arg"), f"{path}.arg")
    elif kind == "power":
        yield from _walk_ast_nodes(node.get("base"), f"{path}.base")


def derive_semantic_assurance(
    manifest: dict[str, Any],
    units: dict[str, dict[str, Any]],
    tasks: list[dict[str, Any]],
    drafts: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Derive atoms only from already structured proposals; invent no new claims."""

    workpack_id = str(manifest.get("workpack_id", ""))
    task_by_id = {
        str(task["id"]): task
        for task in tasks
        if isinstance(task, dict) and isinstance(task.get("id"), str)
    }
    item_rows = _item_rows(drafts)
    assertions: list[dict[str, Any]] = []
    primary_by_item: dict[str, str] = {}

    # Objects and gaps first so relations/conflicts can bind to primary assertions.
    for item_ref, item_kind, unit_id, value, _ in item_rows:
        if item_kind not in {"object", "gap"}:
            continue
        spans = _spans(value.get("evidence_spans"))
        origin = _origin(value, "source-paraphrase" if item_kind == "gap" else "compiler-derived")
        if item_kind == "gap":
            payload = {"field": "statement", "text": str(value.get("statement", "")), "gap": True}
        else:
            payload = {
                "field": "statement",
                "text": str(value.get("statement", "")),
                "object_type": str(value.get("type", "")),
            }
        primary = _assertion(
            workpack_id=workpack_id,
            parent_item_ref=item_ref,
            item_kind=item_kind,
            unit_id=unit_id,
            value=value,
            kind="proposition",
            origin=origin,
            payload=payload,
            evidence_spans=spans,
        )
        assertions.append(primary)
        primary_by_item[item_ref] = primary["assertion_id"]

        if item_kind == "gap":
            continue
        base_dependency = [primary["assertion_id"]] if origin == "compiler-derived" else []
        for field, text in _text_fields(value):
            for occurrence, match in enumerate(NUMBER_RE.finditer(text)):
                assertions.append(
                    _assertion(
                        workpack_id=workpack_id,
                        parent_item_ref=item_ref,
                        item_kind=item_kind,
                        unit_id=unit_id,
                        value=value,
                        kind="numeric",
                        origin=origin,
                        payload={
                            "field": field,
                            "token": match.group(0),
                            "start": match.start(),
                            "end": match.end(),
                            "occurrence": occurrence,
                        },
                        evidence_spans=spans,
                        dependency_assertion_ids=base_dependency,
                    )
                )
            for occurrence, match in enumerate(UNIT_RE.finditer(text)):
                assertions.append(
                    _assertion(
                        workpack_id=workpack_id,
                        parent_item_ref=item_ref,
                        item_kind=item_kind,
                        unit_id=unit_id,
                        value=value,
                        kind="unit",
                        origin=origin,
                        payload={
                            "field": field,
                            "token": match.group(0),
                            "start": match.start(),
                            "end": match.end(),
                            "occurrence": occurrence,
                        },
                        evidence_spans=spans,
                        dependency_assertion_ids=base_dependency,
                    )
                )
        for field, text in _formula_fields(value):
            for occurrence, match in enumerate(SYMBOL_RE.finditer(text)):
                token = match.group(0)
                if token in UNIT_TABLE:
                    continue
                assertions.append(
                    _assertion(
                        workpack_id=workpack_id,
                        parent_item_ref=item_ref,
                        item_kind=item_kind,
                        unit_id=unit_id,
                        value=value,
                        kind="symbol",
                        origin=origin,
                        payload={
                            "field": field,
                            "token": token,
                            "start": match.start(),
                            "end": match.end(),
                            "occurrence": occurrence,
                        },
                        evidence_spans=spans,
                        dependency_assertion_ids=base_dependency,
                    )
                )
        for index, assumption in enumerate(value.get("assumptions", [])):
            if isinstance(assumption, str):
                assertions.append(
                    _assertion(
                        workpack_id=workpack_id,
                        parent_item_ref=item_ref,
                        item_kind=item_kind,
                        unit_id=unit_id,
                        value=value,
                        kind="assumption",
                        origin=origin,
                        payload={"field": f"assumptions[{index}]", "text": assumption},
                        evidence_spans=spans,
                        dependency_assertion_ids=base_dependency,
                    )
                )
        applicability = value.get("applicability")
        evidence_pages = sorted({span["page"] for span in spans})
        declared_pages = (
            applicability.get("physical_pages")
            if isinstance(applicability, dict)
            else None
        )
        mirrors_evidence_envelope = (
            bool(evidence_pages)
            and declared_pages == [evidence_pages[0], evidence_pages[-1]]
        )
        meaningful_applicability = (
            isinstance(applicability, dict)
            and (
                bool(applicability.get("excluded_conclusions"))
                or not mirrors_evidence_envelope
            )
        )
        if meaningful_applicability:
            assertions.append(
                _assertion(
                    workpack_id=workpack_id,
                    parent_item_ref=item_ref,
                    item_kind=item_kind,
                    unit_id=unit_id,
                    value=value,
                    kind="applicability",
                    origin=origin,
                    payload={"field": "applicability", "value": applicability},
                    evidence_spans=spans,
                    dependency_assertion_ids=base_dependency,
                )
            )
        for index, condition in enumerate(value.get("valid_when", [])):
            if isinstance(condition, str):
                assertions.append(
                    _assertion(
                        workpack_id=workpack_id,
                        parent_item_ref=item_ref,
                        item_kind=item_kind,
                        unit_id=unit_id,
                        value=value,
                        kind="applicability",
                        origin=origin,
                        payload={"field": f"valid_when[{index}]", "text": condition},
                        evidence_spans=spans,
                        dependency_assertion_ids=base_dependency,
                    )
                )
        for index, condition in enumerate(value.get("fails_when", [])):
            if isinstance(condition, str):
                assertions.append(
                    _assertion(
                        workpack_id=workpack_id,
                        parent_item_ref=item_ref,
                        item_kind=item_kind,
                        unit_id=unit_id,
                        value=value,
                        kind="failure_condition",
                        origin=origin,
                        payload={"field": f"fails_when[{index}]", "text": condition},
                        evidence_spans=spans,
                        dependency_assertion_ids=base_dependency,
                    )
                )
        formula = value.get("formula")
        ast = formula.get("ast") if isinstance(formula, dict) else None
        if isinstance(ast, dict):
            symbols = ast.get("symbols")
            if isinstance(symbols, dict):
                for name in sorted(symbols):
                    specification = symbols[name]
                    if not isinstance(specification, dict):
                        continue
                    symbol_path = f"formula.ast.symbols.{name}"
                    assertions.append(
                        _assertion(
                            workpack_id=workpack_id,
                            parent_item_ref=item_ref,
                            item_kind=item_kind,
                            unit_id=unit_id,
                            value=value,
                            kind="symbol",
                            origin=origin,
                            payload={"field": symbol_path, "name": name, "specification": specification},
                            evidence_spans=spans,
                            dependency_assertion_ids=base_dependency,
                            formula_node_paths=[symbol_path],
                        )
                    )
                    unit = specification.get("unit")
                    if isinstance(unit, str):
                        assertions.append(
                            _assertion(
                                workpack_id=workpack_id,
                                parent_item_ref=item_ref,
                                item_kind=item_kind,
                                unit_id=unit_id,
                                value=value,
                                kind="unit",
                                origin=origin,
                                payload={"field": f"{symbol_path}.unit", "token": unit},
                                evidence_spans=spans,
                                dependency_assertion_ids=base_dependency,
                                formula_node_paths=[f"{symbol_path}.unit"],
                            )
                        )
            for node_path, node in _walk_ast_nodes(ast.get("expression")):
                node_payload = {key: node[key] for key in sorted(node) if key not in {"left", "right", "arg", "base"}}
                assertions.append(
                    _assertion(
                        workpack_id=workpack_id,
                        parent_item_ref=item_ref,
                        item_kind=item_kind,
                        unit_id=unit_id,
                        value=value,
                        kind="derivation_step",
                        origin="compiler-derived",
                        payload={"field": node_path, "node": node_payload},
                        evidence_spans=spans,
                        dependency_assertion_ids=[primary["assertion_id"]],
                        formula_node_paths=[node_path],
                    )
                )
        for task_id in sorted(set(value.get("visual_task_ids", []))):
            task = task_by_id.get(task_id)
            payload = {
                "task_id": task_id,
                "page": task.get("page") if isinstance(task, dict) else None,
                "kind": task.get("kind") if isinstance(task, dict) else None,
                "candidate_label": task.get("candidate_label") if isinstance(task, dict) else None,
                "visual_object_ids": sorted(task.get("visual_object_ids", [])) if isinstance(task, dict) else [],
                "table_cell_ids": sorted(task.get("table_cell_ids", [])) if isinstance(task, dict) else [],
                "series_ids": sorted(task.get("series_ids", [])) if isinstance(task, dict) else [],
                "render_sha256s": [str(task.get("page_asset_sha256"))] if isinstance(task, dict) and isinstance(task.get("page_asset_sha256"), str) else [],
                "crop_sha256s": sorted(task.get("crop_sha256s", [])) if isinstance(task, dict) else [],
                "bbox_sha256s": sorted(task.get("bbox_sha256s", [])) if isinstance(task, dict) else [],
            }
            object_ids = payload["visual_object_ids"]
            cell_ids = payload["table_cell_ids"]
            series_ids = payload["series_ids"]
            render_hashes = payload["render_sha256s"]
            crop_hashes = payload["crop_sha256s"]
            bbox_hashes = payload["bbox_sha256s"]
            assertions.append(
                _assertion(
                    workpack_id=workpack_id,
                    parent_item_ref=item_ref,
                    item_kind=item_kind,
                    unit_id=unit_id,
                    value=value,
                    kind="visual_fact",
                    origin=origin,
                    payload=payload,
                    evidence_spans=spans,
                    dependency_assertion_ids=base_dependency,
                    visual_task_ids=[task_id],
                    visual_object_ids=object_ids,
                    table_cell_ids=cell_ids,
                    series_ids=series_ids,
                    render_sha256s=render_hashes,
                    crop_sha256s=crop_hashes,
                    bbox_sha256s=bbox_hashes,
                )
            )

    for item_ref, item_kind, unit_id, value, _ in item_rows:
        if item_kind not in {"relation", "conflict"}:
            continue
        if item_kind == "relation":
            dependency_refs = [value.get("source_ref"), value.get("target_ref")]
            payload = {
                "source_ref": value.get("source_ref"),
                "type": value.get("type"),
                "target_ref": value.get("target_ref"),
            }
        else:
            dependency_refs = value.get("claim_refs", [])
            payload = {
                "topic": value.get("topic"),
                "claim_refs": value.get("claim_refs", []),
                "recommended_status": value.get("recommended_status"),
            }
        dependencies = [
            primary_by_item[reference]
            for reference in dependency_refs
            if isinstance(reference, str) and reference in primary_by_item
        ]
        row = _assertion(
            workpack_id=workpack_id,
            parent_item_ref=item_ref,
            item_kind=item_kind,
            unit_id=unit_id,
            value=value,
            kind="proposition",
            origin="compiler-derived",
            payload=payload,
            evidence_spans=[],
            dependency_assertion_ids=dependencies,
        )
        assertions.append(row)
        primary_by_item[item_ref] = row["assertion_id"]

    assertions.sort(key=lambda row: row["assertion_id"])
    seen_ids: set[str] = set()
    for row in assertions:
        if row["assertion_id"] in seen_ids:
            raise ValueError(f"assertion_id_collision:{row['assertion_id']}")
        seen_ids.add(row["assertion_id"])

    support: list[dict[str, Any]] = []
    for assertion in assertions:
        if assertion["kind"] == "visual_fact":
            support_kind = "visual"
        elif assertion["origin"] == "compiler-derived":
            support_kind = "derived"
        elif assertion["origin"] == "source-explicit":
            support_kind = "explicit"
        else:
            support_kind = "paraphrase"
        bound = bool(
            assertion["evidence_spans"]
            or assertion["dependency_assertion_ids"]
            or assertion["formula_node_paths"]
            or assertion["visual_task_ids"]
        )
        row = {
            "schema_version": SUPPORT_SCHEMA,
            "assertion_id": assertion["assertion_id"],
            "parent_item_ref": assertion["parent_item_ref"],
            "support_kind": support_kind,
            "status": "full" if bound else "partial",
            "evidence_spans": assertion["evidence_spans"],
            "dependency_assertion_ids": assertion["dependency_assertion_ids"],
            "formula_node_paths": assertion["formula_node_paths"],
            "visual_task_ids": assertion["visual_task_ids"],
            "visual_object_ids": assertion.get("visual_object_ids", []),
            "table_cell_ids": assertion.get("table_cell_ids", []),
            "series_ids": assertion.get("series_ids", []),
            "render_sha256s": assertion.get("render_sha256s", []),
            "crop_sha256s": assertion.get("crop_sha256s", []),
            "bbox_sha256s": assertion.get("bbox_sha256s", []),
            "assertion_sha256": assertion["assertion_sha256"],
        }
        row["support_sha256"] = _row_hash(row, "support_sha256")
        support.append(row)

    assertions_by_unit: dict[str, list[str]] = {}
    item_refs_by_unit: dict[str, list[str]] = {}
    for row in assertions:
        assertions_by_unit.setdefault(row["unit_id"], []).append(row["assertion_id"])
        item_refs_by_unit.setdefault(row["unit_id"], []).append(row["parent_item_ref"])
    coverage: list[dict[str, Any]] = []
    for unit_id in sorted(units):
        unit = units[unit_id]
        draft = drafts.get(unit_id, {})
        disposition = draft.get("candidate_disposition", {}) if isinstance(draft, dict) else {}
        action = disposition.get("action") if isinstance(disposition, dict) else None
        mapping = {
            "promote": "promoted",
            "context-only": "context-only",
            "non-knowledge": "non-knowledge",
            "reject": "rejected",
        }
        resolved = mapping.get(str(action), "quarantined")
        if draft.get("gaps") and not draft.get("objects") and action == "promote":
            resolved = "gap"
        pages = sorted(
            {
                int(span["page"])
                for span in unit.get("focus_spans", [])
                if isinstance(span, dict) and isinstance(span.get("page"), int)
            }
        )
        row = {
            "schema_version": COVERAGE_SCHEMA,
            "coverage_id": stable_id("cov", workpack_id, unit_id),
            "unit_id": unit_id,
            "segment_id": str(unit.get("segment_id", "")),
            "physical_pages": pages,
            "disposition": resolved,
            "proposal_sha256": sha256_json(draft),
            "item_refs": sorted(set(item_refs_by_unit.get(unit_id, []))),
            "assertion_ids": sorted(set(assertions_by_unit.get(unit_id, []))),
            "rationale": str(disposition.get("reason", "Unaccounted proposal state.")),
        }
        row["coverage_sha256"] = _row_hash(row, "coverage_sha256")
        coverage.append(row)
    return assertions, support, coverage


def materialize_workpack_assurance(
    root: Path,
    manifest: dict[str, Any],
    units: dict[str, dict[str, Any]],
    tasks: list[dict[str, Any]],
    drafts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not is_assurance_enabled(manifest):
        raise ValueError("legacy_semantic_protocol_not_promotable")
    assertions, support, coverage = derive_semantic_assurance(manifest, units, tasks, drafts)
    block = manifest["semantic_assurance"]
    paths = {
        "semantic_assertions": root / str(block["semantic_assertions"]),
        "support_matrix": root / str(block["support_matrix"]),
        "coverage_ledger": root / str(block["coverage_ledger"]),
    }
    _write_jsonl(paths["semantic_assertions"], assertions)
    _write_jsonl(paths["support_matrix"], support)
    _write_jsonl(paths["coverage_ledger"], coverage)
    attestation_path = root / str(block["review_attestations"])
    if not attestation_path.is_file():
        _write_jsonl(attestation_path, [])
    return {
        "protocol": SEMANTIC_ASSURANCE_PROTOCOL,
        "assertion_count": len(assertions),
        "support_count": len(support),
        "coverage_count": len(coverage),
        "assertions_sha256": sha256_file(paths["semantic_assertions"]),
        "support_matrix_sha256": sha256_file(paths["support_matrix"]),
        "coverage_ledger_sha256": sha256_file(paths["coverage_ledger"]),
    }


def load_workpack_assurance(root: Path, manifest: dict[str, Any]) -> tuple[list[Any], list[Any], list[Any], list[Any]]:
    block = manifest.get("semantic_assurance")
    if not isinstance(block, dict):
        raise ValueError("legacy_semantic_protocol_not_promotable")
    return tuple(
        _load_jsonl(root / str(block[field]))
        for field in (
            "semantic_assertions",
            "support_matrix",
            "coverage_ledger",
            "review_attestations",
        )
    )  # type: ignore[return-value]


def _missing_code(kind: Any, row: dict[str, Any] | None = None) -> str:
    mapping = {
        "numeric": "numeric_token_untraced",
        "unit": "unit_untraced",
        "symbol": "symbol_untraced",
        "assumption": "assumption_omitted",
        "applicability": "applicability_overbroad",
        "failure_condition": "failure_condition_omitted",
        "derivation_step": "formula_node_untraced" if row and row.get("formula_node_paths") else "assertion_untraced",
    }
    return mapping.get(kind, "assertion_untraced")


def _issue(issues: list[AssuranceIssue], code: str, path: str, message: str) -> None:
    issues.append(AssuranceIssue("error", code, path, message))


def validate_workpack_assurance(
    root: Path,
    manifest: dict[str, Any],
    units: dict[str, dict[str, Any]],
    tasks: list[dict[str, Any]],
    drafts: dict[str, dict[str, Any]],
) -> tuple[list[AssuranceIssue], dict[str, Any]]:
    issues: list[AssuranceIssue] = []
    if not is_assurance_enabled(manifest):
        _issue(issues, "legacy_semantic_protocol_not_promotable", "workpack.json", "The workpack does not declare the v0.5 semantic-assurance protocol.")
        return issues, {}
    expected_assertions, expected_support, expected_coverage = derive_semantic_assurance(
        manifest, units, tasks, drafts
    )
    try:
        actual_assertions, actual_support, actual_coverage, _ = load_workpack_assurance(root, manifest)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        _issue(issues, "support_matrix_incomplete", "semantic_assurance", str(error))
        return issues, {}

    expected_by_id = {row["assertion_id"]: row for row in expected_assertions}
    actual_by_id: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(actual_assertions):
        path = f"semantic-assertions.jsonl:{index + 1}"
        if not isinstance(row, dict) or row.get("schema_version") != ASSERTION_SCHEMA:
            _issue(issues, "assertion_untraced", path, "Invalid assertion record.")
            continue
        assertion_id = row.get("assertion_id")
        if not isinstance(assertion_id, str) or assertion_id in actual_by_id:
            _issue(issues, "assertion_untraced", path, "Assertion ID is missing or duplicated.")
            continue
        actual_by_id[assertion_id] = row
        if row.get("kind") not in ASSERTION_KINDS:
            _issue(issues, "assertion_untraced", path, "Unknown assertion kind.")
        if row.get("assertion_sha256") != _row_hash(row, "assertion_sha256"):
            _issue(issues, _missing_code(row.get("kind"), row), path, "Assertion content hash mismatch.")
        expected = expected_by_id.get(assertion_id)
        if expected is None:
            code = "assumption_added" if row.get("kind") == "assumption" else "assertion_untraced"
            _issue(issues, code, path, "Assertion is not derived from the frozen proposal.")
        elif row != expected:
            _issue(issues, _missing_code(expected.get("kind"), expected), path, "Assertion differs from deterministic proposal decomposition.")
    for assertion_id, expected in expected_by_id.items():
        if assertion_id not in actual_by_id:
            _issue(issues, _missing_code(expected.get("kind"), expected), assertion_id, "Required semantic atom is missing.")

    support_by_id: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(actual_support):
        path = f"support-matrix.jsonl:{index + 1}"
        if not isinstance(row, dict) or row.get("schema_version") != SUPPORT_SCHEMA:
            _issue(issues, "support_matrix_incomplete", path, "Invalid support row.")
            continue
        assertion_id = row.get("assertion_id")
        if not isinstance(assertion_id, str) or assertion_id in support_by_id:
            _issue(issues, "support_matrix_incomplete", path, "Support row is duplicated or unbound.")
            continue
        support_by_id[assertion_id] = row
        assertion = actual_by_id.get(assertion_id)
        if assertion is None:
            _issue(issues, "support_matrix_incomplete", path, "Support references an unknown assertion.")
            continue
        if row.get("support_sha256") != _row_hash(row, "support_sha256"):
            _issue(issues, "support_matrix_incomplete", path, "Support content hash mismatch.")
        if row.get("assertion_sha256") != assertion.get("assertion_sha256"):
            _issue(issues, "support_matrix_incomplete", path, "Support references stale assertion content.")
        for field in ("evidence_spans", "dependency_assertion_ids", "formula_node_paths", "visual_task_ids", "visual_object_ids", "table_cell_ids", "series_ids", "render_sha256s", "crop_sha256s", "bbox_sha256s"):
            if row.get(field) != assertion.get(field):
                _issue(issues, "support_matrix_incomplete", path, f"Support field differs from assertion: {field}.")
        if assertion.get("origin") == "compiler-derived" and row.get("support_kind") != "derived" and assertion.get("kind") != "visual_fact":
            _issue(issues, "support_matrix_incomplete", path, "Derived content cannot be relabeled source-explicit.")
        if row.get("status") != "full":
            _issue(issues, "assertion_untraced", path, "Partial support cannot pass promotion.")
    if set(support_by_id) != set(actual_by_id):
        _issue(issues, "support_matrix_incomplete", "support-matrix.jsonl", "Support coverage must be exactly one row per assertion.")
    expected_support_by_id = {row["assertion_id"]: row for row in expected_support}
    for assertion_id, expected in expected_support_by_id.items():
        if support_by_id.get(assertion_id) != expected:
            _issue(issues, "support_matrix_incomplete", assertion_id, "Support row differs from deterministic binding.")

    coverage_by_unit: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(actual_coverage):
        path = f"coverage-ledger.jsonl:{index + 1}"
        if not isinstance(row, dict) or row.get("schema_version") != COVERAGE_SCHEMA:
            _issue(issues, "coverage_ledger_incomplete", path, "Invalid coverage row.")
            continue
        unit_id = row.get("unit_id")
        if not isinstance(unit_id, str) or unit_id in coverage_by_unit:
            _issue(issues, "coverage_ledger_incomplete", path, "Coverage unit is missing or duplicated.")
            continue
        coverage_by_unit[unit_id] = row
        if row.get("coverage_sha256") != _row_hash(row, "coverage_sha256"):
            _issue(issues, "coverage_ledger_incomplete", path, "Coverage content hash mismatch.")
        if row.get("disposition") not in COVERAGE_DISPOSITIONS:
            _issue(issues, "coverage_ledger_incomplete", path, "Unknown coverage disposition.")
    expected_coverage_by_unit = {row["unit_id"]: row for row in expected_coverage}
    if set(coverage_by_unit) != set(units):
        _issue(issues, "coverage_ledger_incomplete", "coverage-ledger.jsonl", "Every selected-scope unit requires exactly one row.")
    for unit_id, expected in expected_coverage_by_unit.items():
        if coverage_by_unit.get(unit_id) != expected:
            _issue(issues, "coverage_ledger_incomplete", unit_id, "Coverage row differs from the frozen unit disposition.")

    unique = {(issue.code, issue.path, issue.message): issue for issue in issues}
    result = sorted(unique.values(), key=lambda item: (item.code, item.path, item.message))
    return result, {
        "protocol": SEMANTIC_ASSURANCE_PROTOCOL,
        "assertion_count": len(actual_by_id),
        "support_count": len(support_by_id),
        "coverage_count": len(coverage_by_unit),
        "accounted_unit_count": len(coverage_by_unit),
        "selected_scope_unit_count": len(units),
        "passed": not result,
    }


def enrich_review_material(
    queue: list[dict[str, Any]],
    visual_queue: list[dict[str, Any]],
    assertions: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    *,
    source_sha256: str,
    workpack_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for assertion in assertions:
        if isinstance(assertion, dict) and isinstance(assertion.get("parent_item_ref"), str):
            by_parent.setdefault(assertion["parent_item_ref"], []).append(assertion)
    task_hashes = {
        task["id"]: task.get("page_asset_sha256")
        for task in tasks
        if isinstance(task, dict) and isinstance(task.get("id"), str)
    }
    enriched: list[dict[str, Any]] = []
    for base in queue:
        row = dict(base)
        bound = sorted(by_parent.get(str(row.get("item_ref")), []), key=lambda item: item["assertion_id"])
        assertion_ids = [item["assertion_id"] for item in bound]
        row["assertion_ids"] = assertion_ids
        row["assertion_bundle_sha256"] = sha256_json(
            [{"assertion_id": item["assertion_id"], "assertion_sha256": item["assertion_sha256"]} for item in bound]
        )
        row["risk_tier"] = max((int(item["risk_tier"]) for item in bound), default=2)
        row["required_reviewer_count"] = row["risk_tier"]
        row["evidence_span_sha256s"] = sorted(
            {
                span["span_sha256"]
                for item in bound
                for span in item.get("evidence_spans", [])
                if isinstance(span, dict) and isinstance(span.get("span_sha256"), str)
            }
        )
        row["render_sha256s"] = sorted(
            {
                str(task_hashes[task_id])
                for item in bound
                for task_id in item.get("visual_task_ids", [])
                if isinstance(task_hashes.get(task_id), str)
            }
        )
        row["requires_formula_check"] = any(
            item.get("formula_node_paths") for item in bound
        )
        row["review_input_sha256"] = sha256_json(
            {
                "source_sha256": source_sha256,
                "workpack_id": workpack_id,
                "item_ref": row.get("item_ref"),
                "item_kind": row.get("item_kind"),
                "item_sha256": row.get("item_sha256"),
                "dependency_refs": row.get("dependency_refs"),
                "assertion_bundle_sha256": row["assertion_bundle_sha256"],
                "risk_tier": row["risk_tier"],
            }
        )
        enriched.append(row)
    enriched.sort(key=lambda row: row["item_ref"])
    bundle = sha256_json(
        {
            "source_sha256": source_sha256,
            "workpack_id": workpack_id,
            "semantic_queue": enriched,
            "visual_queue": visual_queue,
            "protocol": SEMANTIC_ASSURANCE_PROTOCOL,
        }
    )
    return enriched, visual_queue, bundle


def review_plan_sha256(plan: dict[str, Any]) -> str:
    return sha256_json(plan)


def expected_attestation_id(plan: dict[str, Any], queue_row: dict[str, Any], reviewer: str) -> str:
    return stable_id(
        "rat",
        review_plan_sha256(plan),
        queue_row.get("item_ref"),
        reviewer,
        queue_row.get("assertion_bundle_sha256"),
    )


def validate_attestations(
    records: list[Any],
    queue: list[dict[str, Any]],
    plan: dict[str, Any],
) -> tuple[list[AssuranceIssue], dict[str, dict[str, Any]], dict[str, Any]]:
    issues: list[AssuranceIssue] = []
    queue_by_ref = {row["item_ref"]: row for row in queue}
    registered = set(plan.get("reviewer_instances", [])) if isinstance(plan.get("reviewer_instances"), list) else set()
    plan_hash = review_plan_sha256(plan)
    by_item: dict[str, list[dict[str, Any]]] = {}
    seen_attestation_ids: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    for index, raw in enumerate(records):
        path = f"review-attestations.jsonl:{index + 1}"
        if not isinstance(raw, dict) or raw.get("schema_version") != ATTESTATION_SCHEMA:
            _issue(issues, "review_attestation_invalid", path, "Unexpected attestation schema.")
            continue
        record = dict(raw)
        item_ref = record.get("item_ref")
        reviewer = record.get("reviewer_instance")
        queue_row = queue_by_ref.get(item_ref)
        if queue_row is None or not isinstance(reviewer, str):
            _issue(issues, "review_attestation_invalid", path, "Attestation item or reviewer is unresolved.")
            continue
        pair = (str(item_ref), reviewer)
        attestation_id = record.get("attestation_id")
        if attestation_id in seen_attestation_ids or pair in seen_pairs:
            _issue(issues, "review_attestation_replay", path, "Attestation identity or reviewer/item pair was replayed.")
        if isinstance(attestation_id, str):
            seen_attestation_ids.add(attestation_id)
        seen_pairs.add(pair)
        if record.get("attestation_id") != expected_attestation_id(plan, queue_row, reviewer):
            _issue(issues, "review_attestation_invalid", path, "Attestation ID is not bound to this plan, item, reviewer, and assertion bundle.")
        if record.get("attestation_level") != ATTESTATION_LEVEL:
            _issue(issues, "review_attestation_invalid", path, "Attestation level is overstated or missing.")
        if reviewer not in registered:
            _issue(issues, "review_attestation_invalid", path, "Reviewer is not registered in the plan.")
        if reviewer == queue_row.get("proposer_instance"):
            _issue(issues, "reviewer_not_independent", path, "Reviewer equals proposer.")
        expected_fields = {
            "item_kind": queue_row.get("item_kind"),
            "proposer_instance": queue_row.get("proposer_instance"),
            "review_session_id": plan.get("review_session_id"),
            "review_plan_sha256": plan_hash,
            "item_sha256": queue_row.get("item_sha256"),
            "review_input_sha256": queue_row.get("review_input_sha256"),
            "assertion_ids": queue_row.get("assertion_ids"),
            "assertion_bundle_sha256": queue_row.get("assertion_bundle_sha256"),
            "review_method": "fresh-source-inspection",
        }
        for field, expected in expected_fields.items():
            if record.get(field) != expected:
                _issue(issues, "review_attestation_invalid", f"{path}.{field}", "Attestation is stale or incomplete.")
        checks = record.get("review_checks")
        required_checks = {
            "evidence_span_checked",
            "support_completeness_checked",
            "scope_and_applicability_checked",
            "conflict_checked",
            "atomic_assertions_checked",
            "differences_recorded",
        }
        if queue_row.get("requires_formula_check") is True:
            required_checks.add("formula_checked")
        if not isinstance(checks, dict) or any(checks.get(field) is not True for field in required_checks):
            _issue(issues, "review_attestation_invalid", f"{path}.review_checks", "Required inspection checks were not host-recorded.")
        inspected = record.get("inspected_artifacts")
        expected_inspected = {
            "source_sha256": plan.get("source_sha256"),
            "evidence_span_sha256s": queue_row.get("evidence_span_sha256s", []),
            "render_sha256s": queue_row.get("render_sha256s", []),
        }
        if inspected != expected_inspected:
            _issue(issues, "review_attestation_invalid", f"{path}.inspected_artifacts", "Inspected artifacts do not match the frozen queue.")
        differences = record.get("differences")
        difference_ids = [row.get("assertion_id") for row in differences if isinstance(row, dict)] if isinstance(differences, list) else []
        if (
            not isinstance(differences, list)
            or set(difference_ids) != set(queue_row.get("assertion_ids", []))
            or len(difference_ids) != len(set(difference_ids))
            or any(
                not isinstance(row, dict)
                or row.get("status") not in {"confirmed", "correction-required", "unsupported"}
                or not isinstance(row.get("observation"), str)
                or not row.get("observation")
                for row in differences or []
            )
        ):
            _issue(issues, "review_difference_missing", f"{path}.differences", "Every assertion requires one explicit difference/no-difference observation.")
        verdict = record.get("verdict")
        issue_codes = record.get("issue_codes")
        if (
            verdict not in {"accepted", "rejected", "quarantined"}
            or not isinstance(issue_codes, list)
            or len(issue_codes) != len(set(issue_codes))
            or any(not isinstance(code, str) or not ISSUE_RE.fullmatch(code) for code in issue_codes or [])
            or not isinstance(record.get("rationale"), str)
            or not record.get("rationale")
        ):
            _issue(issues, "review_attestation_invalid", path, "Verdict, issue codes, or rationale is invalid.")
        if verdict == "accepted":
            if record.get("evidence_support") != "full" or issue_codes:
                _issue(issues, "review_attestation_invalid", path, "Accepted attestations require full support and no issue codes.")
            if isinstance(differences, list) and any(row.get("status") != "confirmed" for row in differences if isinstance(row, dict)):
                _issue(issues, "review_attestation_invalid", path, "Accepted attestations cannot contain unresolved differences.")
        elif not issue_codes:
            _issue(issues, "review_attestation_invalid", path, "Rejected/quarantined attestations require stable issue codes.")
        confidence = record.get("confidence")
        if not isinstance(confidence, dict) or any(
            not isinstance(confidence.get(field), (int, float))
            or isinstance(confidence.get(field), bool)
            or not 0 <= confidence[field] <= 1
            for field in ("extraction", "interpretation")
        ):
            _issue(issues, "review_attestation_invalid", f"{path}.confidence", "Confidence must contain bounded extraction and interpretation values.")
        by_item.setdefault(str(item_ref), []).append(record)

    lead_records: dict[str, dict[str, Any]] = {}
    for item_ref, queue_row in queue_by_ref.items():
        rows = by_item.get(item_ref, [])
        required = int(queue_row.get("required_reviewer_count", 2))
        reviewers = {str(row.get("reviewer_instance")) for row in rows}
        if len(rows) != required or len(reviewers) != required:
            _issue(
                issues,
                "reviewer_coverage_insufficient",
                item_ref,
                f"Risk tier requires exactly {required} distinct reviewer attestations; found {len(rows)}.",
            )
            continue
        verdicts = {str(row.get("verdict")) for row in rows}
        support = {str(row.get("evidence_support")) for row in rows}
        resolutions = {sha256_json(row.get("gap_resolution")) for row in rows}
        if len(verdicts) != 1 or len(support) != 1 or (queue_row.get("item_kind") == "gap" and len(resolutions) != 1):
            _issue(issues, "review_attestation_invalid", item_ref, "Required reviewers disagree; item must be quarantined and re-reviewed.")
            continue
        if len(rows) > 1:
            observation_signatures = {
                sha256_json(
                    {
                        "rationale": str(row.get("rationale", "")).strip().casefold(),
                        "observations": [
                            str(difference.get("observation", "")).strip().casefold()
                            for difference in row.get("differences", [])
                            if isinstance(difference, dict)
                        ],
                    }
                )
                for row in rows
            }
            if len(observation_signatures) != len(rows):
                _issue(
                    issues,
                    "review_attestation_invalid",
                    item_ref,
                    "Independent reviewers must record differentiated source-specific observations.",
                )
                continue
        lead = sorted(rows, key=lambda row: str(row.get("reviewer_instance")))[0]
        legacy = {
            "schema_version": "tkc.semantic-review/v0.1",
            "item_ref": item_ref,
            "item_kind": queue_row.get("item_kind"),
            "reviewer_instance": lead.get("reviewer_instance"),
            "proposer_instance": lead.get("proposer_instance"),
            "item_sha256": lead.get("item_sha256"),
            "review_input_sha256": lead.get("review_input_sha256"),
            "verdict": lead.get("verdict"),
            "evidence_support": lead.get("evidence_support"),
            "issue_codes": lead.get("issue_codes"),
            "rationale": lead.get("rationale"),
            "confidence": lead.get("confidence"),
            "review_session_id": lead.get("review_session_id"),
            "review_method": lead.get("review_method"),
            "review_checks": lead.get("review_checks"),
        }
        if queue_row.get("item_kind") == "gap":
            legacy["gap_resolution"] = lead.get("gap_resolution")
        lead_records[item_ref] = legacy

    unique = {(issue.code, issue.path, issue.message): issue for issue in issues}
    result = sorted(unique.values(), key=lambda item: (item.code, item.path, item.message))
    return result, lead_records, {
        "attestation_count": sum(len(rows) for rows in by_item.values()),
        "reviewed_item_count": len(lead_records),
        "required_item_count": len(queue_by_ref),
        "passed": not result,
    }


def write_promoted_assurance(
    *,
    workpack_root: Path,
    workpack_manifest: dict[str, Any],
    output_root: Path,
    plan: dict[str, Any],
    accepted_item_refs: set[str],
    entity_map: dict[str, str | None],
) -> dict[str, Any]:
    assertions, support, coverage, attestations = load_workpack_assurance(workpack_root, workpack_manifest)
    promoted_assertions = [row for row in assertions if isinstance(row, dict) and row.get("parent_item_ref") in accepted_item_refs]
    assertion_ids = {row["assertion_id"] for row in promoted_assertions}
    promoted_support = [row for row in support if isinstance(row, dict) and row.get("assertion_id") in assertion_ids]
    promoted_attestations = [row for row in attestations if isinstance(row, dict) and row.get("item_ref") in accepted_item_refs]
    promoted_coverage: list[dict[str, Any]] = []
    for raw in coverage:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        row["item_refs"] = sorted(ref for ref in row.get("item_refs", []) if ref in accepted_item_refs)
        row["assertion_ids"] = sorted(identifier for identifier in row.get("assertion_ids", []) if identifier in assertion_ids)
        if row.get("disposition") == "promoted" and not any(entity_map.get(ref) for ref in row["item_refs"]):
            row["disposition"] = "rejected"
            row["rationale"] = "No item in this selected-scope unit passed the complete v0.5 review gate."
        row["coverage_sha256"] = _row_hash(row, "coverage_sha256")
        promoted_coverage.append(row)

    paths = {
        "semantic_assertions": "references/semantic/semantic-assertions.jsonl",
        "support_matrix": "references/semantic/support-matrix.jsonl",
        "coverage_ledger": "references/semantic/coverage-ledger.jsonl",
        "review_attestations": "references/reviews/review-attestations.jsonl",
    }
    rows_by_key = {
        "semantic_assertions": promoted_assertions,
        "support_matrix": promoted_support,
        "coverage_ledger": promoted_coverage,
        "review_attestations": promoted_attestations,
    }
    for key, relative in paths.items():
        _write_jsonl(output_root / relative, rows_by_key[key])
    artifacts = {
        key: {
            "path": relative,
            "sha256": sha256_file(output_root / relative),
            "record_count": len(rows_by_key[key]),
        }
        for key, relative in paths.items()
    }
    assurance_manifest = {
        "schema_version": ASSURANCE_MANIFEST_SCHEMA,
        "protocol": SEMANTIC_ASSURANCE_PROTOCOL,
        "compiler_version": SEMANTIC_ASSURANCE_COMPILER_VERSION,
        "attestation_level": ATTESTATION_LEVEL,
        "source_sha256": workpack_manifest.get("source", {}).get("sha256"),
        "workpack_id": workpack_manifest.get("workpack_id"),
        "review_plan_sha256": review_plan_sha256(plan),
        "selected_scope_complete": len(promoted_coverage) == len(workpack_manifest.get("units", [])),
        "whole_book_completeness_claimed": False,
        "entity_map": [
            {"item_ref": item_ref, "promoted_id": entity_map[item_ref]}
            for item_ref in sorted(entity_map)
        ],
        "artifacts": artifacts,
    }
    _write_json(output_root / "references/semantic/assurance-manifest.json", assurance_manifest)
    return assurance_manifest


def validate_promoted_assurance(root: Path) -> tuple[list[AssuranceIssue], dict[str, Any]]:
    """Validate a generated Skill's compact assurance graph without its PDF."""

    path = root / "references/semantic/assurance-manifest.json"
    if not path.is_file():
        return [], {"protocol": "legacy", "present": False}
    issues: list[AssuranceIssue] = []
    try:
        manifest = _load_json(path)
    except (OSError, json.JSONDecodeError) as error:
        _issue(issues, "support_matrix_incomplete", str(path), str(error))
        return issues, {}
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != ASSURANCE_MANIFEST_SCHEMA
        or manifest.get("protocol") != SEMANTIC_ASSURANCE_PROTOCOL
        or manifest.get("compiler_version") != SEMANTIC_ASSURANCE_COMPILER_VERSION
        or manifest.get("attestation_level") != ATTESTATION_LEVEL
        or manifest.get("whole_book_completeness_claimed") is not False
    ):
        _issue(issues, "support_matrix_incomplete", "assurance-manifest.json", "Semantic assurance identity or truth boundary is invalid.")
        return issues, {}
    artifacts = manifest.get("artifacts")
    loaded: dict[str, list[Any]] = {}
    expected_paths = {
        "semantic_assertions": "references/semantic/semantic-assertions.jsonl",
        "support_matrix": "references/semantic/support-matrix.jsonl",
        "coverage_ledger": "references/semantic/coverage-ledger.jsonl",
        "review_attestations": "references/reviews/review-attestations.jsonl",
    }
    if not isinstance(artifacts, dict):
        _issue(issues, "support_matrix_incomplete", "assurance-manifest.json.artifacts", "Artifact inventory missing.")
        return issues, {}
    for key, expected_relative in expected_paths.items():
        record = artifacts.get(key)
        if not isinstance(record, dict) or record.get("path") != expected_relative:
            _issue(issues, "support_matrix_incomplete", key, "Canonical artifact path is missing or changed.")
            continue
        target = (root / expected_relative).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            _issue(issues, "support_matrix_incomplete", key, "Artifact path escapes package.")
            continue
        if not target.is_file() or record.get("sha256") != sha256_file(target):
            _issue(issues, "support_matrix_incomplete", expected_relative, "Artifact is missing or hash-mismatched.")
            continue
        try:
            rows = _load_jsonl(target)
        except (OSError, ValueError) as error:
            _issue(issues, "support_matrix_incomplete", expected_relative, str(error))
            continue
        if record.get("record_count") != len(rows):
            _issue(issues, "support_matrix_incomplete", expected_relative, "Artifact record count mismatch.")
        loaded[key] = rows
    if set(loaded) != set(expected_paths):
        return sorted(issues, key=lambda item: (item.code, item.path)), {}

    assertions: dict[str, dict[str, Any]] = {}
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for index, raw in enumerate(loaded["semantic_assertions"]):
        path_label = f"semantic-assertions.jsonl:{index + 1}"
        if not isinstance(raw, dict) or raw.get("schema_version") != ASSERTION_SCHEMA:
            _issue(issues, "assertion_untraced", path_label, "Invalid promoted assertion.")
            continue
        identifier = raw.get("assertion_id")
        if not isinstance(identifier, str) or identifier in assertions:
            _issue(issues, "assertion_untraced", path_label, "Assertion ID missing or duplicated.")
            continue
        assertions[identifier] = raw
        by_parent.setdefault(str(raw.get("parent_item_ref")), []).append(raw)
        if raw.get("assertion_sha256") != _row_hash(raw, "assertion_sha256"):
            _issue(issues, _missing_code(raw.get("kind"), raw), path_label, "Assertion hash mismatch.")
    support: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(loaded["support_matrix"]):
        path_label = f"support-matrix.jsonl:{index + 1}"
        if not isinstance(raw, dict) or raw.get("schema_version") != SUPPORT_SCHEMA:
            _issue(issues, "support_matrix_incomplete", path_label, "Invalid promoted support row.")
            continue
        identifier = raw.get("assertion_id")
        if not isinstance(identifier, str) or identifier in support:
            _issue(issues, "support_matrix_incomplete", path_label, "Support ID missing or duplicated.")
            continue
        support[identifier] = raw
        assertion = assertions.get(identifier)
        if assertion is None or raw.get("assertion_sha256") != assertion.get("assertion_sha256"):
            _issue(issues, "support_matrix_incomplete", path_label, "Support is orphaned or stale.")
        if raw.get("support_sha256") != _row_hash(raw, "support_sha256") or raw.get("status") != "full":
            _issue(issues, "support_matrix_incomplete", path_label, "Support hash/status invalid.")
    if set(assertions) != set(support):
        _issue(issues, "support_matrix_incomplete", "support-matrix.jsonl", "Bidirectional assertion/support coverage is incomplete.")

    coverage_units: set[str] = set()
    for index, raw in enumerate(loaded["coverage_ledger"]):
        path_label = f"coverage-ledger.jsonl:{index + 1}"
        if not isinstance(raw, dict) or raw.get("schema_version") != COVERAGE_SCHEMA:
            _issue(issues, "coverage_ledger_incomplete", path_label, "Invalid promoted coverage row.")
            continue
        unit_id = raw.get("unit_id")
        if not isinstance(unit_id, str) or unit_id in coverage_units:
            _issue(issues, "coverage_ledger_incomplete", path_label, "Coverage unit missing or duplicated.")
        else:
            coverage_units.add(unit_id)
        if raw.get("coverage_sha256") != _row_hash(raw, "coverage_sha256"):
            _issue(issues, "coverage_ledger_incomplete", path_label, "Coverage hash mismatch.")
        if any(identifier not in assertions for identifier in raw.get("assertion_ids", [])):
            _issue(issues, "coverage_ledger_incomplete", path_label, "Coverage references an absent assertion.")
    if manifest.get("selected_scope_complete") is not True or not coverage_units:
        _issue(issues, "coverage_ledger_incomplete", "assurance-manifest.json", "Selected-scope coverage is not complete.")

    attestations_by_parent: dict[str, list[dict[str, Any]]] = {}
    attestation_ids: set[str] = set()
    plan_hash = manifest.get("review_plan_sha256")
    for index, raw in enumerate(loaded["review_attestations"]):
        path_label = f"review-attestations.jsonl:{index + 1}"
        if not isinstance(raw, dict) or raw.get("schema_version") != ATTESTATION_SCHEMA:
            _issue(issues, "review_attestation_invalid", path_label, "Invalid promoted attestation.")
            continue
        identifier = raw.get("attestation_id")
        if not isinstance(identifier, str) or identifier in attestation_ids:
            _issue(issues, "review_attestation_replay", path_label, "Attestation ID missing, duplicate, or replayed.")
        else:
            attestation_ids.add(identifier)
        if raw.get("review_plan_sha256") != plan_hash or raw.get("attestation_level") != ATTESTATION_LEVEL:
            _issue(issues, "review_attestation_invalid", path_label, "Attestation plan/level binding mismatch.")
        parent = str(raw.get("item_ref"))
        if parent not in by_parent or set(raw.get("assertion_ids", [])) != {row["assertion_id"] for row in by_parent[parent]}:
            _issue(issues, "review_attestation_invalid", path_label, "Attestation assertion coverage is stale or incomplete.")
        attestations_by_parent.setdefault(parent, []).append(raw)
    for parent, parent_assertions in by_parent.items():
        required = max(int(row.get("risk_tier", 2)) for row in parent_assertions)
        rows = attestations_by_parent.get(parent, [])
        reviewers = {row.get("reviewer_instance") for row in rows}
        proposers = {row.get("proposer_instance") for row in rows}
        if len(rows) != required or len(reviewers) != required:
            _issue(issues, "reviewer_coverage_insufficient", parent, "Promoted risk-tier reviewer coverage is insufficient.")
        if reviewers & proposers:
            _issue(issues, "reviewer_not_independent", parent, "Promoted review contains self-review.")
        if any(row.get("verdict") != "accepted" or row.get("evidence_support") != "full" for row in rows):
            _issue(issues, "review_attestation_invalid", parent, "Only fully accepted assertions may be promoted.")

    entity_rows = manifest.get("entity_map")
    if not isinstance(entity_rows, list) or len({row.get("item_ref") for row in entity_rows if isinstance(row, dict)}) != len(entity_rows):
        _issue(issues, "support_matrix_incomplete", "assurance-manifest.json.entity_map", "Entity mapping is missing or duplicated.")
    accepted_parents = set(by_parent)
    mapped = {
        str(row.get("item_ref"))
        for row in entity_rows or []
        if isinstance(row, dict) and isinstance(row.get("promoted_id"), str)
    }
    if accepted_parents - mapped:
        _issue(issues, "support_matrix_incomplete", "assurance-manifest.json.entity_map", "Promoted assertion parent lacks a promoted entity mapping.")

    unique = {(issue.code, issue.path, issue.message): issue for issue in issues}
    result = sorted(unique.values(), key=lambda item: (item.code, item.path, item.message))
    return result, {
        "protocol": SEMANTIC_ASSURANCE_PROTOCOL,
        "present": True,
        "assertion_count": len(assertions),
        "support_count": len(support),
        "coverage_count": len(coverage_units),
        "attestation_count": len(attestation_ids),
        "passed": not result,
    }


__all__ = [
    "ASSERTION_SCHEMA",
    "SUPPORT_SCHEMA",
    "ATTESTATION_SCHEMA",
    "COVERAGE_SCHEMA",
    "ASSURANCE_MANIFEST_SCHEMA",
    "ATTESTATION_LEVEL",
    "AssuranceIssue",
    "assurance_manifest_block",
    "derive_semantic_assurance",
    "enrich_review_material",
    "expected_attestation_id",
    "initialize_workpack_assurance",
    "is_assurance_enabled",
    "load_workpack_assurance",
    "materialize_workpack_assurance",
    "review_plan_sha256",
    "sha256_json",
    "validate_attestations",
    "validate_promoted_assurance",
    "validate_workpack_assurance",
    "write_promoted_assurance",
]
