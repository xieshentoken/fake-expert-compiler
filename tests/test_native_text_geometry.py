"""Geometry replay and bounded failure isolation, without Review or models."""
import hashlib
import os
from pathlib import Path
from unittest import mock

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from _support import TempDirTestCase, requires_renderer, pdftoppm
from native_text_geometry import text_fragments
import visual_semantics as visual


class NativeGeometryTests(TempDirTestCase):
    def source(self, content, pages=1):
        writer=PdfWriter()
        for _ in range(pages):
            page=writer.add_blank_page(width=400,height=500)
            font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),
                NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
            page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):
                DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
            stream=DecodedStreamObject();stream.set_data(content)
            page[NameObject('/Contents')]=writer._add_object(stream)
        path=self.tmp/'source.pdf'
        with path.open('wb') as f:writer.write(f)
        return path

    def receipts(self,source,pages):
        h=hashlib.sha256(source.read_bytes()).hexdigest()
        return {p:{'source_sha256':h,'physical_page':p,'renderer':'pdftoppm','rotation':0,
            'render_sha256':'f'*64,'dpi':120,'format':'png'} for p in pages}

    def test_text_line_matrix_and_leading_are_not_double_scaled(self):
        source=self.source(b'BT /F1 1 Tf 10 0 0 10 30 300 Tm (alpha) Tj 0 -2 TD (beta) Tj T* (gamma) Tj ET')
        rows=text_fragments(PdfReader(source).pages[0],500)
        self.assertEqual([r['text'] for r in rows],['alpha','beta','gamma'])
        self.assertEqual([r['geometry_provenance']['glyphs'][0]['baseline'] for r in rows],
                         [[30,200],[30,220],[30,240]])
        self.assertEqual([r['font_size'] for r in rows],[10,10,10])
        self.assertTrue(all(r['bbox'][3]-r['bbox'][1]>5 for r in rows))

    def test_mixed_font_metrics_do_not_reorder_same_baseline(self):
        from reportlab.pdfgen import canvas
        source=self.tmp/'mixed-fonts.pdf';pdf=canvas.Canvas(str(source),pagesize=(400,500))
        pdf.setFont('Times-Roman',10);pdf.drawString(40,300,'Figure')
        pdf.setFont('Helvetica',10);pdf.drawString(72,300,'1.1')
        pdf.setFont('Courier',10);pdf.drawString(92,300,'Sample title.')
        pdf.save()
        rows=visual._text_boxes(PdfReader(source).pages[0],500)
        self.assertEqual(len(rows),1)
        self.assertTrue(rows[0]['text'].startswith('Figure 1.1'),rows)

    def test_ctm_and_graphics_restore_are_applied_once(self):
        source=self.source(b'q 2 0 0 2 50 60 cm BT /F1 8 Tf 1 0 0 1 10 20 Tm (scaled) Tj ET Q '
                           b'BT /F1 8 Tf 1 0 0 1 10 20 Tm (plain) Tj ET')
        rows=text_fragments(PdfReader(source).pages[0],500)
        self.assertEqual([r['geometry_provenance']['glyphs'][0]['baseline'] for r in rows],[[70,400],[10,480]])
        self.assertEqual([r['font_size'] for r in rows],[16,8])

    def test_unknown_font_is_explicitly_unavailable(self):
        source=self.source(b'BT /Unknown 12 Tf (unmapped) Tj ET')
        with self.assertRaisesRegex(ValueError,'native_geometry_font_missing'):
            text_fragments(PdfReader(source).pages[0],500)

    def test_one_invalid_equation_does_not_erase_valid_figure(self):
        source=self.source(b'80 250 180 100 re S BT /F1 12 Tf 1 0 0 1 60 220 Tm (Figure 1.1: Frame.) Tj ET')
        original=visual._text_boxes
        def with_bad(*args,**kwargs):
            return original(*args,**kwargs)+[{'text':'(1.99)','bbox':[20,501,80,520],'font_size':12}]
        with mock.patch.object(visual,'_text_boxes',side_effect=with_bad):
            bundle=visual.extract_visual_candidates(source,pages=[1],render_receipts=self.receipts(source,[1]))
        self.assertTrue(any(r['type']=='figure' for r in bundle['objects']))
        self.assertTrue(any(r['details'].get('reason')=='equation_bbox_unavailable' for r in bundle['conflicts']))
        self.assertEqual(visual.validate_visual_bundle(bundle),[])

    def test_page_error_preserves_other_page_and_does_not_mask_programming_errors(self):
        source=self.source(b'BT /F1 12 Tf 1 0 0 1 40 300 Tm (q = k (1.1)) Tj ET',pages=2)
        original=visual._extract_visual_page
        def fail_second(*args,**kwargs):
            if kwargs['pages']==[2]:raise visual.VisualSemanticsError('injected_geometry_error')
            return original(*args,**kwargs)
        with mock.patch.object(visual,'_extract_visual_page',side_effect=fail_second):
            bundle=visual.extract_visual_candidates(source,pages=[1,2],render_receipts=self.receipts(source,[1,2]))
        self.assertTrue(bundle['objects'])
        self.assertEqual({r['physical_page'] for r in bundle['objects']},{1})
        self.assertEqual({r['physical_page'] for r in bundle['conflicts']},{2})
        self.assertEqual(bundle['conflicts'][0]['details']['code'],'injected_geometry_error')
        with mock.patch.object(visual,'_extract_visual_page',side_effect=RuntimeError('programming error')):
            with self.assertRaisesRegex(RuntimeError,'programming error'):
                visual.extract_visual_candidates(source,pages=[1],render_receipts=self.receipts(source,[1]))


@requires_renderer
class RealChapterGeometryTests(TempDirTestCase):
    def test_three_real_equation_labels_and_complete_page_batch(self):
        source=Path(os.environ.get('FAKE_EXPERT_HEAT_CONDUCTION_PDF',''))
        if not source.is_file():self.skipTest('set FAKE_EXPERT_HEAT_CONDUCTION_PDF for private geometry regression')
        reader=PdfReader(source)
        for page,label,expected_y in [(44,'(1-74)',585.114),(46,'(1-81)',588.119985),(47,'(1-83)',523.146803)]:
            rows=text_fragments(reader.pages[page-1],666)
            row=next(r for r in rows if r['text'].strip()==label)
            self.assertAlmostEqual(row['geometry_provenance']['glyphs'][0]['baseline'][1],expected_y,places=3)
            self.assertTrue(0<=row['bbox'][1]<row['bbox'][3]<=666)
        bundle=visual.extract_visual_candidates(source,pages=list(range(19,58)),pdftoppm=Path(pdftoppm()),render_dpi=120)
        self.assertEqual(len(bundle['render_receipts']),39)
        self.assertEqual(visual.validate_visual_bundle(bundle),[])
        self.assertFalse([r for r in bundle['conflicts'] if r['details'].get('code')=='bbox_page_mismatch'])
        for page,label in [(44,'(1-74)'),(46,'(1-81)'),(47,'(1-83)')]:
            self.assertTrue(any(r['physical_page']==page and r.get('label')==label for r in bundle['objects']))
