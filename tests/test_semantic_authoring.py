"""Quote-based authoring ingest (HANDOFF steps 3-4)."""

from __future__ import annotations

import hashlib
import unittest

import _support  # noqa: F401  (sys.path)
from semantic_authoring import _extract_json, _ingest_unit, _resolve_quote

PAGE_TEXT = (
    "1.2 Temperature Dependence Figure 1.1: Thermal conductivity of copper versus temperature. "
    "Above the peak, phonon-phonon (Umklapp) scattering reduces conductivity. "
    "q = -k dT/dx (1.1) holds at steady state. 自然 对流的典型值为2到25 W/(m2 K)。"
)
FOCUS = [{"page": 2, "start": 10, "end": 10 + len(PAGE_TEXT), "text": PAGE_TEXT, "extractable": True}]
UNIT = {"unit_id": "swu-00000000000000000001", "focus_spans": FOCUS, "context_spans": [], "visual_task_ids": ["vis-fig", "vis-eq"]}
TASKS = {
    "vis-fig": {"id": "vis-fig", "page": 2, "kind": "figure-check", "visual_object_ids": ["vo-00000000000000000001"]},
    "vis-eq": {"id": "vis-eq", "page": 2, "kind": "equation", "visual_object_ids": []},
}


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class QuoteResolutionTests(unittest.TestCase):
    def test_exact_whitespace_dash_and_cjk_line_break(self) -> None:
        cases = {
            "Above the peak, phonon-phonon": "Above the peak, phonon-phonon",
            "Above   the\npeak, phonon–phonon": "Above the peak, phonon-phonon",
            "自然对流的典型值为2到25": "自然 对流的典型值为2到25",
        }
        for quote, expected in cases.items():
            span = _resolve_quote(quote, FOCUS)
            self.assertIsNotNone(span, quote)
            local = PAGE_TEXT[span["start"] - 10 : span["end"] - 10]
            self.assertEqual(local, expected)
            self.assertEqual(span["span_sha256"], sha(expected))

    def test_missing_quote(self) -> None:
        self.assertIsNone(_resolve_quote("not in the text", FOCUS))

    def test_extract_json_from_fences_and_wrappers(self) -> None:
        self.assertEqual(_extract_json('Sure:\n```json\n{"objects": []}\n```'), {"objects": []})
        self.assertEqual(_extract_json('{"result": "{\\"objects\\": [1]}"}'), {"objects": [1]})


class IngestTests(unittest.TestCase):
    def ingest(self, *objects: dict) -> tuple[dict, list[dict]]:
        return _ingest_unit(UNIT, {"disposition": "promote", "objects": list(objects)}, TASKS, "test-proposer")

    def test_drops_are_reported_not_fatal(self) -> None:
        draft, problems = self.ingest(
            {"id": "ok", "type": "Claim", "title": "Umklapp", "statement": "Umklapp scattering lowers conductivity above the peak.",
             "evidence": ["Above the peak, phonon-phonon (Umklapp) scattering reduces conductivity."]},
            {"id": "invented", "type": "Claim", "title": "x", "statement": "y", "evidence": ["nowhere"]},
            {"id": "untraced", "type": "Claim", "title": "Peak", "statement": "Copper peaks at 52 W/(m K).",
             "evidence": ["Figure 1.1: Thermal conductivity of copper versus temperature."]},
            {"id": "fig-missing", "type": "DataPoint", "title": "Peak", "statement": "Peak value",
             "evidence": ["Figure 1.1: Thermal conductivity of copper versus temperature."]},
        )
        self.assertEqual([obj["local_id"] for obj in draft["objects"]], ["ok"])
        codes = {problem["code"] for problem in problems}
        self.assertTrue({"object_dropped_no_evidence", "object_pending_numeric_evidence", "object_dropped_figure_missing"} <= codes)
        self.assertNotIn("quote", str(draft))  # drafts carry spans and hashes, never quotes

    def test_figure_values_are_bound_to_figure_tasks(self) -> None:
        draft, problems = self.ingest(
            {"id": "peak", "type": "DataPoint", "title": "Copper peak", "statement": "Copper peaks at about 52 W/(m K) near 18 K.",
             "evidence": ["Figure 1.1: Thermal conductivity of copper versus temperature."], "figures": ["vo-00000000000000000001"]},
        )
        self.assertEqual([p["code"] for p in problems], ["numeric_review_required"])
        obj = draft["objects"][0]
        self.assertEqual(obj["figure_object_ids"], ["vo-00000000000000000001"])
        self.assertEqual(obj["visual_task_ids"], ["vis-fig"])

    def test_equation_needs_equation_task_and_formula(self) -> None:
        draft, problems = self.ingest(
            {"id": "f", "type": "Equation", "title": "Fourier", "statement": "Flux is minus k times the gradient.", "origin": "source-explicit",
             "evidence": ["q = -k dT/dx (1.1)"], "formula": {"source_notation": "q = -k dT/dx"}},
            {"id": "g", "type": "Equation", "title": "Bare", "statement": "Flux relation without formula metadata.",
             "evidence": ["q = -k dT/dx (1.1)"]},
        )
        by_id = {obj["local_id"]: obj for obj in draft["objects"]}
        self.assertEqual(by_id["f"]["type"], "Equation")
        self.assertEqual(by_id["f"]["visual_task_ids"], ["vis-eq"])
        self.assertEqual(by_id["f"]["formula"]["representation_relation"], "identical")
        self.assertEqual(by_id["g"]["type"], "Claim")
        self.assertIn("equation_downgraded_to_claim", {problem["code"] for problem in problems})

    def test_unit_with_no_surviving_proposal_is_rejected(self) -> None:
        draft, _problems = self.ingest({"id": "x", "type": "Claim", "title": "x", "statement": "y", "evidence": ["nowhere"]})
        self.assertEqual(draft["candidate_disposition"]["action"], "reject")


if __name__ == "__main__":
    unittest.main()
