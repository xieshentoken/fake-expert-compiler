import copy
import unittest
from science.m2_support import library, tokenizer, request, sc, si
from science.support import record, quantity, unknown
import science_reference_runtime as runtime


class EvidenceClosureTests(unittest.TestCase):
    def close(self, lib, **limits):
        return runtime.closure(lib, [si.object_ref(lib[0]["package"], lib[0]["package"]["objects"][0])],
                               limits={**runtime.DEFAULT_LIMITS, **limits})

    def test_actual_cross_unit_definition_is_retrieved(self):
        lib = library(count=2); package = lib[0]["package"]
        row = lib[0]["sidecar"]["records"][0]
        row["definition_ref"] = si.object_ref(package, package["objects"][1]); row = sc.bind_record(row)
        lib[0]["sidecar"] = sc.make_sidecar([row], package, lib[0]["profile"], data_origin="synthetic")
        result = self.close(lib)
        self.assertEqual(len(result["selected_objects"]), 2)
        self.assertEqual(result["science_records"][0]["record"]["definition_ref"]["object_id"], package["objects"][1]["id"])

    def test_missing_definition_produces_bounded_compile_task(self):
        lib = library([record(definition_ref=unknown())])
        result = self.close(lib)
        self.assertTrue(any(r["task"] == "compile-actual-definition-unit" for r in result["unresolved_dependencies"]))
        self.assertEqual(result["closure_status"], "incomplete")

    def test_hop_limit_and_cycle_terminate(self):
        lib = library(count=6); p = lib[0]["package"]
        p["relations"] = [{"source_id": p["objects"][i]["id"], "target_id": p["objects"][(i+1)%6]["id"], "type": "depends_on"} for i in range(6)]
        result = self.close(lib, max_hops=1)
        self.assertTrue(result["closure_truncated"])
        self.assertLess(len(result["selected_objects"]), 6)

    def test_64_node_limit_is_explicit(self):
        lib = library([], count=70); p = lib[0]["package"]
        p["relations"] = [{"source_id": p["objects"][0]["id"], "target_id": o["id"], "type": "depends_on"} for o in p["objects"][1:]]
        result = self.close(lib)
        self.assertEqual(len(result["selected_objects"]), 64)
        self.assertTrue(result["closure_truncated"])

    def test_byte_budget_returns_incomplete(self):
        lib = library(); result = self.close(lib, max_bytes=8192)
        self.assertTrue(result["closure_truncated"])
        self.assertEqual(result["closure_status"], "incomplete")

    def test_zero_hops_cannot_claim_complete_with_unread_sidecar(self):
        result = self.close(library(), max_hops=0)
        self.assertTrue(result["closure_truncated"])

    def test_negative_and_oversized_limits_rejected(self):
        for changes in ({"max_hops": -1}, {"max_objects": 65}, {"max_bytes": 1048577}, {"max_hops": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.close(library(), **changes)

    def test_unknown_conditions_and_missing_inputs_survive(self):
        lib = library([record("ConditionSet")]); req = request(lib); tok = tokenizer()
        result = runtime.query(req, lib, si.build_index(lib,tok), tok)
        self.assertEqual(result["missing_inputs"], ["request:thickness"])
        self.assertEqual(result["condition_results"][0]["status"], "unknown")
        self.assertFalse(result["assurance"]["execution_authorized"])

    def test_user_assumption_does_not_fill_missing_fact(self):
        lib = library([record("ConditionSet")]); req = request(lib); tok = tokenizer()
        req["user_assumptions"] = [{"name": "thickness", "value": quantity(2,"mm","length"), "reason": "synthetic scenario"}]
        result = runtime.query(req, lib, si.build_index(lib,tok), tok)
        self.assertIn("request:thickness", result["missing_inputs"])

    def test_conflicts_and_gaps_are_disclosed(self):
        lib = library([]); p = lib[0]["package"]
        p["conflicts"] = [{"id": "synthetic-conflict", "claim_ids": [p["objects"][0]["id"]]}]
        p["gaps"] = [{"id": "synthetic-gap", "statement": "Synthetic missing data"}]
        tok = tokenizer(); result = runtime.query(request(lib), lib, si.build_index(lib,tok), tok)
        self.assertEqual(len(result["conflicts"]), 1); self.assertEqual(len(result["gaps"]), 1)

    def test_object_and_locator_are_separate_from_original(self):
        lib = library(); p = lib[0]["package"]; obj = p["objects"][0]
        body = runtime.read_object(lib, si.object_ref(p,obj))
        self.assertFalse(body["body_is_original_source_text"])
        result = runtime.read_source(lib, si.evidence_refs(p,obj)[0])
        self.assertEqual(result["status"], "external-source-unavailable")
        self.assertNotIn("evidence_spans", result)

    def test_neighbor_cannot_be_used_as_owner_evidence(self):
        lib = library(count=2); p=lib[0]["package"]
        p["objects"][1]["evidence_ids"] = []
        reference = si.evidence_refs(p,p["objects"][0])[0]
        reference["object_ref"] = si.object_ref(p,p["objects"][1])
        with self.assertRaises(ValueError): runtime.read_locator(lib, reference)

    def test_formula_typed_dependencies_include_conditions_property_and_material(self):
        lib = library([record("ConditionSet"),record("MaterialState"),record("PropertyObservation"),record("FormulaCard"),record()])
        rows = lib[0]["sidecar"]["records"]; condition, material, observation, formula, symbol = rows
        observation["material_state_ref"] = material["id"]
        next(p for p in observation["provenance"] if p["field"] == "material_state_ref")["status"] = "assumed"
        observation = sc.bind_record(observation)
        formula.update(condition_set_ref=condition["id"], symbol_bindings=[symbol["id"]], dependency_refs=[observation["id"]])
        next(p for p in formula["provenance"] if p["field"] == "condition_set_ref")["status"] = "assumed"
        formula = sc.bind_record(formula)
        lib[0]["sidecar"] = sc.make_sidecar([condition,material,observation,formula,symbol],lib[0]["package"],lib[0]["profile"],data_origin="synthetic")
        result = self.close(lib)
        self.assertEqual({r["record"]["kind"] for r in result["science_records"]},
                         {"ConditionSet","MaterialState","PropertyObservation","FormulaCard","SymbolBinding"})
        self.assertTrue({"condition_set_ref","symbol_bindings","dependency_refs","material_state_ref"} <= {r["type"] for r in result["typed_links"]})

    def test_same_glyph_multiple_quantities_remains_ambiguous(self):
        lib = library([record(),record(quantity_kind="extinction-coefficient", canonical_quantity_id="extinction-coefficient",unit="1",dimension=[0]*7)])
        tok = tokenizer(); result = runtime.query(request(lib,"k"),lib,si.build_index(lib,tok),tok)
        self.assertEqual(result["symbol_resolutions"][0]["status"],"ambiguous")
        self.assertEqual(len(result["symbol_resolutions"][0]["candidate_refs"]),2)
        self.assertIn("symbol-ambiguous",result["limitations"])

    def test_condition_violation_cannot_turn_into_satisfied(self):
        lib=library([record("ConditionSet")]); req=request(lib); tok=tokenizer()
        req["facts"]=[{"name":"thickness","value":quantity(-2,"mm","length"),"source_ref":unknown()}]
        result=runtime.query(req,lib,si.build_index(lib,tok),tok)
        self.assertEqual(result["condition_results"][0]["status"],"violated")
        self.assertIn("condition-violated",result["limitations"])

    def test_dangling_relation_is_an_unresolved_dependency(self):
        lib=library([]); p=lib[0]["package"]
        p["relations"]=[{"source_id":p["objects"][0]["id"],"target_id":"missing-object","type":"depends_on"}]
        result=self.close(lib)
        self.assertEqual(result["closure_status"],"incomplete")
        self.assertTrue(any(r["reason_code"]=="related_object_missing" for r in result["unresolved_dependencies"]))

    def test_profile_required_property_condition_not_silently_omitted(self):
        lib=library([record("PropertyObservation")]); tok=tokenizer()
        result=runtime.query(request(lib),lib,si.build_index(lib,tok),tok)
        self.assertTrue(any(r["reason_code"]=="required_measurement_condition_unknown" for r in result["unresolved_dependencies"]))
        self.assertTrue(any(name.endswith(":temperature") for name in result["missing_inputs"]))
