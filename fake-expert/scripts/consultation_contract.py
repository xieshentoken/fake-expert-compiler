#!/usr/bin/env python3
"""Validate host-authored consultation drafts; never generate answers or approval."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import science_contract as sc
from compiler_version import (SCIENCE_CONSULTATION_SCHEMA, SCIENCE_REFERENCE_PROTOCOL,
    SCIENCE_TOKENIZER_SCHEMA, SCIENCE_LIBRARY_SCHEMA, SCIENCE_SOURCE_MAP_SCHEMA,
    SCIENCE_SUPPORT_REVIEW_SCHEMA)

SCHEMA_PATH = sc.ROOT.parent / "assets/schemas/science-consultation.schema.json"


def shape(value, definition):
    """Resolve only the two bundled schemas, with one authority for M1 shapes."""
    sc.canonical_json(value)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    expected = {"request": SCIENCE_CONSULTATION_SCHEMA, "answer": SCIENCE_CONSULTATION_SCHEMA,
                "tokenizer": SCIENCE_TOKENIZER_SCHEMA, "library": SCIENCE_LIBRARY_SCHEMA,
                "source_map": SCIENCE_SOURCE_MAP_SCHEMA, "review_plan": SCIENCE_SUPPORT_REVIEW_SCHEMA,
                "support_review": SCIENCE_SUPPORT_REVIEW_SCHEMA}
    if schema["$id"] != SCIENCE_CONSULTATION_SCHEMA or any(
            schema["$defs"][name]["properties"]["schema_version"]["const"] != version
            for name, version in expected.items()):
        raise ValueError("consultation_schema_registry_drift")
    for name in ("request", "answer"):
        if schema["$defs"][name]["properties"]["protocol"]["const"] != SCIENCE_REFERENCE_PROTOCOL:
            raise ValueError("consultation_protocol_registry_drift")
    for row in sc._walk(schema):
        reference = row.get("$ref", "")
        if reference.startswith("science-sidecar.schema.json#/$defs/"):
            row["$ref"] = reference.split(".json", 1)[1]
    old_defs = sc._schema()["$defs"]
    if set(old_defs) & set(schema["$defs"]):
        raise ValueError("consultation_schema_definition_collision")
    schema["$defs"].update(old_defs)
    sc._check(value, schema["$defs"][definition], schema)


def tool_hashes():
    paths = [sc.ROOT / name for name in ("science_contract.py", "science_workpack.py", "science_index.py",
             "science_reference_runtime.py", "consultation_contract.py", "compiler_version.py",
             "portable_reference_runtime.py", "verify_source_anchors.py")]
    paths += [SCHEMA_PATH, sc.SCHEMA_PATH]
    return {str(p.relative_to(sc.ROOT.parent)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def validate_request(request, library):
    shape(request, "request")
    scope = request["source_scope"]
    if len({sc.sha256_json(fp) for fp in scope}) != len(scope):
        raise ValueError("consultation_duplicate_scope")
    packages = [entry["package"] for entry in library]
    if any(fp not in [p["fingerprint"] for p in packages] for fp in scope):
        raise ValueError("consultation_scope_stale_or_unknown")
    if any(len({r["name"] for r in request[key]}) != len(request[key]) for key in ("facts", "user_assumptions")):
        raise ValueError("consultation_duplicate_input")
    if {r["name"] for r in request["facts"]} & {r["name"] for r in request["user_assumptions"]}:
        raise ValueError("consultation_fact_assumption_collision")
    for fact in request["facts"]:
        if not sc._unknown(fact["source_ref"]):
            ref = fact["source_ref"]
            matches = [p for p in packages if p["fingerprint"] in scope
                       and p["fingerprint"]["package_id"] == ref["object_ref"]["package_id"]
                       and p["fingerprint"]["package_version"] == ref["object_ref"]["package_version"]]
            if len(matches) != 1:
                raise ValueError("consultation_fact_source_outside_scope")
            sc._evidence(ref, matches[0])
    return {"status": "request-valid", "execution_authorized": False}


def validate_bundle(bundle, request, library, index, tokenizer):
    # Exact reconstruction closes every nested field, including legacy assurance.
    # A caller cannot rehash a truncated or edited bundle into a stronger claim.
    from science_reference_runtime import query
    shape(bundle.get("limits"), "limits")
    expected = query(request, library, index, tokenizer, limits=bundle["limits"])
    if bundle != expected:
        raise ValueError("consultation_bundle_stale_or_modified")


def validate_answer(answer, bundle, request, library, index, tokenizer):
    validate_bundle(bundle, request, library, index, tokenizer)
    shape(answer, "answer")
    if (answer["request_id"] != request["request_id"] or answer["request_sha256"] != sc.sha256_json(request)
            or answer["bundle_sha256"] != bundle["bundle_sha256"]
            or answer["assurance_sha256"] != sc.sha256_json(bundle["assurance"])):
        raise ValueError("consultation_answer_binding_drift")
    for key in ("missing_inputs", "conflict_disclosures", "limitations", "warnings"):
        required = bundle[key] if key != "conflict_disclosures" else [r["sha256"] for r in bundle["conflicts"]]
        if len(set(answer[key])) != len(answer[key]) or not set(required) <= set(answer[key]):
            raise ValueError("consultation_disclosure_missing:" + key)
    if answer["calculation_receipt_refs"] or any(c["kind"] == "calculated" for c in answer["claims"]):
        raise ValueError("consultation_calculation_unavailable_m2")
    claims = {c["claim_id"]: c for c in answer["claims"]}
    if len(claims) != len(answer["claims"]):
        raise ValueError("consultation_duplicate_claim")
    available = [ref for row in bundle["selected_objects"] for ref in row["evidence_refs"]]
    assumptions = {r["name"] for r in request["user_assumptions"]}
    for claim in answer["claims"]:
        if bundle["behavior"] == "abstain" and claim["kind"] != "assumed":
            raise ValueError("consultation_legacy_abstain_must_be_preserved")
        if any(ref not in available for ref in claim["supports"]):
            raise ValueError("consultation_citation_stale_or_outside_bundle")
        if not set(claim["assumptions"]) <= assumptions:
            raise ValueError("consultation_unknown_assumption")
        if claim["kind"] in {"source-statement", "interpretation"} and not claim["supports"]:
            raise ValueError("consultation_support_required")
        if claim["kind"] == "source-statement" and (claim["assumptions"] or claim["derivation_ref"]):
            raise ValueError("consultation_source_claim_cannot_be_derivation")
        if claim["kind"] == "assumed" and (not claim["assumptions"] or claim["supports"]):
            raise ValueError("consultation_assumption_must_be_explicit")
        if claim["kind"] == "derived" and not claim["derivation_ref"]:
            raise ValueError("consultation_derivation_parents_required")
        if claim["kind"] != "derived" and claim["derivation_ref"]:
            raise ValueError("consultation_derivation_kind_required")
        if any(parent not in claims for parent in claim["derivation_ref"]):
            raise ValueError("consultation_derivation_parent_missing")
    visited, active = set(), set()
    def visit(ident):
        if ident in active:
            raise ValueError("consultation_derivation_cycle")
        if ident in visited:
            return
        active.add(ident)
        for parent in claims[ident]["derivation_ref"]:
            visit(parent)
        active.remove(ident); visited.add(ident)
    for ident in claims:
        visit(ident)
    return {"status": "structure-valid", "claim_count": len(claims),
            "citation_map": [{"claim_id": c["claim_id"], "supports": c["supports"]} for c in answer["claims"]],
            "semantic_support": "unavailable", "semantically_verified": False,
            "production_eligible": False, "execution_authorized": False, "domain_accuracy": "blocked",
            "assurance": copy.deepcopy(bundle["assurance"]), "warnings": copy.deepcopy(answer["warnings"])}


def prepare_support_review(answer, bundle, request, library, index, tokenizer, *, proposer_instance,
                           reviewer_instances, review_session_id):
    validate_answer(answer, bundle, request, library, index, tokenizer)
    plan = {"schema_version": SCIENCE_SUPPORT_REVIEW_SCHEMA, "request_sha256": sc.sha256_json(request),
            "bundle_sha256": bundle["bundle_sha256"], "answer_sha256": sc.sha256_json(answer),
            "claims_sha256": sc.sha256_json(answer["claims"]), "proposer_instance": proposer_instance,
            "reviewer_instances": sorted(reviewer_instances), "review_session_id": review_session_id,
            "attestation_level": "host-orchestrator-recorded-not-cryptographic"}
    plan["plan_sha256"] = sc.sha256_json(plan)
    shape(plan, "review_plan")
    if proposer_instance in reviewer_instances or len(set(reviewer_instances)) != len(reviewer_instances):
        raise ValueError("consultation_reviewer_separation_required")
    return plan


def validate_support_reviews(receipts, plan, answer, bundle, request, library, index, tokenizer):
    result = validate_answer(answer, bundle, request, library, index, tokenizer)
    if not isinstance(receipts, list) or len(receipts) > 8:
        raise ValueError("consultation_review_list_required")
    if plan is None:
        if receipts:
            raise ValueError("consultation_external_review_plan_required")
        return {**result, "review_binding_status": "unavailable", "reason_codes": ["independent_review_required"]}
    shape(plan, "review_plan")
    expected = prepare_support_review(answer, bundle, request, library, index, tokenizer,
        proposer_instance=plan["proposer_instance"], reviewer_instances=plan["reviewer_instances"],
        review_session_id=plan["review_session_id"])
    if plan != expected:
        raise ValueError("consultation_review_plan_stale")
    seen, observations, verdicts = set(), set(), []
    for receipt in receipts:
        shape(receipt, "support_review")
        who = receipt["reviewer_instance"]
        if who not in plan["reviewer_instances"] or who in seen or who == plan["proposer_instance"]:
            raise ValueError("consultation_review_identity_invalid")
        seen.add(who)
        for field in ("plan_sha256", "request_sha256", "bundle_sha256", "answer_sha256", "claims_sha256", "review_session_id"):
            if receipt[field] != plan[field]:
                raise ValueError("consultation_external_review_stale")
        rows = {row["claim_id"]: row for row in receipt["claims"]}
        if len(rows) != len(receipt["claims"]) or set(rows) != {c["claim_id"] for c in answer["claims"]}:
            raise ValueError("consultation_review_coverage_invalid")
        for claim in answer["claims"]:
            row = rows[claim["claim_id"]]
            if row["claim_sha256"] != sc.sha256_json(claim) or row["inspected_supports"] != claim["supports"]:
                raise ValueError("consultation_review_claim_or_evidence_drift")
            observation = (claim["claim_id"], row["observation"].strip())
            if observation in observations:
                raise ValueError("consultation_duplicate_review_observation")
            observations.add(observation); verdicts.append(row["verdict"])
    complete = seen == set(plan["reviewer_instances"])
    supported = complete and bool(verdicts) and all(v == "supported" for v in verdicts)
    return {**result, "review_binding_status": "valid" if complete else "incomplete",
            "semantic_support": "external-review-reported-supported" if supported else "unavailable",
            "external_verdicts": verdicts, "attestation_level": plan["attestation_level"],
            "reason_codes": [] if supported else ["independent_support_not_established"]}
