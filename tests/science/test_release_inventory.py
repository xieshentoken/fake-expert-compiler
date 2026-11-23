import unittest
import json
import tempfile
import shutil
from pathlib import Path
from science.support import ROOT, SKILL_ROOT
import compiler_version as versions
import package_fake_expert_skill as package
import verify_fake_expert_release as verifier


class ScienceReleaseInventoryTests(unittest.TestCase):
    def test_science_candidate_registered_without_changing_formal_version(self):
        self.assertEqual(versions.RELEASE_VERSION,'1.2.0')
        self.assertIn(versions.SCIENCE_LIFECYCLE_COMPILER_VERSION,package.SOURCE_CANDIDATE_VERSIONS)
        self.assertIn(versions.SCIENCE_LIFECYCLE_COMPILER_VERSION,verifier.CANDIDATE_VERSIONS)

    def test_science_inventory_contains_source_only_entrypoints(self):
        files={str(p.relative_to(SKILL_ROOT)) for p in package._source_files(SKILL_ROOT,science=True)}
        for path in ('scripts/science_acceptance.py','assets/domain-profiles/heat-transfer.json','assets/domain-profiles/materials-optics.json','references/science-consultation.md'):
            self.assertIn(path,files)
        self.assertFalse(any('/tests/' in p or '/evidence/' in p or p.endswith('.pdf') for p in files))

    def test_runtime_payload_and_hidden_answers_cannot_enter_science_inventory(self):
        for path,value in (('assets/operator-private.json',{'schema_version':'tkc.science-sidecar/v0.1'}),
                           ('assets/domain-profiles/heat-transfer.json',{'schema_version':'tkc.science-domain-profile/v0.1','knowledge_verified':False,'execution_authorized':False,'question_catalog':[{'expected_answer':'hidden'}]})):
            self.assertIsNotNone(verifier.science_source_file_error(path,json.dumps(value)))

    def test_m4_cannot_be_packaged_as_formal_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(package.CompilerReleasePackagingError,'release_stage_requires_current_version'):
                package.package_fake_expert_skill(SKILL_ROOT,Path(tmp)/'candidate',created_at='2026-09-19T00:00:00Z',version=versions.SCIENCE_LIFECYCLE_COMPILER_VERSION,artifact_stage='release-verified')

    def test_standalone_verifier_pins_science_schema_identity(self):
        for path,identity in verifier.SCIENCE_SCHEMA_IDS.items():
            self.assertIsNone(verifier.science_source_file_error(path,json.dumps({'$id':identity})))
            self.assertEqual(verifier.science_source_file_error(path,json.dumps({'$id':identity+'-drift'})),'science_schema_identity_drift')
