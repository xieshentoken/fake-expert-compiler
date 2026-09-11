#!/usr/bin/env python3
"""Deterministically generate a seven-category competency suite from a reviewed draft.

The ``equation`` category is included only when the package actually contains
equation objects (``Equation`` type or a ``formula`` field); pure-concept
packages get the remaining six categories, and the reference-tier publisher
accepts that adaptive set.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from expert_skill_contract import load_json, load_jsonl, object_has_equation
from portable_reference_runtime import evaluate_response, query_catalog, tokenize
from reference_tier import (
    REQUIRED_CATEGORIES,
    SUITE_SCHEMA,
    build_runtime_catalog,
)

DEFAULT_DECISION_GUARD_TERMS = [
    "approve",
    "certification",
    "certify",
    "choose",
    "decision",
    "ready",
    "recommend",
    "safe",
    "safety",
    "stability",
    "stable",
]

RUNTIME_LIMITS = {
    "definition": 8,
    "comparison": 4,
    "equation": 10,
    "failure-condition": 10,
    "trace": 8,
    "decision": 8,
    "out-of-scope": 8,
}

SUPPORTED_ANSWER_REASON = "supported_reference_found"
CONFLICT_REASON = "unresolved_conflict_present"
DECISION_REASON = "decision_capability_unavailable"
SCOPE_REASON = "outside_package_scope"


class CompetencyGenerationError(RuntimeError):
    """Stable deterministic suite-generation failure."""


def _tokens(text: str) -> set[str]:
    return set(tokenize(text))


def _load_objects(package: Path) -> list[dict[str, Any]]:
    index = load_json(package / "references/knowledge/index.json")
    objects: list[dict[str, Any]] = []
    for record in index.get("objects", []):
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise CompetencyGenerationError("knowledge_index_invalid")
        obj = load_json(package / record["path"])
        objects.append(obj)
    return sorted(objects, key=lambda row: str(row.get("id")))


def _load_conflicts(package: Path) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for conflict in load_jsonl(package / "references/conflicts.jsonl"):
        if not isinstance(conflict, dict):
            raise CompetencyGenerationError("conflicts_invalid")
        claim_ids = sorted(
            claim.get("claim_id")
            for claim in conflict.get("claims", [])
            if isinstance(claim, dict) and isinstance(claim.get("claim_id"), str)
        )
        conflicts.append(
            {
                "id": str(conflict.get("id")),
                "claim_ids": claim_ids,
            }
        )
    return sorted(conflicts, key=lambda row: row["id"])


def _eligible(
    objects: list[dict[str, Any]],
    guard_terms: set[str],
    scope_terms: set[str],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for obj in objects:
        title = obj.get("title")
        evidence = obj.get("evidence_ids")
        if not isinstance(title, str) or not title:
            continue
        if not isinstance(evidence, list) or not evidence:
            continue
        if _tokens(title) & (guard_terms | scope_terms):
            continue
        output.append(obj)
    return output


def _collision_free(prompt: str, guard_terms: set[str], scope_terms: set[str]) -> bool:
    tokens = _tokens(prompt)
    return not (tokens & guard_terms) and not (tokens & scope_terms)


def _prompt_with_id(prefix: str, obj: dict[str, Any]) -> str:
    title = obj["title"]
    return f"{prefix} {title} (object {obj['id']})."


def _answer_expected(
    obj: dict[str, Any],
    conflicts: list[dict[str, Any]],
) -> dict[str, Any]:
    reason_codes = [SUPPORTED_ANSWER_REASON]
    required_conflicts: list[str] = []
    for conflict in conflicts:
        if obj.get("id") in conflict["claim_ids"]:
            required_conflicts.append(conflict["id"])
    if required_conflicts:
        reason_codes.append(CONFLICT_REASON)
    return {
        "behavior": "answer",
        "required_object_ids": [obj["id"]],
        "forbidden_object_ids": [],
        "required_evidence_ids": sorted(set(obj.get("evidence_ids", []))),
        "required_conflict_ids": required_conflicts,
        "required_reason_codes": sorted(reason_codes),
    }


def _select(
    pool: list[dict[str, Any]],
    prefix: str,
    guard_terms: set[str],
    scope_terms: set[str],
    category: str,
    conflicts: list[dict[str, Any]],
    *,
    with_conflicts: bool,
) -> dict[str, Any]:
    for obj in pool:
        prompt = _prompt_with_id(prefix, obj)
        if _collision_free(prompt, guard_terms, scope_terms):
            return {
                "id": f"ct-{category}-01",
                "category": category,
                "prompt": prompt,
                "runtime": {"limit": RUNTIME_LIMITS[category]},
                "expected": _answer_expected(obj, conflicts if with_conflicts else []),
            }
    if category == "equation":
        raise CompetencyGenerationError(
            "no_collision_free_object: category=equation — the package contains "
            "equation objects but every candidate title collides with a decision "
            "guard or out-of-scope term; use fewer --decision-guard-term values, a "
            "narrower --out-of-scope-term, or hand-write this test"
        )
    raise CompetencyGenerationError(
        f"no_collision_free_object: category={category}"
    )


def generate_suite(
    package: Path,
    out_of_scope_terms: list[str],
    decision_guard_terms: list[str],
) -> dict[str, Any]:
    objects = _load_objects(package)
    conflicts = _load_conflicts(package)
    if not objects:
        raise CompetencyGenerationError("no_knowledge_objects")

    if not out_of_scope_terms:
        raise CompetencyGenerationError(
            "out_of_scope_term_required: pass at least one --out-of-scope-term"
        )
    guard_terms = {term.casefold() for term in decision_guard_terms or DEFAULT_DECISION_GUARD_TERMS}
    scope_terms = {term.casefold() for term in out_of_scope_terms}
    for term in out_of_scope_terms:
        if not _tokens(term):
            raise CompetencyGenerationError(
                f"out_of_scope_term_unusable: {term!r} must tokenize to at least one non-stopword"
            )
    if scope_terms & guard_terms:
        raise CompetencyGenerationError("guard_and_scope_term_overlap")

    eligible = _eligible(objects, guard_terms, scope_terms)
    equation_pool = [
        obj for obj in eligible if object_has_equation(obj)
    ]
    failure_pool = [obj for obj in eligible if obj.get("fails_when")]

    tests: list[dict[str, Any]] = []

    tests.append(
        _select(eligible, "Define", guard_terms, scope_terms, "definition", [], with_conflicts=False)
    )
    if equation_pool:
        tests.append(
            _select(
                equation_pool,
                "Explain the printed",
                guard_terms,
                scope_terms,
                "equation",
                conflicts,
                with_conflicts=True,
            )
        )
    tests.append(
        _select(
            failure_pool,
            "When does",
            guard_terms,
            scope_terms,
            "failure-condition",
            [],
            with_conflicts=False,
        )
    )

    comparison = None
    for index, first in enumerate(eligible):
        for second in eligible[index + 1 :]:
            prompt = (
                f"Compare {first['title']} with {second['title']} "
                f"(objects {first['id']} and {second['id']})."
            )
            if not _collision_free(prompt, guard_terms, scope_terms):
                continue
            comparison = {
                "id": "ct-comparison-01",
                "category": "comparison",
                "prompt": prompt,
                "runtime": {"limit": RUNTIME_LIMITS["comparison"]},
                "expected": {
                    "behavior": "answer",
                    "required_object_ids": sorted([first["id"], second["id"]]),
                    "forbidden_object_ids": [],
                    "required_evidence_ids": sorted(
                        set(first.get("evidence_ids", [])) | set(second.get("evidence_ids", []))
                    ),
                    "required_conflict_ids": [],
                    "required_reason_codes": [SUPPORTED_ANSWER_REASON],
                },
            }
            break
        if comparison is not None:
            break
    if comparison is None:
        raise CompetencyGenerationError(
            "no_collision_free_pair: category=comparison"
        )
    tests.append(comparison)

    trace_test = _select(
        list(reversed(eligible)),
        "Trace object",
        guard_terms,
        scope_terms,
        "trace",
        [],
        with_conflicts=False,
    )
    tests.append(trace_test)

    scope_term = out_of_scope_terms[0]
    tests.append(
        {
            "id": "ct-out-of-scope-01",
            "category": "out-of-scope",
            "prompt": f"Calculate {scope_term} from this package.",
            "runtime": {"limit": RUNTIME_LIMITS["out-of-scope"]},
            "expected": {
                "behavior": "abstain",
                "required_object_ids": [],
                "forbidden_object_ids": [],
                "required_evidence_ids": [],
                "required_conflict_ids": [],
                "required_reason_codes": [SCOPE_REASON],
            },
        }
    )
    if _tokens(tests[-1]["prompt"]) & guard_terms:
        raise CompetencyGenerationError(
            f"out_of_scope_term_overlaps_guard: {scope_term!r}"
        )

    decision_prompt = "Certify that this design is safe and ready for operation."
    if not _tokens(decision_prompt) & guard_terms:
        raise CompetencyGenerationError("decision_guard_terms_unusable")
    if _tokens(decision_prompt) & scope_terms:
        raise CompetencyGenerationError("decision_prompt_overlaps_scope")
    tests.append(
        {
            "id": "ct-decision-01",
            "category": "decision",
            "prompt": decision_prompt,
            "runtime": {"limit": RUNTIME_LIMITS["decision"]},
            "expected": {
                "behavior": "abstain",
                "required_object_ids": [],
                "forbidden_object_ids": [],
                "required_evidence_ids": [],
                "required_conflict_ids": [],
                "required_reason_codes": [DECISION_REASON],
            },
        }
    )

    tests.sort(key=lambda row: str(row.get("id")))
    suite = {
        "schema_version": SUITE_SCHEMA,
        "runtime_policy": {
            "decision_guard_terms": sorted(guard_terms),
            "out_of_scope_terms": sorted(scope_terms),
        },
        "tests": tests,
    }
    _self_check(package, suite)
    return suite


def _self_check(package: Path, suite: dict[str, Any]) -> None:
    catalog = build_runtime_catalog(package, suite["runtime_policy"])
    for test in suite["tests"]:
        response = query_catalog(catalog, test["prompt"], limit=test["runtime"]["limit"])
        failed = [
            check["code"]
            for check in evaluate_response(test, response)
            if not check["passed"]
        ]
        if failed:
            raise CompetencyGenerationError(
                f"generated_test_would_fail: test={test['id']} checks={','.join(failed)}"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Reviewed draft package directory.")
    parser.add_argument("--output", required=True, type=Path, help="Output competency-suite JSON path.")
    parser.add_argument(
        "--out-of-scope-term",
        action="append",
        required=True,
        help="Domain term the package does not cover (repeatable).",
    )
    parser.add_argument(
        "--decision-guard-term",
        action="append",
        default=[],
        help="Decision verb that must abstain (repeatable; defaults are used when omitted).",
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        suite = generate_suite(args.input, args.out_of_scope_term, args.decision_guard_term)
    except (CompetencyGenerationError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(suite, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = {
        "output": str(args.output.expanduser().resolve()),
        "test_count": len(suite["tests"]),
        "categories": sorted({test["category"] for test in suite["tests"]}),
        "self_check": "passed",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) if args.json_output else
          "\n".join(f"{key}: {value}" for key, value in summary.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
