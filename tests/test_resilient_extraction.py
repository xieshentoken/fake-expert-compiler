"""Regression coverage for original-page extraction and portable retrieval profiles."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

from PIL import Image, ImageDraw
from pypdf import PdfReader
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from _support import SCRIPTS, TempDirTestCase, requires_renderer, run_cli, run_json
from direct_reference_skill import build_direct_reference, validate_direct_reference
from pdf_structure import normalize_text
from printed_page_labels import from_page_texts
from raster_table_geometry import horizontal_rules, ruled_candidate
from reference_alias_profiles import merged_aliases
from semantic_authoring import _visual_review_entries
from semantic_promotion import _attach_visual_ids_to_tasks
from visual_semantics import build_visual_conflict
import reference_search
import test_direct_reference_skill as direct_tests
from test_reference_search import catalog, object_entry


@requires_renderer
class ResilientNativeWorkflowTests(TempDirTestCase):
    def source(self, *, long=False):
        path = self.tmp / "book.pdf"
        c = canvas.Canvas(str(path))
        c.drawString(60, 700, "Cover")
        c.showPage()
        count = 8 if long else 2
        paragraph = "In this experiment a bounded fixture preserves its source evidence and a controlled input through repeated observations."
        for page in range(count):
            if page == 0:
                c.bookmarkPage("chapter")
                c.addOutlineEntry("1 Fixture chapter", "chapter")
                c.drawString(60, 760, "1 Fixture chapter")
            elif not long:
                c.drawString(60, 760, "Independent test section")
            c.setFont("Helvetica", 8)
            for index in range(26 if long else 2):
                c.drawString(60, 740 - 18 * index, f"{paragraph} {page}.{index}.")
            c.showPage()
        c.save()
        return path, count + 1

    def test_long_chapter_is_partitioned_at_original_page_boundaries(self):
        source, end = self.source(long=True)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        job = self.tmp / "workflow"
        plan = run_json("fake_expert.py", "plan", "--source", str(source), "--workspace", str(job),
                        "--start-page", "2", "--end-page", str(end), "--json")
        preview = plan["native_focus"]["preview"]
        self.assertEqual(preview["status"], "ready", preview)
        self.assertEqual(preview["split_segments"], 1)
        self.assertGreater(preview["focus_characters"], 20000)
        result = run_json("fake_expert.py", "compile", "--workflow", str(job / "intake-workflow.json"), "--json")
        self.assertEqual(result["code"], "semantic_authoring_required", result["children"])
        workpack = Path(result["children"][0]["workpack"])
        rows = json.loads((workpack / "workpack.json").read_text())["units"]
        self.assertEqual(len(rows), preview["unit_count"])
        self.assertEqual(len({row["segment_id"] for row in rows}), 1)
        spans = {}
        for i, row in enumerate(rows, 1):
            self.assertLessEqual(row["focus_character_count"], 20000)
            unit = json.loads((workpack / row["path"]).read_text())
            self.assertEqual(unit["focus_partition"]["part"], i)
            for span in unit["focus_spans"]:
                self.assertIn(span["page"], range(2, end + 1))
                self.assertNotIn(span["page"], spans)
                spans[span["page"]] = span["text"]
        expected = {}
        reader = PdfReader(source)
        for page in range(2, end + 1):
            raw = normalize_text(reader.pages[page - 1].extract_text())
            expected[page] = raw.removeprefix("1 Fixture chapter").strip() if page == 2 else raw
        self.assertEqual(spans, expected)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)

    def test_explicit_sections_and_original_scope_are_bound(self):
        source, end = self.source()
        reader = PdfReader(source)
        rows = []
        for p, heading in [(2, "1 Fixture chapter"), (3, "Independent test section")]:
            self.assertTrue(normalize_text(reader.pages[p - 1].extract_text()).startswith(heading))
            rows.append({"page": p, "start": 0, "end": len(heading), "span_sha256": hashlib.sha256(heading.encode()).hexdigest()})
        selection = self.tmp / "selection.json"
        selection.write_text(json.dumps({"schema_version": "tkc.native-structure-selections/v0.1",
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "boundaries": rows}))
        run_json("reconstruct_pdf.py", str(source), "--start-page", "2", "--end-page", str(end),
                 "--structure-selections", str(selection), "--output", str(self.tmp / "ir"), "--json")
        job = self.tmp / "job"
        plan = run_json("fake_expert.py", "plan", "--source", str(source), "--workspace", str(job),
                        "--start-page", "2", "--end-page", str(end), "--structure-selections", str(selection), "--json")
        self.assertEqual(plan["native_focus"]["preview"]["unit_count"], 2)
        selection.write_text("{}")
        result = run_cli("fake_expert.py", "compile", "--workflow", str(job / "intake-workflow.json"), "--json", check=False)
        self.assertIn("native_structure_selections_drift", result.stdout + result.stderr)
        self.assertFalse((job / "children").exists())

    def test_raster_equation_packet_preserves_unreviewed_render_location(self):
        source = self.tmp / "raster-equation.pdf"
        raster = Image.new("RGB", (300, 400), "white")
        ImageDraw.Draw(raster).text((65, 175), "x = y + z", fill="black")
        c = canvas.Canvas(str(source), pagesize=(300, 400))
        c.drawImage(ImageReader(raster), 0, 0, width=300, height=400)
        c.bookmarkPage("chapter")
        c.addOutlineEntry("1 Bounded fixture", "chapter")
        c.drawString(25, 360, "1 Bounded fixture")
        c.drawString(25, 330, "The controlled input is measured in a bounded fixture.")
        c.drawString(25, 310, "The output is preserved with the original conditions.")
        c.drawString(235, 215, "(1.2)")
        c.drawString(235, 195, "(10-4)")
        c.drawString(235, 175, "(a)")
        c.drawString(235, 155, "(b)")
        c.drawString(235, 135, "(DD.M)")
        c.drawString(260, 115, "(7)")
        c.drawString(25, 95, "(8)")
        c.save()
        job = self.tmp / "job"
        run_json("fake_expert.py", "plan", "--source", str(source), "--workspace", str(job), "--json")
        result = run_json("fake_expert.py", "compile", "--workflow", str(job / "intake-workflow.json"), "--json")
        self.assertEqual(result["code"], "semantic_authoring_required", result["children"])
        workpack = Path(result["children"][0]["workpack"])
        packets = [json.loads(p.read_text()) for p in (workpack / "authoring/packets").rglob("packet.json")]
        review = next(row for packet in packets for row in packet["visual_review"] if row["label"] == "(1.2)")
        self.assertEqual(review["reason"], "equation_number_only_in_text")
        self.assertEqual(review["page"], 1)
        x0, y0, x1, y1 = review["bbox_pdf_points"]
        self.assertTrue(x0 < 65 < x1 and y0 < 180 < y1)
        self.assertEqual(hashlib.sha256(Path(review["page_render"]).read_bytes()).hexdigest(), review["render_sha256"])
        labels = {row["label"] for packet in packets for row in packet.get("visual_review", [])}
        self.assertEqual(labels, {"(1.2)", "(10-4)", "(7)"})
        objects = [json.loads(line) for line in (workpack / "visual-objects.jsonl").read_text().splitlines()]
        self.assertEqual({obj["label"] for obj in objects if obj["type"] == "equation_display"}, labels)


class EvidenceQualityTests(TempDirTestCase):
    def test_printed_folios_reject_axis_ticks_and_accept_restart(self):
        raw = {1: "i\nPreface", 2: "ii\nPreface", 3: "iii\n", 4: "1\nChapter 1",
               5: "2 Chapter 1\nchart\n800", 6: "Chart without folio\n800",
               7: "4 Chapter 1", 8: "5 Chapter 1", 9: "1 Chapter 2",
               10: "2 Chapter 2", 11: "3 Chapter 2"}
        self.assertEqual(from_page_texts(raw), {1:"i",2:"ii",3:"iii",4:"1",5:"2",6:None,7:"4",8:"5",9:"1",10:"2",11:"3"})

    def test_visible_raster_rules_include_table_body_and_reject_prose_anchor(self):
        width, height = 200, 200
        pixels = bytearray(b"\xff" * (width * height * 3))
        for y in (67, 80, 110):
            pixels[(y * width + 25) * 3:(y * width + 180) * 3] = b"\x00" * (155 * 3)
        rules = horizontal_rules((width, height, 2, bytes(pixels)), width, height)
        self.assertIsNone(ruled_candidate([25, 3, 150, 12], rules, width, height, height))
        bbox, proof = ruled_candidate([25, 53, 145, 62], rules, width, height, height)
        self.assertLess(bbox[1], 67)
        self.assertGreater(bbox[3], 110)
        self.assertEqual(proof["geometry_evidence"], "raster-table-rules-v0.1")
        ambiguous_rules = sorted([*rules, [25, 125, 180]], key=lambda row: row[1])
        self.assertIsNone(ruled_candidate([25, 53, 145, 62], ambiguous_rules, width, height, height))

    def test_tall_grouped_raster_header_requires_an_interior_group_rule(self):
        caption = [25, 40, 245, 55]
        aligned = [[25, 70, 250], [25, 128, 250], [25, 158, 250]]
        grouped = [aligned[0], [105, 95, 250], *aligned[1:]]
        self.assertIsNone(ruled_candidate(caption, aligned, 300, 400, 400))
        bbox, proof = ruled_candidate(caption, grouped, 300, 400, 400)
        self.assertEqual(proof["rule_y_pdf_points"], [70, 128, 158])
        self.assertGreater(bbox[3], 158)
        self.assertIsNone(ruled_candidate(caption, grouped, 300, 400, 150))
        self.assertIsNone(ruled_candidate(caption, [*grouped, [25, 182, 250]], 300, 400, 400))

    def test_layout_conflicts_are_referenced_once_and_distinct_locations_survive(self):
        conflicts = [build_visual_conflict(
            source_sha256="a" * 64, physical_page=1, conflict_type="missing",
            details={"candidate": "table", "label": "Table 1.1",
                     "reason": "raster_table_boundaries_unconfirmed", "caption_bbox": [25, y, 40, y + 2]},
        ) for y in (10, 20)]
        tasks = [{"id": name, "kind": "table", "page": 1} for name in ("task-a", "task-b")]
        _attach_visual_ids_to_tasks(tasks, {"conflicts": [*conflicts, conflicts[0]]})
        by_id = {c["visual_conflict_id"]: c for c in conflicts}
        for task in tasks:
            self.assertEqual(set(task["layout_issue_ids"]), set(by_id))
            self.assertNotIn("layout_issues", task)
        notices = _visual_review_entries(self.tmp, {"visual_task_ids": [t["id"] for t in tasks]},
                                         {t["id"]: t for t in tasks}, {}, by_id)
        self.assertEqual(len(notices), 2)
        self.assertEqual({r["bbox_pdf_points"][1] for r in notices}, {10, 20})

    def test_retrieval_protects_material_names_without_changing_decision_guard(self):
        aliases = merged_aliases()
        rows = [object_entry("ko-polymer", "Silicone sealant", "A silicone sealant."),
                object_entry("ko-element", "Elemental silicon material", "A silicon substrate.")]
        frozen = catalog(rows)
        frozen["policy"]["term_aliases"] = aliases
        for row in rows:
            row["terms"], row["length"] = reference_search._weighted_terms(row, reference_search.OBJECT_FIELD_WEIGHTS, aliases)
        self.assertEqual(reference_search.query_catalog(frozen, "silicon")["matched_object_ids"], ["ko-element"])
        self.assertEqual(reference_search.query_catalog(frozen, "silicone")["matched_object_ids"], ["ko-polymer"])
        self.assertEqual(reference_search.query_catalog(frozen, "Which silicone should I use?")["reason_codes"],
                         ["decision_capability_unavailable"])

    def test_custom_alias_replays_in_installed_runtime(self):
        fixture = direct_tests.DirectReferenceSkillTests()
        fixture.setUp()
        try:
            path = self.tmp / "aliases.json"
            path.write_text(json.dumps({"schema_version":"tkc.reference-term-aliases/v0.1",
                "terms":[{"term":"bounded_candidate", "aliases":["bounded candidate system","受限候选系统"]}]}))
            out = self.tmp / "draft"
            build_direct_reference(fixture.source, fixture.workpack, out, name="bounded", display_name="Bounded",
                                   domain="bounded", version="0.1.0", created_at="2026-01-01T00:00:00Z",
                                   term_aliases=path)
            self.assertEqual(validate_direct_reference(out), [])
            cmd = [sys.executable, "-B", str(out / "scripts/query_reference.py"), "--query", "受限候选系统"]
            result = json.loads(subprocess.check_output(cmd, text=True))
            self.assertTrue(result["matched_object_ids"])
            self.assertFalse(result["assurance"]["knowledge_verified"])
        finally:
            fixture.tearDown()

    def test_authoring_lint_reuses_ingest_diagnostics_without_writing(self):
        fixture = direct_tests.DirectReferenceSkillTests()
        fixture.setUp()
        try:
            workpack = fixture.workpack
            unit = json.loads((workpack / "workpack.json").read_text())["units"][0]["unit_id"]
            responses = self.tmp / "responses"
            responses.mkdir()
            (responses / (unit + ".json")).write_text(json.dumps({"unit_id": unit,
                "disposition": "promote", "objects": [{"id":"bad","type":"Claim","title":"Wrong quote",
                 "statement":"Raised to 234.", "evidence":["A phrase not present in the PDF."]}]}))
            before = {p: p.read_bytes() for p in workpack.rglob("*") if p.is_file()}
            cmd = [sys.executable, "-B", str(SCRIPTS / "semantic_authoring.py"), "lint",
                   "--workpack", str(workpack), "--responses", str(responses), "--unit", unit]
            result = subprocess.run(cmd, text=True, capture_output=True)
            data = json.loads(result.stdout)
            self.assertEqual(result.returncode, 1)
            self.assertIn("evidence_quote_unresolved", {p["code"] for p in data["units"][0]["problems"]})
            self.assertFalse(data["writes"])
            self.assertEqual(before, {p: p.read_bytes() for p in workpack.rglob("*") if p.is_file()})
        finally:
            fixture.tearDown()
