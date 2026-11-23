import unittest
from science.support import quantity, unknown
import science_units as units


class ScienceUnitTests(unittest.TestCase):
    def test_bounds_do_not_round_with_callers_decimal_precision(self):
        from decimal import localcontext
        with localcontext() as context:
            context.prec=3
            with self.assertRaises(ValueError): units.decimal_number("1.0000001e100")

    def test_condition_conversion_never_rounds_a_boundary_silently(self):
        with self.assertRaisesRegex(ValueError,"precision_loss"):
            units.canonical_quantity(quantity(26.99999999999999,"degC","temperature"))

    def normalize(self, value, unit, kind, target, **kwargs):
        return units.normalize_quantity(quantity(value,unit,kind), expected_kind=kind, target_unit=target, **kwargs)

    def test_area_and_pressure_explicit_mappings(self):
        self.assertEqual(self.normalize(100,"mm²","area","m^2")["decimal_value"],"0.0001")
        self.assertEqual(self.normalize(2,"MPa","pressure","Pa")["decimal_value"],"2000000")

    def test_length_round_trip(self):
        q=self.normalize(2,"mm","length","m")["quantity"]
        self.assertEqual(units.normalize_quantity(q,expected_kind="length",target_unit="mm")["decimal_value"],"2")

    def test_absolute_celsius_and_temperature_difference(self):
        self.assertEqual(self.normalize(25,"degC","temperature","K")["decimal_value"],"298.15")
        self.assertEqual(self.normalize(10,"delta_degC","temperature-difference","K")["decimal_value"],"10")
        self.assertEqual(self.normalize(273.15,"K","temperature","degC")["decimal_value"],"0")

    def test_ambiguous_delta_and_absolute_zero_rejected(self):
        for value,unit,kind in ((10,"degC","temperature-difference"),(-274,"degC","temperature"),(10,"delta_K","temperature")):
            with self.subTest(unit=unit,kind=kind),self.assertRaises(ValueError): self.normalize(value,unit,kind,"K")

    def test_same_dimension_is_not_quantity_identity(self):
        with self.assertRaisesRegex(ValueError,"quantity_kind"):
            units.normalize_quantity(quantity(5,"1","extinction-coefficient"),expected_kind="thermal-conductivity",target_unit="W/m/K")

    def test_percent_basis_required_and_preserved(self):
        result=self.normalize(40,"%","mass-fraction","1",basis="mass")
        self.assertEqual(result["decimal_value"],"0.4")
        self.assertEqual(result["conversion"]["basis"],"mass")
        for basis in ("unknown","volume","not-applicable"):
            with self.subTest(basis=basis),self.assertRaises(ValueError): self.normalize(40,"%","mass-fraction","1",basis=basis)

    def test_mass_volume_percent_never_converted(self):
        with self.assertRaises(ValueError): self.normalize(40,"mass%","mass-fraction","vol%",basis="mass")

    def test_fraction_range_checked(self):
        for value in (-1,101):
            with self.subTest(value=value),self.assertRaises(ValueError): self.normalize(value,"%","volume-fraction","1",basis="volume")

    def test_unknown_nonfinite_boolean_and_code_rejected(self):
        for value in (float("nan"),float("inf"),True,"__import__('os').system('false')",10**101):
            with self.subTest(value=str(value)),self.assertRaises(ValueError): self.normalize(value,"mm","length","m")
        with self.assertRaises(ValueError): units.normalize_quantity(unknown(),expected_kind="length",target_unit="m")

    def test_unsupported_unit_and_frame_rejected(self):
        with self.assertRaises(ValueError): self.normalize(1,"mm^99999","length","m")
        with self.assertRaises(ValueError): self.normalize(1,"m","length","m",expected_frame="different-frame")

    def test_legacy_registry_not_expanded(self):
        import formula_execution as legacy
        self.assertNotIn("MPa",legacy.UNIT_TABLE); self.assertNotIn("degC",legacy.UNIT_TABLE)

    def test_condition_normalization_retains_unknown(self):
        self.assertEqual(units.canonical_quantity(unknown()),unknown())
        self.assertEqual(units.canonical_quantity(quantity(True)),quantity(True))
