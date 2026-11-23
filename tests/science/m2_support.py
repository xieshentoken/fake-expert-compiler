"""Original in-memory software fixtures. No externally accepted Review or Gold."""
import copy
from science.support import context, record, profile, sc, unknown
import science_index as si
from compiler_version import SCIENCE_TOKENIZER_SCHEMA, SCIENCE_CONSULTATION_SCHEMA, SCIENCE_REFERENCE_PROTOCOL
from portable_reference_runtime import CATALOG_SCHEMA


def tokenizer():
    return {"schema_version": SCIENCE_TOKENIZER_SCHEMA, "aliases": [
        {"phrase": "热怎么过去", "terms": ["heat conduction"]},
        {"phrase": "热传导", "terms": ["heat conduction"]}]}


def library(records=None, count=1):
    package = context()
    package["objects"][0]["statement"] = "Heat conduction synthetic equation k K q′ q″ λ q_1 𝐪"
    package["objects"][0]["title"] = "Heat conduction"
    source = package["objects"][0]
    for i in range(1, count):
        obj = copy.deepcopy(source); obj["id"] = "synthetic-object-" + str(i)
        obj["title"] = "Definition " + str(i); obj["statement"] = "Synthetic definition " + str(i)
        package["objects"].append(obj)
        package["anchors"][0]["supports"].append(obj["id"])
    package.update(relations=[], conflicts=[], gaps=[])
    ref = si.object_ref(package, source); eref = si.evidence_refs(package, source)[0]
    def replace(value):
        if isinstance(value, dict):
            if "evidence_sha256" in value: return copy.deepcopy(eref)
            if "object_sha256" in value: return copy.deepcopy(ref)
            return {k: replace(v) for k, v in value.items()}
        return [replace(v) for v in value] if isinstance(value, list) else value
    rows = [sc.bind_record(replace(r)) for r in (records if records is not None else [record()])]
    sidecar = sc.make_sidecar(rows, package, profile(), data_origin="synthetic") if rows else None
    catalog = {"schema_version": CATALOG_SCHEMA, "objects": [{"id": source["id"], "title": "Heat conduction",
         "weighted_terms": {"heat": 20, "conduction": 20}, "evidence_ids": source["evidence_ids"]}],
         "capabilities": {"decision_support": False, "executable": False}, "policy": {
             "decision_guard_terms": ["calculate", "design"], "out_of_scope_terms": ["forbidden"],
             "assurance": {"knowledge_verified": False, "review_state": "deferred", "warnings": ["unreviewed-candidate"]}}}
    return [{"package": package, "sidecar": sidecar, "profile": profile(), "catalog": catalog}]


def request(lib, question="heat conduction"):
    return {"schema_version": SCIENCE_CONSULTATION_SCHEMA, "protocol": SCIENCE_REFERENCE_PROTOCOL,
            "request_id": "synthetic-request", "question": question, "intent": "explain", "domain_candidates": [],
            "source_scope": [e["package"]["fingerprint"] for e in lib], "facts": [], "requested_output": "answer-draft",
            "user_assumptions": [], "execution_requested": False}


def answer(req, bundle, claims=None):
    supports = [r for row in bundle["selected_objects"] for r in row["evidence_refs"]]
    return {"schema_version": SCIENCE_CONSULTATION_SCHEMA, "protocol": SCIENCE_REFERENCE_PROTOCOL,
        "request_id": req["request_id"], "request_sha256": sc.sha256_json(req), "bundle_sha256": bundle["bundle_sha256"],
        "claims": claims if claims is not None else [{"claim_id": "claim-1", "text": "An unsupported invented sentence for structural tests only.",
            "kind": "source-statement", "supports": supports[:1], "assumptions": [], "derivation_ref": []}],
        "missing_inputs": bundle["missing_inputs"], "conflict_disclosures": [c["sha256"] for c in bundle["conflicts"]],
        "limitations": bundle["limitations"], "calculation_receipt_refs": [],
        "assurance_sha256": sc.sha256_json(bundle["assurance"]), "warnings": bundle["warnings"]}
