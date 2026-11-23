import unittest
from science.support import sc, record, object_ref, unknown


class SymbolTests(unittest.TestCase):
    def test_case_greek_prime_and_subscript_preserved(self):
        glyphs = ["k", "K", "ρ", "p", "ν", "v", "λ", "q", "q′", "q″", "q‴", "c_p", "c_v", "k_xx", "k_zz"]
        records = [record(original_glyph=g, display_latex=g) for g in glyphs]
        self.assertEqual(len({r["id"] for r in records}), len(glyphs))
        for glyph in glyphs:
            found = sc.resolve_symbols(records, glyph, scope_ref=object_ref())
            self.assertEqual([r["original_glyph"] for r in found["candidates"]], [glyph])

    def test_same_glyph_same_dimension_does_not_merge(self):
        records = [record(), record(quantity_kind="optical-extinction", canonical_quantity_id="optical-extinction")]
        result = sc.resolve_symbols(records, "k", scope_ref=object_ref())
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(sc.resolve_symbols(records, "k", quantity_kind="thermal-conductivity")["status"], "resolved")

    def test_missing_definition_is_unknown(self):
        self.assertEqual(sc.resolve_symbols([record(definition_ref=unknown())], "k")["status"], "unknown")

    def test_missing_scope_is_unknown(self):
        self.assertEqual(sc.resolve_symbols([record(scope_ref=unknown())], "k")["status"], "unknown")

    def test_scalar_tensor_shape_is_not_inferred(self):
        r = record(value_type="tensor", shape=[3, 3])
        self.assertEqual(sc.resolve_symbols([r], "k")["candidates"][0]["shape"], [3, 3])
        r = record(value_type="scalar", shape=[3, 3])
        from science.support import sidecar, context, profile
        with self.assertRaises(ValueError): sc.validate_sidecar(sidecar([r]), context(), profile())

    def test_lookalike_is_not_normalized(self):
        self.assertEqual(sc.resolve_symbols([record(original_glyph="ρ")], "p")["status"], "unknown")
