import copy
import unittest
from science.m2_support import library, tokenizer, sc, si


class ScienceIndexTests(unittest.TestCase):
    def setUp(self):
        self.lib = library(); self.tok = tokenizer(); self.index = si.build_index(self.lib, self.tok)

    def search(self, question, **kwargs):
        return si.search(self.index, question, self.tok, scope=[self.lib[0]["package"]["fingerprint"]], **kwargs)

    def test_deterministic_rebuild(self):
        self.assertEqual(self.index, si.build_index(copy.deepcopy(self.lib), self.tok))
        self.assertFalse(self.index["execution_authorized"])

    def test_chinese_colloquial_alias_recall(self):
        self.assertTrue(self.search("热怎么过去"))
        self.assertTrue(self.search("热传导"))

    def test_cjk_bigrams_trigrams_without_model(self):
        terms = si.tokens("材料温度条件", self.tok)["natural"]
        for word in ("材料", "温度", "温度条"): self.assertIn(word, terms)

    def test_math_preserves_case_prime_subscript_bold_greek(self):
        terms = si.tokens("k K q′ q″ λ q_1 𝐪 \\mathbf{q} \\vec{q}", self.tok)["math"]
        for word in ("k", "K", "q′", "q″", "λ", "q_1", "𝐪", "\\mathbf{q}", "\\vec{q}"):
            with self.subTest(word=word): self.assertIn(word, terms)
        self.assertNotIn("q", terms)

    def test_symbol_case_channels_differ(self):
        self.assertNotEqual(self.search("k")[0]["matched_math"], self.search("K")[0]["matched_math"])

    def test_braced_subscript_and_combining_vector_not_flattened(self):
        terms = si.tokens("k_{eff} q⃗", self.tok)["math"]
        self.assertIn("k_{eff}", terms); self.assertIn("q⃗", terms)
        self.assertNotIn("k", terms); self.assertNotIn("q", terms)

    def test_quantity_kind_filter_does_not_merge_same_glyph(self):
        self.assertTrue(self.search("k", quantity_kind="thermal-conductivity"))
        self.assertEqual(self.search("k", quantity_kind="extinction-coefficient"), [])

    def test_scope_and_domain_filters(self):
        self.assertEqual(self.search("heat", domains=["unrelated-domain"]), [])
        self.assertEqual(si.search(self.index, "heat", self.tok, scope=[]), [])

    def test_unknown_query_never_invents_result(self):
        self.assertEqual(self.search("opaqueunrelated"), [])

    def test_index_mutation_rejected_even_if_rehashed(self):
        self.index["entries"][0]["math_tokens"].append("invented")
        self.index["index_sha256"] = sc.sha256_json({k:v for k,v in self.index.items() if k != "index_sha256"})
        with self.assertRaisesRegex(ValueError, "index_stale"): si.validate_index(self.index, self.lib, self.tok)

    def test_tokenizer_drift_rejected(self):
        self.tok["aliases"] = []
        with self.assertRaises(ValueError): si.validate_index(self.index, self.lib, self.tok)

    def test_profile_drift_rejected(self):
        self.lib[0]["profile"]["version"] = "0.2.0"
        with self.assertRaises(ValueError): si.validate_index(self.index, self.lib, self.tok)

    def test_duplicate_package_identity_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate_package"): si.build_index(self.lib + self.lib, self.tok)

    def test_duplicate_alias_and_unknown_fields_rejected(self):
        self.tok["aliases"].append(self.tok["aliases"][0])
        with self.assertRaises(ValueError): si.build_index(self.lib, self.tok)
        self.tok = tokenizer(); self.tok["execute"] = True
        with self.assertRaises(ValueError): si.build_index(self.lib, self.tok)

    def test_huge_integer_fails_as_value_error(self):
        with self.assertRaises(ValueError): sc.canonical_json({"value": 10**1000})
