import copy
import json
import unittest
from science.support import sc, sidecar, context, profile, record, reseal, object_ref


class ScienceContractTests(unittest.TestCase):
    def validate(self, value):
        return sc.validate_sidecar(value, context(), profile())

    def test_candidate_has_no_production_authority(self):
        result = self.validate(sidecar())
        self.assertEqual(result["status"], "candidate-valid")
        for key in ("production_eligible", "knowledge_verified", "executable"):
            self.assertFalse(result[key])
        self.assertEqual(result["domain_accuracy"], "blocked")

    def test_optional_absent_vs_required_unknown_version(self):
        self.assertEqual(self.validate(None)["status"], "unavailable")
        value = sidecar(); value["schema_version"] = "tkc.science-sidecar/v99"
        with self.assertRaises(ValueError): self.validate(value)

    def test_extra_fields_are_rejected_at_every_level(self):
        for route in ([], ["base_package"], ["records", 0], ["records", 0, "provenance", 0]):
            value = sidecar(); target = value
            for key in route: target = target[key]
            target["executable_override"] = True
            with self.subTest(route=route), self.assertRaises(ValueError): self.validate(value)

    def test_nan_and_infinity_rejected(self):
        for invalid in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError): sc.canonical_json({"value": invalid})

    def test_size_depth_and_string_limits(self):
        value = sidecar(); value["records"][0]["original_glyph"] = "x" * 5000
        with self.assertRaises(ValueError): self.validate(value)
        with self.assertRaises(ValueError): sc.canonical_json([0] * 1000000)
        value = 0
        for _ in range(80): value = [value]
        with self.assertRaises(ValueError): sc.canonical_json(value)

    def test_inventory_and_content_drift(self):
        value = sidecar(); value["records"][0]["unit"] = "1"
        with self.assertRaises(ValueError): self.validate(reseal(value))
        value = sidecar(); value["inventory"] = []
        with self.assertRaises(ValueError): self.validate(value)

    def test_base_profile_and_tool_drift(self):
        for field in ("base_package", "profile_sha256", "tool_sha256s"):
            value = sidecar()
            if field == "base_package": value[field]["package_version"] = "0.2.0"
            elif field == "tool_sha256s": value[field]["science_contract.py"] = "a" * 64
            else: value[field] = "a" * 64
            with self.subTest(field=field), self.assertRaises(ValueError): self.validate(reseal(value))

    def test_cross_package_object_and_evidence_rejected(self):
        value = sidecar(); value["records"][0]["source_object_ref"]["package_id"] = "different-package"
        value["records"][0] = sc.bind_record(value["records"][0])
        with self.assertRaises(ValueError): self.validate(reseal(value))

    def test_focus_evidence_cannot_be_borrowed(self):
        ctx = context(); ctx["anchors"][0]["supports"] = ["another-object"]
        with self.assertRaises(ValueError): sc.validate_sidecar(sidecar(), ctx, profile())

    def test_missing_field_provenance(self):
        value = sidecar(); value["records"][0]["provenance"].pop()
        value["records"][0] = sc.bind_record(value["records"][0])
        with self.assertRaises(ValueError): self.validate(reseal(value))

    def test_empty_source_evidence_is_not_explicit_support(self):
        value = sidecar(); value["records"][0]["provenance"][0]["evidence_refs"] = []
        value["records"][0] = sc.bind_record(value["records"][0])
        with self.assertRaises(ValueError): self.validate(reseal(value))

    def test_stable_identity_and_separate_content_hash(self):
        first = record(); updated = copy.deepcopy(first)
        updated["provenance"][0]["reason"] = "Clarified synthetic attribution"
        updated = sc.bind_record(updated)
        self.assertEqual(first["id"], updated["id"])
        self.assertNotEqual(first["content_sha256"], updated["content_sha256"])
        changed = copy.deepcopy(first); changed["quantity_kind"] = "extinction-coefficient"
        self.assertNotEqual(first["id"], sc.bind_record(changed)["id"])

    def test_duplicate_and_dangling_record_refs(self):
        r = record()
        with self.assertRaises(ValueError): self.validate(sidecar([r, r]))
        r = record("FormulaCard", symbol_bindings=["scr-" + "a" * 24])
        with self.assertRaises(ValueError): self.validate(sidecar([r]))

    def test_self_asserted_review_and_execution_rejected(self):
        for field in ("production_eligible", "knowledge_verified", "executable", "review_state"):
            value = sidecar(); value[field] = "accepted" if field == "review_state" else True
            with self.subTest(field=field), self.assertRaises(ValueError): self.validate(reseal(value))

    def test_placeholder_production_refs_rejected(self):
        value = sidecar(); value["data_origin"] = "source-backed-proposal"
        with self.assertRaisesRegex(ValueError, "placeholder|synthetic"):
            self.validate(reseal(value))

    def test_profile_missing_material_requirements_reported(self):
        result = self.validate(sidecar([record("MaterialState"), record("PropertyObservation")]))
        self.assertEqual({r["field"] for r in result["missing_requirements"]},
                         {"material_identity", "sample_identity", "orientation_frame", "temperature"})
