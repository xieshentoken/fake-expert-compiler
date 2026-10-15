"""Observable P1 numeric gates on isolated synthetic documents and Review fixtures."""
import copy
import json
from pathlib import Path
import unittest

from _support import TempDirTestCase, requires_renderer, pdftoppm
from _external_review_fixture import install_synthetic_review
from numeric_evidence import untraced_numbers
from semantic_authoring import _ingest_unit
from pdf_structure import compile_pdf_intermediate_representation, write_intermediate_representation
from semantic_promotion import build_semantic_workpack, validate_semantic_workpack
from direct_reference_skill import build_direct_reference, DirectReferenceError, validate_direct_reference


@requires_renderer
class NumericEvidenceTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        from reportlab.pdfgen import canvas
        self.source = self.tmp / "synthetic.pdf"
        c = canvas.Canvas(str(self.source), pagesize=(400,500))
        c.drawString(40,450,"1.1 Synthetic conductivity")
        c.drawString(40,420,"A synthetic conductivity graph illustrates a measured peak.")
        c.rect(70,230,180,140)
        c.drawString(90,300,"52 W/(m K)")
        c.drawString(40,200,"Figure 1.1: Synthetic conductivity peak.")
        c.save()
        ir = self.tmp / "ir"
        write_intermediate_representation(ir, compile_pdf_intermediate_representation(self.source,1,None))
        self.wp = self.tmp / "wp"
        m = build_semantic_workpack(self.source, ir, self.wp, pdftoppm=Path(pdftoppm()))
        units = [json.loads((self.wp/entry["path"]).read_text()) for entry in m["units"]]
        self.unit = next(unit for unit in units if any("Figure 1.1:" in span["text"] for span in unit["focus_spans"]))
        for unit in units:
            blank = {"schema_version":"tkc.semantic-draft/v0.1", "unit_id":unit["unit_id"],
                "proposer_instance":"test-proposer", "candidate_disposition":{"action":"context-only","reason":"Synthetic background"},
                "objects":[],"relations":[],"conflicts":[],"gaps":[]}
            (self.wp/"drafts"/f"{unit['unit_id']}.json").write_text(json.dumps(blank))
        tasks = {row["id"]:row for row in self.rows("visual-tasks.jsonl")}
        figure = next(row for row in self.rows("visual-objects.jsonl") if row["type"] == "figure")
        response = {"objects":[{"id":"peak", "type":"DataPoint", "title":"Synthetic conductivity peak",
            "statement":"Conductivity reaches 52 W/(m K).", "evidence":["Figure 1.1: Synthetic conductivity peak."],
            "figures":[figure["visual_object_id"]]}]}
        self.draft, _ = _ingest_unit(self.unit, response, tasks, "test-proposer")
        self.path = self.wp/"drafts"/f"{self.unit['unit_id']}.json"
        self.save()

    def rows(self,name):
        return [json.loads(line) for line in (self.wp/name).read_text().splitlines() if line.strip()]

    def save(self):
        self.path.write_text(json.dumps(self.draft))

    def build(self):
        return build_direct_reference(self.source,self.wp,self.tmp/"fake-numeric",name="numeric",
            display_name="Synthetic numeric",domain="synthetic",version="0.1.0",created_at="2026-10-15T00:00:00+00:00")

    def test_unreviewed_values_remain_proposed_but_cannot_build(self):
        issues,_ = validate_semantic_workpack(self.source,self.wp,require_complete_drafts=True)
        self.assertIn("numeric_review_required",{i.code for i in issues})
        with self.assertRaisesRegex(DirectReferenceError,"numeric_review_required"):
            self.build()
        self.assertEqual(json.loads(self.path.read_text())["objects"][0]["statement"],"Conductivity reaches 52 W/(m K).")
        self.assertFalse((self.tmp/"fake-numeric").exists())

    def test_origin_or_boolean_does_not_authorize_numbers(self):
        obj=self.draft["objects"][0]
        obj["origin"]="compiler-derived"
        obj["type"]="Claim"
        obj.pop("figure_object_ids")
        obj["statement"]="Conductivity reaches 98765 W/(m K)."
        self.save()
        with self.assertRaisesRegex(DirectReferenceError,"numeric_token_untraced"):
            self.build()

    def test_two_independent_review_fragments_allow_bound_values(self):
        install_synthetic_review(self.source,self.wp,self.tmp/"external")
        self.build()
        package=self.tmp/"fake-numeric"
        self.assertEqual(validate_direct_reference(package),[])
        obj=json.loads(next((package/"references/knowledge/objects").glob("*.json")).read_text())
        self.assertEqual(obj["value_basis"]["source"],"figure-reviewed")
        self.assertEqual(len(obj["value_basis"]["review_binding_sha256"]),64)

    def test_review_is_invalidated_by_changed_statement(self):
        install_synthetic_review(self.source,self.wp,self.tmp/"external")
        self.draft["objects"][0]["statement"]="Conductivity reaches 98765 W/(m K)."
        self.save()
        with self.assertRaisesRegex(DirectReferenceError,"numeric_review_required"):
            self.build()

    def test_single_visual_reviewer_cannot_clear_gate(self):
        install_synthetic_review(self.source,self.wp,self.tmp/"external")
        records=[row for row in self.rows("visual-review-attestations.jsonl") if row["reviewer_instance"]=="test-reviewer-a"]
        (self.wp/"visual-review-attestations.jsonl").write_text("".join(json.dumps(r)+"\n" for r in records))
        with self.assertRaisesRegex(DirectReferenceError,"numeric_review_required"):
            self.build()

    def test_boolean_and_wrong_figure_do_not_clear_gate(self):
        self.draft["objects"][0]["reviewed"] = True
        self.save()
        with self.assertRaises(DirectReferenceError):
            self.build()
        self.draft["objects"][0].pop("reviewed")
        self.draft["objects"][0]["figure_object_ids"] = ["vo-" + "0"*20]
        self.save()
        with self.assertRaisesRegex(DirectReferenceError,"figure_object_unresolved"):
            self.build()

    def test_crop_and_reviewer_drift_do_not_clear_gate(self):
        install_synthetic_review(self.source,self.wp,self.tmp/"external")
        manifest = json.loads((self.wp/"workpack.json").read_text())
        crop = next(iter(manifest["visual_semantics"]["figure_crops"].values()))
        target = self.wp/crop["path"]
        saved = target.read_bytes()
        target.write_bytes(b"changed crop")
        with self.assertRaisesRegex(DirectReferenceError,"figure_crop_drift"):
            self.build()
        target.write_bytes(saved)
        records = self.rows("review-attestations.jsonl")
        records[0]["reviewer_instance"] = "test-proposer"
        (self.wp/"review-attestations.jsonl").write_text("".join(json.dumps(r)+"\n" for r in records))
        with self.assertRaisesRegex(DirectReferenceError,"numeric_review_required"):
            self.build()


class NumericFieldTests(unittest.TestCase):
    def test_conditions_and_formulas_also_need_evidence(self):
        self.assertEqual(untraced_numbers({"statement":"A relation", "valid_when":["at 98765 K"],
            "formula":{"normalized_notation":"x = 87654"}},""),["87654","98765"])
