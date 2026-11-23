import copy
import unittest
from science.m2_support import library, tokenizer, request, answer, sc, si
from science.support import quantity, unknown, record
import consultation_contract as cc
import science_reference_runtime as runtime


class ConsultationContractTests(unittest.TestCase):
    def setUp(self):
        self.lib = library([record("ConditionSet")]); self.tok = tokenizer()
        self.req = request(self.lib); self.index = si.build_index(self.lib, self.tok)
        self.bundle = runtime.query(self.req, self.lib, self.index, self.tok)
        self.draft = answer(self.req, self.bundle)

    def validate(self):
        return cc.validate_answer(self.draft, self.bundle, self.req, self.lib, self.index, self.tok)

    def test_structure_is_not_semantic_support(self):
        result = self.validate()
        self.assertEqual(result["status"], "structure-valid")
        self.assertFalse(result["semantically_verified"])
        self.assertEqual(result["semantic_support"], "unavailable")
        self.assertEqual(result["domain_accuracy"], "blocked")

    def test_forged_citation_rejected(self):
        self.draft["claims"][0]["supports"][0]["evidence_sha256"] = "a" * 64
        with self.assertRaises(ValueError): self.validate()

    def test_request_hash_drift_rejected(self):
        self.req["question"] += " changed"
        with self.assertRaises(ValueError): self.validate()

    def test_rehashed_bundle_cannot_hide_unknown_conditions(self):
        self.bundle["condition_results"][0]["status"] = "satisfied"
        self.bundle["bundle_sha256"] = sc.sha256_json({k:v for k,v in self.bundle.items() if k != "bundle_sha256"})
        self.draft["bundle_sha256"] = self.bundle["bundle_sha256"]
        with self.assertRaisesRegex(ValueError, "bundle_stale"): self.validate()

    def test_assurance_tamper_rejected(self):
        self.draft["assurance_sha256"] = "a" * 64
        with self.assertRaises(ValueError): self.validate()

    def test_required_missing_inputs_warnings_and_limits_cannot_be_dropped(self):
        for key in ("missing_inputs", "limitations", "warnings"):
            original = self.draft[key]; self.draft[key] = []
            with self.subTest(key=key), self.assertRaises(ValueError): self.validate()
            self.draft[key] = original

    def test_conflict_disclosure_cannot_be_dropped(self):
        self.lib[0]["package"]["conflicts"] = [{"id": "synthetic-conflict"}]
        self.bundle = runtime.query(self.req, self.lib, self.index, self.tok)
        self.draft = answer(self.req, self.bundle); self.draft["conflict_disclosures"] = []
        with self.assertRaises(ValueError): self.validate()

    def test_no_support_rejected(self):
        self.draft["claims"][0]["supports"] = []
        with self.assertRaises(ValueError): self.validate()

    def test_no_implicit_calculation_receipt(self):
        self.draft["claims"][0]["kind"] = "calculated"
        with self.assertRaisesRegex(ValueError, "calculation_unavailable"): self.validate()

    def test_derived_claim_requires_parents_and_is_not_source(self):
        claim = self.draft["claims"][0]; claim["kind"] = "derived"
        with self.assertRaises(ValueError): self.validate()
        claim["derivation_ref"] = ["missing-claim"]
        with self.assertRaises(ValueError): self.validate()

    def test_derivation_cycle_rejected(self):
        claim = self.draft["claims"][0]; claim.update(kind="derived", derivation_ref=[claim["claim_id"]])
        with self.assertRaisesRegex(ValueError, "cycle"): self.validate()

    def test_assumptions_cannot_be_source_statements(self):
        self.draft["claims"][0]["assumptions"] = ["unknown-assumption"]
        with self.assertRaises(ValueError): self.validate()

    def test_duplicate_claims_rejected(self):
        self.draft["claims"].append(copy.deepcopy(self.draft["claims"][0]))
        with self.assertRaises(ValueError): self.validate()

    def test_closed_schema_forbids_approval_flags(self):
        self.draft["semantically_verified"] = True
        with self.assertRaises(ValueError): self.validate()

    def test_unknown_request_version_and_scope_rejected(self):
        self.req["schema_version"] = "tkc.science-consultation/v999"
        with self.assertRaises(ValueError): cc.validate_request(self.req, self.lib)
        self.req = request(self.lib); self.req["source_scope"][0] = {**self.req["source_scope"][0], "manifest_sha256": "a"*64}
        with self.assertRaises(ValueError): cc.validate_request(self.req, self.lib)

    def test_duplicate_and_colliding_fact_names_rejected(self):
        fact = {"name": "thickness", "value": quantity(2,"mm","length"), "source_ref": unknown()}
        self.req["facts"] = [fact, fact]
        with self.assertRaises(ValueError): cc.validate_request(self.req, self.lib)
        self.req["facts"] = [fact]
        self.req["user_assumptions"] = [{"name": "thickness", "value": fact["value"], "reason": "test"}]
        with self.assertRaises(ValueError): cc.validate_request(self.req, self.lib)

    def test_fact_source_outside_scope_rejected(self):
        ref = copy.deepcopy(self.draft["claims"][0]["supports"][0]); ref["object_ref"]["package_id"] = "other"
        self.req["facts"] = [{"name": "thickness", "value": quantity(2), "source_ref": ref}]
        with self.assertRaises(ValueError): cc.validate_request(self.req, self.lib)

    def test_non_finite_and_extra_fields_fail_closed(self):
        self.req["facts"] = [{"name": "x", "value": quantity(float("inf")), "source_ref": unknown()}]
        with self.assertRaises(ValueError): cc.validate_request(self.req,self.lib)

    def test_explicit_derived_parent_chain_is_structural_only(self):
        self.draft["claims"].append({"claim_id": "claim-2", "text": "Synthetic inferred statement", "kind": "derived",
            "supports": [], "assumptions": [], "derivation_ref": ["claim-1"]})
        self.assertFalse(self.validate()["semantically_verified"])
