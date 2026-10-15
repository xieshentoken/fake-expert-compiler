"""New native Drafts carry portable local images; text-only jobs remain explicit."""
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import unittest

from _support import HEAT_BOOK, FAKE_MODEL, CREATED_AT, query_package, run_json, run_cli, requires_renderer


@requires_renderer
class PrivateImageDefaultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory(prefix='private-image-default-')
        cls.root=Path(cls.temporary.name);cls.job=cls.root/'job'
        run_json('fake_expert.py','plan','--source',str(HEAT_BOOK),'--workspace',str(cls.job),
                 '--profile','native-text','--json')
        run_json('fake_expert.py','compile','--job',str(cls.job/'fake-expert-job.json'),'--json')
        cls.wp=cls.job/'extraction/workpack'
        run_json('semantic_authoring.py','run','--workpack',str(cls.wp),'--source',str(HEAT_BOOK),
                 '--command',shlex.join([sys.executable,str(FAKE_MODEL),'--text-only-values']),
                 '--proposer-instance','synthetic-test-only','--json')
        cls.imaged=cls.root/'fake-images';cls.text=cls.root/'fake-text'
        cls.imaged_result=cls.build(cls.imaged)
        cls.text_result=cls.build(cls.text,'--figures','none')

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @classmethod
    def build(cls,output,*extra,workpack=None,check=True):
        args=('build','--source',str(HEAT_BOOK),'--workpack',str(workpack or cls.wp),'--output',str(output),
            '--name',output.name.removeprefix('fake-'),'--display-name','Synthetic images','--domain','heat-transfer',
            '--version','0.1.0','--created-at',CREATED_AT,*extra)
        return run_json('direct_reference_skill.py',*args) if check else run_cli('direct_reference_skill.py',*args,check=False)

    def test_default_plan_freezes_images_and_query_returns_existing_image(self):
        job=json.loads((self.job/'fake-expert-job.json').read_text())
        self.assertEqual(job['package']['figures'],'index+images')
        self.assertGreater(self.imaged_result['image_count'],0)
        self.assertEqual(self.imaged_result['usage_scope'],'private-source-derived')
        response=query_package(self.imaged,'copper conductivity peak')
        self.assertTrue(response['figures'])
        self.assertFalse(response['assurance']['knowledge_verified'])
        from PIL import Image
        index=json.loads((self.imaged/'references/figures/index.json').read_text())
        by_id={row['id']:row for row in index['figures']}
        for figure in response['figures']:
            image=(self.imaged/figure['image']).resolve()
            self.assertTrue(image.is_relative_to((self.imaged/'assets/figures').resolve()))
            self.assertEqual(hashlib.sha256(image.read_bytes()).hexdigest(),by_id[figure['id']]['image_sha256'])
            with Image.open(image) as bitmap:bitmap.verify()
        self.assertEqual(query_package(self.imaged,'certify a safe design for me')['figures'],[])

    def test_figure_only_query_returns_image_without_fabricating_facts(self):
        response=query_package(self.imaged,'Figure 1.1')
        self.assertEqual(response['behavior'],'abstain')
        self.assertIn('figure_candidates_only',response['reason_codes'])
        self.assertEqual(response['results'],[])
        self.assertTrue(response['figures'])
        self.assertTrue(all((self.imaged/row['image']).is_file() for row in response['figures']))

    def test_explicit_text_only_and_legacy_job_selection_stay_text_only(self):
        self.assertEqual(self.text_result['figure_count'],0)
        self.assertEqual(self.text_result['image_count'],0)
        self.assertFalse((self.text/'assets/figures').exists())
        self.assertEqual(query_package(self.text,'copper conductivity peak')['figures'],[])
        workspace=self.root/'legacy-text-job'
        run_json('fake_expert.py','plan','--source',str(HEAT_BOOK),'--workspace',str(workspace),
                 '--profile','native-text','--figures','none','--workpack',str(self.wp),'--json')
        job=json.loads((workspace/'fake-expert-job.json').read_text())
        self.assertNotIn('figures',job['package'])
        result=run_json('fake_expert.py','compile','--job',str(workspace/'fake-expert-job.json'),'--json')
        self.assertEqual(result['direct_reference']['figure_count'],0)

    def test_explicit_index_and_legacy_index_flags_keep_images_optional(self):
        for flag in (('--figures','index'),('--figure-index',)):
            output=self.root/('fake-index-'+str(len(flag)))
            result=self.build(output,*flag)
            self.assertGreater(result['figure_count'],0)
            self.assertEqual(result['image_count'],0)
            self.assertFalse((output/'assets/figures').exists())
        bad=self.build(self.root/'fake-conflict','--figures','none','--figure-index',check=False)
        self.assertNotEqual(bad.returncode,0)

    def test_missing_crop_does_not_silently_build_without_images(self):
        workpack=self.root/'missing-crop';shutil.copytree(self.wp,workpack)
        next((workpack/'crops').glob('*.png')).unlink()
        result=self.build(self.root/'fake-missing',workpack=workpack,check=False)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'fake-missing/manifest.json').exists())
