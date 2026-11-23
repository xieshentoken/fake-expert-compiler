from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "fake-expert/scripts"
sys.path.insert(0, str(SCRIPTS))

import direct_reference_skill
import fake_expert
import native_extraction
from environment_preflight import _check_source
from environment_preflight import build_report
from book_planner import build_book_plan
from native_blank_pages import confirm_blank_page, diagnose_blank_page
from pdf_structure import compile_pdf_intermediate_representation, sha256_file, write_intermediate_representation
from semantic_promotion import DRAFT_SCHEMA, build_semantic_workpack, validate_semantic_workpack
from verify_pdf_ir import verify_pdf_ir
from pdf_parser_adapters import sha256_json
from scanned_pdf_ocr import build_quality_routing


@unittest.skipUnless(shutil.which("pdftoppm"), "local canonical renderer required")
class NativeBlankPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "book.pdf"
        pdf = canvas.Canvas(str(self.source), pagesize=(300, 400))
        for page_number, title in enumerate((None, "1 First section", None, None, "2 Second section", None), start=1):
            if title:
                pdf.setFont("Helvetica", 12)
                pdf.drawString(20, 360, title)
                pdf.drawString(20, 330, "A bounded system accepts a controlled input.")
                pdf.drawString(20, 310, "The system reports a stable output within its scope.")
                pdf.drawString(150, 20, str(100 + page_number))
            pdf.showPage()
        pdf.save()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def cli(self, script: str, *args: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), *map(str, args)], capture_output=True,
            text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, check=False,
        )

    def workpack(self, start: int, end: int, name: str) -> tuple[Path, dict]:
        ir = compile_pdf_intermediate_representation(self.source, start, end)
        ir_path, workpack = self.root / f"{name}-ir", self.root / name
        write_intermediate_representation(ir_path, ir)
        manifest = build_semantic_workpack(self.source, ir_path, workpack, semantic_assurance=False)
        for entry in manifest["units"]:
            unit = json.loads((workpack / entry["path"]).read_text())
            spans = unit["focus_spans"]
            draft = {
                "schema_version": DRAFT_SCHEMA, "unit_id": unit["unit_id"],
                "proposer_instance": "synthetic-fixture-proposer",
                "candidate_disposition": {"action": "promote" if spans else "non-knowledge", "reason": "Synthetic source-backed fixture."},
                "objects": [], "relations": [], "conflicts": [], "gaps": [],
            }
            if spans:
                span = spans[0]
                draft["objects"] = [{
                    "local_id": "bounded-system", "type": "Concept", "origin": "source-paraphrase",
                    "title": "Bounded system", "statement": "A bounded system accepts a controlled input and reports an output.",
                    "evidence_spans": [{key: span[key] for key in ("page", "start", "end", "span_sha256")}],
                    "assumptions": [], "valid_when": ["Within the stated bounded scope."],
                    "fails_when": ["Outside the stated bounded scope."],
                    "applicability": {"physical_pages": [span["page"], span["page"]], "excluded_conclusions": []},
                    "visual_task_ids": [], "formula": None,
                }]
            (workpack / "drafts" / f"{unit['unit_id']}.json").write_text(json.dumps(draft))
        return workpack, ir

    def build(self, workpack: Path, output: Path) -> dict:
        return direct_reference_skill.build_direct_reference(
            self.source, workpack, output, name="blank-fixture", display_name="Blank Fixture",
            domain="fixture", version="0.1.0", created_at="2026-11-22T00:00:00Z",
        )

    def extend(self, base: Path, workpack: Path, output: Path) -> dict:
        return direct_reference_skill.extend_direct_reference(
            base, self.source, workpack, output, version="0.2.0", created_at="2026-11-22T00:00:00Z",
        )

    def test_preflight_scope_and_workpack_preserve_physical_blank_pages(self) -> None:
        report = _check_source(str(self.source), 40)
        workpack, ir = self.workpack(1, 6, "whole")
        self.assertTrue(report["passed"])
        self.assertEqual(report["confirmed_blank_pages"], [1, 3, 4, 6])
        self.assertEqual(report["page_summary"], ir["preflight"]["page_summary"])
        self.assertEqual(json.loads((workpack / "workpack.json").read_text())["pdf_ir"]["page_summary"], report["page_summary"])
        self.assertEqual(ir["preflight"]["confirmed_blank_pages"], [1, 3, 4, 6])
        self.assertEqual(report["blank_page_proofs"], [row["blank_page_proof"] for row in ir["preflight"]["pages"] if "blank_page_proof" in row])
        self.assertEqual(ir["preflight"]["scope"], {"start_page": 1, "end_page": 6})
        self.assertEqual(ir["preflight"]["pages_needing_ocr"], [])
        self.assertTrue(ir["preflight"]["eligible_for_current_compiler"])
        self.assertTrue(all(not set(row["pages"]) & {1, 3, 4, 6} for row in ir["anchor_candidates"]))
        separated_anchor = next(row for row in ir["anchor_candidates"] if row["pages"] == [2, 5])
        self.assertEqual(separated_anchor["printed_page_labels"], ["102", "105"])
        self.assertEqual(separated_anchor["locator"]["hash_scope"], "normalized full native text of physical PDF pages 2,5")
        page_rows = [json.loads(line) for line in (workpack / "page-index.jsonl").read_text().splitlines()]
        self.assertEqual([row["page"] for row in page_rows], list(range(1, 7)))
        for entry in json.loads((workpack / "workpack.json").read_text())["units"]:
            unit = json.loads((workpack / entry["path"]).read_text())
            self.assertFalse({span["page"] for span in unit["focus_spans"]} & {1, 3, 4, 6})
        issues, _ = validate_semantic_workpack(self.source, workpack)
        self.assertFalse([issue for issue in issues if issue.severity == "error"], issues)
        blank_row = next(row for row in page_rows if row["page"] == 3)
        blank_row["blank_page_proof"]["render"]["sha256"] = "0" * 64
        (workpack / "page-index.jsonl").write_text("\n".join(map(json.dumps, page_rows)) + "\n")
        issues, _ = validate_semantic_workpack(self.source, workpack)
        self.assertTrue(any("blank_page_proof_mismatch" in issue.message for issue in issues), issues)
        for mutation in ("copy-to-text", "missing-page", "duplicate-page", "missing-proof", "not-an-object"):
            with self.subTest(mutation=mutation):
                rows = [json.loads(json.dumps(row)) for row in page_rows]
                # Reset the earlier render tamper before each independent case.
                rows[2]["blank_page_proof"] = ir["preflight"]["pages"][2]["blank_page_proof"]
                if mutation == "copy-to-text":
                    rows[1]["blank_page_proof"] = rows[2]["blank_page_proof"]
                elif mutation == "missing-page":
                    rows[2].pop("page")
                elif mutation == "duplicate-page":
                    rows.append(rows[2])
                elif mutation == "missing-proof":
                    rows[2].pop("blank_page_proof")
                else:
                    rows[2] = "invalid row"
                (workpack / "page-index.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n")
                issues, _ = validate_semantic_workpack(self.source, workpack)
                self.assertTrue(any(issue.code == "blank_page_proof_invalid" for issue in issues), issues)

    def special_source(self, name: str, content: bytes, *, image: bool = False, annotation: bool = False) -> Path:
        writer = PdfWriter()
        writer.add_page(PdfReader(self.source).pages[1])
        page = writer.add_blank_page(width=300, height=400)
        page[NameObject("/Resources")] = writer.pages[0]["/Resources"]
        if image:
            image_stream = DecodedStreamObject()
            image_stream.set_data(b"\xff\xff\xff")
            image_stream.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                                 NameObject("/Width"): NumberObject(1), NameObject("/Height"): NumberObject(1),
                                 NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): writer._add_object(image_stream)})})
        if content:
            stream = DecodedStreamObject()
            stream.set_data(content)
            page[NameObject("/Contents")] = writer._add_object(stream)
        if annotation:
            from pypdf.annotations import Text
            writer.add_annotation(1, Text(rect=(10, 10, 30, 30), text="Annotation content"))
        path = self.root / f"{name}.pdf"
        writer.write(path)
        return path

    def test_only_nonpainting_content_qualifies_even_with_unused_shared_images(self) -> None:
        for name, content, image, annotation in (
            ("no-content", b"", False, False),
            ("state-only", b"q 1 0 0 1 0 0 cm BT ET Q", False, False),
            ("unused-image", b"q Q", True, False),
            ("white-vector", b"1 g 1 1 10 10 re f", False, False),
            ("white-vector-16", b"1 g " + b"1 1 10 10 re f " * 16, False, False),
        ):
            with self.subTest(name=name):
                source = self.special_source(name, content, image=image, annotation=annotation)
                report = _check_source(str(source), 40)
                self.assertTrue(report["passed"], report)
                self.assertEqual(report["confirmed_blank_pages"], [2])
        for name, content, image, annotation in (
            ("thin-vector", b"0.001 w 1 1 m 2 2 l S", False, False),
            ("near-white-vector", b"0.99999 g 1 1 10 10 re f", False, False),
            ("restore-default-black", b"q 1 g Q 1 1 10 10 re f", False, False),
            ("black-covered-white", b"0 g 1 1 10 10 re f 1 g 0 0 300 400 re f", False, False),
            ("unknown-opacity", b"/GS0 gs 1 g 0 0 300 400 re f", False, False),
            ("white-scan", b"q 300 0 0 400 0 0 cm /Im0 Do Q", True, False),
            ("unknown", b"unknownOperator", False, False),
            ("annotation", b"", False, True),
            ("page-number", b"BT /F1 12 Tf 10 10 Td (3) Tj ET", False, False),
            ("hidden-text", b"BT /F1 12 Tf 3 Tr 10 10 Td (x) Tj ET", False, False),
            ("shadow", b"0.99 g 0 0 300 400 re f", False, False),
        ):
            with self.subTest(name=name):
                source = self.special_source(name, content, image=image, annotation=annotation)
                report = _check_source(str(source), 40)
                ir = compile_pdf_intermediate_representation(source, 1, 2)
                self.assertFalse(report["passed"])
                self.assertEqual(report["confirmed_blank_pages"], [])
                self.assertEqual(report["pages_needing_ocr"], [2])
                self.assertEqual(ir["preflight"]["pages_needing_ocr"], [2])
                self.assertIn(report["page_diagnostics"][1]["classification"], {"needs_ocr", "review_required"})
                self.assertTrue(report["page_diagnostics"][1]["reason_codes"])

    def test_renderer_failure_and_source_drift_never_confirm_blank(self) -> None:
        reader = PdfReader(self.source)
        page = reader.pages[0]
        self.assertIsNone(confirm_blank_page(page, "", self.source, sha256_file(self.source), 1, self.root / "missing-renderer"))
        diagnostic = diagnose_blank_page(page, "", self.source, sha256_file(self.source), 1, self.root / "missing-renderer")
        self.assertEqual(diagnostic["classification"], "review_required")
        self.assertEqual(diagnostic["reason_codes"], ["renderer_missing"])
        self.assertIsNone(confirm_blank_page(page, "", self.source, "0" * 64, 1))
        with patch("native_blank_pages.subprocess.run", side_effect=subprocess.TimeoutExpired("pdftoppm", 30)):
            self.assertIsNone(confirm_blank_page(page, "", self.source, sha256_file(self.source), 1))
        report = build_report(source=str(self.source), renderer=str(self.root / "missing-renderer"), minimum_native_characters=40)
        self.assertFalse(report["source"]["passed"])
        self.assertEqual(report["source"]["confirmed_blank_pages"], [])

    def test_native_threshold_uses_the_same_nfkc_in_whole_and_scoped_checks(self) -> None:
        reader = PdfReader(self.source)
        # Forty decomposed Jamo normalize into twenty Hangul characters.
        with patch.object(reader.pages[1], "extract_text", return_value="\u1100\u1161" * 20):
            with patch("pypdf.PdfReader", return_value=reader):
                report = _check_source(str(self.source), 40)
                ir = compile_pdf_intermediate_representation(self.source, 1, 6)
                self.assertIn(2, report["pages_needing_ocr"])
                self.assertEqual(report["pages_needing_ocr"], ir["preflight"]["pages_needing_ocr"])
        for threshold in (0, -1):
            self.assertFalse(_check_source(str(self.source), threshold)["passed"])

    def test_verifier_replays_blank_proof_after_receipt_rehash(self) -> None:
        ir = compile_pdf_intermediate_representation(self.source, 1, 6)
        path = self.root / "verify-ir"
        write_intermediate_representation(path, ir)
        self.assertTrue(verify_pdf_ir(self.source, path, pdftoppm=None, docling_allow_network=False)["passed"])
        for field in ("source_sha256", "physical_page", "render_sha256", "removed"):
            with self.subTest(field=field):
                preflight = json.loads(json.dumps(ir["preflight"]))
                proof = preflight["pages"][0]["blank_page_proof"]
                if field == "removed":
                    preflight["pages"][0].pop("blank_page_proof")
                    preflight["pages"][0]["page_role"] = "text"
                    preflight["pages"][0]["page_disposition"] = "compile"
                    preflight["confirmed_blank_pages"].remove(1)
                elif field == "render_sha256":
                    proof["render"]["sha256"] = "0" * 64
                else:
                    proof[field] = 2 if field == "physical_page" else "0" * 64
                source = json.loads(json.dumps(ir["source"]))
                source["pdf_ir_receipts"]["preflight_sha256"] = sha256_json(preflight)
                (path / "preflight.json").write_text(json.dumps(preflight))
                (path / "source.json").write_text(json.dumps(source))
                result = verify_pdf_ir(self.source, path, pdftoppm=None, docling_allow_network=False)
                self.assertIn("blank_page_proof_missing:page=1" if field == "removed" else "blank_page_proof_mismatch:page=1", result["failures"])
                self.assertFalse(result["passed"])

    def test_book_planner_and_scan_quality_never_anchor_blank_pages(self) -> None:
        plan = self.root / "book-plan"
        build_book_plan(self.source, plan)
        rows = [json.loads(line) for line in (plan / "book-coverage-ledger.jsonl").read_text().splitlines()]
        blank_rows = [row for row in rows if row["scope_kind"] == "physical_page" and row["physical_pages"][0] in {1, 3, 4, 6}]
        self.assertEqual(len(blank_rows), 4)
        self.assertTrue(all(row["planning_status"] == "non-knowledge" for row in blank_rows))
        modules = [json.loads(line) for line in (plan / "module-plan.jsonl").read_text().splitlines()]
        self.assertTrue(any(row["planning_status"] == "compile" and set(row["physical_pages"]) & {2, 5} for row in modules), modules)
        preflight = compile_pdf_intermediate_representation(self.source, 1, 6)["preflight"]
        preflight["page_routing"]["ocr_required_pages"] = [5]
        quality = build_quality_routing(preflight=preflight, observations=[], transcripts=[], conflicts=[], confidence_threshold=0.8)
        blank = [row for row in quality["pages"] if row["physical_page"] in {1, 3, 4, 6}]
        self.assertTrue(all(row["route"] == "confirmed-blank" and not row["canonical_anchor_allowed"] for row in blank))
        self.assertEqual(quality["ocr_only_pages_needing_route"], [5])

    def test_blank_bookmark_is_retained_in_ir_and_omitted_only_for_blank_scope(self) -> None:
        writer = PdfWriter()
        writer.add_page(PdfReader(self.source).pages[1])
        writer.add_blank_page(width=300, height=400)
        writer.add_page(PdfReader(self.source).pages[4])
        writer.add_outline_item("1 First section", 0)
        writer.add_outline_item("Separator", 1)
        writer.write(self.source)
        workpack, ir = self.workpack(1, 2, "separator")
        self.assertEqual([segment["title"] for segment in ir["document_map"]["segments"]], ["1 First section", "Separator"])
        self.assertEqual(ir["document_map"]["segments"][1]["physical_pages"], [2])
        self.assertEqual(len(ir["anchor_candidates"]), 1)
        manifest = json.loads((workpack / "workpack.json").read_text())
        self.assertEqual(len(manifest["units"]), 1)
        self.assertEqual(manifest["source"]["scope"], {"start_page": 1, "end_page": 2})
        from semantic_promotion import SemanticPromotionError
        with self.assertRaisesRegex(SemanticPromotionError, "heading_locator|heading_resolution"):
            self.workpack(1, 3, "separator-with-content")

    def test_real_cli_whole_book_and_chapter_routes_agree_and_bind_tools(self) -> None:
        workspace = self.root / "job"
        planned = self.cli("fake_expert.py", "plan", "--source", self.source, "--workspace", workspace,
                           "--profile", "native-text", "--figures", "none", "--json")
        self.assertEqual(planned.returncode, 0, planned.stderr + planned.stdout)
        job_path = workspace / "fake-expert-job.json"
        job = json.loads(job_path.read_text())
        self.assertEqual(job["tool_bindings"]["native_blank"]["sha256"], sha256_file(SCRIPTS / "native_blank_pages.py"))
        self.assertIn("native_blank_pages.py", native_extraction.TOOLS)
        compiled = self.cli("fake_expert.py", "compile", "--job", job_path, "--json")
        self.assertEqual(compiled.returncode, 0, compiled.stderr + compiled.stdout)
        self.assertEqual(json.loads(compiled.stdout)["code"], "semantic_authoring_required")
        chapter = self.cli("reconstruct_pdf.py", self.source, "--start-page", 1, "--end-page", 4,
                           "--output", self.root / "chapter-cli", "--json")
        self.assertEqual(chapter.returncode, 0, chapter.stderr)
        self.assertEqual(json.loads((self.root / "chapter-cli/preflight.json").read_text())["confirmed_blank_pages"], [1, 3, 4])
        job.pop("job_sha256")
        job["tool_bindings"].pop("native_blank")
        job["job_sha256"] = fake_expert._hash_without(job, "job_sha256")
        fake_expert._validate_job_shape(job)
        self.assertIn("tool_hash_mismatch:native_blank", fake_expert._validate_job_bindings(job_path, job))

    def test_all_blank_scope_is_not_a_knowledge_source(self) -> None:
        report = _check_source(str(self.special_source("all-blank-placeholder", b"")), 40)
        self.assertTrue(report["passed"])  # contains the fixture's first text page
        empty = self.root / "all-blank.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=400)
        writer.write(empty)
        report = _check_source(str(empty), 40)
        self.assertFalse(report["passed"])
        self.assertEqual(report["code"], "source_contains_no_native_text")
        ir = compile_pdf_intermediate_representation(empty, 1, 1)
        self.assertFalse(ir["preflight"]["eligible_for_current_compiler"])
        self.assertEqual(ir["anchor_candidates"], [])

    def test_adjacent_extend_keeps_original_scope_and_rejects_skipped_blanks(self) -> None:
        first, _ = self.workpack(1, 4, "first")
        second, _ = self.workpack(5, 6, "second")
        base = self.root / "base"
        self.build(first, base)
        before = {path.relative_to(base): path.read_bytes() for path in base.rglob("*") if path.is_file()}
        for existing_empty in (False, True):
            target = self.root / f"adjacent-{existing_empty}"
            if existing_empty:
                target.mkdir()
            result = self.extend(base, second, target)
            self.assertEqual(result["package"], str(target.resolve()))
            self.assertEqual(direct_reference_skill.validate_direct_reference(target), [])
            manifest = json.loads((target / "manifest.json").read_text())
            self.assertEqual(manifest["sources"][0]["scope"]["physical_pages"], [1, 6])
        short, _ = self.workpack(1, 2, "short")
        short_base = self.root / "short-base"
        self.build(short, short_base)
        target = self.root / "gap"
        result = self.cli("direct_reference_skill.py", "--json", "extend", "--base", short_base,
                          "--source", self.source, "--workpack", second, "--output", target,
                          "--version", "0.2.0", "--created-at", "2026-11-22T00:00:00Z")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non_contiguous_source_scope_requires_separate_package", result.stdout + result.stderr)
        self.assertFalse(target.exists())
        self.assertEqual(before, {path.relative_to(base): path.read_bytes() for path in base.rglob("*") if path.is_file()})

    def test_extend_write_failure_cleans_staging_and_protects_existing_output(self) -> None:
        first, _ = self.workpack(1, 4, "first")
        second, _ = self.workpack(5, 6, "second")
        base = self.root / "base"
        self.build(first, base)
        for existing_empty in (False, True):
            target = self.root / f"failed-{existing_empty}"
            if existing_empty:
                target.mkdir()
            with patch.object(direct_reference_skill, "_write_package", side_effect=OSError("synthetic write failure")):
                with self.assertRaisesRegex(OSError, "synthetic write failure"):
                    self.extend(base, second, target)
            self.assertEqual(target.exists(), existing_empty)
            if existing_empty:
                self.assertEqual(list(target.iterdir()), [])
            self.assertEqual(list(self.root.glob(f".{target.name}-extend-*")), [])
        target = self.root / "user-output"
        target.mkdir()
        marker = target / "keep.txt"
        marker.write_text("user content")
        with self.assertRaisesRegex(direct_reference_skill.DirectReferenceError, "output_not_empty"):
            self.extend(base, second, target)
        self.assertEqual(marker.read_text(), "user content")
        link = self.root / "output-link"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(direct_reference_skill.DirectReferenceError, "output_not_empty"):
            self.extend(base, second, link)
        self.assertTrue(link.is_symlink())
        self.assertEqual(direct_reference_skill.validate_direct_reference(base), [])


if __name__ == "__main__":
    unittest.main()
