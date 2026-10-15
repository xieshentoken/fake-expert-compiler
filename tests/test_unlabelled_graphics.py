"""P2 unlabeled geometry, caption continuation, and explicit localization gaps."""
import io
from pathlib import Path

from _support import TempDirTestCase, requires_renderer, pdftoppm
from visual_semantics import extract_visual_candidates, render_crop_png, validate_visual_bundle, _find_label_boxes, FIGURE_RE


@requires_renderer
class UnlabelledGraphicsTests(TempDirTestCase):
    def source(self,decorative=False,separate=False):
        from reportlab.pdfgen import canvas
        path=self.tmp/'graphics.pdf'
        c=canvas.Canvas(str(path),pagesize=(600,800))
        c.drawString(50,740,'1.1 Bounded geometry')
        c.drawString(50,710,'This synthetic native-text page has drawings without figure captions.')
        for i in range(12):
            if decorative:c.line(50,780-i,550,780-i)
            else:c.rect(100+i*15,400,10,60,stroke=1,fill=0)
        if separate:c.rect(400,200,80,80,stroke=1,fill=0)
        c.save();return path

    def test_uncaptioned_vector_cluster_has_exact_bounded_candidate_and_crop(self):
        from PIL import Image
        source=self.source()
        bundle=extract_visual_candidates(source,pdftoppm=Path(pdftoppm()),render_dpi=72)
        figures=[r for r in bundle['objects'] if r['type']=='figure']
        self.assertEqual(len(figures),1)
        self.assertEqual(figures[0]['bbox'],[100,340,275,400])
        self.assertIsNone(figures[0]['label'])
        self.assertEqual(validate_visual_bundle(bundle),[])
        crop=Image.open(io.BytesIO(render_crop_png(source,figures[0],Path(pdftoppm()),dpi=72))).convert('L')
        self.assertEqual(crop.getextrema(),(0,255))

    def test_decoration_creates_gap_instead_of_page_sized_figure(self):
        bundle=extract_visual_candidates(self.source(decorative=True),pdftoppm=Path(pdftoppm()),render_dpi=72)
        self.assertFalse([r for r in bundle['objects'] if r['type']=='figure'])
        self.assertIn('unlocalised_or_ambiguous_graphics',{r['details'].get('reason') for r in bundle['conflicts']})

    def test_separate_graphics_are_not_merged(self):
        bundle=extract_visual_candidates(self.source(separate=True),pdftoppm=Path(pdftoppm()),render_dpi=72)
        self.assertEqual(len([r for r in bundle['objects'] if r['type']=='figure']),2)

    def test_off_page_annotation_records_conflict_without_clipping_or_aborting(self):
        from reportlab.pdfgen import canvas
        source=self.tmp/'edge-label.pdf'
        c=canvas.Canvas(str(source),pagesize=(600,800))
        c.rect(0,400,120,60,stroke=1,fill=0)
        c.drawString(-2,430,'edge label')
        c.save()
        bundle=extract_visual_candidates(source,pdftoppm=Path(pdftoppm()),render_dpi=72)
        self.assertEqual(len([r for r in bundle['objects'] if r['type']=='figure']),1)
        self.assertFalse([r for r in bundle['objects'] if r['type']=='annotation'])
        self.assertIn('annotation_bbox_out_of_page',{r['details'].get('reason') for r in bundle['conflicts']})
        self.assertEqual(validate_visual_bundle(bundle),[])


class CaptionTests(TempDirTestCase):
    def test_lowercase_title_and_continuation_are_preserved_but_prose_is_not(self):
        rows=[{'text':'Figure 1.2 heat transfer coefficient','bbox':[50,100,300,112],'font_size':10},
              {'text':'measured under bounded conditions.','bbox':[50,116,300,128],'font_size':10},
              {'text':'Figure 1.3 shows the same network.','bbox':[50,200,300,212],'font_size':10}]
        result=_find_label_boxes(rows,FIGURE_RE,line_start_only=True)
        self.assertEqual(len(result),1)
        self.assertIn('measured under bounded conditions.',result[0][2])
        self.assertEqual(result[0][1],[50,100,300,128])
