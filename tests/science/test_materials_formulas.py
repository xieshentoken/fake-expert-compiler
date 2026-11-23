import unittest
from science.support import sc, record, sidecar, context, profile, quantity, unknown


class MaterialFormulaTests(unittest.TestCase):
    def test_all_five_record_contracts(self):
        records = [record(k) for k in ("SymbolBinding", "FormulaCard", "ConditionSet", "MaterialState", "PropertyObservation")]
        self.assertEqual(sc.validate_sidecar(sidecar(records), context(), profile())["record_count"], 5)

    def test_unknown_material_is_not_zero(self):
        r = record("MaterialState")
        self.assertEqual(r["material_identity"]["status"], "unknown")
        r = record("PropertyObservation", value_representation=unknown())
        sc.validate_sidecar(sidecar([r]), context(), profile())

    def test_typical_value_never_upgrades(self):
        r = record("PropertyObservation")
        result = sc.assess_property(r, record("MaterialState"), {"temperature": quantity(150, "degC", "temperature")})
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["value_qualifier"], "typical")
        self.assertFalse(result["execution_authorized"])

    def test_temperature_orientation_and_sample_mismatch(self):
        mat = record("MaterialState", sample_identity={"status": "known", "value": "sample-a"}, orientation_frame={"status": "known", "value": "in-plane"})
        obs = record("PropertyObservation", measurement_conditions=[{"name": "temperature", "value": quantity(25, "degC", "temperature")}])
        for request in ({"temperature": quantity(150, "degC", "temperature")}, {"orientation_frame": quantity("through-plane")}, {"sample_identity": quantity("sample-b")}):
            self.assertEqual(sc.assess_property(obs, mat, request)["status"], "violated")

    def test_composition_basis_and_scale_required(self):
        for basis in ("mass", "volume"):
            r = record("MaterialState", composition={"status": "known", "basis": basis, "scale": "percent", "components": [{"component": "synthetic filler", "value": 40}]})
            sc.validate_sidecar(sidecar([r]), context(), profile())
        r = record("MaterialState", composition={"status": "known", "components": [{"component": "filler", "value": 40}]})
        with self.assertRaises(ValueError): sc.validate_sidecar(sidecar([r]), context(), profile())

    def test_curve_and_tensor_are_reference_only(self):
        shapes = [{"kind": "curve", "x_unit": "K", "points": [{"x": 300, "y": 5}, {"x": 400, "y": 4}]},
                  {"kind": "tensor", "shape": [2, 2], "values": [1, 0, 0, 2], "frame": "sample-frame"}]
        for value in shapes:
            r = record("PropertyObservation", value_representation=value)
            sc.validate_sidecar(sidecar([r]), context(), profile())
            self.assertFalse(sc.assess_property(r, record("MaterialState"), {})["execution_authorized"])

    def test_wrong_tensor_size_rejected(self):
        r = record("PropertyObservation", value_representation={"kind": "tensor", "shape": [3, 3], "values": [1], "frame": "sample-frame"})
        with self.assertRaises(ValueError): sc.validate_sidecar(sidecar([r]), context(), profile())

    def test_opaque_integral_formula_kept_as_reference(self):
        r = record("FormulaCard")
        r["notation_refs"][0]["latex"] = "\\int_0^L k(x) dx"
        r = sc.bind_record(r)
        self.assertFalse(sc.validate_sidecar(sidecar([r]), context(), profile())["executable"])

    def test_numerical_equation_without_unit_convention_is_reference_only(self):
        r = record("FormulaCard", equation_kind="numerical-value-equation")
        sc.validate_sidecar(sidecar([r]), context(), profile())
        r["execution_state"] = "requires-existing-gates"; r = sc.bind_record(r)
        with self.assertRaises(ValueError): sc.validate_sidecar(sidecar([r]), context(), profile())

    def test_ast_reference_cannot_invent_review(self):
        from science.support import object_ref
        r = record("FormulaCard", representation_state="scalar-ast", execution_state="requires-existing-gates",
                   executable_ast_ref={"source_object_ref": object_ref(), "ast_sha256": "a" * 64})
        with self.assertRaises(ValueError): sc.validate_sidecar(sidecar([r]), context(), profile())

    def test_observation_cannot_use_a_different_material(self):
        first = record("MaterialState")
        second = record("MaterialState", sample_identity={"status": "known", "value": "other-sample"})
        obs = record("PropertyObservation", material_state_ref=first["id"])
        with self.assertRaisesRegex(ValueError, "material_binding_mismatch"):
            sc.assess_property(obs, second, {})
