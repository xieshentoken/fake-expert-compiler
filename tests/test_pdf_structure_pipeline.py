from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from reportlab.pdfgen import canvas


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = PROJECT_ROOT / "fake-expert/scripts"
RECONSTRUCT = SCRIPT_ROOT / "reconstruct_pdf.py"
VERIFY_IR = SCRIPT_ROOT / "verify_pdf_ir.py"
sys.path.insert(0, str(SCRIPT_ROOT))

from pdf_structure import (
    OutlineNode,
    PdfPipelineError,
    compile_pdf_intermediate_representation,
    normalize_text,
    resolve_outline_headings,
)
from pdf_parser_adapters import build_parser_receipt


def create_structured_pdf(path: Path) -> None:
    pdf = canvas.Canvas(str(path))
    pages = (
        (
            "chapter",
            "1 Synthetic systems",
            "1.1 Introduction",
            "This page contains enough native text to exercise deterministic extraction and structure checks.",
            "101",
        ),
        (
            "section-two",
            "1.2 Governing equation",
            "E = mc^2 (1.1)",
            "The /Lambda marker intentionally simulates a decoded mathematical glyph that needs visual review.",
            "102",
        ),
        (
            "section-three",
            "1.3 Operating boundary",
            "The operating boundary is valid only while the stated assumptions and measurements remain available.",
            "This sentence keeps the born-digital text layer above the configured quality threshold.",
            "103",
        ),
    )
    for index, (bookmark, heading, line_two, body, printed_page) in enumerate(pages):
        pdf.bookmarkPage(bookmark)
        if index == 0:
            pdf.bookmarkPage("section-one")
            pdf.addOutlineEntry("1 Synthetic systems", bookmark, level=0, closed=False)
            pdf.addOutlineEntry("1.1 Introduction", "section-one", level=1, closed=False)
        else:
            pdf.addOutlineEntry(heading, bookmark, level=1, closed=False)
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(72, 750, heading)
        pdf.setFont("Helvetica", 11)
        pdf.drawString(72, 720, line_two)
        text = pdf.beginText(72, 690)
        for chunk in (body[:85], body[85:]):
            if chunk:
                text.textLine(chunk)
        pdf.drawText(text)
        pdf.drawCentredString(306, 36, printed_page)
        pdf.showPage()
    pdf.save()


def create_blank_pdf(path: Path) -> None:
    pdf = canvas.Canvas(str(path))
    pdf.showPage()
    pdf.save()


def create_duplicate_heading_pdf(path: Path) -> None:
    pdf = canvas.Canvas(str(path))
    pages = (
        (
            "start-bookmark",
            "1 Plasma book",
            "1.1 Start",
            "Introductory native text for the duplicate-heading test.",
            "101",
        ),
        (
            "problems-two",
            "Problems",
            "Chapter 2 problems are listed on this page.",
            "Enough native text to exercise deterministic duplicate-heading resolution.",
            "102",
        ),
        (
            "problems-three",
            "Problems",
            "Chapter 3 problems are listed on this page.",
            "Enough native text to exercise deterministic duplicate-heading resolution.",
            "103",
        ),
    )
    for index, (bookmark, heading, line_two, body, printed_page) in enumerate(pages):
        pdf.bookmarkPage(bookmark)
        if index == 0:
            pdf.bookmarkPage("start-child")
            pdf.addOutlineEntry("1 Plasma book", bookmark, level=0, closed=False)
            pdf.addOutlineEntry("1.1 Start", "start-child", level=1, closed=False)
        else:
            pdf.addOutlineEntry(heading, bookmark, level=1, closed=False)
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(72, 750, heading)
        pdf.setFont("Helvetica", 11)
        pdf.drawString(72, 720, line_two)
        text = pdf.beginText(72, 690)
        for chunk in (body[:85], body[85:]):
            if chunk:
                text.textLine(chunk)
        pdf.drawText(text)
        pdf.drawCentredString(306, 36, printed_page)
        pdf.showPage()
    pdf.save()


def create_out_of_order_outline_pdf(path: Path) -> None:
    pdf = canvas.Canvas(str(path))
    pages = (
        ("first", "1.1 First section", "Opening text on physical page one."),
        ("second", "1.2 Second section", "Middle text on physical page two."),
        ("third", "1.3 Third section", "Closing text on physical page three."),
    )
    for bookmark, heading, body in pages:
        pdf.bookmarkPage(bookmark)
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(72, 750, heading)
        pdf.setFont("Helvetica", 11)
        text = pdf.beginText(72, 700)
        for chunk in (body[:85], body[85:]):
            if chunk:
                text.textLine(chunk)
        pdf.drawText(text)
        pdf.showPage()
    pdf.addOutlineEntry("1.3 Third section", "third", level=0, closed=False)
    pdf.addOutlineEntry("1.1 First section", "first", level=1, closed=False)
    pdf.addOutlineEntry("1.2 Second section", "second", level=1, closed=False)
    pdf.save()


def create_rename_offset_pdf(path: Path) -> None:
    pdf = canvas.Canvas(str(path))
    pdf.bookmarkPage("intro")
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(72, 750, "1.1 Intro")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(72, 720, "Introductory text for the rename-offset ordering regression test.")
    pdf.showPage()

    pdf.bookmarkPage("alpha")
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(72, 750, "1.2 Alpha original")
    pdf.bookmarkPage("beta")
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(72, 700, "1.3 Beta")
    pdf.setFont("Helvetica", 11)
    text = pdf.beginText(72, 650)
    text.textLine("The corrected body heading reads 1.2 Alpha renamed and sits below the beta section.")
    pdf.drawText(text)
    pdf.showPage()

    pdf.addOutlineEntry("1.1 Intro", "intro", level=0, closed=False)
    pdf.addOutlineEntry("1.2 Alpha original", "alpha", level=1, closed=False)
    pdf.addOutlineEntry("1.3 Beta", "beta", level=1, closed=False)
    pdf.save()


def create_no_outline_pdf(path: Path) -> None:
    pdf = canvas.Canvas(str(path))
    for heading, printed_page in (("2 Fallback chapter", "201"), ("2.1 Child section", "202")):
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(72, 750, heading)
        pdf.setFont("Helvetica", 11)
        pdf.drawString(
            72,
            710,
            "Native text heading reconstruction remains a heuristic and requires semantic review.",
        )
        pdf.drawCentredString(306, 36, printed_page)
        pdf.showPage()
    pdf.save()


def create_offset_outline_pdf(path: Path) -> None:
    """Synthetic LaTeX-like layout: outline lands on a chapter contents page."""

    pdf = canvas.Canvas(str(path))
    pdf.bookmarkPage("chapter")
    pdf.bookmarkPage("first")
    pdf.bookmarkPage("second")
    pdf.addOutlineEntry("6 Chapter Six", "chapter", level=0, closed=False)
    pdf.addOutlineEntry("6.1 First section", "chapter", level=1, closed=False)
    pdf.addOutlineEntry("6.2 Second section", "chapter", level=1, closed=False)
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(72, 750, "6 Chapter Six")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(72, 710, "6.1 First section 102")
    pdf.drawString(72, 690, "6.2 Second section 103")
    pdf.drawCentredString(306, 36, "101")
    pdf.showPage()

    pdf.bookmarkPage("first-body")
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(72, 750, "6.1 First section")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(72, 710, "The first body section follows the chapter contents list.")
    pdf.drawCentredString(306, 36, "102")
    pdf.showPage()

    pdf.bookmarkPage("second-body")
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(72, 750, "6.2 Second section")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(72, 710, "The second body section follows the first section.")
    pdf.drawCentredString(306, 36, "103")
    pdf.showPage()
    pdf.save()


class PdfStructurePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "structured.pdf"
        create_structured_pdf(self.source)

    def _fake_renderer(self) -> Path:
        renderer = self.root / "fake-pdftoppm.py"
        if renderer.exists():
            return renderer
        renderer.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib\n"
            "import sys\n"
            "if '-v' in sys.argv:\n"
            "    print('fake-pdftoppm 1.0', file=sys.stderr)\n"
            "    raise SystemExit(0)\n"
            "pathlib.Path(sys.argv[-1]).with_suffix('.png').write_bytes(b'\\x89PNG\\r\\n\\x1a\\nfixture')\n",
            encoding="utf-8",
        )
        renderer.chmod(0o700)
        return renderer

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_preflight_separates_ocr_from_visual_review(self) -> None:
        result = compile_pdf_intermediate_representation(self.source, 1, 3)
        preflight = result["preflight"]
        self.assertEqual(preflight["scope_classification"], "text-based")
        self.assertTrue(preflight["eligible_for_current_compiler"])
        self.assertEqual(preflight["pages_needing_ocr"], [])
        self.assertIn(2, preflight["pages_needing_visual_verification"])
        second_page = preflight["pages"][1]
        self.assertIn("equation_label_present", second_page["visual_verification_reasons"])
        self.assertIn("suspicious_glyph_decoding", second_page["visual_verification_reasons"])
        self.assertEqual(second_page["printed_page_label_candidate"], "102")
        self.assertFalse(second_page["pdf_label_matches_printed_candidate"])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("This page contains enough native text", serialized)
        self.assertNotIn("The /Lambda marker intentionally simulates", serialized)
        parser_receipt = result["parser_receipt"]
        self.assertEqual(parser_receipt["source_sha256"], result["source"]["sha256"])
        self.assertEqual(
            [row["physical_page"] for row in parser_receipt["pages"]], [1, 2, 3]
        )
        self.assertEqual(parser_receipt["layout_parser"]["id"], "pypdf")
        self.assertEqual(result["layout_candidates"], [])
        self.assertEqual(result["source"]["pdf_ir_receipts"]["render_receipt_status"], "deferred")

    def test_outline_structure_and_page_envelope_anchor_hashes(self) -> None:
        result = compile_pdf_intermediate_representation(self.source, 1, 3)
        document_map = result["document_map"]
        self.assertEqual(document_map["structure_source"], "pdf-outline")
        titles = [segment["title"] for segment in document_map["segments"]]
        self.assertEqual(
            titles,
            [
                "1 Synthetic systems",
                "1.1 Introduction",
                "1.2 Governing equation",
                "1.3 Operating boundary",
            ],
        )
        candidates = result["anchor_candidates"]
        self.assertEqual(len(candidates), 4)
        second = next(
            candidate for candidate in candidates if candidate["locator"]["title"] == "1.2 Governing equation"
        )
        self.assertEqual(second["pages"], [2, 3])

        from pypdf import PdfReader

        reader = PdfReader(str(self.source))
        text = "\f".join(
            normalize_text(reader.pages[index].extract_text() or "") for index in (1, 2)
        )
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.assertEqual(second["excerpt_sha256"], expected)
        self.assertTrue(second["visual_verification_required"])

    def test_cli_outputs_are_deterministic_and_refuse_nonempty_directory(self) -> None:
        output_one = self.root / "run-one"
        output_two = self.root / "run-two"
        for output in (output_one, output_two):
            completed = subprocess.run(
                [
                    sys.executable,
                    str(RECONSTRUCT),
                    str(self.source),
                    "--start-page",
                    "1",
                    "--end-page",
                    "3",
                    "--output",
                    str(output),
                    "--pdftoppm",
                    str(self._fake_renderer()),
                    "--json",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertTrue(json.loads(completed.stdout)["passed"])

        expected_files = {
            "source.json",
            "preflight.json",
            "document-map.json",
            "anchor-candidates.jsonl",
            "visual-review.json",
            "parser-receipt.json",
            "layout-candidates.jsonl",
            "render-receipts.jsonl",
            "heading-candidates.jsonl",
            "heading-resolutions.jsonl",
        }
        self.assertEqual({path.name for path in output_one.iterdir()}, expected_files)
        for filename in expected_files:
            self.assertEqual(
                (output_one / filename).read_bytes(),
                (output_two / filename).read_bytes(),
            )

        repeated = subprocess.run(
            [
                sys.executable,
                str(RECONSTRUCT),
                str(self.source),
                "--output",
                str(output_one),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(repeated.returncode, 2)
        self.assertIn("output_not_empty", repeated.stderr)

    def test_cli_binds_a_render_receipt_for_every_scoped_physical_page(self) -> None:
        output = self.root / "rendered-ir"
        completed = subprocess.run(
            [
                sys.executable,
                str(RECONSTRUCT),
                str(self.source),
                "--start-page",
                "1",
                "--end-page",
                "3",
                "--output",
                str(output),
                "--pdftoppm",
                str(self._fake_renderer()),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["render_receipt_count"], 3)
        receipts = [
            json.loads(line)
            for line in (output / "render-receipts.jsonl").read_text(encoding="utf-8").splitlines()
            if line
        ]
        self.assertEqual([row["physical_page"] for row in receipts], [1, 2, 3])
        self.assertEqual({row["source_sha256"] for row in receipts}, {summary["source_sha256"]})
        self.assertTrue(all(len(row["render_sha256"]) == 64 for row in receipts))
        verified = subprocess.run(
            [
                sys.executable,
                str(VERIFY_IR),
                "--source",
                str(self.source),
                "--ir",
                str(output),
                "--pdftoppm",
                str(self._fake_renderer()),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertTrue(json.loads(verified.stdout)["passed"])

    def test_pymupdf_adapter_normalizes_only_hash_bound_coordinates(self) -> None:
        class FakePage:
            rect = SimpleNamespace(width=612, height=792)

            @staticmethod
            def get_text(mode: str, *, sort: bool) -> list[tuple[object, ...]]:
                if mode != "words" or sort is not False:
                    raise AssertionError("unexpected PyMuPDF word extraction request")
                return [(72, 720, 132, 735, "Alpha", 0, 1, 2)]

        class FakeDocument:
            def __len__(self) -> int:
                return 1

            @staticmethod
            def __getitem__(index: int) -> FakePage:
                if index != 0:
                    raise IndexError(index)
                return FakePage()

            @staticmethod
            def close() -> None:
                return None

        fake_fitz = SimpleNamespace(VersionBind="mock-1.0", open=lambda _: FakeDocument())
        source_hash = "a" * 64
        with patch.dict(sys.modules, {"pymupdf": fake_fitz, "fitz": fake_fitz}):
            receipt, candidates = build_parser_receipt(
                self.source,
                source_hash,
                1,
                1,
                {1: {"width": 612.0, "height": 792.0}},
                "pypdf-6.10.0",
                "pymupdf",
            )
        self.assertEqual(receipt["layout_parser"]["id"], "pymupdf")
        self.assertEqual(receipt["pages"][0]["coordinate_status"], "available")
        self.assertEqual(candidates[0]["physical_page"], 1)
        self.assertEqual(candidates[0]["bbox"], [72.0, 720.0, 132.0, 735.0])
        self.assertNotIn("Alpha", json.dumps(candidates))

    def test_blank_pdf_is_routed_out_of_current_compiler(self) -> None:
        blank = self.root / "blank.pdf"
        create_blank_pdf(blank)
        result = compile_pdf_intermediate_representation(blank, 1, 1)
        self.assertEqual(result["preflight"]["scope_classification"], "image-based-or-unusable")
        self.assertFalse(result["preflight"]["eligible_for_current_compiler"])
        self.assertEqual(result["preflight"]["pages_needing_ocr"], [1])

    def test_native_text_heading_fallback_reconstructs_parent(self) -> None:
        no_outline = self.root / "no-outline.pdf"
        create_no_outline_pdf(no_outline)
        result = compile_pdf_intermediate_representation(no_outline, 1, 2)
        document_map = result["document_map"]
        self.assertEqual(document_map["structure_source"], "native-text-heading-heuristic")
        self.assertEqual([row["title"] for row in document_map["segments"]], [
            "2 Fallback chapter",
            "2.1 Child section",
        ])
        self.assertEqual(
            document_map["segments"][1]["parent_id"],
            document_map["segments"][0]["id"],
        )

    def test_outline_offset_and_chapter_contents_are_resolved_in_pdf_ir(self) -> None:
        nodes = [
            OutlineNode("6 Chapter Six", 0, 1, None),
            OutlineNode("6.1 First section", 1, 1, 0),
            OutlineNode("6.2 Second section", 1, 1, 1),
        ]
        resolutions_by_index, resolutions = resolve_outline_headings(
            nodes,
            {
                1: "6 Chapter Six\n6.1 First section 102\n6.2 Second section 103",
                2: "6.1 First section\nThe first body section follows the chapter contents list.",
                3: "6.2 Second section\nThe second body section follows the first section.",
            },
            3,
        )
        first = next(row for row in resolutions if row["title"] == "6.1 First section")
        second = next(row for row in resolutions if row["title"] == "6.2 Second section")
        self.assertEqual(first["outline_page"], 1)
        self.assertEqual(first["selected_page"], 2)
        self.assertEqual(first["status"], "resolved")
        self.assertEqual(second["selected_page"], 3)
        self.assertEqual(resolutions_by_index[1], 2)
        self.assertEqual(resolutions_by_index[2], 3)

    def test_page_routing_records_blank_and_native_roles(self) -> None:
        result = compile_pdf_intermediate_representation(self.source, 1, 3)
        roles = [row["page_role"] for row in result["preflight"]["pages"]]
        self.assertEqual(roles, ["text", "text", "text"])
        self.assertEqual(result["preflight"]["page_routing"]["compile_pages"], [1, 2, 3])

        blank = self.root / "blank-routed.pdf"
        create_blank_pdf(blank)
        blank_result = compile_pdf_intermediate_representation(blank, 1, 1)
        self.assertEqual(blank_result["preflight"]["pages"][0]["page_role"], "blank-or-unusable")
        self.assertEqual(blank_result["preflight"]["page_routing"]["manual_review_pages"], [1])

    def test_pdf_ir_receipts_lock_every_ir_file(self) -> None:
        result = compile_pdf_intermediate_representation(self.source, 1, 3)
        receipts = result["source"]["pdf_ir_receipts"]
        for field in (
            "preflight_sha256",
            "document_map_sha256",
            "visual_review_sha256",
            "anchor_candidates_sha256",
        ):
            self.assertIn(field, receipts)
            self.assertEqual(len(receipts[field]), 64)
        self.assertNotIn("heading_overrides_sha256", receipts)

    def test_tampered_document_map_is_rejected_by_ir_verifier(self) -> None:
        output = self.root / "locked-ir"
        completed = subprocess.run(
            [
                sys.executable,
                str(RECONSTRUCT),
                str(self.source),
                "--start-page",
                "1",
                "--end-page",
                "3",
                "--output",
                str(output),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        document_map = json.loads((output / "document-map.json").read_text(encoding="utf-8"))
        document_map["segments"][0]["title"] = "1 Synthetic systems tampered"
        (output / "document-map.json").write_text(
            json.dumps(document_map, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        verified = subprocess.run(
            [
                sys.executable,
                str(VERIFY_IR),
                "--source",
                str(self.source),
                "--ir",
                str(output),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(verified.returncode, 1)
        self.assertIn("document_map_hash_mismatch", json.loads(verified.stdout)["failures"])

    def test_heading_overrides_are_applied_recorded_and_locked(self) -> None:
        overrides = self.root / "heading-overrides.jsonl"
        overrides.write_text(
            json.dumps(
                {
                    "schema_version": "tkc.heading-override/v0.1",
                    "before_title": "1.2 Governing equation",
                    "new_title": "1.2 Governing equation corrected",
                    "physical_page": 2,
                    "rationale": "The outline title omitted the correction visible in the body heading.",
                    "reviewer_instance": "ir-corrector",
                    "before_sha256": hashlib.sha256(
                        "1.2 Governing equation".encode("utf-8")
                    ).hexdigest(),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        output = self.root / "override-ir"
        completed = subprocess.run(
            [
                sys.executable,
                str(RECONSTRUCT),
                str(self.source),
                "--start-page",
                "1",
                "--end-page",
                "3",
                "--heading-overrides",
                str(overrides),
                "--output",
                str(output),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["heading_override_count"], 1)
        self.assertTrue((output / "heading-overrides.jsonl").is_file())

        document_map = json.loads((output / "document-map.json").read_text(encoding="utf-8"))
        segment = next(
            row
            for row in document_map["segments"]
            if row.get("heading_override", {}).get("before_title") == "1.2 Governing equation"
        )
        self.assertEqual(segment["title"], "1.2 Governing equation corrected")
        self.assertEqual(segment["heading_override"]["new_title"], "1.2 Governing equation corrected")
        self.assertEqual(segment["heading_override"]["reviewer_instance"], "ir-corrector")
        self.assertEqual(len(segment["heading_override"]["override_sha256"]), 64)
        resolution = next(
            row
            for row in document_map["heading_resolutions"]
            if row["title"] == "1.2 Governing equation corrected"
        )
        self.assertEqual(resolution["outline_page"], 2)

        candidates = [
            json.loads(line)
            for line in (output / "anchor-candidates.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        candidate = next(
            row
            for row in candidates
            if row["locator"]["title"] == "1.2 Governing equation corrected"
        )
        self.assertEqual(candidate["segment_id"], segment["id"])

        source = json.loads((output / "source.json").read_text(encoding="utf-8"))
        receipts = source["pdf_ir_receipts"]
        self.assertIn("heading_overrides_sha256", receipts)
        self.assertEqual(len(receipts["heading_overrides_sha256"]), 64)

        verified = subprocess.run(
            [
                sys.executable,
                str(VERIFY_IR),
                "--source",
                str(self.source),
                "--ir",
                str(output),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertTrue(json.loads(verified.stdout)["passed"])

        document_map["segments"][0]["title"] = "1 Synthetic systems tampered"
        (output / "document-map.json").write_text(
            json.dumps(document_map, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tampered = subprocess.run(
            [
                sys.executable,
                str(VERIFY_IR),
                "--source",
                str(self.source),
                "--ir",
                str(output),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(tampered.returncode, 1)
        self.assertIn("document_map_hash_mismatch", json.loads(tampered.stdout)["failures"])

    def test_heading_override_must_bind_an_existing_segment(self) -> None:
        overrides = self.root / "unbound-overrides.jsonl"
        overrides.write_text(
            json.dumps(
                {
                    "schema_version": "tkc.heading-override/v0.1",
                    "before_title": "No such heading",
                    "new_title": "Still no such heading",
                    "physical_page": 1,
                    "rationale": "This override targets nothing and must fail closed.",
                    "reviewer_instance": "ir-corrector",
                    "before_sha256": hashlib.sha256("No such heading".encode("utf-8")).hexdigest(),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        output = self.root / "unbound-ir"
        completed = subprocess.run(
            [
                sys.executable,
                str(RECONSTRUCT),
                str(self.source),
                "--heading-overrides",
                str(overrides),
                "--output",
                str(output),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("heading_override_unresolved", completed.stderr)

    def test_heading_override_disambiguates_duplicate_titles(self) -> None:
        source = self.root / "duplicate-headings.pdf"
        create_duplicate_heading_pdf(source)
        base = self.root / "dup-base-ir"
        base_completed = subprocess.run(
            [
                sys.executable,
                str(RECONSTRUCT),
                str(source),
                "--start-page",
                "1",
                "--end-page",
                "3",
                "--output",
                str(base),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(base_completed.returncode, 0, base_completed.stdout + base_completed.stderr)
        document_map = json.loads((base / "document-map.json").read_text(encoding="utf-8"))
        problems = [row for row in document_map["segments"] if row["title"] == "Problems"]
        self.assertEqual(len(problems), 2)
        problems.sort(key=lambda row: row["physical_pages"][0])
        self.assertEqual([row["physical_pages"][0] for row in problems], [2, 3])

        def override_row(before_title, physical_page, segment_id=None):
            row = {
                "schema_version": "tkc.heading-override/v0.1",
                "before_title": before_title,
                "new_title": before_title + " corrected",
                "physical_page": physical_page,
                "rationale": "A unique page target disambiguates the repeated section title.",
                "reviewer_instance": "ir-corrector",
                "before_sha256": hashlib.sha256(before_title.encode("utf-8")).hexdigest(),
            }
            if segment_id is not None:
                row["segment_id"] = segment_id
            return row

        page_targeted = self.root / "page-targeted-overrides.jsonl"
        page_targeted.write_text(
            json.dumps(override_row("Problems", 2)) + "\n", encoding="utf-8"
        )
        page_output = self.root / "page-targeted-ir"
        page_completed = subprocess.run(
            [
                sys.executable,
                str(RECONSTRUCT),
                str(source),
                "--start-page",
                "1",
                "--end-page",
                "3",
                "--heading-overrides",
                str(page_targeted),
                "--output",
                str(page_output),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(page_completed.returncode, 0, page_completed.stdout + page_completed.stderr)
        page_map = json.loads((page_output / "document-map.json").read_text(encoding="utf-8"))
        page_renamed = [
            row for row in page_map["segments"] if row.get("heading_override") is not None
        ]
        self.assertEqual(len(page_renamed), 1)
        self.assertEqual(page_renamed[0]["id"], problems[0]["id"])
        self.assertEqual(page_renamed[0]["title"], "Problems corrected")
        self.assertEqual(
            [row["title"] for row in page_map["segments"] if row["id"] == problems[1]["id"]],
            ["Problems"],
        )

        id_targeted = self.root / "id-targeted-overrides.jsonl"
        id_targeted.write_text(
            json.dumps(override_row("Problems", 3, segment_id=problems[1]["id"])) + "\n",
            encoding="utf-8",
        )
        id_output = self.root / "id-targeted-ir"
        id_completed = subprocess.run(
            [
                sys.executable,
                str(RECONSTRUCT),
                str(source),
                "--start-page",
                "1",
                "--end-page",
                "3",
                "--heading-overrides",
                str(id_targeted),
                "--output",
                str(id_output),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(id_completed.returncode, 0, id_completed.stdout + id_completed.stderr)
        id_map = json.loads((id_output / "document-map.json").read_text(encoding="utf-8"))
        id_renamed = [row for row in id_map["segments"] if row.get("heading_override") is not None]
        self.assertEqual(len(id_renamed), 1)
        self.assertEqual(id_renamed[0]["id"], problems[1]["id"])
        self.assertEqual(id_renamed[0]["title"], "Problems corrected")

        ambiguous = self.root / "ambiguous-overrides.jsonl"
        ambiguous.write_text(
            json.dumps(override_row("Problems", 2, segment_id=problems[1]["id"])) + "\n",
            encoding="utf-8",
        )
        ambiguous_output = self.root / "ambiguous-ir"
        ambiguous_completed = subprocess.run(
            [
                sys.executable,
                str(RECONSTRUCT),
                str(source),
                "--start-page",
                "1",
                "--end-page",
                "3",
                "--heading-overrides",
                str(ambiguous),
                "--output",
                str(ambiguous_output),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(ambiguous_completed.returncode, 2)
        self.assertIn("heading_override_unresolved", ambiguous_completed.stderr)

    def test_out_of_order_outline_is_sorted_by_physical_position(self) -> None:
        source = self.root / "out-of-order-outline.pdf"
        create_out_of_order_outline_pdf(source)

        def run_reconstruct() -> tuple[int, list[str]]:
            output = self.root / f"order-ir-{len(list(self.root.glob('order-ir-*')))}"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(RECONSTRUCT),
                    str(source),
                    "--start-page",
                    "1",
                    "--end-page",
                    "3",
                    "--output",
                    str(output),
                    "--json",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            document_map = json.loads((output / "document-map.json").read_text(encoding="utf-8"))
            first_pages = [row["physical_pages"][0] for row in document_map["segments"]]
            self.assertEqual(first_pages, sorted(first_pages))
            titles = [row["title"] for row in document_map["segments"]]
            return completed.returncode, titles

        _, first_titles = run_reconstruct()
        _, second_titles = run_reconstruct()
        self.assertEqual(first_titles, second_titles)
        self.assertEqual(
            first_titles,
            ["1.1 First section", "1.2 Second section", "1.3 Third section"],
        )

    def test_physical_sort_uses_final_override_titles(self) -> None:
        source = self.root / "rename-offset.pdf"
        create_rename_offset_pdf(source)
        overrides = self.root / "rename-overrides.jsonl"
        overrides.write_text(
            json.dumps(
                {
                    "schema_version": "tkc.heading-override/v0.1",
                    "before_title": "1.2 Alpha original",
                    "new_title": "1.2 Alpha renamed",
                    "physical_page": 2,
                    "rationale": "The body heading reads '1.2 Alpha renamed'.",
                    "reviewer_instance": "ir-corrector",
                    "before_sha256": hashlib.sha256(
                        "1.2 Alpha original".encode("utf-8")
                    ).hexdigest(),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        output = self.root / "rename-ir"
        completed = subprocess.run(
            [
                sys.executable,
                str(RECONSTRUCT),
                str(source),
                "--start-page",
                "1",
                "--end-page",
                "2",
                "--heading-overrides",
                str(overrides),
                "--output",
                str(output),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        document_map = json.loads((output / "document-map.json").read_text(encoding="utf-8"))
        titles = [row["title"] for row in document_map["segments"]]
        self.assertEqual(
            titles,
            ["1.1 Intro", "1.3 Beta", "1.2 Alpha renamed"],
        )

    def test_rejects_non_pdf_input(self) -> None:
        text_file = self.root / "not-a-pdf.txt"
        text_file.write_text("not a PDF", encoding="utf-8")
        with self.assertRaisesRegex(PdfPipelineError, "unsupported_input_format"):
            compile_pdf_intermediate_representation(text_file, 1, 1)


if __name__ == "__main__":
    unittest.main()
