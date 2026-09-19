#!/usr/bin/env python3
"""Validate answer-free, grouped evaluation inputs and report imported observations.

This module does not grade answers, author Gold, or grant knowledge/execution
authority. Independent reviewers and an external evaluator supply their own
hash-bound artifacts through the existing composer review contract.
"""
from __future__ import annotations

import argparse
import copy
from pathlib import Path
import re
import sys

import science_contract as sc
import science_workpack as sw
from compiler_version import (SCIENCE_EVALUATION_REVIEW_SCHEMA, SCIENCE_EVALUATION_REPORT_SCHEMA,
                              SCIENCE_LIFECYCLE_COMPILER_VERSION)

LAYERS = ("unit", "parse", "retrieval", "support", "applicability", "execution", "host")
CATEGORIES = ("concept-paraphrase", "symbol", "conditions", "scalar-calculation", "conflict-or-complex")


def validate_suite(suite, library=None):
    sw.lifecycle_shape(suite, "evaluation_suite")
    seen = set()
    groups = {}
    for case in suite["cases"]:
        if case["case_id"] in seen:
            raise ValueError("science_evaluation_duplicate_case")
        seen.add(case["case_id"])
        if case["split"] == "blind" and case["data_origin"] != "source-backed":
            raise ValueError("science_evaluation_synthetic_blind_forbidden")
        if case["data_origin"] == "source-backed" and not case["evidence_refs"]:
            raise ValueError("science_evaluation_source_evidence_required")
        grouping = case["groups"]
        for dimension, group in grouping.items():
            if group in {"unknown", "not-applicable"}:
                continue
            # Chapter numbers are local to a source. Formula families and
            # sample identities are global groups supplied by the curator.
            key = (dimension, grouping["source_sha256"] + ":" + group if dimension == "chapter" else group)
            if key in groups and groups[key] != case["split"]:
                raise ValueError("science_evaluation_group_leak:" + dimension)
            groups[key] = case["split"]
    if library is not None:
        import science_index as index
        index.validate_library(library)
        for case in suite["cases"]:
            if case["data_origin"] != "source-backed":
                continue
            for evidence in case["evidence_refs"]:
                ref = evidence["object_ref"]
                matches = [entry for entry in library if entry["package"]["fingerprint"]["package_id"] == ref["package_id"]
                           and entry["package"]["fingerprint"]["package_version"] == ref["package_version"]]
                if len(matches) != 1:
                    raise ValueError("science_evaluation_source_package_missing")
                entry = matches[0]
                anchor = sc._evidence(evidence, entry["package"])
                sources = [s for s in entry["package"]["manifest"]["sources"]
                           if s.get("source_id", s.get("id")) == anchor["source_id"]]
                if len(sources) != 1 or case["groups"]["source_sha256"] != sources[0]["sha256"]:
                    raise ValueError("science_evaluation_source_hash_mismatch")
                if entry["sidecar"] is None or entry["sidecar"]["data_origin"] != "source-backed-proposal":
                    raise ValueError("science_evaluation_real_source_binding_required")
    return {"status": "suite-shape-valid", "case_count": len(seen),
            "source_bindings": "validated" if library is not None else "not-validated",
            "independence_verified": False, "knowledge_verified": False}


def blind_plan(suite):
    validate_suite(suite)
    return {"suite_sha256": sc.sha256_json(suite), "status": "paused-independent-evaluation",
            "cases": [copy.deepcopy(c) for c in suite["cases"] if c["split"] == "blind"],
            "contains_answers": False, "knowledge_verified": False}


def freeze_review(suite, gold_payload_sha256, *, proposer_instance, reviewer_instances=None,
                  review_session_id=None, created_at):
    validate_suite(suite)
    if not isinstance(gold_payload_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", gold_payload_sha256) is None:
        raise ValueError("science_evaluation_external_gold_hash_required")
    reviewers = sw._external_registry(proposer_instance, reviewer_instances, review_session_id)
    tools = sw.lifecycle_tool_hashes()
    bound = {"suite_sha256": sc.sha256_json(suite), "gold_payload_sha256": gold_payload_sha256,
             "proposer_instance": proposer_instance, "reviewer_instances": reviewers,
             "review_session_id": review_session_id, "created_at": created_at, "tool_sha256s": tools}
    items = [{"item_id": c["case_id"], "item_type": "alignment_candidate", "risk_tier": "high",
              "required_reviewer_count": 2, "reasons": ["independent_science_blind_case"],
              "candidate_sha256": sc.sha256_json({"case": c, "bindings": bound}),
              "evidence_binding_ids": [sc.sha256_json(e) for e in c["evidence_refs"]], "status": "pending"}
             for c in suite["cases"] if c["split"] == "blind"]
    plan = sw._composer_plan(workpack_id="science-evaluation-" + sc.sha256_json(bound)[:24], items=items,
        sources=sorted({c["groups"]["source_sha256"] for c in suite["cases"] if c["split"] == "blind"}),
        proposer=proposer_instance, reviewers=reviewers, session=review_session_id, created_at=created_at)
    value = {"schema_version": SCIENCE_EVALUATION_REVIEW_SCHEMA, **bound, "review_plan": plan,
             "status": "paused-independent-review", "verified_gold": False, "knowledge_verified": False}
    value["review_sha256"] = sc.sha256_json(value)
    return value


def evaluate(suite, results, *, library=None, review=None, attestations=None):
    validate_suite(suite, library)
    sw.lifecycle_shape(results, "evaluation_results")
    if results["suite_sha256"] != sc.sha256_json(suite):
        raise ValueError("science_evaluation_suite_hash_drift")
    cases = {c["case_id"]: c for c in suite["cases"]}
    rows = {}
    for row in results["rows"]:
        ident = row["case_id"]
        if ident not in cases or ident in rows or row["case_sha256"] != sc.sha256_json(cases[ident]):
            raise ValueError("science_evaluation_result_identity_or_hash_invalid")
        if row["status"] in {"passed", "failed"} and row["evidence_sha256"] is None:
            raise ValueError("science_evaluation_result_evidence_required")
        if row["status"] in {"skipped", "blocked"} and not row["reason_codes"]:
            raise ValueError("science_evaluation_result_reason_required")
        rows[ident] = row
    layers = []
    for layer in LAYERS:
        selected = [c for c in suite["cases"] if c["layer"] == layer]
        counts = {name: 0 for name in ("passed", "failed", "skipped", "blocked", "not_run")}
        for case in selected:
            counts[rows.get(case["case_id"], {}).get("status", "not_run")] += 1
        layers.append({"layer": layer, "planned": len(selected), "executed": counts["passed"] + counts["failed"], **counts})
    reasons = []
    blind = [c for c in suite["cases"] if c["split"] == "blind"]
    if not blind:
        reasons.append("independent_blind_cases_required")
    if len(blind) < 100 or any(sum(c["category"] == category for c in blind) < 20 for category in CATEGORIES):
        reasons.append("blind_minimum_100_and_20_per_category_required")
    if library is None:
        reasons.append("real_source_bindings_required")
    if review is None:
        reasons.append("independent_gold_review_required")
    else:
        sc.canonical_json(review)
        expected = freeze_review(suite, review.get("gold_payload_sha256"), proposer_instance=review.get("proposer_instance"),
            reviewer_instances=review.get("reviewer_instances"), review_session_id=review.get("review_session_id"), created_at=review.get("created_at"))
        if review != expected:
            raise ValueError("science_evaluation_review_stale_or_modified")
        check = sw._composer_review(review["review_plan"], attestations or [])
        reasons.extend(check["reason_codes"])
        if results["evaluator_instance"] in review["reviewer_instances"] + [review["proposer_instance"]]:
            reasons.append("independent_evaluator_required")
    if any(rows.get(c["case_id"], {}).get("status") not in {"passed", "failed"} for c in blind):
        reasons.append("blind_results_incomplete")
    # These are imported external judgments, not a score computed by grading
    # hidden answers here. Never collapse the seven layers into an expert score.
    domain = None
    if not reasons:
        domain = [{"layer": layer, "denominator": len(selected),
                   "passed": sum(rows[c["case_id"]]["status"] == "passed" for c in selected)}
                  for layer in LAYERS if (selected := [c for c in blind if c["layer"] == layer])]
    value = {"schema_version": SCIENCE_EVALUATION_REPORT_SCHEMA, "compiler_version": SCIENCE_LIFECYCLE_COMPILER_VERSION,
        "suite_sha256": sc.sha256_json(suite), "results_sha256": sc.sha256_json(results), "layers": layers,
        "evidence_mode": "imported-observations-not-internal-grading", "domain_accuracy": domain,
        "domain_accuracy_status": "blocked" if reasons else "externally-reported",
        "reason_codes": sorted(set(reasons)), "unreported_case_ids": sorted(set(cases) - set(rows)),
        "cases": [{"case_id": c["case_id"], "layer": c["layer"], "category": c["category"],
                   "split": c["split"], "data_origin": c["data_origin"],
                   "status": rows.get(c["case_id"], {}).get("status", "not_run"),
                   "reason_codes": rows.get(c["case_id"], {}).get("reason_codes", ["result_not_supplied"])} for c in suite["cases"]],
        "gold_payload_verified": False, "knowledge_verified": False, "execution_authorized": False,
        "production_knowledge_publication": "blocked"}
    value["report_sha256"] = sc.sha256_json(value)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("validate-suite", "blind-plan", "freeze-review", "report"):
        p = sub.add_parser(command)
        p.add_argument("--suite", type=Path, required=True)
        p.add_argument("--library", type=Path)
        if command != "validate-suite":
            p.add_argument("--output", type=Path, required=True)
        if command == "freeze-review":
            p.add_argument("--gold-payload-sha256", required=True)
            p.add_argument("--proposer-instance", required=True)
            p.add_argument("--reviewer-instance", action="append")
            p.add_argument("--review-session-id")
            p.add_argument("--created-at", required=True)
        if command == "report":
            p.add_argument("--results", type=Path, required=True)
            p.add_argument("--review", type=Path)
            p.add_argument("--attestations", type=Path)
    args = parser.parse_args(argv)
    try:
        import science_index as index
        suite = sc.load_json(args.suite)
        library = index.load_library(args.library)[0] if args.library else None
        validate_suite(suite, library)
        if args.command == "validate-suite":
            value = validate_suite(suite, library)
        elif args.command == "blind-plan":
            value = blind_plan(suite)
        elif args.command == "freeze-review":
            value = freeze_review(suite, args.gold_payload_sha256, proposer_instance=args.proposer_instance,
                reviewer_instances=args.reviewer_instance, review_session_id=args.review_session_id, created_at=args.created_at)
        else:
            value = evaluate(suite, sc.load_json(args.results), library=library,
                review=sc.load_json(args.review) if args.review else None,
                attestations=sc.load_json(args.attestations) if args.attestations else None)
        if args.command != "validate-suite":
            if args.library:
                index.check_output(args.output, args.library)
            if args.output.resolve().is_relative_to(sc.ROOT.parent.resolve()):
                raise ValueError("science_output_inside_compiler_forbidden")
            sw._write_new(args.output, value)
        print(sc.canonical_json(value).decode())
        return 2 if value.get("domain_accuracy_status") == "blocked" else 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print("error: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
