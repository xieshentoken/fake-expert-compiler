#!/usr/bin/env python3
"""Portable, read-only retrieval runtime for a compiled reference-tier Skill."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any


CATALOG_SCHEMA = "tkc.runtime-catalog/v0.1"
RESPONSE_SCHEMA = "tkc.runtime-response/v0.1"
RUNTIME_VERSION = "deterministic-token-retrieval-v0.1"
EVALUATOR_VERSION = "deterministic-reference-evaluator-v0.1"

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "does",
    "for", "from", "give", "how", "in", "is", "it", "of", "on", "or",
    "that", "the", "their", "this", "to", "use", "what", "when", "which",
    "why", "with",
}

TOKEN_RE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*|[\u3400-\u4dbf\u4e00-\u9fff]+")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = normalized.replace("\\", " ")
    normalized = re.sub(r"[{}^]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def tokenize(value: str) -> list[str]:
    return [
        token
        for token in TOKEN_RE.findall(normalize(value))
        if token not in STOPWORDS and len(token) > 1
    ]


def _add_weighted_terms(value: Any, weight: int, result: dict[str, int]) -> None:
    if isinstance(value, str):
        for token in tokenize(value):
            result[token] = max(result.get(token, 0), weight)
    elif isinstance(value, list):
        for item in value:
            _add_weighted_terms(item, weight, result)
    elif isinstance(value, dict):
        for item in value.values():
            _add_weighted_terms(item, weight, result)


def object_weighted_terms(value: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    _add_weighted_terms(value.get("title"), 12, result)
    _add_weighted_terms(value.get("statement"), 6, result)
    _add_weighted_terms(value.get("formula"), 8, result)
    _add_weighted_terms(value.get("assumptions"), 3, result)
    _add_weighted_terms(value.get("valid_when"), 3, result)
    _add_weighted_terms(value.get("fails_when"), 4, result)
    _add_weighted_terms(value.get("applicability"), 2, result)
    return dict(sorted(result.items()))


def conflict_weighted_terms(value: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    _add_weighted_terms(value.get("topic"), 12, result)
    return dict(sorted(result.items()))


def gap_weighted_terms(value: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    _add_weighted_terms(value.get("statement"), 10, result)
    return dict(sorted(result.items()))


def _entry_score(entry: dict[str, Any], query: str, query_terms: set[str]) -> int:
    score = 0
    entry_id = entry.get("id")
    if isinstance(entry_id, str) and entry_id.casefold() in query.casefold():
        score += 1000
    title = entry.get("title")
    if isinstance(title, str) and normalize(title) in normalize(query):
        score += 80
    weighted_terms = entry.get("weighted_terms")
    if isinstance(weighted_terms, dict):
        for term in query_terms:
            weight = weighted_terms.get(term)
            if isinstance(weight, int) and weight > 0:
                score += weight
    return score


def _policy_term_match(query_terms: set[str], values: Any) -> bool:
    return isinstance(values, list) and any(
        isinstance(value, str) and value.casefold() in query_terms
        for value in values
    )


def query_catalog(
    catalog: dict[str, Any], query: str, *, limit: int = 8
) -> dict[str, Any]:
    if catalog.get("schema_version") != CATALOG_SCHEMA:
        raise ValueError("runtime_catalog_schema_invalid")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("runtime_query_required")
    if not 1 <= limit <= 25:
        raise ValueError("runtime_limit_out_of_range")

    query_terms = set(tokenize(query))
    policy = catalog.get("policy") if isinstance(catalog.get("policy"), dict) else {}
    capabilities = catalog.get("capabilities") if isinstance(catalog.get("capabilities"), dict) else {}

    if (
        capabilities.get("decision_support") is False
        and _policy_term_match(query_terms, policy.get("decision_guard_terms"))
    ):
        return {
            "schema_version": RESPONSE_SCHEMA,
            "runtime_version": RUNTIME_VERSION,
            "behavior": "abstain",
            "reason_codes": ["decision_capability_unavailable"],
            "matched_object_ids": [],
            "evidence_ids": [],
            "conflict_ids": [],
            "gap_ids": [],
        }
    if _policy_term_match(query_terms, policy.get("out_of_scope_terms")):
        return {
            "schema_version": RESPONSE_SCHEMA,
            "runtime_version": RUNTIME_VERSION,
            "behavior": "abstain",
            "reason_codes": ["outside_package_scope"],
            "matched_object_ids": [],
            "evidence_ids": [],
            "conflict_ids": [],
            "gap_ids": [],
        }

    entries = catalog.get("objects")
    if not isinstance(entries, list):
        raise ValueError("runtime_catalog_objects_invalid")
    scored = sorted(
        (
            (_entry_score(entry, query, query_terms), str(entry.get("id")), entry)
            for entry in entries
            if isinstance(entry, dict)
        ),
        key=lambda row: (-row[0], row[1]),
    )
    top_score = scored[0][0] if scored else 0
    score_floor = max(12, (top_score * 30 + 99) // 100)
    selected = [entry for score, _, entry in scored if score >= score_floor][:limit]
    score_by_id = {
        entry.get("id"): score
        for score, _, entry in scored
        if isinstance(entry.get("id"), str)
    }
    selected_by_id = {
        entry["id"]: entry
        for entry in selected
        if isinstance(entry.get("id"), str)
    }

    conflict_ids: list[str] = []
    conflicts = catalog.get("conflicts")
    if isinstance(conflicts, list):
        for conflict in sorted(
            (row for row in conflicts if isinstance(row, dict)),
            key=lambda row: str(row.get("id")),
        ):
            claim_ids = [
                value for value in conflict.get("claim_ids", [])
                if isinstance(value, str)
            ]
            conflict_score = _entry_score(conflict, query, query_terms)
            explicitly_named = any(claim_id.casefold() in query.casefold() for claim_id in claim_ids)
            strongly_matched_claim = any(score_by_id.get(claim_id, 0) >= 80 for claim_id in claim_ids)
            if conflict_score >= 48 or explicitly_named or strongly_matched_claim:
                conflict_id = conflict.get("id")
                if isinstance(conflict_id, str):
                    conflict_ids.append(conflict_id)
                object_by_id = {
                    row.get("id"): row for row in entries if isinstance(row, dict)
                }
                for claim_id in claim_ids:
                    claim = object_by_id.get(claim_id)
                    if isinstance(claim, dict):
                        selected_by_id[claim_id] = claim

    gap_ids: list[str] = []
    gaps = catalog.get("gaps")
    if isinstance(gaps, list):
        for gap in sorted(
            (row for row in gaps if isinstance(row, dict)),
            key=lambda row: str(row.get("id")),
        ):
            if _entry_score(gap, query, query_terms) >= 30:
                gap_id = gap.get("id")
                if isinstance(gap_id, str):
                    gap_ids.append(gap_id)

    if not selected_by_id:
        reason = "known_package_gap" if gap_ids else "no_supported_reference"
        return {
            "schema_version": RESPONSE_SCHEMA,
            "runtime_version": RUNTIME_VERSION,
            "behavior": "abstain",
            "reason_codes": [reason],
            "matched_object_ids": [],
            "evidence_ids": [],
            "conflict_ids": conflict_ids,
            "gap_ids": gap_ids,
        }

    ordered_objects = sorted(selected_by_id)
    evidence_ids = sorted(
        {
            evidence_id
            for entry in selected_by_id.values()
            for evidence_id in entry.get("evidence_ids", [])
            if isinstance(evidence_id, str)
        }
    )
    reason_codes = ["supported_reference_found"]
    if conflict_ids:
        reason_codes.append("unresolved_conflict_present")
    if gap_ids:
        reason_codes.append("known_package_gap_present")
    return {
        "schema_version": RESPONSE_SCHEMA,
        "runtime_version": RUNTIME_VERSION,
        "behavior": "answer",
        "reason_codes": reason_codes,
        "matched_object_ids": ordered_objects,
        "evidence_ids": evidence_ids,
        "conflict_ids": sorted(set(conflict_ids)),
        "gap_ids": sorted(set(gap_ids)),
    }


def evaluate_response(test: dict[str, Any], response: dict[str, Any]) -> list[dict[str, Any]]:
    expected = test["expected"]
    checks: list[tuple[str, bool]] = [
        ("behavior_matches", response.get("behavior") == expected["behavior"]),
        (
            "required_objects_present",
            set(expected["required_object_ids"]).issubset(response.get("matched_object_ids", [])),
        ),
        (
            "forbidden_objects_absent",
            not set(expected["forbidden_object_ids"]) & set(response.get("matched_object_ids", [])),
        ),
        (
            "required_evidence_present",
            set(expected["required_evidence_ids"]).issubset(response.get("evidence_ids", [])),
        ),
        (
            "required_conflicts_present",
            set(expected["required_conflict_ids"]).issubset(response.get("conflict_ids", [])),
        ),
        (
            "required_reasons_present",
            set(expected["required_reason_codes"]).issubset(response.get("reason_codes", [])),
        ),
        (
            "answer_has_evidence",
            response.get("behavior") != "answer" or bool(response.get("evidence_ids")),
        ),
        (
            "abstention_has_no_objects",
            response.get("behavior") == "answer" or not response.get("matched_object_ids"),
        ),
    ]
    return [{"code": code, "passed": passed} for code, passed in checks]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument(
        "--catalog",
        type=Path,
        help="Defaults to references/runtime/catalog.json relative to the Skill root.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    script_root = Path(__file__).resolve().parent
    catalog_path = args.catalog or script_root.parent / "references/runtime/catalog.json"
    try:
        with catalog_path.open("r", encoding="utf-8") as handle:
            catalog = json.load(handle)
        response = query_catalog(catalog, args.query, limit=args.limit)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
