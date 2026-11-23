from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = PROJECT_ROOT / "fake-expert/scripts"
sys.path.insert(0, str(SCRIPT_ROOT))

from expert_skill_contract import validate_expert_skill
from pdf_structure import compile_pdf_intermediate_representation, write_intermediate_representation
from semantic_promotion import (
    build_semantic_workpack,
    merge_review_fragments,
    prepare_review_plan,
    promote_reviewed_workpack,
    sha256_json as promotion_sha256_json,
    validate_reviewed_workpack,
)
from visual_adapters import VisualAdapterError, adapter_receipt, map_candidate_locators, normalize_candidate_rows
from visual_semantics import (
    VISUAL_REVIEW_PROTOCOL,
    FIGURE_RE,
    TABLE_RE,
    VisualSemanticsError,
    _caption_adjacent_xobject,
    _concise_plot_marker_rows,
    _consume_xobject_candidate,
    _find_label_boxes,
    _object_hash,
    _page_xobject_candidates,
    _recombine_text_boxes,
    _text_boxes,
    build_visual_object,
    build_table_continuation_candidates,
    build_visual_review_attestation,
    canonical_json,
    deduplicate_visual_candidates,
    extract_visual_candidates,
    normalize_bbox,
    sha256_json,
    visual_input_sha256,
    visual_assurance_manifest,
    visual_review_overlay_manifest,
    validate_visual_bundle,
    validate_visual_assurance_manifest,
    validate_visual_review_overlay_manifest,
    validate_visual_review_attestations,
)


def _pdftoppm() -> Path | None:
    found = shutil.which("pdftoppm")
    if found:
        return Path(found)
    candidate = Path("/opt/homebrew/bin/pdftoppm")
    return candidate if candidate.is_file() else None


def _make_synthetic_pdf(path: Path) -> None:
    pdf = canvas.Canvas(str(path), pagesize=(600, 800))
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(72, 740, "Figure 1.1")
    pdf.rect(100, 500, 180, 130, stroke=1, fill=0)
    pdf.line(180, 560, 250, 563)
    pdf.drawString(330, 740, "Figure 1.2")
    pdf.rect(350, 500, 180, 130, stroke=1, fill=0)
    pdf.setFont("Helvetica", 10)
    pdf.drawString(72, 470, "Table 1.1")
    pdf.drawString(80, 440, "Parameter Units Value")
    pdf.drawString(80, 420, "temperature K 10")
    pdf.drawString(80, 400, "density m-3 20")
    pdf.drawString(72, 350, "x = 2 + y (1.3)")
    pdf.drawString(72, 300, "plot")
    pdf.drawString(72, 280, "x axis")
    pdf.drawString(72, 260, "legend")
    pdf.drawString(72, 240, "series")
    pdf.drawString(72, 220, "series 12.5 kW")
    pdf.showPage()
    pdf.setFont("Helvetica", 10)
    pdf.drawString(72, 740, "See Table 1.1 and Table 1.2 in the text; no table is present here.")
    pdf.showPage()
    pdf.save()


def _make_xobject_pdf(path: Path) -> None:
    image = BytesIO()
    Image.new("RGB", (8, 6), (40, 120, 200)).save(image, format="PNG")
    pdf = canvas.Canvas(str(path), pagesize=(600, 800))
    pdf.setFont("Helvetica", 12)
    pdf.drawString(72, 430, "Figure 9.1")
    pdf.drawImage(ImageReader(BytesIO(image.getvalue())), 220, 300, width=210, height=150, mask="auto")
    pdf.showPage()
    pdf.save()


def _make_nested_xobject_pdf(path: Path) -> None:
    image = BytesIO()
    Image.new("RGB", (8, 6), (200, 80, 40)).save(image, format="PNG")
    pdf = canvas.Canvas(str(path), pagesize=(600, 800))
    pdf.setFont("Helvetica", 12)
    pdf.drawString(72, 260, "Figure 9.2")
    pdf.beginForm("nested_panel", 0, 0, 210, 150)
    pdf.drawImage(ImageReader(BytesIO(image.getvalue())), 0, 0, width=210, height=150, mask="auto")
    pdf.endForm()
    pdf.saveState()
    pdf.translate(220, 360)
    pdf.doForm("nested_panel")
    pdf.restoreState()
    pdf.showPage()
    pdf.save()


def _make_two_panel_xobject_pdf(path: Path) -> None:
    image_a = BytesIO()
    image_b = BytesIO()
    Image.new("RGB", (8, 6), (40, 120, 200)).save(image_a, format="PNG")
    Image.new("RGB", (8, 6), (200, 120, 40)).save(image_b, format="PNG")
    pdf = canvas.Canvas(str(path), pagesize=(600, 800))
    pdf.setFont("Helvetica", 12)
    pdf.drawString(72, 260, "Figure 9.3")
    pdf.drawImage(ImageReader(BytesIO(image_a.getvalue())), 100, 320, width=150, height=120, mask="auto")
    pdf.drawImage(ImageReader(BytesIO(image_b.getvalue())), 330, 320, width=150, height=120, mask="auto")
    pdf.showPage()
    pdf.save()


class VisualSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "synthetic.pdf"
        _make_synthetic_pdf(self.source)
        self.render = _pdftoppm()
        if self.render is None:
            self.skipTest("pdftoppm is required for visual semantic fixtures")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _bundle(self) -> dict[str, object]:
        return extract_visual_candidates(self.source, pages=[1, 2], pdftoppm=self.render)

    def _attestation_for_objects(
        self,
        bundle: dict[str, object],
        object_ids: list[str],
        *,
        reviewer: str = "reviewer-a",
        proposer: str = "proposal-agent",
        session: str = "review-session",
        plan_hash: str = "d" * 64,
        cell_ids: list[str] | None = None,
        series_ids: list[str] | None = None,
        relation_ids: list[str] | None = None,
        relation_hashes: list[str] | None = None,
        grid_ids: list[str] | None = None,
        grid_hashes: list[str] | None = None,
    ) -> dict[str, object]:
        by_id = {row["visual_object_id"]: row for row in bundle["objects"]}
        bound = [by_id[identifier] for identifier in object_ids]
        payload = {
            "review_plan_sha256": plan_hash,
            "source_sha256": bundle["source_sha256"],
            "visual_object_ids": sorted(object_ids),
            "table_cell_ids": sorted(cell_ids or []),
            "series_ids": sorted(series_ids or []),
            "full_page_sha256": bound[0]["canonical_render_sha256"],
            "crop_sha256s": sorted(row["crop_sha256"] for row in bound if row.get("crop_sha256")),
            "bbox_sha256s": sorted(sha256_json(row["bbox"]) for row in bound),
            "render_sha256": bound[0]["canonical_render_sha256"],
            "render_dpi": bound[0]["canonical_render_dpi"],
            "render_rotation": bound[0]["canonical_render_rotation"],
        }
        if relation_ids:
            payload["visual_relation_ids"] = sorted(relation_ids)
            payload["relation_sha256s"] = sorted(relation_hashes or [])
        if grid_ids:
            payload["table_grid_ids"] = sorted(grid_ids)
            payload["table_grid_sha256s"] = sorted(grid_hashes or [])
        return build_visual_review_attestation(
            reviewer_instance=reviewer,
            proposer_instance=proposer,
            review_session_id=session,
            review_plan_sha256=plan_hash,
            source_sha256=bundle["source_sha256"],
            visual_object_ids=object_ids,
            table_cell_ids=cell_ids or [],
            series_ids=series_ids or [],
            full_page_sha256=bound[0]["canonical_render_sha256"],
            crop_sha256s=payload["crop_sha256s"],
            render_sha256=bound[0]["canonical_render_sha256"],
            render_dpi=bound[0]["canonical_render_dpi"],
            render_rotation=bound[0]["canonical_render_rotation"],
            bbox_sha256s=payload["bbox_sha256s"],
            input_sha256=sha256_json(payload),
            verdict="accepted",
            differences=[
                {
                    "visual_object_id": identifier,
                    "status": "confirmed",
                    "observation": f"Independent inspection of {identifier}.",
                }
                for identifier in object_ids
            ],
            rationale=f"Independent page and crop inspection by {reviewer}.",
            confidence={"extraction": 0.9, "interpretation": 0.8},
            reviewer_independence={"independent": True, "basis": "separate review session"},
            visual_relation_ids=relation_ids or [],
            relation_sha256s=relation_hashes or [],
            table_grid_ids=grid_ids or [],
            table_grid_sha256s=grid_hashes or [],
        )

    def test_synthetic_objects_grid_equation_plot_and_reference_only_page(self) -> None:
        bundle = self._bundle()
        objects = bundle["objects"]
        self.assertGreaterEqual(sum(row["type"] == "figure" for row in objects), 2)
        figures = [row for row in objects if row["type"] == "figure"]
        self.assertNotEqual(figures[0]["bbox"], figures[1]["bbox"])
        self.assertGreaterEqual(sum(row["type"] == "caption" for row in objects), 2)
        self.assertGreaterEqual(sum(row["type"] == "table" for row in objects), 1)
        self.assertGreaterEqual(len(bundle["table_grids"]), 1)
        self.assertGreaterEqual(len(bundle["table_grids"][0]["cells"]), 3)
        self.assertGreaterEqual(sum(row["type"] == "equation_display" for row in objects), 1)
        self.assertGreaterEqual(sum(row["type"] == "plot" for row in objects), 1)
        self.assertGreaterEqual(sum(row["type"] == "axis" for row in objects), 1)
        self.assertGreaterEqual(sum(row["type"] == "legend" for row in objects), 1)
        self.assertGreaterEqual(sum(row["type"] == "series" for row in objects), 1)
        self.assertTrue(any(row["type"] == "annotation" and row["subtype"] == "arrow" for row in objects))
        digitized = [row for row in objects if row["type"] == "annotation" and row["subtype"] == "digitized_value"]
        self.assertTrue(digitized)
        self.assertEqual(digitized[0]["attributes"]["error_model"]["kind"], "axis-resolution-candidate")
        self.assertFalse(any(row["physical_page"] == 2 and row["type"] == "table" for row in objects))
        self.assertEqual(validate_visual_bundle(bundle), [])
        for row in objects:
            self.assertEqual(row["status"], "candidate")
            self.assertTrue(row["source_sha256"] and row["canonical_render_sha256"])
            self.assertTrue(row["parser_receipt"] and row["backend_receipt"] and row["model_receipt"] and row["config_receipt"])

    def test_plot_marker_requires_concise_explicit_or_two_role_evidence(self) -> None:
        prose = [
            {"text": "Bars were tested with the tensile axis normal to the powder bed while the remainder of the sentence describes processing.", "bbox": [72, 600, 520, 612]},
            {"text": "rotation axis", "bbox": [72, 580, 140, 592]},
        ]
        self.assertEqual(_concise_plot_marker_rows(prose), [])

        labels = [
            {"text": "x axis", "bbox": [72, 300, 110, 312]},
            {"text": "legend", "bbox": [72, 280, 115, 292]},
        ]
        self.assertEqual(_concise_plot_marker_rows(labels), labels)

        explicit = [{"text": "plot", "bbox": [72, 300, 95, 312]}]
        self.assertEqual(_concise_plot_marker_rows(explicit), explicit)

    def test_prose_axis_with_drawing_does_not_create_plot_candidate(self) -> None:
        source = self.root / "prose-axis.pdf"
        pdf = canvas.Canvas(str(source), pagesize=(600, 800))
        pdf.setFont("Helvetica", 10)
        pdf.drawString(72, 700, "Bars were tested with the tensile axis normal to the powder bed and then sintered.")
        pdf.rect(70, 120, 460, 620, stroke=1, fill=0)
        pdf.showPage()
        pdf.save()

        bundle = extract_visual_candidates(source, pages=[1], pdftoppm=self.render)
        self.assertFalse(any(row["type"] == "plot" for row in bundle["objects"]))
        self.assertTrue(any(row["details"].get("reason") == "prose-only-or-insufficient-chart-label-evidence" for row in bundle["conflicts"]))

    def test_split_cjk_label_fragments_recombine_with_provenance_and_reject_prose_reference(self) -> None:
        fragments = [
            {"text": "图", "bbox": [100, 100, 110, 112], "font_size": 10.0, "fragment_index": 0},
            {"text": "2", "bbox": [110, 100, 116, 112], "font_size": 10.0, "fragment_index": 1},
            {"text": "-", "bbox": [116, 100, 121, 112], "font_size": 10.0, "fragment_index": 2},
            {"text": "2", "bbox": [121, 100, 127, 112], "font_size": 10.0, "fragment_index": 3},
            {"text": "-", "bbox": [127, 100, 132, 112], "font_size": 10.0, "fragment_index": 4},
            {"text": "1", "bbox": [132, 100, 138, 112], "font_size": 10.0, "fragment_index": 5},
        ]
        merged = _recombine_text_boxes(fragments)
        labels = _find_label_boxes(merged, FIGURE_RE, line_start_only=True)
        self.assertEqual([row[0] for row in labels], ["2-2-1"])
        self.assertEqual(labels[0][1], [100.0, 100.0, 138.0, 112.0])
        provenance = labels[0][3]
        self.assertEqual(provenance["method"], "pypdf-visitor-text-recombine-v0.1")
        self.assertEqual(len(provenance["fragment_hashes"]), 6)
        self.assertEqual(len(provenance["fragments"]), 6)

        split_line = _recombine_text_boxes(
            [
                {"text": "表", "bbox": [200, 150, 210, 162], "font_size": 10.0, "fragment_index": 0},
                {"text": "2-2-1", "bbox": [200, 164, 235, 176], "font_size": 10.0, "fragment_index": 1},
            ]
        )
        table_labels = _find_label_boxes(split_line, TABLE_RE, line_start_only=True)
        self.assertEqual([row[0] for row in table_labels], ["2-2-1"])
        self.assertEqual(table_labels[0][1], [200.0, 150.0, 235.0, 176.0])

        prose = _recombine_text_boxes(
            [
                {"text": "如图", "bbox": [20, 200, 40, 212], "font_size": 10.0, "fragment_index": 0},
                {"text": "2-2-1", "bbox": [40, 200, 75, 212], "font_size": 10.0, "fragment_index": 1},
            ]
        )
        self.assertEqual(_find_label_boxes(prose, FIGURE_RE, line_start_only=True), [])

    def test_native_xobject_do_geometry_is_hash_bound_and_caption_adjacent(self) -> None:
        source = self.root / "xobject.pdf"
        _make_xobject_pdf(source)
        bundle = extract_visual_candidates(source, pages=[1], pdftoppm=self.render, render_dpi=120)
        figure = next(row for row in bundle["objects"] if row["type"] == "figure")
        provenance = figure["attributes"]["geometry_provenance"]
        self.assertEqual(provenance["geometry_evidence"], "pdf-xobject-image-do-v0.1")
        self.assertTrue(provenance["xobject_stream_sha256"])
        self.assertTrue(provenance["xobject_object_sha256"])
        self.assertEqual(provenance["xobject_path"][-1].startswith("/"), True)
        self.assertGreater((figure["bbox"][2] - figure["bbox"][0]) * (figure["bbox"][3] - figure["bbox"][1]), 1000.0)
        self.assertEqual(validate_visual_bundle(bundle), [])

        selected = _caption_adjacent_xobject(
            [100.0, 600.0, 220.0, 620.0],
            [
                {"kind": "image", "bbox": [0.0, 0.0, 600.0, 800.0], "xobject_provenance": {"xobject_object_sha256": "a" * 64}},
                {"kind": "image", "bbox": [120.0, 350.0, 420.0, 570.0], "xobject_provenance": {"xobject_object_sha256": "b" * 64}},
            ],
            600.0,
            800.0,
            used=set(),
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["bbox"], [120.0, 350.0, 420.0, 570.0])

    def test_nested_form_image_is_one_placement_not_a_panel_and_self_loops_fail(self) -> None:
        source = self.root / "nested-xobject.pdf"
        _make_nested_xobject_pdf(source)
        bundle = extract_visual_candidates(source, pages=[1], pdftoppm=self.render, render_dpi=120)
        figures = [row for row in bundle["objects"] if row["type"] == "figure"]
        self.assertEqual(len(figures), 1)
        self.assertEqual(figures[0]["subtype"], "figure")
        self.assertIsNone(figures[0]["parent"])
        self.assertFalse(any(row["subtype"] == "panel" for row in figures))
        self.assertFalse(any(row["source_visual_object_id"] == row["target_visual_object_id"] for row in bundle["relations"]))
        self.assertEqual(validate_visual_bundle(bundle), [])

        invalid = copy.deepcopy(bundle)
        invalid["objects"][0]["parent"] = invalid["objects"][0]["visual_object_id"]
        invalid["objects"][0]["object_sha256"] = _object_hash(invalid["objects"][0])
        self.assertIn("visual_parent_self_loop", {issue.code for issue in validate_visual_bundle(invalid)})
        invalid_relation = copy.deepcopy(bundle)
        identifier = invalid_relation["objects"][0]["visual_object_id"]
        invalid_relation["relations"].append({
            "schema_version": "tkc.visual-relation/v0.1",
            "visual_relation_id": "vr-" + "a" * 20,
            "relation_type": "contains",
            "source_visual_object_id": identifier,
            "target_visual_object_id": identifier,
            "source_sha256": invalid_relation["source_sha256"],
            "physical_page": 1,
            "evidence_hashes": [],
            "status": "candidate",
            "relation_sha256": sha256_json({
                "schema_version": "tkc.visual-relation/v0.1",
                "visual_relation_id": "vr-" + "a" * 20,
                "relation_type": "contains",
                "source_visual_object_id": identifier,
                "target_visual_object_id": identifier,
                "source_sha256": invalid_relation["source_sha256"],
                "physical_page": 1,
                "evidence_hashes": [],
                "status": "candidate",
            }),
        })
        self.assertIn("visual_relation_self_loop", {issue.code for issue in validate_visual_bundle(invalid_relation)})
        invalid_geometry = copy.deepcopy(bundle)
        invalid_figure = next(row for row in invalid_geometry["objects"] if row["type"] == "figure" and row["subtype"] == "figure")
        invalid_caption = next(row for row in invalid_geometry["objects"] if row["type"] == "caption")
        invalid_figure["bbox"] = list(invalid_caption["bbox"])
        invalid_figure["attributes"] = {**invalid_figure["attributes"], "geometry_provenance": {"geometry_evidence": "caption-only-xobject-gap", "candidate_only": True}}
        self.assertIn("visual_unlocalized_bbox_forbidden", {issue.code for issue in validate_visual_bundle(invalid_geometry)})

    def test_two_independent_xobjects_emit_candidate_panel_children(self) -> None:
        source = self.root / "two-panel-xobject.pdf"
        _make_two_panel_xobject_pdf(source)
        bundle = extract_visual_candidates(source, pages=[1], pdftoppm=self.render, render_dpi=120)
        figures = [row for row in bundle["objects"] if row["type"] == "figure"]
        parents = [row for row in figures if row["subtype"] == "figure"]
        panels = [row for row in figures if row["subtype"] == "panel"]
        self.assertEqual(len(parents), 1)
        self.assertEqual(len(panels), 2)
        self.assertTrue(all(row["parent"] == parents[0]["visual_object_id"] for row in panels))
        self.assertTrue(all(row["visual_object_id"] != row["parent"] for row in panels))
        self.assertFalse(any(row["source_visual_object_id"] == row["target_visual_object_id"] for row in bundle["relations"]))
        self.assertEqual(validate_visual_bundle(bundle), [])

    def test_visual_review_overlay_is_strict_hash_bound_and_not_an_attestation(self) -> None:
        bundle = self._bundle()
        overlay = visual_review_overlay_manifest(source_sha256=bundle["source_sha256"], objects=bundle["objects"], grids=bundle["table_grids"], relations=bundle["relations"])
        self.assertEqual(validate_visual_review_overlay_manifest(overlay, source_sha256=bundle["source_sha256"], objects=bundle["objects"], grids=bundle["table_grids"], relations=bundle["relations"]), [])
        tampered = copy.deepcopy(overlay)
        tampered["overlay_sha256"] = "0" * 64
        self.assertIn("visual_review_overlay_hash_mismatch", {issue.code for issue in validate_visual_review_overlay_manifest(tampered, source_sha256=bundle["source_sha256"], objects=bundle["objects"], grids=bundle["table_grids"], relations=bundle["relations"])})
        unknown = copy.deepcopy(overlay)
        unknown["reviewer_attestation"] = {"verdict": "accepted"}
        self.assertIn("visual_review_overlay_unknown_field", {issue.code for issue in validate_visual_review_overlay_manifest(unknown, source_sha256=bundle["source_sha256"], objects=bundle["objects"], grids=bundle["table_grids"], relations=bundle["relations"])})
        changed = copy.deepcopy(bundle["objects"])
        changed_index = next(index for index, row in enumerate(changed) if row.get("type") not in {"table_cell", "series"})
        changed[changed_index]["object_sha256"] = "f" * 64
        self.assertIn("visual_review_overlay_replay_mismatch", {issue.code for issue in validate_visual_review_overlay_manifest(overlay, source_sha256=bundle["source_sha256"], objects=changed, grids=bundle["table_grids"], relations=bundle["relations"])})

    def test_cross_page_table_continuation_is_review_candidate_not_auto_merge(self) -> None:
        source_hash = "a" * 64
        render_hash = "b" * 64
        first = build_visual_object(source_sha256=source_hash, physical_page=10, object_type="table", subtype="table", page_width=600, page_height=800, bbox=[80, 200, 500, 500], render_sha256=render_hash, render_dpi=120, label="Table 2.1")
        second = build_visual_object(source_sha256=source_hash, physical_page=11, object_type="table", subtype="table", page_width=600, page_height=800, bbox=[80, 180, 500, 480], render_sha256=render_hash, render_dpi=120, label="Table 2.1 continued")
        rows = build_table_continuation_candidates([first, second], [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "candidate")
        self.assertTrue(rows[0]["requires_external_review"])
        self.assertFalse(rows[0]["auto_merge"])

    def _require_local_pdf(self, variable: str) -> Path:
        value = os.environ.get(variable)
        if not value:
            self.skipTest(f"set {variable} for read-only real-PDF regression")
        source = Path(value).resolve()
        self.assertTrue(source.is_file(), f"configured PDF is unavailable: {variable}")
        return source

    def test_real_chinese_visual_labels_remain_candidate_only(self) -> None:
        source = self._require_local_pdf("FAKE_EXPERT_TEST_CHINESE_VISUAL_PDF")
        render = _pdftoppm()
        if render is None:
            self.skipTest("pdftoppm is required for the bounded Wang regression")
        bundles = {page: extract_visual_candidates(source, pages=[page], pdftoppm=render, render_dpi=120) for page in (36, 37, 40)}
        page36_types = {row["type"] for row in bundles[36]["objects"]}
        self.assertTrue({"figure", "table", "caption"} <= page36_types)
        expected_figures = {
            36: {"Figure 2-2-2"},
            37: {"Figure 2-2-3", "Figure 2-2-4", "Figure 2-2-5"},
            40: {"Figure 2-3-5", "Figure 2-3-6"},
        }
        expected_tables = {
            36: {"Table 2-2-1"},
            37: {"Table 2-2-2"},
            40: {"Table 2-3-1"},
        }
        aggregate_types = {row["type"] for bundle in bundles.values() for row in bundle["objects"]}
        self.assertTrue({"figure", "table", "caption"} <= aggregate_types)
        for page, bundle in bundles.items():
            self.assertEqual(
                {row["label"] for row in bundle["objects"] if row["type"] == "figure" and row["subtype"] == "figure"},
                expected_figures[page],
                page,
            )
            self.assertEqual(
                {row["label"] for row in bundle["objects"] if row["type"] == "table"},
                expected_tables[page],
                page,
            )
            self.assertTrue(any(row["type"] == "figure" for row in bundle["objects"]), page)
            self.assertTrue(any(row["type"] == "caption" for row in bundle["objects"]), page)
            self.assertTrue(all(row["status"] == "candidate" for row in bundle["objects"]), page)
            self.assertFalse(any(row["status"] == "promoted" for row in bundle["objects"]), page)
            self.assertEqual(validate_visual_bundle(bundle), [], page)
        self.assertFalse(any(row.get("label") == "Figure 2-3-4" for row in bundles[40]["objects"]))
        self.assertFalse(any(row.get("details", {}).get("label") == "Figure 2-3-4" and row.get("details", {}).get("reason") == "caption_not_adjacent_to_xobject" for row in bundles[40]["conflicts"]))
        self.assertTrue(bundles[36]["table_grids"])
        self.assertGreaterEqual(len(bundles[36]["table_grids"][0]["cells"]), 2)
        self.assertEqual([row for row in bundles[36]["objects"] if row["type"] == "figure" and row["subtype"] == "panel"], [])
        self.assertFalse(any(row["source_visual_object_id"] == row["target_visual_object_id"] for row in bundles[36]["relations"]))

        from pypdf import PdfReader

        reader = PdfReader(str(source))
        page = reader.pages[35]
        xobjects = _page_xobject_candidates(page, float(page.mediabox.width), float(page.mediabox.height), reader)
        image_candidate = next(row for row in xobjects if row["kind"] == "image")
        used_xobjects: set[str] = set()
        selected_xobject = _caption_adjacent_xobject([125.9, 367.946, 235.226, 380.618], xobjects, float(page.mediabox.width), float(page.mediabox.height), used=used_xobjects)
        self.assertIsNotNone(selected_xobject)
        _consume_xobject_candidate(selected_xobject, xobjects, used_xobjects)
        self.assertIsNone(_caption_adjacent_xobject([125.9, 367.946, 235.226, 380.618], xobjects, float(page.mediabox.width), float(page.mediabox.height), used=used_xobjects))
        figure = next(row for row in bundles[36]["objects"] if row["type"] == "figure" and row["label"] == "Figure 2-2-2")
        expected = image_candidate["bbox"]
        intersection = [
            max(figure["bbox"][0], expected[0]),
            max(figure["bbox"][1], expected[1]),
            min(figure["bbox"][2], expected[2]),
            min(figure["bbox"][3], expected[3]),
        ]
        intersection_area = max(0.0, intersection[2] - intersection[0]) * max(0.0, intersection[3] - intersection[1])
        figure_area = (figure["bbox"][2] - figure["bbox"][0]) * (figure["bbox"][3] - figure["bbox"][1])
        expected_area = (expected[2] - expected[0]) * (expected[3] - expected[1])
        self.assertGreaterEqual(intersection_area / (figure_area + expected_area - intersection_area), 0.95)
        self.assertGreater(figure_area, 10000.0)
        self.assertEqual(figure["attributes"]["geometry_provenance"]["xobject_path"], ["/Meta52", "/Image53"])
        mutated = copy.deepcopy(figure)
        mutated["attributes"]["geometry_provenance"]["do_ctm"][0] += 0.001
        mutated["input_sha256"] = visual_input_sha256(
            source_sha256=mutated["source_sha256"],
            physical_page=mutated["physical_page"],
            render_sha256=mutated["canonical_render_sha256"],
            render_dpi=mutated["canonical_render_dpi"],
            rotation=mutated["canonical_render_rotation"],
            coordinate_space=mutated["coordinate_space"],
            bbox=mutated["bbox"],
            crop_sha256=mutated["crop_sha256"],
            parser_receipt=mutated["parser_receipt"],
            backend_receipt=mutated["backend_receipt"],
            model_receipt=mutated["model_receipt"],
            config_receipt=mutated["config_receipt"],
            page_geometry=mutated["page_geometry"],
            attributes=mutated["attributes"],
            uncertainty=mutated["uncertainty"],
        )
        mutated["object_sha256"] = _object_hash(mutated)
        self.assertNotEqual(mutated["input_sha256"], figure["input_sha256"])
        self.assertNotEqual(mutated["object_sha256"], figure["object_sha256"])
        table = next(row for row in bundles[36]["objects"] if row["type"] == "table" and row["label"] == "Table 2-2-1")
        self.assertEqual(table["attributes"]["geometry_provenance"]["geometry_evidence"], "pdf-ruling-line-cluster-v0.1")
        self.assertLessEqual(table["bbox"][1], 556.0)
        self.assertGreaterEqual(table["bbox"][3], 733.0)
        self.assertLess(table["bbox"][3], 760.0)
        self.assertNotIn("page-number", json.dumps(table["attributes"], ensure_ascii=False))

    def test_deterministic_bundle_bytes_and_duplicate_conflict(self) -> None:
        first = self._bundle()
        second = self._bundle()
        self.assertEqual(canonical_json(first), canonical_json(second))
        duplicate = copy.deepcopy(first["objects"][0])
        duplicate["visual_object_id"] = "vo-" + "f" * 20
        objects, relations, grids, conflicts = deduplicate_visual_candidates(
            [first["objects"][0], duplicate], [], [], []
        )
        self.assertEqual(len(objects), 1)
        self.assertTrue(any(row["conflict_type"] == "duplicate" for row in conflicts))
        self.assertEqual(validate_visual_bundle({"source_sha256": first["source_sha256"], "objects": objects, "relations": relations, "table_grids": grids, "conflicts": conflicts}), [])

    def test_coordinate_rotation_crop_render_and_bbox_tamper_fail_closed(self) -> None:
        self.assertEqual(
            normalize_bbox([0, 0, 50, 100], 100, 50, "render-pixels-top-left-v1", 90, 72),
            [0.0, 0.0, 100.0, 50.0],
        )
        bundle = self._bundle()
        for field, code in (("bbox", "bbox_page_mismatch"), ("canonical_render_sha256", "render_hash_mismatch"), ("crop_sha256", "crop_hash_mismatch")):
            tampered = copy.deepcopy(bundle)
            row = tampered["objects"][0]
            if field == "bbox":
                row[field] = [-1, 0, 3, 4]
            else:
                row[field] = "0" * 64
            self.assertIn(code, {issue.code for issue in validate_visual_bundle(tampered)})
        promoted = copy.deepcopy(bundle)
        promoted["objects"][0]["status"] = "promoted"
        self.assertIn("visual_candidate_promotion_forbidden", {issue.code for issue in validate_visual_bundle(promoted)})

    def test_nonzero_cropbox_rotation_and_closed_geometry_cell_contract(self) -> None:
        from pypdf import PdfReader, PdfWriter

        base = self.root / "geometry-base.pdf"
        document = canvas.Canvas(str(base), pagesize=(600, 800))
        document.setFont("Helvetica-Bold", 14)
        document.drawString(72, 740, "Figure 1.1")
        document.rect(100, 500, 180, 130, stroke=1, fill=0)
        document.showPage()
        document.save()
        reader = PdfReader(str(base))
        page = reader.pages[0]
        page.cropbox.lower_left = (20, 30)
        page.cropbox.upper_right = (580, 770)
        page.rotate(90)
        writer = PdfWriter()
        writer.add_page(page)
        rotated = self.root / "geometry-rotated.pdf"
        with rotated.open("wb") as handle:
            writer.write(handle)
        bundle = extract_visual_candidates(rotated, pages=[1], pdftoppm=self.render)
        self.assertTrue(bundle["objects"])
        for row in bundle["objects"]:
            geometry = row["page_geometry"]
            self.assertEqual(geometry["coordinate_origin"], "mediabox-top-left")
            self.assertEqual(geometry["rotation"], 90)
            self.assertEqual(geometry["crop_box"], [20.0, 30.0, 580.0, 770.0])
            self.assertEqual(geometry["physical_page"], 1)
        self.assertEqual(validate_visual_bundle(bundle), [])

        missing_geometry = copy.deepcopy(bundle)
        missing_geometry["objects"][0].pop("page_geometry")
        self.assertIn("visual_provenance_incomplete", {issue.code for issue in validate_visual_bundle(missing_geometry)})
        unknown_geometry = copy.deepcopy(bundle)
        unknown_geometry["objects"][0]["page_geometry"]["unexpected"] = True
        self.assertIn("visual_page_geometry_invalid", {issue.code for issue in validate_visual_bundle(unknown_geometry)})
        unknown_uncertainty = copy.deepcopy(bundle)
        unknown_uncertainty["objects"][0]["uncertainty"]["unexpected"] = True
        self.assertIn("visual_uncertainty_invalid", {issue.code for issue in validate_visual_bundle(unknown_uncertainty)})

    def test_offset_mediabox_is_translated_to_canonical_top_left_geometry(self) -> None:
        from pypdf import PdfReader, PdfWriter

        base = self.root / "offset-mediabox-base.pdf"
        document = canvas.Canvas(str(base), pagesize=(600, 800))
        document.setFont("Helvetica-Bold", 14)
        document.drawString(72, 740, "Figure 4.2")
        document.rect(100, 500, 180, 130, stroke=1, fill=0)
        document.showPage()
        document.save()
        reader = PdfReader(str(base))
        page = reader.pages[0]
        page.mediabox.lower_left = (-10, -20)
        page.mediabox.upper_right = (590, 780)
        page.cropbox.lower_left = (20, 30)
        page.cropbox.upper_right = (580, 770)
        writer = PdfWriter()
        writer.add_page(page)
        source = self.root / "offset-mediabox.pdf"
        with source.open("wb") as handle:
            writer.write(handle)

        bundle = extract_visual_candidates(source, pages=[1], pdftoppm=self.render, render_dpi=120)
        self.assertTrue(bundle["objects"])
        self.assertEqual(validate_visual_bundle(bundle), [])
        for row in bundle["objects"]:
            self.assertEqual(row["page_geometry"]["media_box"], [-10.0, -20.0, 590.0, 780.0])
            self.assertEqual(row["page_geometry"]["crop_box"], [20.0, 30.0, 580.0, 770.0])
            self.assertGreaterEqual(row["bbox"][0], 0.0)
            self.assertGreaterEqual(row["bbox"][1], 0.0)
            self.assertLessEqual(row["bbox"][2], 600.0)
            self.assertLessEqual(row["bbox"][3], 800.0)

    def test_outside_caption_keeps_bounded_xobject_figure_candidate(self) -> None:
        from pypdf import PdfReader, PdfWriter

        image = BytesIO()
        Image.new("RGB", (8, 6), (80, 120, 160)).save(image, format="PNG")
        base = self.root / "outside-caption-base.pdf"
        document = canvas.Canvas(str(base), pagesize=(600, 800))
        document.setFont("Helvetica-Bold", 14)
        document.drawString(72, 740, "Figure 4.3")
        document.drawImage(ImageReader(BytesIO(image.getvalue())), 180, 300, width=240, height=180, mask="auto")
        document.showPage()
        document.save()
        reader = PdfReader(str(base))
        page = reader.pages[0]
        page.mediabox.lower_left = (-10, -100)
        page.mediabox.upper_right = (590, 700)
        writer = PdfWriter()
        writer.add_page(page)
        source = self.root / "outside-caption.pdf"
        with source.open("wb") as handle:
            writer.write(handle)

        bundle = extract_visual_candidates(source, pages=[1], pdftoppm=self.render, render_dpi=120)
        figures = [row for row in bundle["objects"] if row["type"] == "figure"]
        self.assertEqual(len(figures), 1)
        self.assertEqual(figures[0]["label"], "Figure 4.3")
        self.assertEqual(figures[0]["attributes"]["geometry_provenance"]["caption_geometry_status"], "outside-page-review-required")
        self.assertFalse(any(row["type"] == "caption" for row in bundle["objects"]))
        self.assertTrue(any(row["details"].get("reason") == "caption_bbox_outside_page" for row in bundle["conflicts"]))
        self.assertEqual(validate_visual_bundle(bundle), [])

        table_bundle = self._bundle()
        grid = next(row for row in table_bundle["table_grids"] if row["cells"])
        bad_anchor = copy.deepcopy(table_bundle)
        bad_grid = next(row for row in bad_anchor["table_grids"] if row["table_grid_id"] == grid["table_grid_id"])
        bad_grid["cells"][0]["source_anchor"]["source_sha256"] = "0" * 64
        bad_grid["cells"][0]["typed_candidates"]["unexpected"] = True
        codes = {issue.code for issue in validate_visual_bundle(bad_anchor)}
        self.assertIn("table_cell_source_anchor_mismatch", codes)
        self.assertIn("table_cell_typed_candidates_invalid", codes)

    def test_external_review_hash_and_reviewer_tamper(self) -> None:
        bundle = self._bundle()
        obj = next(row for row in bundle["objects"] if row["type"] == "plot")
        source_hash = bundle["source_sha256"]
        plan_hash = "d" * 64
        input_payload = {
            "review_plan_sha256": plan_hash,
            "source_sha256": source_hash,
            "visual_object_ids": [obj["visual_object_id"]],
            "table_cell_ids": [],
            "series_ids": [],
            "full_page_sha256": obj["canonical_render_sha256"],
            "crop_sha256s": [obj["crop_sha256"]],
            "bbox_sha256s": [sha256_json(obj["bbox"])],
            "render_sha256": obj["canonical_render_sha256"],
            "render_dpi": obj["canonical_render_dpi"],
            "render_rotation": obj["canonical_render_rotation"],
        }
        attestation = build_visual_review_attestation(
            reviewer_instance="reviewer-a",
            proposer_instance="proposal-agent",
            review_session_id="rws-test",
            review_plan_sha256=plan_hash,
            source_sha256=source_hash,
            visual_object_ids=[obj["visual_object_id"]],
            full_page_sha256=obj["canonical_render_sha256"],
            crop_sha256s=[obj["crop_sha256"]],
            render_sha256=obj["canonical_render_sha256"],
            render_dpi=obj["canonical_render_dpi"],
            render_rotation=obj["canonical_render_rotation"],
            bbox_sha256s=[sha256_json(obj["bbox"])],
            input_sha256=sha256_json(input_payload),
            verdict="accepted",
            differences=[{"visual_object_id": obj["visual_object_id"], "status": "confirmed", "observation": "independently checked plot geometry"}],
            rationale="Independent full-page and crop inspection.",
            confidence={"extraction": 0.9, "interpretation": 0.9},
            reviewer_independence={"independent": True, "basis": "separate instance"},
        )
        self.assertEqual(validate_visual_review_attestations([attestation], [obj], review_plan_sha256=plan_hash, source_sha256=source_hash, proposer_instances=["proposal-agent"]), [])
        for field, code in (("bbox_sha256s", "bbox_hash_mismatch"), ("crop_sha256s", "crop_hash_mismatch"), ("render_sha256", "visual_render_hash_mismatch"), ("reviewer_instance", "visual_reviewer_not_independent")):
            tampered = copy.deepcopy(attestation)
            if field == "reviewer_instance":
                tampered[field] = "proposal-agent"
            elif field == "render_sha256":
                tampered[field] = "0" * 64
            else:
                tampered[field] = ["0" * 64]
            self.assertIn(code, {issue.code for issue in validate_visual_review_attestations([tampered], [obj], review_plan_sha256=plan_hash, source_sha256=source_hash, proposer_instances=["proposal-agent"])})

    def test_visual_review_p0_coverage_session_confidence_and_difference_fail_closed(self) -> None:
        """Every frozen object must be covered by strict v0.2 review records."""

        bundle = self._bundle()
        figures = [row for row in bundle["objects"] if row["type"] == "figure" and row["physical_page"] == 1]
        self.assertGreaterEqual(len(figures), 2)
        first_id, second_id = figures[0]["visual_object_id"], figures[1]["visual_object_id"]
        attestation = self._attestation_for_objects(bundle, [first_id])
        tampered = copy.deepcopy(attestation)
        tampered["review_session_id"] = "wrong-session"
        tampered["confidence"] = {"extraction": 9, "interpretation": 9}
        tampered["differences"][0].pop("status")
        issues = validate_visual_review_attestations(
            [tampered],
            bundle["objects"],
            review_plan_sha256="d" * 64,
            source_sha256=bundle["source_sha256"],
            proposer_instances=["proposal-agent"],
            required_reviewers={first_id: 1, second_id: 1},
            expected_review_session_id="review-session",
            registered_reviewers=["reviewer-a"],
        )
        codes = {issue.code for issue in issues}
        self.assertIn("visual_review_session_mismatch", codes)
        self.assertIn("visual_review_confidence_invalid", codes)
        self.assertIn("visual_review_difference_invalid", codes)
        self.assertIn("visual_reviewer_coverage_insufficient", codes)

        unregistered = copy.deepcopy(attestation)
        unregistered["reviewer_instance"] = "rogue-reviewer"
        unregistered_codes = {
            issue.code
            for issue in validate_visual_review_attestations(
                [unregistered],
                bundle["objects"],
                review_plan_sha256="d" * 64,
                source_sha256=bundle["source_sha256"],
                proposer_instances=["proposal-agent"],
                required_reviewers={first_id: 1},
                expected_review_session_id="review-session",
                registered_reviewers=["reviewer-a"],
            )
        }
        self.assertIn("visual_reviewer_unregistered", unregistered_codes)
        replay_codes = {
            issue.code
            for issue in validate_visual_review_attestations(
                [attestation, copy.deepcopy(attestation)],
                bundle["objects"],
                review_plan_sha256="d" * 64,
                source_sha256=bundle["source_sha256"],
                proposer_instances=["proposal-agent"],
                required_reviewers={first_id: 1},
                expected_review_session_id="review-session",
                registered_reviewers=["reviewer-a"],
            )
        }
        self.assertIn("visual_review_attestation_replay", replay_codes)

    def test_visual_review_antiblanket_quarantines_batch_but_allows_single_object_reviewers(self) -> None:
        bundle = self._bundle()
        figures = [row for row in bundle["objects"] if row["type"] == "figure" and row["physical_page"] == 1]
        first_id, second_id = figures[0]["visual_object_id"], figures[1]["visual_object_id"]
        batch = self._attestation_for_objects(bundle, [first_id, second_id])
        for difference in batch["differences"]:
            difference["observation"] = "same generic observation"
        batch["confidence"] = {"extraction": 0.8, "interpretation": 0.8}
        batch_codes = {
            issue.code
            for issue in validate_visual_review_attestations(
                [batch],
                [figures[0], figures[1]],
                review_plan_sha256="d" * 64,
                source_sha256=bundle["source_sha256"],
                proposer_instances=["proposal-agent"],
                required_reviewers={first_id: 1, second_id: 1},
                expected_review_session_id="review-session",
                registered_reviewers=["reviewer-a"],
            )
        }
        self.assertIn("visual_review_blanket_pattern", batch_codes)
        first = self._attestation_for_objects(bundle, [first_id], reviewer="reviewer-a")
        second = self._attestation_for_objects(bundle, [first_id], reviewer="reviewer-b")
        self.assertEqual(
            validate_visual_review_attestations(
                [first, second],
                [figures[0]],
                review_plan_sha256="d" * 64,
                source_sha256=bundle["source_sha256"],
                proposer_instances=["proposal-agent"],
                required_reviewers={first_id: 2},
                expected_review_session_id="review-session",
                registered_reviewers=["reviewer-a", "reviewer-b"],
            ),
            [],
        )

    def test_visual_relation_and_table_grid_hash_bindings_reject_recomputed_tamper(self) -> None:
        bundle = self._bundle()
        table = next(row for row in bundle["objects"] if row["type"] == "table")
        bound_objects = [table]
        table_id = table["visual_object_id"]
        grid = next(row for row in bundle["table_grids"] if row["table_visual_object_id"] == table_id)
        cell_ids = [cell["cell_id"] for cell in grid["cells"]]
        bound_relations = [
            row
            for row in bundle["relations"]
            if row["source_visual_object_id"] == table_id or row["target_visual_object_id"] == table_id
        ]
        attestation = self._attestation_for_objects(
            bundle,
            [table_id],
            cell_ids=cell_ids,
            relation_ids=[row["visual_relation_id"] for row in bound_relations],
            relation_hashes=[row["relation_sha256"] for row in bound_relations],
            grid_ids=[grid["table_grid_id"]],
            grid_hashes=[grid["grid_sha256"]],
        )
        self.assertEqual(
            validate_visual_review_attestations(
                [attestation],
                bound_objects,
                review_plan_sha256="d" * 64,
                source_sha256=bundle["source_sha256"],
                proposer_instances=["proposal-agent"],
                required_reviewers={table_id: 1},
                table_grids=bundle["table_grids"],
                visual_relations=bundle["relations"],
                expected_review_session_id="review-session",
                registered_reviewers=["reviewer-a"],
            ),
            [],
        )
        relation_tampered = copy.deepcopy(bundle["relations"])
        relation_index = next(
            index
            for index, row in enumerate(relation_tampered)
            if row["source_visual_object_id"] == table_id or row["target_visual_object_id"] == table_id
        )
        relation_tampered[relation_index]["evidence_hashes"] = ["0" * 64]
        relation_tampered[relation_index]["relation_sha256"] = sha256_json(
            {key: value for key, value in relation_tampered[relation_index].items() if key not in {"relation_sha256", "status"}}
        )
        relation_codes = {
            issue.code
            for issue in validate_visual_review_attestations(
                [attestation],
                bound_objects,
                review_plan_sha256="d" * 64,
                source_sha256=bundle["source_sha256"],
                proposer_instances=["proposal-agent"],
                required_reviewers={table_id: 1},
                table_grids=bundle["table_grids"],
                visual_relations=relation_tampered,
                expected_review_session_id="review-session",
                registered_reviewers=["reviewer-a"],
            )
        }
        self.assertIn("visual_relation_hash_mismatch", relation_codes)

        grid_tampered = copy.deepcopy(bundle["table_grids"])
        grid_tampered[0]["cells"][0]["value_candidate"] = "tampered-value"
        grid_tampered[0]["cells"][0]["unit_candidate"] = "tampered-unit"
        grid_tampered[0]["cells"][0]["row_span"] = 2
        grid_tampered[0]["grid_sha256"] = sha256_json(
            {key: value for key, value in grid_tampered[0].items() if key not in {"grid_sha256", "status"}}
        )
        grid_codes = {
            issue.code
            for issue in validate_visual_review_attestations(
                [attestation],
                bound_objects,
                review_plan_sha256="d" * 64,
                source_sha256=bundle["source_sha256"],
                proposer_instances=["proposal-agent"],
                required_reviewers={table_id: 1},
                table_grids=grid_tampered,
                visual_relations=bundle["relations"],
                expected_review_session_id="review-session",
                registered_reviewers=["reviewer-a"],
            )
        }
        self.assertIn("table_grid_hash_mismatch", grid_codes)

    def test_visual_assurance_manifest_freezes_reviewer_requirements(self) -> None:
        bundle = self._bundle()
        obj = next(row for row in bundle["objects"] if row["type"] == "plot")
        object_id = obj["visual_object_id"]
        first = self._attestation_for_objects(bundle, [object_id], reviewer="reviewer-a")
        second = self._attestation_for_objects(bundle, [object_id], reviewer="reviewer-b")
        kwargs = {
            "source_sha256": bundle["source_sha256"],
            "objects": [obj],
            "relations": [],
            "grids": [],
            "conflicts": [],
            "review_plan_sha256": "d" * 64,
            "review_session_id": "review-session",
            "registered_reviewers": ["reviewer-a", "reviewer-b"],
            "required_reviewers": {object_id: 2},
        }
        manifest = visual_assurance_manifest(attestations=[first, second], **kwargs)
        self.assertEqual(
            validate_visual_assurance_manifest(
                manifest,
                source_sha256=bundle["source_sha256"],
                objects=[obj],
                relations=[],
                grids=[],
                conflicts=[],
                attestations=[first, second],
                review_plan_sha256="d" * 64,
                review_session_id="review-session",
                registered_reviewers=["reviewer-a", "reviewer-b"],
                required_reviewers={object_id: 2},
            ),
            [],
        )
        deleted_second = visual_assurance_manifest(attestations=[first], **kwargs)
        missing_codes = {
            issue.code
            for issue in validate_visual_assurance_manifest(
                deleted_second,
                source_sha256=bundle["source_sha256"],
                objects=[obj],
                relations=[],
                grids=[],
                conflicts=[],
                attestations=[first],
                review_plan_sha256="d" * 64,
                review_session_id="review-session",
                registered_reviewers=["reviewer-a", "reviewer-b"],
                required_reviewers={object_id: 2},
            )
        }
        self.assertIn("visual_reviewer_coverage_insufficient", missing_codes)
        forged_requirement = visual_assurance_manifest(
            attestations=[first],
            **{**kwargs, "required_reviewers": {object_id: 1}},
        )
        forged_codes = {
            issue.code
            for issue in validate_visual_assurance_manifest(
                forged_requirement,
                source_sha256=bundle["source_sha256"],
                objects=[obj],
                relations=[],
                grids=[],
                conflicts=[],
                attestations=[first],
                review_plan_sha256="d" * 64,
                review_session_id="review-session",
                registered_reviewers=["reviewer-a", "reviewer-b"],
                required_reviewers={object_id: 2},
            )
        }
        self.assertIn("visual_reviewer_requirement_mismatch", forged_codes)

    def test_adapter_mapping_is_pure_hash_bound_and_fail_closed(self) -> None:
        bundle = self._bundle()
        sample = next(row for row in bundle["objects"] if row["physical_page"] == 1)
        receipts = {
            "parser": {"id": "adapter-parser", "version": "1", "candidate_only": True},
            "backend": {"id": "adapter-backend", "version": "1", "candidate_only": True},
            "model": {"id": "local-model", "version": "1", "available": True, "download": False},
            "config": {"id": "adapter-config", "version": "1", "candidate_only": True},
        }
        common = {
            "source_sha256": bundle["source_sha256"],
            "physical_page": 1,
            "page_width": 600,
            "page_height": 800,
            "render_sha256": sample["canonical_render_sha256"],
            "render_dpi": sample["canonical_render_dpi"],
            "parser_receipt": receipts["parser"],
            "backend_receipt": receipts["backend"],
            "model_receipt": receipts["model"],
            "config_receipt": receipts["config"],
        }
        mapped = map_candidate_locators("pymupdf", [{"kind": "image", "bbox": [10, 20, 80, 100], "content": "image-content"}], **common)
        self.assertEqual(mapped[0]["type"], "figure")
        self.assertTrue(mapped[0]["candidate_only"])
        mapped_bundle = extract_visual_candidates(self.source, pages=[1], pdftoppm=self.render, confirmed_locators=mapped)
        self.assertTrue(any(row["type"] == "figure" for row in mapped_bundle["objects"]))

        model_root = self.root / "local-models"
        model_root.mkdir()
        for adapter_id, kind, expected_type in (("docling", "formula", "equation_display"), ("paddleocr-ppstructure", "chart", "plot")):
            rows = map_candidate_locators(adapter_id, [{"kind": kind, "bbox": [10, 20, 80, 100], "content": kind}], model_root=str(model_root), **common)
            self.assertEqual(rows[0]["type"], expected_type)
        with self.assertRaisesRegex(VisualAdapterError, "local_model_artifacts_required"):
            map_candidate_locators("docling", [{"kind": "table", "bbox": [10, 20, 80, 100]}], **common)
        with self.assertRaisesRegex(VisualAdapterError, "unknown_kind"):
            map_candidate_locators("pymupdf", [{"kind": "unknown", "bbox": [10, 20, 80, 100]}], **common)
        with self.assertRaisesRegex(VisualAdapterError, "provenance_receipts_required"):
            map_candidate_locators("pymupdf", [{"kind": "image", "bbox": [10, 20, 80, 100]}], source_sha256=common["source_sha256"], physical_page=1, page_width=600, page_height=800, render_sha256=common["render_sha256"], render_dpi=150)
        with self.assertRaisesRegex(VisualAdapterError, "coordinate_invalid"):
            map_candidate_locators("pymupdf", [{"kind": "image", "bbox": [-1, 20, 80, 100]}], **common)
        with self.assertRaisesRegex(VisualAdapterError, "hash_mismatch"):
            map_candidate_locators("pymupdf", [{"kind": "image", "bbox": [10, 20, 80, 100], "source_sha256": "0" * 64}], **common)
        tampered_receipts = dict(common)
        tampered_receipts["parser_receipt"] = {"id": "adapter-parser", "candidate_only": False}
        with self.assertRaisesRegex(VisualAdapterError, "receipt_not_candidate_only"):
            map_candidate_locators("pymupdf", [{"kind": "image", "bbox": [10, 20, 80, 100]}], **tampered_receipts)

    def test_optional_adapters_are_explicit_and_fail_closed(self) -> None:
        receipt = adapter_receipt("docling")
        self.assertFalse(receipt.available)
        self.assertFalse(receipt.model_download)
        with self.assertRaises(VisualAdapterError):
            normalize_candidate_rows("docling", [{"type": "figure", "status": "promoted"}])
        invalid = build_visual_object(source_sha256="a" * 64, physical_page=1, object_type="figure", page_width=100, page_height=100, bbox=[0, 0, 10, 10], render_sha256="b" * 64, render_dpi=150, crop_sha256="bad")
        self.assertIn("crop_hash_mismatch", {issue.code for issue in validate_visual_bundle({"source_sha256": "a" * 64, "objects": [invalid], "relations": [], "table_grids": [], "conflicts": []})})

    def test_external_visual_attestation_workpack_gate_and_runtime_projection(self) -> None:
        """The v0.2 route must stop at external review and project only reviewed IDs."""

        source = self.root / "visual-workpack.pdf"
        pdf = canvas.Canvas(str(source), pagesize=(600, 800))
        pdf.bookmarkPage("chapter")
        pdf.addOutlineEntry("1 Chapter", "chapter", level=0, closed=False)
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(72, 750, "1 Chapter")
        pdf.setFont("Helvetica", 11)
        pdf.drawString(72, 710, "Native visual candidate fixture.")
        pdf.drawString(72, 670, "Figure 1.1")
        pdf.rect(100, 500, 180, 120)
        pdf.drawString(72, 450, "Table 1.1")
        pdf.drawString(80, 420, "Name Unit Value")
        pdf.drawString(80, 400, "temperature K 10")
        pdf.showPage()
        pdf.save()
        ir = self.root / "visual-workpack-ir"
        write_intermediate_representation(
            ir, compile_pdf_intermediate_representation(source, 1, 1)
        )
        workpack = self.root / "visual-workpack"
        build_semantic_workpack(
            source, ir, workpack, pdftoppm=self.render, semantic_assurance=True
        )
        manifest = json.loads((workpack / "workpack.json").read_text(encoding="utf-8"))
        for entry in manifest["units"]:
            draft = {
                "schema_version": "tkc.semantic-draft/v0.1",
                "unit_id": entry["unit_id"],
                "proposer_instance": "proposal-agent",
                "candidate_disposition": {
                    "action": "non-knowledge",
                    "reason": "Visual gate fixture; no semantic fact is proposed.",
                },
                "objects": [],
                "relations": [],
                "conflicts": [],
                "gaps": [],
            }
            (workpack / "drafts" / f"{entry['unit_id']}.json").write_text(
                json.dumps(draft, sort_keys=True) + "\n", encoding="utf-8"
            )
        plan = prepare_review_plan(source, workpack, ["reviewer-a", "reviewer-b"])
        plan_hash = promotion_sha256_json(plan)
        queue = [
            json.loads(line)
            for line in (workpack / "visual-review-queue.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        objects = [
            json.loads(line)
            for line in (workpack / "visual-objects.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        by_id = {row["visual_object_id"]: row for row in objects}
        visual_row = queue[0]
        visual_ids = sorted(set(visual_row["visual_object_ids"]))
        bound = [by_id[identifier] for identifier in visual_ids]
        table_cell_ids = sorted(set(visual_row.get("table_cell_ids", [])))
        series_ids = sorted(set(visual_row.get("series_ids", [])))
        crop_hashes = sorted(
            set(row["crop_sha256"] for row in bound if row.get("crop_sha256"))
        )
        bbox_hashes = sorted(set(promotion_sha256_json(row["bbox"]) for row in bound))
        records = []
        for reviewer in ("reviewer-a", "reviewer-b"):
            input_payload = {
                "review_plan_sha256": plan_hash,
                "source_sha256": plan["source_sha256"],
                "visual_object_ids": visual_ids,
                "table_cell_ids": table_cell_ids,
                "series_ids": series_ids,
                "visual_relation_ids": sorted(set(visual_row.get("visual_relation_ids", []))),
                "relation_sha256s": sorted(set(visual_row.get("relation_sha256s", []))),
                "table_grid_ids": sorted(set(visual_row.get("table_grid_ids", []))),
                "table_grid_sha256s": sorted(set(visual_row.get("table_grid_sha256s", []))),
                "full_page_sha256": bound[0]["canonical_render_sha256"],
                "crop_sha256s": crop_hashes,
                "bbox_sha256s": bbox_hashes,
                "render_sha256": bound[0]["canonical_render_sha256"],
                "render_dpi": bound[0]["canonical_render_dpi"],
                "render_rotation": bound[0]["canonical_render_rotation"],
            }
            records.append(
                build_visual_review_attestation(
                    reviewer_instance=reviewer,
                    proposer_instance="proposal-agent",
                    review_session_id=plan["review_session_id"],
                    review_plan_sha256=plan_hash,
                    source_sha256=plan["source_sha256"],
                    visual_object_ids=visual_ids,
                    table_cell_ids=table_cell_ids,
                    series_ids=series_ids,
                    visual_relation_ids=input_payload["visual_relation_ids"],
                    relation_sha256s=input_payload["relation_sha256s"],
                    table_grid_ids=input_payload["table_grid_ids"],
                    table_grid_sha256s=input_payload["table_grid_sha256s"],
                    full_page_sha256=input_payload["full_page_sha256"],
                    crop_sha256s=crop_hashes,
                    render_sha256=input_payload["render_sha256"],
                    render_dpi=input_payload["render_dpi"],
                    render_rotation=input_payload["render_rotation"],
                    bbox_sha256s=bbox_hashes,
                    input_sha256=promotion_sha256_json(input_payload),
                    verdict="accepted",
                    differences=[
                        {
                            "visual_object_id": identifier,
                            "status": "confirmed",
                            "observation": f"{reviewer} checked {identifier}.",
                        }
                        for identifier in visual_ids
                    ],
                    rationale=f"{reviewer} independently inspected the bound page and crops.",
                    confidence={"extraction": 0.9, "interpretation": 0.9},
                    reviewer_independence={"independent": True, "basis": "external session"},
                )
            )
        external = self.root / "external-visual-review"
        external.mkdir()
        semantic_fragment = external / "semantic.jsonl"
        semantic_fragment.write_text("", encoding="utf-8")
        visual_fragment = external / "visual.jsonl"
        visual_fragment.write_text(
            "".join(
                json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                for row in records
            ),
            encoding="utf-8",
        )
        merge_review_fragments(source, workpack, [semantic_fragment], [visual_fragment])
        issues, summary = validate_reviewed_workpack(source, workpack)
        self.assertEqual([issue for issue in issues if issue.severity == "error"], [])
        self.assertEqual(summary["visual_review_protocol"], VISUAL_REVIEW_PROTOCOL)
        output = self.root / "visual-promoted"
        promote_reviewed_workpack(
            source,
            workpack,
            output,
            name="visual-fixture",
            display_name="Visual Fixture",
            domain="synthetic",
        )
        promoted = [
            json.loads(line)
            for line in (output / "references/visual/visual-objects.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        self.assertTrue(promoted)
        self.assertEqual({row["status"] for row in promoted}, {"promoted"})
        self.assertEqual(
            [
                issue
                for issue in validate_expert_skill(output, level="structure")
                if issue.severity == "error"
            ],
            [],
        )
        # A v0.7 runtime projection carrying only a page task must fail closed
        # even if an attacker rewrites the candidate/package rows.  The exact
        # visual-object binding is the promotion boundary.
        knowledge_index_path = output / "references/knowledge/index.json"
        knowledge_index = json.loads(knowledge_index_path.read_text(encoding="utf-8"))
        object_id = "page-only-visual-fact"
        object_path = output / "references/knowledge/objects/page-only-visual-fact.json"
        object_path.parent.mkdir(parents=True, exist_ok=True)
        object_path.write_text(
            json.dumps(
                {
                    "schema_version": "tkc.expert-skill/v0.1",
                    "id": object_id,
                    "type": "Claim",
                    "origin": "source-paraphrase",
                    "title": "Page-only visual fact",
                    "statement": "This deliberately lacks an exact visual object binding.",
                    "source_ids": ["synthetic-source"],
                    "evidence_ids": [],
                    "assumptions": [],
                    "valid_when": [],
                    "fails_when": [],
                    "confidence": {"extraction": 0.5, "interpretation": 0.5},
                    "visual_task_ids": ["page-task-only"],
                    "status": "independently-reviewed-draft",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        knowledge_index.setdefault("objects", []).append(
            {"id": object_id, "path": "references/knowledge/objects/page-only-visual-fact.json"}
        )
        knowledge_index_path.write_text(json.dumps(knowledge_index, sort_keys=True) + "\n", encoding="utf-8")
        page_only_codes = {issue.code for issue in validate_expert_skill(output, level="structure")}
        self.assertIn("visual_fact_object_support_missing", page_only_codes)

    def test_real_visual_chapter_candidate_acceptance(self) -> None:
        source = self._require_local_pdf("FAKE_EXPERT_TEST_VISUAL_CHAPTER_PDF")
        bundle = extract_visual_candidates(source, pages=[653, 660, 661, 662, 663], pdftoppm=self.render)
        self.assertTrue(any(row["physical_page"] == 653 and row["type"] == "equation_display" for row in bundle["objects"]))
        self.assertEqual(sum(row["physical_page"] == 660 and row["type"] == "figure" and row["subtype"] == "figure" for row in bundle["objects"]), 2)
        self.assertTrue(all(row["parent"] is None or row["parent"] in {candidate["visual_object_id"] for candidate in bundle["objects"]} for row in bundle["objects"]))
        self.assertFalse(any(row["source_visual_object_id"] == row["target_visual_object_id"] for row in bundle["relations"]))
        self.assertEqual(sum(row["physical_page"] == 661 and row["type"] == "table" for row in bundle["objects"]), 1)
        self.assertFalse(any(row["physical_page"] == 662 and row["type"] == "table" for row in bundle["objects"]))
        self.assertEqual(sum(row["physical_page"] == 663 and row["type"] == "table" for row in bundle["objects"]), 1)
        self.assertGreaterEqual(len(bundle["table_grids"]), 2)
        self.assertEqual(validate_visual_bundle(bundle), [])
        self.assertFalse((self.root / "copy-of-source.pdf").exists())

    def test_real_figure_16_2_uses_computed_xobject_geometry(self) -> None:
        source = self._require_local_pdf("FAKE_EXPERT_TEST_VISUAL_CHAPTER_PDF")
        from pypdf import PdfReader

        reader = PdfReader(str(source))
        page = reader.pages[659]
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        page_boxes = _text_boxes(page, height)
        figure_label = next(row for row in _find_label_boxes(page_boxes, FIGURE_RE, line_start_only=True) if row[0] == "16.2")
        xobjects = _page_xobject_candidates(page, width, height, reader)
        expected = _caption_adjacent_xobject(figure_label[1], xobjects, width, height, used=set())
        self.assertIsNotNone(expected)
        expected_box = list(expected["bbox"])

        bundle = extract_visual_candidates(source, pages=[660], pdftoppm=self.render)
        figures = [row for row in bundle["objects"] if row["type"] == "figure" and row["subtype"] == "figure"]
        figure = next(row for row in figures if row["label"] == "Figure 16.2")
        caption = next(row for row in bundle["objects"] if row["type"] == "caption" and row["label"] == "Figure 16.2")
        self.assertEqual(figure["attributes"]["geometry_provenance"]["geometry_evidence"], "pdf-xobject-image-do-v0.1")
        self.assertEqual(figure["attributes"]["geometry_provenance"]["xobject_path"], expected["xobject_provenance"]["xobject_path"])
        left = figure["bbox"]
        ix0, iy0 = max(left[0], expected_box[0]), max(left[1], expected_box[1])
        ix1, iy1 = min(left[2], expected_box[2]), min(left[3], expected_box[3])
        intersection = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
        area_left = (left[2] - left[0]) * (left[3] - left[1])
        area_expected = (expected_box[2] - expected_box[0]) * (expected_box[3] - expected_box[1])
        self.assertGreater(intersection / max(1e-9, area_left + area_expected - intersection), 0.95)
        self.assertGreater(area_left, 30000.0)
        self.assertEqual(caption["parent"], figure["visual_object_id"])
        self.assertNotEqual(figure["bbox"], caption["bbox"])
        self.assertNotIn("caption-only", figure["attributes"]["geometry_provenance"]["geometry_evidence"])
        self.assertEqual(sum(row["type"] == "figure" and row["subtype"] == "panel" for row in bundle["objects"]), 0)
        self.assertEqual(validate_visual_bundle(bundle), [])


if __name__ == "__main__":
    unittest.main()
