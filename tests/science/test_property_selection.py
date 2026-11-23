import copy
import unittest
from science.m3_support import property_fixture
from science.support import sc,quantity,unknown
import science_calculation as calc


class PropertySelectionTests(unittest.TestCase):
    def test_scalar_equivalent_temperature_and_typical_not_guaranteed(self):
        result=calc.select_property(*property_fixture())
        self.assertEqual(result["decimal_value"],"5")
        self.assertEqual(result["value_qualifier"],"typical")
        self.assertFalse(result["execution_authorized"])

    def test_linear_interpolation_explicit_coordinate(self):
        result=calc.select_property(*property_fixture(True))
        self.assertEqual(result["decimal_value"],"6")
        self.assertEqual(len(result["selected_points"]),2)
        self.assertEqual(result["uncertainty_status"],"unavailable")

    def test_curve_endpoints_and_equivalent_celsius(self):
        for number in (300,350,400):
            side,p,sel=property_fixture(True)
            sel["coordinate"]=quantity(number-273.15,"degC","temperature")
            # Use exact decimal Celsius values rather than binary subtraction.
            sel["coordinate"]["value"]={300:26.85,350:76.85,400:126.85}[number]
            sel["conditions"][0]["value"]=quantity(number,"K","temperature")
            self.assertEqual(calc.select_property(side,p,sel)["decimal_value"],str(5+(number-300)//50))

    def test_no_extrapolation(self):
        for x in (299,401):
            side,p,sel=property_fixture(True); sel["coordinate"]=quantity(x,"K","temperature"); sel["conditions"][0]["value"]=sel["coordinate"]
            with self.assertRaisesRegex(ValueError,"extrapolation"): calc.select_property(side,p,sel)

    def test_sample_direction_method_and_record_version_are_exact(self):
        for key,value in (("sample_identity","another sample"),("orientation_frame","through-plane"),
                          ("method_ref",{}),("material_state",{"id":"absent","sha256":"a"*64}),
                          ("observation",{"id":"absent","sha256":"a"*64})):
            side,p,sel=property_fixture(); sel[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError): calc.select_property(side,p,sel)

    def test_wrong_temperature_never_reuses_room_temperature(self):
        side,p,sel=property_fixture(); sel["conditions"][0]["value"]=quantity(150,"degC","temperature")
        with self.assertRaisesRegex(ValueError,"condition_mismatch"): calc.select_property(side,p,sel)

    def test_unknown_and_missing_conditions_fail(self):
        for value in ([],[{"name":"temperature","value":unknown()}]):
            side,p,sel=property_fixture(); sel["conditions"]=value
            with self.assertRaises(ValueError): calc.select_property(side,p,sel)

    def test_unknown_required_material_field_fails(self):
        side,p,sel=property_fixture(); p["required_material_fields"].append("processing_history")
        with self.assertRaisesRegex(ValueError,"material_state_unknown"): calc.select_property(side,p,sel)

    def test_axis_never_inferred_from_unit(self):
        for axis in (None,{"name":"pressure","quantity_kind":"pressure","frame":"not-applicable"}):
            side,p,sel=property_fixture(True); sel["axis"]=axis
            with self.assertRaises(ValueError): calc.select_property(side,p,sel)

    def test_fixed_temperature_cannot_be_overridden_by_curve_axis(self):
        side,p,sel=property_fixture(True)
        obs=side["records"][1]; obs["measurement_conditions"]=[{"name":"temperature","value":quantity(300,"K","temperature")}]
        side["records"][1]=sc.bind_record(obs); sel["observation"]=calc.record_ref(side["records"][1])
        with self.assertRaisesRegex(ValueError,"fixed_condition_mismatch"): calc.select_property(side,p,sel)

    def test_reported_uncertainty_not_propagated_or_made_independent(self):
        uncertainty={"status":"known","value":0.2,"unit":"W/m/K","kind":"standard","basis":"synthetic source field"}
        scalar=calc.select_property(*property_fixture(uncertainty=uncertainty))
        interpolated=calc.select_property(*property_fixture(True,uncertainty))
        self.assertEqual(scalar["uncertainty_status"],"reported")
        self.assertEqual(scalar["source_uncertainty_sha256"],sc.sha256_json(uncertainty))
        self.assertEqual(interpolated["uncertainty_status"],"unavailable")
        self.assertNotIn("covariance",interpolated)

    def test_tensor_unknown_and_curve_order_rejected(self):
        for value in ({"kind":"tensor","shape":[2,2],"values":[1,0,0,1],"frame":"in-plane"},unknown(),
                      {"kind":"curve","x_unit":"K","points":[{"x":400,"y":5},{"x":300,"y":7}]}):
            side,p,sel=property_fixture(value.get("kind")=="curve")
            side["records"][1]["value_representation"]=value; side["records"][1]=sc.bind_record(side["records"][1])
            sel["observation"]=calc.record_ref(side["records"][1])
            with self.assertRaises(ValueError): calc.select_property(side,p,sel)

    def test_repeating_decimal_interpolation_is_explicitly_unsupported(self):
        side,p,sel=property_fixture(True); obs=side["records"][1]
        obs["value_representation"]["points"]=[{"x":300,"y":5},{"x":303,"y":6}]
        side["records"][1]=sc.bind_record(obs); sel["observation"]=calc.record_ref(side["records"][1])
        sel["coordinate"]=quantity(301,"K","temperature"); sel["conditions"][0]["value"]=sel["coordinate"]
        with self.assertRaisesRegex(ValueError,"precision_loss"): calc.select_property(side,p,sel)
