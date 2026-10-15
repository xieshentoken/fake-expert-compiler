"""Source-backed exponent normalization and negative evidence boundaries."""
import copy
import hashlib
import os
from pathlib import Path
import unittest
from _support import TempDirTestCase, requires_renderer
from formula_numeric_evidence import create_native_binding, verify_native_binding
from numeric_evidence import untraced_numbers, checked_untraced_numbers, NumericEvidenceError
from semantic_authoring import _ingest_unit
from pdf_structure import normalize_text
from pypdf import PdfReader

class NumericRolesTests(unittest.TestCase):
    def test_whole_tokens_and_numeric_roles(self):
        self.assertEqual(untraced_numbers({'formula':{'normalized_notation':'k12'}}, ''), [])
        for text in ('L2 (2)', '2*L (1-2)', 'A**2 (1-2)'):
            self.assertEqual(untraced_numbers({'formula':{'normalized_notation':'L**2'}}, text), ['2'])
        for text in ('L²', 'L^2', 'L**2'):
            self.assertEqual(untraced_numbers({'formula':{'normalized_notation':'L**2'}}, text), [])
        self.assertEqual(untraced_numbers({'formula':{'normalized_notation':'2*L'}},'L**2'), ['2'])
        self.assertEqual(untraced_numbers({'statement':'0.20'},'2e-1'), [])
        self.assertEqual(untraced_numbers({'formula':{'normalized_notation':'x = 2'}},'x = 2. (1.1)'), [])
        self.assertEqual(untraced_numbers({'statement':'0.5'},'.5'), [])

@requires_renderer
class NativeFormulaTests(TempDirTestCase):
    def fixture(self, *, rule=True, raised=True):
        from reportlab.pdfgen import canvas
        source=self.tmp/'fraction.pdf'
        c=canvas.Canvas(str(source), pagesize=(400,500));c.setFont('Helvetica',10)
        c.drawString(45,440,'1.1 Synthetic nondimensional time')
        c.drawString(80,300,'t*');c.drawString(110,300,'=')
        c.drawString(140,307,'at');c.drawString(140,292,'L')
        c.setFont('Helvetica',7);c.drawString(145.56,295 if raised else 292,'2')
        if rule:c.line(139.5,302,150,302)
        c.setFont('Helvetica',10);c.drawString(330,300,'(1-1)')
        c.drawString(45,200,'A separate equation label is (2).');c.save()
        text=normalize_text(PdfReader(source).pages[0].extract_text())
        self.assertIn('t* = at L 2 (1-1)',text)
        span={'page':1,'start':0,'end':len(text),'span_sha256':hashlib.sha256(text.encode()).hexdigest()}
        proposal={'type':'Equation','title':'Nondimensional time','statement':'Synthetic source ratio.',
            'formula':{'source_notation':'t* = at L2 (1-1)','normalized_notation':'Fo = alpha*t/(L**2)',
                       'notation_symbols':{'Fo':'t*','alpha':'a'}},'evidence_spans':[span]}
        return source,text,proposal

    def test_valid_binding_replays_and_ingest_materializes(self):
        source,text,p=self.fixture();receipt=create_native_binding(source,p)
        self.assertIsNotNone(receipt);self.assertEqual(untraced_numbers(p,text),['2'])
        p['formula']['native_numeric_evidence']=receipt
        self.assertEqual(checked_untraced_numbers(p,text,source),[])
        self.assertTrue(verify_native_binding(source,p)['candidate_only'])
        unit={'unit_id':'synthetic','focus_spans':[{**p['evidence_spans'][0],'text':text}], 'visual_task_ids':['task']}
        raw={**p,'id':'time','evidence':[text]};raw['formula']['native_numeric_evidence']={'forged':True}
        draft,problems=_ingest_unit(unit,{'objects':[raw]}, {'task':{'id':'task','kind':'equation','page':1}},'proposer',source=source)
        self.assertEqual(len(draft['objects']),1,problems)
        self.assertEqual(draft['objects'][0]['formula']['native_numeric_evidence'],receipt)

    def test_flat_or_subscript_text_cannot_create_receipt(self):
        for options in ({'rule':False},{'raised':False}):
            source,text,p=self.fixture(**options)
            self.assertIsNone(create_native_binding(source,p));self.assertEqual(untraced_numbers(p,text),['2'])

    def test_wrong_exponent_or_coefficient_not_bound(self):
        source,text,p=self.fixture()
        for notation in ('Fo = alpha*t/(L**3)', 'Fo = alpha*t/(2*L)'):
            p['formula']['normalized_notation']=notation
            self.assertIsNone(create_native_binding(source,p));self.assertTrue(untraced_numbers(p,text))

    def test_tampered_stale_and_missing_receipts_fail_closed(self):
        source,text,p=self.fixture();receipt=create_native_binding(source,p);self.assertIsNotNone(receipt)
        p['formula']['native_numeric_evidence']=receipt
        for field in ('source_sha256','rule_sha256','glyph_sha256','receipt_sha256'):
            wrong=copy.deepcopy(p);wrong['formula']['native_numeric_evidence'][field]='0'*64
            with self.assertRaises(NumericEvidenceError):checked_untraced_numbers(wrong,text,source)
        wrong=copy.deepcopy(p);wrong['formula']['normalized_notation']='Fo = alpha*t/(L**3)'
        with self.assertRaises(NumericEvidenceError):checked_untraced_numbers(wrong,text,source)
        wrong=copy.deepcopy(p);wrong['evidence_spans'][0]['start']=1
        with self.assertRaises(NumericEvidenceError):checked_untraced_numbers(wrong,text,source)
        p['formula'].pop('native_numeric_evidence');self.assertEqual(checked_untraced_numbers(p,text,source),['2'])
        source.write_bytes(source.read_bytes()+b'\n% source drift\n');p['formula']['native_numeric_evidence']=receipt
        with self.assertRaises(NumericEvidenceError):checked_untraced_numbers(p,text,source)

    def test_workpack_build_and_anchor_replay_reject_drift(self):
        import json
        from _support import pdftoppm, run_cli
        from pdf_structure import compile_pdf_intermediate_representation, write_intermediate_representation
        from semantic_promotion import build_semantic_workpack, validate_semantic_workpack
        from direct_reference_skill import build_direct_reference, DirectReferenceError
        source,text,p=self.fixture()
        ir=self.tmp/'ir';wp=self.tmp/'wp'
        write_intermediate_representation(ir,compile_pdf_intermediate_representation(source,1,None))
        manifest=build_semantic_workpack(source,ir,wp,pdftoppm=Path(pdftoppm()))
        tasks={r['id']:r for r in map(json.loads,(wp/'visual-tasks.jsonl').read_text().splitlines())}
        for entry in manifest['units']:
            unit=json.loads((wp/entry['path']).read_text())
            response={'objects':[], 'disposition':'context-only','reason':'Synthetic context'}
            if any('t* = at L 2 (1-1)' in span['text'] for span in unit['focus_spans']):
                response={'objects':[{**p,'id':'time','evidence':['t* = at L 2 (1-1)']}]}
            draft,problems=_ingest_unit(unit,response,tasks,'test-proposer',source=source)
            (wp/'drafts'/f"{unit['unit_id']}.json").write_text(json.dumps(draft))
        issues,_=validate_semantic_workpack(source,wp)
        self.assertFalse([i for i in issues if i.severity=='error'],issues)
        package=self.tmp/'fake-time'
        build_direct_reference(source,wp,package,name='time',display_name='Synthetic time',domain='synthetic',version='0.1.0',created_at='2026-10-15T00:00:00+00:00')
        result=run_cli('verify_source_anchors.py',str(package),'--source',str(source),'--json')
        self.assertEqual(json.loads(result.stdout)['native_formula_binding_count'],1)
        objpath=next((package/'references/knowledge/objects').glob('*.json'))
        obj=json.loads(objpath.read_text())
        from numeric_evidence import check_legacy_numeric
        anchors=[json.loads(line) for line in (package/'references/evidence/anchors.jsonl').read_text().splitlines()]
        package_manifest=json.loads((package/'manifest.json').read_text())
        check_legacy_numeric({'objects':[obj],'anchors':anchors},package_manifest,source)
        obj['formula']['native_numeric_evidence']['glyph_sha256']='0'*64
        with self.assertRaises(NumericEvidenceError):
            check_legacy_numeric({'objects':[obj],'anchors':anchors},package_manifest,source)
        objpath.write_text(json.dumps(obj))
        self.assertNotEqual(run_cli('verify_source_anchors.py',str(package),'--source',str(source),'--json',check=False).returncode,0)
        for draftpath in (wp/'drafts').glob('*.json'):
            draft=json.loads(draftpath.read_text())
            if not draft['objects']:continue
            draft['objects'][0]['formula']['native_numeric_evidence']['glyph_sha256']='0'*64
            draftpath.write_text(json.dumps(draft))
        issues,_=validate_semantic_workpack(source,wp)
        self.assertIn('formula_numeric_binding_invalid',{i.code for i in issues})
        with self.assertRaisesRegex(DirectReferenceError,'formula_numeric_binding_invalid'):
            build_direct_reference(source,wp,self.tmp/'fake-wrong',name='wrong',display_name='Wrong',domain='synthetic',version='0.1.0',created_at='2026-10-15T00:00:00+00:00')

    @unittest.skipUnless(os.environ.get('FAKE_EXPERT_HEAT_CONDUCTION_PDF'),'private real PDF not supplied')
    def test_real_fourier_number(self):
        source=Path(os.environ['FAKE_EXPERT_HEAT_CONDUCTION_PDF']);text=normalize_text(PdfReader(source).pages[42].extract_text())
        quote='t∗ = αt L2 (1-67b)';start=text.index(quote)
        p={'formula':{'source_notation':quote,'normalized_notation':'Fo = alpha*t/(L**2)',
            'notation_symbols':{'Fo':'t*','alpha':'α'}}, 'evidence_spans':[{'page':43,'start':start,'end':start+len(quote),'span_sha256':hashlib.sha256(quote.encode()).hexdigest()}]}
        receipt=create_native_binding(source,p);self.assertIsNotNone(receipt)
        p['formula']['native_numeric_evidence']=receipt;self.assertEqual(checked_untraced_numbers(p,quote,source),[])
        p['formula']['normalized_notation']='Fo = alpha*t/(L**3)'
        with self.assertRaises(NumericEvidenceError):checked_untraced_numbers(p,quote,source)
