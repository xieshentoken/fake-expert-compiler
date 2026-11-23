"""Actual subprocess routes with synthetic local input; no Review or Gold."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from science.support import ROOT, SKILL_ROOT,sc,profile
from science.m4_support import suite,observations,tokenizer
import science_index as si
import compiler_version as versions


class ScienceLifecycleCLITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name).resolve()

    def tearDown(self):self.temp.cleanup()

    def run_cli(self,script,*arguments):
        return subprocess.run([sys.executable,'-B',str(SKILL_ROOT/'scripts'/script),*map(str,arguments)],capture_output=True,text=True)

    def write(self,name,value):
        path=self.root/name; path.write_text(json.dumps(value)); return path

    def test_domain_export_is_new_file_only_and_cannot_change_compiler(self):
        domain=SKILL_ROOT/'assets/domain-profiles/heat-transfer.json'; output=self.root/'profile.json'
        args=('domain-config','--domain',domain,'--part','profile','--output',output)
        result=self.run_cli('science_workpack.py',*args); self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(output.read_text())['profile_id'],'heat-transfer')
        original=output.read_bytes(); self.assertEqual(self.run_cli('science_workpack.py',*args).returncode,2)
        self.assertEqual(output.read_bytes(),original)
        result=self.run_cli('science_workpack.py','domain-config','--domain',domain,'--part','profile','--output',domain.parent/'forbidden.json')
        self.assertEqual(result.returncode,2); self.assertFalse((domain.parent/'forbidden.json').exists())

    def test_report_writes_denominators_but_exits_blocked(self):
        s=suite(); suite_path=self.write('suite.json',s); results=self.write('results.json',observations(s)); output=self.root/'report.json'
        result=self.run_cli('science_acceptance.py','report','--suite',suite_path,'--results',results,'--output',output)
        self.assertEqual(result.returncode,2,result.stderr); report=json.loads(output.read_text())
        self.assertEqual(sum(r['planned'] for r in report['layers']),7); self.assertIsNone(report['domain_accuracy'])
        self.assertEqual(output.stat().st_mode & 0o777,0o600)

    def test_synthetic_blind_cli_rejects_without_writing(self):
        s=suite(); s['cases'][0]['split']='blind'; source=self.write('suite.json',s); output=self.root/'blind.json'
        result=self.run_cli('science_acceptance.py','blind-plan','--suite',source,'--output',output)
        self.assertEqual(result.returncode,2); self.assertFalse(output.exists())

    def test_versioned_snapshot_enters_original_incremental_cli(self):
        try:import test_direct_reference_skill as legacy
        except ImportError as error:raise unittest.SkipTest('Synthetic PDF dependency unavailable') from error
        fixture=legacy.DirectReferenceSkillTests(); fixture.setUp()
        try:
            base=fixture.root.resolve()/'science-m4-synthetic'; fixture._build(base)
            before={str(p.relative_to(base)):hashlib.sha256(p.read_bytes()).hexdigest() for p in base.rglob('*') if p.is_file()}
            prof=self.write('profile.json',profile()); tk=tokenizer(); token=self.write('tokenizer.json',tk)
            config=self.write('library.json',{'schema_version':versions.SCIENCE_LIBRARY_SCHEMA,'packages':[{'package':str(base),'profile':str(prof),'sidecar':None}],'tokenizer':tk})
            lib,_=si.load_library(config); index=self.write('index.json',si.build_index(lib,tk)); old=self.root/'old.json'
            result=self.run_cli('pipeline_orchestrator.py','science-snapshot','--library',config,'--tokenizer',token,'--index',index,
                '--created-at','2026-09-19T00:00:00Z','--output',old,'--json')
            self.assertEqual(result.returncode,0,result.stderr)
            tk['aliases'].append({'phrase':'保温','terms':['insulation']}); token=self.write('tokenizer2.json',tk)
            index=self.write('index2.json',si.build_index(lib,tk)); new=self.root/'new.json'
            result=self.run_cli('pipeline_orchestrator.py','science-snapshot','--library',config,'--tokenizer',token,'--index',index,
                '--created-at','2026-09-19T00:00:00Z','--output',new,'--json')
            self.assertEqual(result.returncode,0,result.stderr)
            result=self.run_cli('pipeline_orchestrator.py','incremental-plan','--job',self.root/'job.json','--old',old,'--new',new,
                '--output',self.root/'comparison','--created-at','2026-09-19T00:00:00Z','--json')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(before,{str(p.relative_to(base)):hashlib.sha256(p.read_bytes()).hexdigest() for p in base.rglob('*') if p.is_file()})
        finally:fixture.tearDown()
