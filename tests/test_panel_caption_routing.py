"""Panel references remain prose while real captions retain their geometry."""
import hashlib
import os
from pathlib import Path
import unittest
from _support import TempDirTestCase
from visual_semantics import _find_label_boxes, FIGURE_RE, extract_visual_candidates, validate_visual_bundle

class CaptionRoutingTests(TempDirTestCase):
    def test_panel_verb_is_prose_and_panel_title_remains_caption(self):
        for text,expected in [('Figure 1-12(c)depicts the limiting case.',False),
            ('Figure 1-12 ( c ) shows cooling.',False),('Figure 1-12(c) Temperature distribution.',True),
            ('Figure 1-12 Temperature distribution.',True),('Figure 1-12 shows cooling.',False)]:
            with self.subTest(text=text):
                rows=[{'text':text,'bbox':[40,100,320,112],'font_size':10}]
                self.assertEqual(bool(_find_label_boxes(rows,FIGURE_RE,line_start_only=True)),expected)

    def test_wrapped_reference_and_standalone_real_caption(self):
        label={'text':'Figure 1-7.','bbox':[80,112,130,122],'font_size':10}
        self.assertTrue(_find_label_boxes([label],FIGURE_RE,line_start_only=True))
        for prefix in ('Show all assumptions. Consider', 'See', 'Refer to', '参见'):
            previous={'text':prefix,'bbox':[80,100,320,110],'font_size':10}
            self.assertFalse(_find_label_boxes([previous,label],FIGURE_RE,line_start_only=True))
        previous={'text':'A real title','bbox':[80,100,320,110],'font_size':10}
        self.assertTrue(_find_label_boxes([previous,label],FIGURE_RE,line_start_only=True))

    @unittest.skipUnless(os.environ.get('FAKE_EXPERT_HEAT_CONDUCTION_PDF'),'private real PDF not supplied')
    def test_real_figure_six_and_twelve_ownership(self):
        source=Path(os.environ['FAKE_EXPERT_HEAT_CONDUCTION_PDF']);h=hashlib.sha256(source.read_bytes()).hexdigest()
        pages=[30,49,50,55]
        receipts={p:{'source_sha256':h,'physical_page':p,'renderer':'pdftoppm','rotation':0,
            'render_sha256':'f'*64,'dpi':120,'format':'png'} for p in pages}
        bundle=extract_visual_candidates(source,pages=pages,render_receipts=receipts)
        self.assertEqual(validate_visual_bundle(bundle),[])
        for page,label in [(30,'Figure 1-6'),(49,'Figure 1-12')]:
            figures=[r for r in bundle['objects'] if r['physical_page']==page and r['type']=='figure' and r.get('label')==label]
            self.assertEqual(len(figures),1)
            figure=figures[0]
            captions=[r for r in bundle['objects'] if r['type']=='caption' and r.get('parent')==figure['visual_object_id']]
            self.assertEqual(len(captions),1)
            self.assertLess((figure['bbox'][2]-figure['bbox'][0])*(figure['bbox'][3]-figure['bbox'][1]),441*666*.5)
            self.assertTrue(any(r['relation_type']=='caption_of' and r['target_visual_object_id']==figure['visual_object_id'] for r in bundle['relations']))
        self.assertFalse([r for r in bundle['objects'] if r['physical_page']==50 and r.get('label')=='Figure 1-12'])
        self.assertFalse([r for r in bundle['objects'] if r['physical_page']==55 and r.get('label','').startswith('Figure')])
