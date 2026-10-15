"""Figure/table/equation/heading detection (HANDOFF step 1) and figure content (step 2)."""

from __future__ import annotations

import json
import unittest

from _support import HEAT_BOOK, TempDirTestCase, pdftoppm, requires_renderer

import pdf_structure
import visual_semantics
from pdf_structure import _heading_line, equation_labels, figure_labels, table_labels


class LabelPatternTests(unittest.TestCase):
    def test_figure_labels_books_papers_and_chinese(self) -> None:
        self.assertEqual(figure_labels("see Figure 1.2 and Fig. 3"), ["1.2", "3"])
        self.assertEqual(figure_labels("如图1.4所示，图 2-3 给出"), ["1.4", "2-3"])
        self.assertEqual(figure_labels("FIGURE 7 shows"), ["7"])

    def test_prose_numbers_are_not_figures(self) -> None:
        self.assertEqual(figure_labels("a figure 5 times higher"), [])
        self.assertEqual(figure_labels("地图3号线"), [])
        self.assertEqual(figure_labels("lower-case figure 1.2 still counts"), ["1.2"])

    def test_table_labels(self) -> None:
        self.assertEqual(table_labels("Table 2 lists; 如表3所示"), ["2", "3"])
        self.assertEqual(table_labels("代表3种方法，列表4"), [])

    def test_equation_labels(self) -> None:
        self.assertEqual(equation_labels("q = -k dT/dx (1.1) and （2-3）"), ["(1.1)", "(2-3)"])
        self.assertEqual(equation_labels("", "E = mc^2    (7)\nsee item (3) above"), ["(7)"])


class HeadingTests(unittest.TestCase):
    def test_cjk_and_chapter_headings(self) -> None:
        self.assertEqual(_heading_line("1.5 对流换热"), ("1.5", "对流换热", 1))
        self.assertEqual(_heading_line("1.5对流换热"), ("1.5", "对流换热", 1))
        self.assertEqual(_heading_line("第三章 传热基础"), ("第三章", "传热基础", 0))
        self.assertEqual(_heading_line("第2节 导热"), ("第2节", "导热", 1))
        self.assertEqual(_heading_line("3 Results and Discussion"), ("3", "Results and Discussion", 0))

    def test_cjk_sentences_are_not_headings(self) -> None:
        self.assertIsNone(_heading_line("2025年发布了新标准"))
        self.assertIsNone(_heading_line("1 对流换热系数取决于流速，物性以及几何形状"))
        self.assertIsNone(_heading_line("1.2 本节讨论换热。"))


class PageGraphicsTests(unittest.TestCase):
    def test_nested_form_and_vector_graphics_are_counted(self) -> None:
        from pypdf import PdfReader

        reader = PdfReader(str(HEAT_BOOK))
        summaries = [pdf_structure.page_graphics_summary(page) for page in reader.pages]
        self.assertEqual(summaries[1]["raster_image_count"], 1)  # raster plot
        self.assertGreaterEqual(summaries[2]["vector_paint_op_count"], pdf_structure.VECTOR_GRAPHICS_MIN_PAINT_OPS)
        self.assertGreaterEqual(summaries[3]["form_xobject_count"], 1)  # LaTeX-style embedded PDF figure
        self.assertEqual(summaries[4]["raster_image_count"] + summaries[4]["form_xobject_count"], 0)


class IntermediateRepresentationTests(TempDirTestCase):
    def test_ir_routes_figure_pages_and_splits_chinese_section(self) -> None:
        result = pdf_structure.compile_pdf_intermediate_representation(HEAT_BOOK, 1, None)
        pages = {row["physical_page"]: row for row in result["preflight"]["pages"]}
        self.assertIn("vector_graphics_present", pages[3]["visual_verification_reasons"])
        self.assertIn("vector_graphics_present", pages[4]["visual_verification_reasons"])
        self.assertIn("figure_reference_present", pages[5]["visual_verification_reasons"])  # 图1.4
        titles = [segment["title"] for segment in result["document_map"]["segments"]]
        self.assertIn("1.5 对流换热", titles)


class CaptionProseTests(unittest.TestCase):
    def rows(self, *texts: str) -> list[dict]:
        return [{"text": text, "bbox": [60.0, 100.0 + 20 * index, 400.0, 112.0 + 20 * index], "font_size": 10.0} for index, text in enumerate(texts)]

    def test_body_reference_is_not_a_caption(self) -> None:
        found = visual_semantics._find_label_boxes(
            self.rows("Figure 1.2 shows the network.", "Figure 1.2: Composite wall.", "图1.4给出了速度分布", "图1.5 平板边界层"),
            visual_semantics.FIGURE_RE,
            line_start_only=True,
        )
        self.assertEqual([row[2] for row in found], ["Figure 1.2: Composite wall.", "图1.5 平板边界层"])


@requires_renderer
class VisualCandidateTests(TempDirTestCase):
    def test_figure_geometry_and_in_figure_text(self) -> None:
        from pathlib import Path

        bundle = visual_semantics.extract_visual_candidates(HEAT_BOOK, pdftoppm=Path(pdftoppm()))
        figures = {row["label"]: row for row in bundle["objects"] if row["type"] == "figure"}
        self.assertEqual(sorted(figures), ["Figure 1.1", "Figure 1.2", "Figure 1.3"])
        # vector diagram: wall + resistor network above its caption, not the table below
        x0, y0, x1, y1 = figures["Figure 1.2"]["bbox"]
        self.assertLess(abs(x0 - 100) + abs(y0 - 140) + abs(x1 - 515) + abs(y1 - 300), 20)
        # Form-XObject figure text lands inside the figure, once
        texts = [row["text"] for row in figures["Figure 1.3"]["attributes"]["figure_text_candidates"]]
        self.assertEqual(sorted(texts), ["eta_f", "mL"])
        captions = [row["attributes"].get("caption_text_candidate") for row in bundle["objects"] if row["type"] == "caption"]
        self.assertIn("Figure 1.1: Thermal conductivity of copper versus temperature.", captions)
        self.assertEqual(visual_semantics.validate_visual_bundle(bundle), [])


if __name__ == "__main__":
    unittest.main()
