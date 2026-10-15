"""Open-sided tables use real native closing rules, without a height truncation."""
import hashlib
import os
from pathlib import Path
import unittest
from _support import TempDirTestCase, requires_renderer, pdftoppm
from native_table_geometry import ruled_table_candidate
from visual_semantics import extract_visual_candidates

class NativeTableRulesTests(unittest.TestCase):
    def test_native_body_requires_aligned_header_and_closing_rules(self):
        caption=[40,30,300,40]
        rules=[{'bbox':[40,y,350,y]} for y in [45,65,380]]
        rows=[{'text':'Header','bbox':[50,50,200,60]}, {'text':'Row A','bbox':[50,90,220,100]},
              {'text':'Final row','bbox':[50,360,220,370]}, {'text':'Following prose','bbox':[50,410,300,420]}]
        box,receipt=ruled_table_candidate(caption,rules,rows,400,500)
        self.assertEqual(box,[39.5,44.5,350.5,380.5])
        self.assertEqual(receipt['closing_rule_y'],380)
        for bad in ([*rules,{'bbox':[40,450,350,450]}],rules[:2],[*rules[:2],{'bbox':[60,380,350,380]}],[*rules[:2],{'bbox':[40,501,350,501]}]):
            self.assertIsNone(ruled_table_candidate(caption,bad,rows,400,500)[0])

@requires_renderer
class TableExtractionTests(TempDirTestCase):
    def test_tall_native_table_keeps_final_row_and_excludes_following_prose(self):
        from reportlab.pdfgen import canvas
        source=self.tmp/'table.pdf';c=canvas.Canvas(str(source),pagesize=(400,500))
        c.setFont('Helvetica',10);c.drawString(40,460,'Table 1.1 Tall native table.')
        for y in [450,430,100]:c.line(40,y,350,y)
        c.drawString(50,438,'Name');c.drawString(220,438,'Value')
        for y in [400,350,300,250,200,150,110]:
            c.drawString(50,y,'Synthetic row');c.drawString(220,y,'12')
        c.drawString(40,70,'Following body paragraph must stay outside.');c.save()
        b=extract_visual_candidates(source,pages=[1],pdftoppm=Path(pdftoppm()),render_dpi=120)
        table=next(r for r in b['objects'] if r['type']=='table')
        self.assertGreater(table['bbox'][3],400)
        self.assertLess(table['bbox'][3],410)
        self.assertEqual(table['attributes']['geometry_provenance']['geometry_evidence'],'native-horizontal-table-rules-v0.1')
        caption=next(r for r in b['objects'] if r['type']=='caption')
        self.assertNotIn('Name',caption['attributes']['caption_text_candidate'])

    @unittest.skipUnless(os.environ.get('FAKE_EXPERT_HEAT_CONDUCTION_PDF'),'private real PDF not supplied')
    def test_three_real_tables_have_actual_closing_rules(self):
        source=Path(os.environ['FAKE_EXPERT_HEAT_CONDUCTION_PDF']);h=hashlib.sha256(source.read_bytes()).hexdigest()
        pages=[29,36,41];receipts={p:{'source_sha256':h,'physical_page':p,'renderer':'pdftoppm','rotation':0,'render_sha256':'f'*64,'dpi':120,'format':'png'} for p in pages}
        bundle=extract_visual_candidates(source,pages=pages,render_receipts=receipts)
        for page,label,bottom in [(29,'Table 1-1',616.587),(36,'Table 1-2',369.474),(41,'Table 1-3',367.593)]:
            obj=next(r for r in bundle['objects'] if r['type']=='table' and r['physical_page']==page)
            self.assertEqual(obj['label'],label)
            self.assertAlmostEqual(obj['bbox'][3],bottom+.5,places=3)
            self.assertEqual(obj['attributes']['geometry_provenance']['geometry_evidence'],'native-horizontal-table-rules-v0.1')
