#!/usr/bin/env python3
"""Portable read-only runtime for a composed Expert Skill."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any


RESPONSE_SCHEMA = "tkc.composite-runtime-response/v0.1"
CATALOG_SCHEMA = "tkc.composite-runtime-catalog/v0.1"
RUNTIME_VERSION = "0.1.1-composite"


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _tokens(value: str) -> set[str]:
    normalized = _normalize(value)
    words = set(re.findall(r"[a-z0-9_]+", normalized))
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", normalized))
    words.update(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
    return {word for word in words if word}


def _score(entry: dict[str, Any], query: str, terms: set[str]) -> int:
    score = 0
    normalized_query = _normalize(query)
    identifier = entry.get("id")
    if isinstance(identifier, str) and _normalize(identifier) in normalized_query:
        score += 200
    weighted_terms = entry.get("weighted_terms")
    if not isinstance(weighted_terms, dict):
        weighted_terms = {}
    for term, weight in weighted_terms.items():
        if not isinstance(term, str) or not isinstance(weight, int):
            continue
        normalized_term = _normalize(term)
        if normalized_term in normalized_query or _tokens(normalized_term) & terms:
            score += weight
    return score


def _policy_term_match(query_terms: set[str], values: Any) -> bool:
    return isinstance(values, list) and any(
        isinstance(value, str) and bool(_tokens(value) & query_terms)
        for value in values
    )


def query_catalog(catalog: dict[str, Any], query: str, limit: int = 8) -> dict[str, Any]:
    if catalog.get("schema_version") != CATALOG_SCHEMA:
        raise ValueError("catalog_schema_invalid")
    if not query.strip():
        raise ValueError("query_empty")
    query_terms = _tokens(query)
    capabilities = (
        catalog.get("capabilities")
        if isinstance(catalog.get("capabilities"), dict)
        else {}
    )
    policy = catalog.get("policy") if isinstance(catalog.get("policy"), dict) else {}
    if (
        capabilities.get("decision_support") is False
        and _policy_term_match(query_terms, policy.get("decision_guard_terms"))
    ):
        return {
            "schema_version": RESPONSE_SCHEMA,
            "runtime_version": RUNTIME_VERSION,
            "behavior": "abstain",
            "reason_codes": ["decision_capability_unavailable"],
            "matches": [],
            "conflict_ids": [],
            "gap_ids": [],
        }
    rows = [row for row in catalog.get("objects", []) if isinstance(row, dict)]
    scored = sorted(
        ((_score(row, query, query_terms), str(row.get("id")), row) for row in rows),
        key=lambda item: (-item[0], item[1]),
    )
    top = scored[0][0] if scored else 0
    floor = max(12, (top * 30 + 99) // 100)
    selected = [row for score, _, row in scored if score >= floor and score > 0][:limit]
    selected_by_id = {row["id"]: row for row in selected if isinstance(row.get("id"), str)}
    conflict_ids: list[str] = []
    for conflict in sorted(
        (row for row in catalog.get("conflicts", []) if isinstance(row, dict)),
        key=lambda row: str(row.get("id")),
    ):
        claim_ids = [value for value in conflict.get("claim_ids", []) if isinstance(value, str)]
        if _score(conflict, query, query_terms) >= 48 or any(value in selected_by_id for value in claim_ids):
            if isinstance(conflict.get("id"), str):
                conflict_ids.append(conflict["id"])
            for row in rows:
                if row.get("id") in claim_ids:
                    selected_by_id[row["id"]] = row
    gap_ids = sorted(
        row["id"]
        for row in catalog.get("gaps", [])
        if isinstance(row, dict)
        and isinstance(row.get("id"), str)
        and _score(row, query, query_terms) >= 30
    )
    matched = [
        {
            "object_id": row["id"],
            "package_id": row["package_id"],
            "module_ids": row.get("module_ids", []),
            "path": row["path"],
            "evidence_ids": row.get("evidence_ids", []),
        }
        for row in sorted(selected_by_id.values(), key=lambda value: value["id"])
    ]
    if not matched:
        return {
            "schema_version": RESPONSE_SCHEMA,
            "runtime_version": RUNTIME_VERSION,
            "behavior": "abstain",
            "reason_codes": ["known_composite_gap" if gap_ids else "no_supported_reference"],
            "matches": [],
            "conflict_ids": conflict_ids,
            "gap_ids": gap_ids,
        }
    reasons = ["supported_reference_found", "module_attribution_required"]
    if conflict_ids:
        reasons.append("unresolved_conflict_present")
    if len({row["package_id"] for row in matched}) > 1:
        reasons.append("cross_module_consensus_forbidden")
    return {
        "schema_version": RESPONSE_SCHEMA,
        "runtime_version": RUNTIME_VERSION,
        "behavior": "answer",
        "reason_codes": sorted(reasons),
        "matches": matched,
        "conflict_ids": sorted(set(conflict_ids)),
        "gap_ids": gap_ids,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--catalog", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    catalog_path = args.catalog or root / "references/runtime/catalog.json"
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        response = query_catalog(catalog, args.query, limit=args.limit)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
