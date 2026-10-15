"""Search runtime: tokenizer, BM25 ranking, coverage gate, guard, figures, embeddings (HANDOFF step 6)."""

from __future__ import annotations

import unittest

import _support  # noqa: F401  (sys.path)
import reference_search as rs


def object_entry(identifier: str, title: str, statement: str, *, figure_refs=(), warnings=()) -> dict:
    terms, length = rs._weighted_terms({"title": title, "statement": statement}, rs.OBJECT_FIELD_WEIGHTS)
    return {
        "id": identifier, "kind": "object", "type": "Claim", "title": title, "statement": statement,
        "assumptions": [], "valid_when": [], "fails_when": [], "formula": None,
        "physical_pages": [1], "printed_page_labels": ["1"], "evidence_ids": [f"ev-{identifier}"],
        "source_ids": ["src-x"], "figure_refs": list(figure_refs), "value_basis": None,
        "warnings": list(warnings), "path": f"references/knowledge/objects/{identifier}.json",
        "terms": terms, "length": length,
    }


def figure_entry(identifier: str, label: str, caption: str, object_ids=()) -> dict:
    terms, length = rs._weighted_terms({"label": label, "caption": caption}, rs.FIGURE_FIELD_WEIGHTS)
    return {
        "id": identifier, "kind": "figure", "figure_kind": "figure", "label": label, "caption": caption,
        "physical_page": 2, "printed_page_label": "2", "image": None, "object_ids": list(object_ids),
        "terms": terms, "length": length,
    }


def catalog(objects, figures=()) -> dict:
    return {
        "schema_version": rs.CATALOG_SCHEMA,
        "runtime_version": rs.RUNTIME_VERSION,
        "package_id": "fake-test",
        "module_id": "fake-test-core",
        "capabilities": {"reference": True, "decision_support": False, "executable": False},
        "policy": {"out_of_scope_terms": ["unrelated"], "assurance": {"warnings": ["unreviewed-candidate"]}},
        "objects": list(objects),
        "figures": list(figures),
        "conflicts": [],
        "gaps": [],
    }


HEAT = catalog(
    [
        object_entry("ko-def", "Thermal conductivity", "Thermal conductivity k is a material property linking heat flux to the temperature gradient."),
        object_entry("ko-cu", "Copper conductivity at 300 K", "Copper has a thermal conductivity of 401 W/(m K) at 300 K."),
        object_entry("ko-fourier", "Fourier's law of heat conduction", "Heat flux equals minus conductivity times the temperature gradient."),
        object_entry("ko-wall", "Series resistance of composite wall", "The total resistance of a layered wall is the sum of layer resistances."),
        object_entry("ko-peak", "Copper conductivity peak", "Copper conductivity peaks at about 52 W/(m K) near 18 K.", figure_refs=["vo-00000000000000000001"], warnings=["figure_value_unreviewed"]),
        object_entry("ko-h", "对流换热系数", "对流换热系数h表示流体和固体表面间的换热强弱，与流速、物性和几何有关。"),
    ],
    [figure_entry("vo-00000000000000000001", "Figure 1.1", "Figure 1.1: Thermal conductivity of copper versus temperature.", ["ko-peak"])],
)


def titles(response: dict) -> list[str]:
    return [row["title"] for row in response["results"]]


class TokenizerTests(unittest.TestCase):
    def test_stemming_and_numbers(self) -> None:
        self.assertEqual(rs.tokenize("conductivities conduction conductive"), ["conductivity", "conduct", "conduct"])
        self.assertEqual(rs.tokenize("Fins at 300 K and 0.026"), ["fin", "300", "0.026"])

    def test_cjk_ngrams_and_function_words(self) -> None:
        tokens = rs.tokenize("什么是换热系数？")
        self.assertIn("换热", tokens)
        self.assertIn("热系数", tokens)
        self.assertNotIn("什么", tokens)


class RankingTests(unittest.TestCase):
    def test_specific_fact_is_not_cut_by_generic_definition(self) -> None:
        response = rs.query_catalog(HEAT, "What is the thermal conductivity of copper?")
        self.assertEqual(titles(response)[0], "Copper conductivity at 300 K")
        self.assertIn("Thermal conductivity", titles(response))

    def test_weak_partial_match_abstains(self) -> None:
        response = rs.query_catalog(HEAT, "what is a heat-transfer coefficient")
        self.assertEqual(response["behavior"], "abstain")
        self.assertEqual(response["reason_codes"], ["no_supported_reference"])

    def test_chinese_partial_terms_and_sentences(self) -> None:
        for query in ("换热系数", "换热强度", "流速 物性 几何", "流体和固体表面的换热强弱用什么描述"):
            self.assertEqual(titles(rs.query_catalog(HEAT, query)), ["对流换热系数"], query)

    def test_mixed_language_query(self) -> None:
        self.assertEqual(titles(rs.query_catalog(HEAT, "铜的导热系数 copper conductivity at 300 K"))[0], "Copper conductivity at 300 K")

    def test_rich_results_and_linked_figures(self) -> None:
        response = rs.query_catalog(HEAT, "copper conductivity peak")
        top = response["results"][0]
        self.assertEqual(top["statement"], "Copper conductivity peaks at about 52 W/(m K) near 18 K.")
        self.assertEqual(top["warnings"], ["figure_value_unreviewed"])
        self.assertIn("figure_value_unreviewed", response["reason_codes"])
        self.assertEqual([figure["label"] for figure in response["figures"]], ["Figure 1.1"])

    def test_exact_title_without_indexable_tokens(self) -> None:
        cat = catalog([object_entry("ko-k", "k", "The symbol used for conductivity.")])
        self.assertEqual(rs.query_catalog(cat, "k")["matched_object_ids"], ["ko-k"])


class GuardTests(unittest.TestCase):
    def test_decision_intent_abstains(self) -> None:
        for query in ("certify safe design decision", "Design a heat exchanger for me", "Is copper safe for heat exchangers?",
                      "Should we use copper?", "推荐一个安全设计并批准执行", "推荐使用铜吗", "帮我设计散热器"):
            response = rs.query_catalog(HEAT, query)
            self.assertEqual(response["reason_codes"], ["decision_capability_unavailable"], query)

    def test_lookups_using_design_or_safety_words_answer(self) -> None:
        self.assertEqual(rs.query_catalog(HEAT, "What is the design heat flux for a wall?")["behavior"], "abstain")
        cat = catalog([object_entry("ko-design", "Design heat flux for test wall", "The stated design heat flux for the test wall is 2500 W/m2.")])
        self.assertEqual(rs.query_catalog(cat, "What is the design heat flux for a wall?")["behavior"], "answer")

    def test_out_of_scope_terms(self) -> None:
        self.assertEqual(rs.query_catalog(HEAT, "unrelated question")["reason_codes"], ["outside_package_scope"])


class EmbeddingFusionTests(unittest.TestCase):
    def test_fusion_adds_semantic_neighbours_and_marks_semantic_only(self) -> None:
        vectors = {entry["id"]: [1.0, 0.0] for entry in HEAT["objects"]}
        vectors["ko-wall"] = [0.0, 1.0]
        vectors["vo-00000000000000000001"] = [0.0, 1.0]
        embeddings = {"vectors": vectors}
        response = rs.query_catalog(HEAT, "polystyrene foam boards", embeddings=embeddings, query_vector=[0.0, 1.0])
        self.assertEqual(response["retrieval"]["method"], "bm25+embedding-rrf")
        self.assertEqual(response["matched_object_ids"], ["ko-wall"])
        self.assertIn("semantic_match_only", response["reason_codes"])

    def test_command_mismatch_falls_back_to_lexical(self) -> None:
        embeddings = {"command_sha256": rs.sha256_text("other-model"), "vectors": {}}
        response = rs.query_package(HEAT, "copper", embeddings=embeddings, embedding_command="python3 -c pass")
        self.assertEqual(response["retrieval"]["method"], "bm25")
        self.assertEqual(response["retrieval"]["notes"], ["embedding_command_mismatch"])


if __name__ == "__main__":
    unittest.main()
