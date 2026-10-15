"""P2 extraction checkpoints, explicit structure recovery and model-command failures."""
import hashlib
import json
from pathlib import Path
import shlex
import sys

from _support import HEAT_BOOK, TempDirTestCase, run_json, run_cli, requires_renderer


@requires_renderer
class ExtractionRecoveryTests(TempDirTestCase):
    def plan(self,source=HEAT_BOOK,*extra):
        workspace=self.tmp/"含 空格 job"
        run_json('fake_expert.py','plan','--source',str(source),'--workspace',str(workspace),
                 '--profile','native-text',*extra,'--json')
        return workspace,workspace/'fake-expert-job.json'

    def test_no_headings_pause_repeatedly_and_explicit_boundaries_recover(self):
        from reportlab.pdfgen import canvas
        from pypdf import PdfReader
        from pdf_structure import normalize_text
        source=self.tmp/'without-headings.pdf'
        c=canvas.Canvas(str(source))
        c.drawString(60,700,'A source paragraph without numbered headings.')
        c.drawString(60,670,'This synthetic input contains enough native text for extraction.')
        c.save()
        workspace,job=self.plan(source)
        for action in ('compile','resume'):
            result=run_json('fake_expert.py',action,'--job',str(job),'--json')
            self.assertEqual(result['code'],'structure_review_required')
            self.assertTrue(Path(result['retained_extraction']).is_dir())
        self.assertFalse((workspace/'extraction').exists())
        text=normalize_text(PdfReader(source).pages[0].extract_text())
        title='A source paragraph without numbered headings.'
        selection=self.tmp/'boundaries.json'
        selection.write_text(json.dumps({'schema_version':'tkc.native-structure-selections/v0.1',
            'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'boundaries':[{'page':1,'start':0,'end':len(title),'span_sha256':hashlib.sha256(title.encode()).hexdigest()}]}))
        recovered=self.tmp/'recovered'
        run_json('fake_expert.py','plan','--source',str(source),'--workspace',str(recovered),
                 '--profile','native-text','--structure-selections',str(selection),'--json')
        result=run_json('fake_expert.py','compile','--job',str(recovered/'fake-expert-job.json'),'--json')
        self.assertEqual(result['code'],'semantic_authoring_required')
        self.assertEqual(result['extraction_run']['unit_count'],1)
        selection.write_text('{}')
        drift=json.loads(run_cli('fake_expert.py','status','--job',str(recovered/'fake-expert-job.json'),'--json',check=False).stdout)
        self.assertIn('structure_selections_hash_mismatch',drift['drift'])

    def test_partial_extraction_is_preserved_and_retry_succeeds(self):
        workspace,job=self.plan()
        old=workspace/'extraction/ir/interrupted.txt';old.parent.mkdir(parents=True);old.write_text('retained')
        result=run_json('fake_expert.py','resume','--job',str(job),'--json')
        self.assertEqual(result['code'],'semantic_authoring_required')
        self.assertEqual(next(workspace.glob('extraction-incomplete-*/ir/interrupted.txt')).read_text(),'retained')

    def test_checkpoint_drift_is_rejected_and_status_is_readonly(self):
        workspace,job=self.plan()
        result=run_json('fake_expert.py','compile','--job',str(job),'--json')
        def files():return {str(p.relative_to(workspace)):hashlib.sha256(p.read_bytes()).hexdigest() for p in workspace.rglob('*') if p.is_file()}
        before=files()
        run_cli('fake_expert.py','status','--job',str(job),'--json',check=False)
        run_json('fake_expert.py','compile','--job',str(job),'--dry-run','--json')
        self.assertEqual(files(),before)
        meaning=result['next_action']['meaning']
        command=meaning.split(': ',1)[1].split(' (or write drafts',1)[0]
        argv=shlex.split(command)
        self.assertIn(str((workspace/'extraction/workpack').resolve()),argv)
        self.assertIn(str(HEAT_BOOK),argv)
        ir=workspace/'extraction/ir/document-map.json';ir.write_text('{}')
        drift=json.loads(run_cli('fake_expert.py','status','--job',str(job),'--json',check=False).stdout)
        self.assertEqual(drift['code'],'extraction_workpack_mismatch')

    def test_bad_model_commands_fail_and_existing_drafts_survive(self):
        workspace,job=self.plan()
        run_json('fake_expert.py','compile','--job',str(job),'--json')
        wp=workspace/'extraction/workpack'
        unit=json.loads((wp/'workpack.json').read_text())['units'][0]['unit_id']
        draft=wp/'drafts'/f'{unit}.json';draft.write_text('{"user":"unsaved draft"}')
        for code in ('import sys;sys.exit(7)','print("no JSON")','print("{\\\"objects\\\": 7}")','import time;time.sleep(2)'):
            with self.subTest(code=code):
                result=run_cli('semantic_authoring.py','run','--workpack',str(wp),'--unit',unit,
                    '--command',shlex.join([sys.executable,'-c',code]),'--proposer-instance','test',
                    '--timeout','1','--rerun','--json',check=False)
                self.assertNotEqual(result.returncode,0,result.stdout)
                self.assertEqual(draft.read_text(),'{"user":"unsaved draft"}')
