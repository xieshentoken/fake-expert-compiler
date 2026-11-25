"""Synthetic page-role, scoped intake, title lookup and read-only reuse regressions."""
from __future__ import annotations
import argparse
import copy
import subprocess
import sys
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import _support
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
from reportlab.pdfgen import canvas
import direct_reference_skill as direct
import intake_workflow as workflow
from intake_workpack_reuse import inventory
from native_blank_pages import diagnose_blank_page
from native_intake import inspect_source
from native_page_disposition import prepare, import_submission, load_receipt, structural_proof, verify_package_dispositions
from pdf_structure import compile_pdf_intermediate_representation, write_intermediate_representation
from semantic_promotion import build_semantic_workpack, validate_semantic_workpack
from verify_pdf_ir import verify_pdf_ir
import test_native_blank_pages as blank_tests
from test_reference_search import object_entry, figure_entry, catalog
import reference_search as search


@unittest.skipUnless(shutil.which('pdftoppm'),'canonical local renderer required')
class PageFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.source=self.root/'source.pdf'
        pdf=canvas.Canvas(str(self.source),pagesize=(300,400))
        for n,title in enumerate(('Cover','1 Input systems','Part 2 Output',None,'2 Output systems','License'),1):
            if title:
                pdf.setFont('Helvetica',12)
                pdf.drawString(20,360,title)
                if n in (2,3,5):
                    pdf.bookmarkPage(str(n));pdf.addOutlineEntry(title,str(n),0)
                if n in (2,5):
                    pdf.drawString(20,330,'A bounded system accepts a controlled input.')
                    pdf.drawString(20,310,'The system reports a stable output within its scope.')
            pdf.showPage()
        pdf.save()
        reader=PdfReader(self.source);writer=PdfWriter();writer.clone_document_from_reader(reader)
        stream=DecodedStreamObject();stream.set_data(b'q 0 400 300 -400 re W n q 1 0 0 1 -8 400 cm .5 w 0 0 m -20 0 l S Q Q')
        writer.pages[3][NameObject('/Contents')]=writer._add_object(stream)
        writer.write(self.source)
        self.original=self.source.read_bytes()

    def tearDown(self):
        self.assertEqual(self.source.read_bytes(),self.original)
        self.temp.cleanup()

    def args(self,name,**extra):
        values=dict(source=self.source,workspace=self.root/name,name='fixture',display_name='Fixture',domain='fixture',version='0.1.0',created_at='2026-01-01T00:00:00Z',figures='none',start_page=2,end_page=5,workpack=[])
        values.update(extra)
        return argparse.Namespace(**values)

    def complete(self,start=2,end=5,name='workpack',page_dispositions=None):
        if page_dispositions:
            compile_original=compile_pdf_intermediate_representation
            def compile_with_receipt(*args,**kwargs):
                return compile_original(*args,**kwargs,page_dispositions=page_dispositions)
            with patch('test_native_blank_pages.compile_pdf_intermediate_representation',side_effect=compile_with_receipt):
                return blank_tests.NativeBlankPageTests.workpack(self,start,end,name)[0]
        return blank_tests.NativeBlankPageTests.workpack(self,start,end,name)[0]

    def test_scope_retains_full_ledger_and_structural_page_has_no_evidence(self):
        report=inspect_source(self.source,start_page=2,end_page=5)
        self.assertEqual(len(report['page_ledger']),6)
        self.assertEqual(report['native_ranges'],[[2,5]])
        self.assertEqual(report['selection']['outside_pages'],[1,6])
        self.assertEqual(report['page_ledger'][2]['page_role'],'structural')
        self.assertFalse(report['native_whole_source'])
        workpack=self.complete()
        manifest=json.loads((workpack/'workpack.json').read_text())
        for entry in manifest['units']:
            unit=json.loads((workpack/entry['path']).read_text())
            self.assertFalse(any(span['page'] in (3,4) for span in unit['focus_spans']))
        issues,_=validate_semantic_workpack(self.source,workpack)
        self.assertFalse([i for i in issues if i.severity=='error'],issues)
        plan=workflow.plan(self.args('plan'))
        self.assertEqual(plan['route'],'native')
        self.assertFalse(plan['whole_book_complete'])
        self.assertEqual(plan['ocr_availability']['status'],'not_required')

    def test_structural_title_rejects_graphics_hidden_white_and_outside_text(self):
        for suffix in (b'0 0 m 100 100 l S',b'0 0 10 10 re f',b'newunknown',b''):
            with self.subTest(suffix=suffix):
                writer=PdfWriter();writer.clone_document_from_reader(PdfReader(self.source))
                p=writer.pages[2];data=p.get_contents().get_data()+b'\n'+suffix
                if not suffix:
                    data=data.replace(b'BT',b'BT 3 Tr')
                stream=DecodedStreamObject();stream.set_data(data);p[NameObject('/Contents')]=writer._add_object(stream)
                altered=self.root/('altered-'+hashlib.sha256(suffix).hexdigest()[:5]+'.pdf');writer.write(altered)
                self.assertIsNone(structural_proof(altered,3))
        proof=structural_proof(self.source,3)
        self.assertNotEqual(proof['binding']['content_sha256'],hashlib.sha256(b'').hexdigest())

    def test_print_mark_proof_handles_transforms_negative_rectangles_and_rejects_interior(self):
        page=PdfReader(self.source).pages[3]
        proof=diagnose_blank_page(page,'',self.source,hashlib.sha256(self.original).hexdigest(),4)
        self.assertEqual(proof['proof']['protocol'],'native-outside-print-marks-v0.1')
        for content in (b'0 0 m 1 1 l S',b'q 10 0 0 10 0 0 cm 5 w -1 0 m -1 20 l S Q',b'q /Unknown gs -30 0 m -20 0 l S Q',b'0 0 1 1 re f'):
            writer=PdfWriter();writer.clone_document_from_reader(PdfReader(self.source))
            stream=DecodedStreamObject();stream.set_data(content);writer.pages[3][NameObject('/Contents')]=writer._add_object(stream)
            target=self.root/'negative.pdf';writer.write(target)
            diagnostic=diagnose_blank_page(PdfReader(target).pages[3],'',target,hashlib.sha256(target.read_bytes()).hexdigest(),4)
            self.assertNotEqual(diagnostic['classification'],'confirmed_blank')

    def test_operator_receipt_is_bound_replayed_and_excluded_from_knowledge(self):
        submission=prepare(self.source,[6])
        self.assertIsNone(submission['operator_id'])
        self.assertIsNone(submission['items'][0]['role'])
        with self.assertRaisesRegex(ValueError,'operator_required'):
            import_submission(self.source,submission,None)
        submission.update(operator_id='synthetic-operator-fixture',operator_attestation='Synthetic fixture only: inspected the complete page.')
        submission['items'][0].update(role='context',reason='Synthetic license context, excluded from knowledge.')
        receipt=import_submission(self.source,submission,'synthetic-operator-fixture')
        path=self.root/'receipt.json';path.write_text(json.dumps(receipt))
        cli=subprocess.run([sys.executable,str(Path(_support.__file__).parents[1]/'fake-expert/scripts/reconstruct_pdf.py'),str(self.source),'--start-page','2','--end-page','6','--page-dispositions',str(path),'--output',str(self.root/'cli-receipt-ir'),'--json'],capture_output=True,text=True)
        self.assertEqual(cli.returncode,0,cli.stdout+cli.stderr)
        cli_ir=json.loads((self.root/'cli-receipt-ir/preflight.json').read_text())
        self.assertTrue(next(row for row in cli_ir['pages'] if row['physical_page']==6).get('page_disposition_proof'))
        ir=compile_pdf_intermediate_representation(self.source,2,6,page_dispositions=path)
        ir_root=self.root/'receipt-ir';write_intermediate_representation(ir_root,ir)
        self.assertTrue(verify_pdf_ir(self.source,ir_root,pdftoppm=None,docling_allow_network=False)['passed'])
        workpack=self.root/'receipt-workpack';manifest=build_semantic_workpack(self.source,ir_root,workpack,semantic_assurance=False)
        for entry in manifest['units']:
            unit=json.loads((workpack/entry['path']).read_text())
            self.assertNotIn(6,[s['page'] for s in unit['focus_spans']])
        issues,_=validate_semantic_workpack(self.source,workpack,require_complete_drafts=False)
        self.assertFalse([i for i in issues if i.severity=='error'],issues)
        for mutation in ('duplicate','render','operator','blank','reason','source'):
            altered=copy.deepcopy(submission)
            if mutation=='duplicate':altered['items']*=2
            if mutation=='render':altered['items'][0]['binding']['render']['sha256']='0'*64
            if mutation=='operator':altered['operator_id']='different'
            if mutation=='blank':altered['items'][0]['role']='blank'
            if mutation=='reason':altered['items'][0]['reason']=''
            if mutation=='source':altered['source_sha256']='0'*64
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                import_submission(self.source,altered,'synthetic-operator-fixture')
        tampered=copy.deepcopy(receipt);tampered['submission']['items'][0]['reason']='changed'
        with self.assertRaisesRegex(ValueError,'receipt_invalid'):
            load_receipt(self.source,tampered)

    def test_operator_receipts_survive_build_extend_and_require_explicit_replay_input(self):
        submission=prepare(self.source,[4,6])
        submission.update(operator_id='synthetic-operator-fixture',operator_attestation='Synthetic fixture complete-page inspection.')
        for item in submission['items']:
            item.update(role='blank' if item['binding']['physical_page']==4 else 'context',reason='Synthetic page-role decision.')
        receipt=import_submission(self.source,submission,'synthetic-operator-fixture')
        path=self.root/'manual-receipt.json';path.write_text(json.dumps(receipt))
        first=self.complete(2,4,'first',page_dispositions=path)
        second=self.complete(5,6,'second',page_dispositions=path)
        base=self.root/'base';extended=self.root/'extended'
        direct.build_direct_reference(self.source,first,base,name='fixture',display_name='Fixture',domain='fixture',version='0.1.0',created_at='2026-01-01T00:00:00Z')
        before=inventory(base)
        direct.extend_direct_reference(base,self.source,second,extended,version='0.2.0',created_at='2026-01-01T00:00:00Z')
        self.assertEqual(inventory(base),before)
        self.assertEqual(direct.validate_direct_reference(extended),[])
        missing=verify_package_dispositions(self.source,extended)
        self.assertTrue(any(row.get('error')=='page_disposition_receipt_required' for row in missing))
        replayed=verify_package_dispositions(self.source,extended,[path])
        self.assertEqual({row['physical_page'] for row in replayed},{3,4,6})
        self.assertTrue(all(row['passed'] and row['full_replay'] for row in replayed),replayed)
        cli=subprocess.run([sys.executable,str(Path(_support.__file__).parents[1]/'fake-expert/scripts/verify_source_anchors.py'),str(extended),'--source',str(self.source),'--page-dispositions',str(path),'--json'],capture_output=True,text=True)
        self.assertEqual(cli.returncode,0,cli.stdout+cli.stderr)
        # Removing a proof, while retaining its disposition role, fails both
        # workpack integrity and IR replay even after normal component rehashing.
        index=first/'page-index.jsonl';rows=[json.loads(line) for line in index.read_text().splitlines()]
        for row in rows:
            if row['page']==4:row.pop('page_disposition_proof')
        index.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        issues,_=validate_semantic_workpack(self.source,first)
        self.assertTrue(any(i.severity=='error' and 'blank_page_proof' in i.code for i in issues),issues)
        ir=compile_pdf_intermediate_representation(self.source,2,6,page_dispositions=path)
        for row in ir['preflight']['pages']:
            if row['physical_page']==6:row.pop('page_disposition_proof')
        from pdf_parser_adapters import sha256_json
        ir['source']['pdf_ir_receipts']['preflight_sha256']=sha256_json(ir['preflight'])
        destination=self.root/'tampered-ir';write_intermediate_representation(destination,ir)
        result=verify_pdf_ir(self.source,destination,pdftoppm=None,docling_allow_network=False)
        self.assertFalse(result['passed'])
        self.assertIn('page_disposition_proof_missing:page=6',result['failures'])

    def test_complete_workpack_reuse_is_read_only_and_skips_extraction_authoring(self):
        workpack=self.complete();before=inventory(workpack)
        plan=workflow.plan(self.args('reuse',workpack=[workpack]))
        with patch('scoped_native_workflow.compile_pdf_intermediate_representation',side_effect=AssertionError('must not extract')),patch('scoped_native_workflow.prepare',side_effect=AssertionError('must not prepare')):
            result=workflow.run(Path(plan['workflow']))
        self.assertEqual(result['status'],'draft',result)
        self.assertEqual(inventory(workpack),before)
        self.assertEqual(direct.validate_direct_reference(Path(result['children'][0]['package'])),[])
        self.assertFalse((Path(plan['workflow']).parent/'children/pages-0002-0005/ir').exists())
        # A same-source scope mismatch is explicit and produces no workspace.
        with self.assertRaisesRegex(ValueError,'scope_gap'):
            workflow.plan(self.args('wrongscope',workpack=[workpack],end_page=6))
        with self.assertRaisesRegex(ValueError,'overlap'):
            workflow.plan(self.args('overlap',workpack=[workpack,workpack]))
        self.assertFalse((self.root/'wrongscope').exists())
        (workpack/'drafts'/'unexpected.json').write_text('{}')
        rejected=workflow.status(Path(plan['workflow']))
        self.assertEqual(rejected['code'],'intake_binding_drift_replan')

    def test_incomplete_workpack_is_preserved_and_reports_authoring_pause(self):
        workpack=self.complete()
        for path in (workpack/'drafts').glob('*.json'):path.unlink()
        before=inventory(workpack)
        plan=workflow.plan(self.args('incomplete',workpack=[workpack]))
        with patch('scoped_native_workflow.prepare',side_effect=AssertionError('must not author')):
            result=workflow.run(Path(plan['workflow']))
        self.assertEqual(result['children'][0]['code'],'semantic_authoring_required')
        self.assertEqual(result['children'][0]['workpack'],str(workpack.resolve()))
        self.assertEqual(inventory(workpack),before)

    def test_auto_entry_points_bind_term_aliases_and_reuse_workpack(self):
        workpack = self.complete()
        before = inventory(workpack)
        alias_path = self.root / "aliases.json"
        alias_path.write_text(json.dumps({"schema_version": "tkc.reference-term-aliases/v0.1",
            "terms": [{"term": "fixture_system", "aliases": ["bounded system", "受限夹具系统"]}]}))
        script = _support.SCRIPTS / "fake_expert.py"
        common = ("--source", str(self.source), "--start-page", "2", "--end-page", "5", "--figures", "none",
                  "--workpack", str(workpack), "--term-aliases", str(alias_path), "--created-at", "2026-01-01T00:00:00Z", "--json")
        plans = []
        for words in (("plan",), ("intake", "plan")):
            folder = self.root / ("-".join(words))
            output = subprocess.check_output([sys.executable, "-B", str(script), *words, "--workspace", str(folder), *common])
            plan = json.loads(output)
            self.assertEqual(plan["term_aliases_sha256"], hashlib.sha256(alias_path.read_bytes()).hexdigest())
            plans.append(Path(plan["workflow"]))
        alias_path.write_text(alias_path.read_text() + "\n")
        self.assertIn("term_aliases_binding_drift", workflow.run(plans[0])["drift"])
        self.assertFalse((plans[0].parent / "children").exists())
        alias_path.write_text(alias_path.read_text().rstrip())
        result = json.loads(subprocess.check_output([sys.executable, "-B", str(script), "resume", "--workflow", str(plans[1]), "--json"]))
        self.assertEqual(result["status"], "draft", result)
        draft = Path(result["children"][0]["package"])
        query = json.loads(subprocess.check_output([sys.executable, "-B", str(draft / "scripts/query_reference.py"), "--query", "受限夹具系统"]))
        self.assertTrue(query["matched_object_ids"])
        self.assertEqual(inventory(workpack), before)
        self.assertNotIn(str(alias_path), (draft / "references/runtime/catalog.json").read_text())
        alias_path.write_text("{}")
        with self.assertRaisesRegex(ValueError, "reference_alias_profile_invalid"):
            workflow.plan(self.args("invalid", workpack=[workpack], term_aliases=alias_path))
        self.assertFalse((self.root / "invalid").exists())

    def test_titles_with_decision_words_build_and_precheck_without_weakening_guard(self):
        workpack=self.complete()
        for index,path in enumerate(sorted((workpack/'drafts').glob('*.json'))):
            value=json.loads(path.read_text())
            for obj in value['objects']:obj['title']='This removal method is acceptable' if index%2 else 'Recommended cleaning methods'
            path.write_text(json.dumps(value))
        checked=direct.precheck_workpack(self.source,workpack)
        self.assertTrue(checked['objects'])
        output=self.root/'draft'
        direct.build_direct_reference(self.source,workpack,output,name='fixture',display_name='Fixture',domain='fixture',version='0.1.0',created_at='2026-01-01T00:00:00Z')
        self.assertEqual(direct.validate_direct_reference(output),[])
        control_path=output/'references/runtime/direct-reference.json'
        control=json.loads(control_path.read_text())
        for binding in control['workpack_bindings']:
            binding.pop('page_disposition_protocol');binding.pop('page_dispositions')
        control_path.write_text(json.dumps(control))
        self.assertIn('page_disposition_binding_required',direct.validate_direct_reference(output))


class TitleLookupTests(unittest.TestCase):
    def test_lookup_preserves_figures_and_large_duplicate_title_selfcheck(self):
        title='Approve the operation'
        objects=[object_entry('ko-'+str(i),title,'A bounded system reports an output.',figure_refs=['vo-x']) for i in range(30)]
        cat=catalog(objects,[figure_entry('vo-x','Figure 1','A bounded system',['ko-29'])])
        response=search.query_package(cat,title,lookup_title=True,lookup_object_id='ko-29')
        self.assertEqual(response['matched_object_ids'],['ko-29'])
        self.assertEqual(response['figures'][0]['id'],'vo-x')
        self.assertEqual(search.query_package(cat,title)['behavior'],'abstain')
        self.assertEqual(search.query_package(cat,'Recommend a cleaning method for me')['behavior'],'abstain')
        self.assertEqual(search.query_package(cat,'This removal method is acceptable')['behavior'],'abstain')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'scripts').mkdir();shutil.copyfile(search.__file__,root/'scripts/query_reference.py')
            self.assertTrue(direct._search_runtime_self_check(root,cat)['passed'])
