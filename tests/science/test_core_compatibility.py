"""Current input compatibility plus actual review/execution gate rejection."""
import copy
import json
import os
from pathlib import Path
import unittest
import zipfile

from science.paths import ROOT, SKILL_ROOT
import compiler_version as versions
from expert_skill_contract import validate_expert_skill
from science_workpack import load_package
from semantic_promotion import (validate_semantic_workpack, validate_reviewed_workpack,
    promote_reviewed_workpack, SemanticPromotionError)


class CoreGenerationCompatibilityTests(unittest.TestCase):
    def test_current_generation_is_declared_and_schema_extension_is_additive(self):
        self.assertEqual(versions.INTAKE_COMPILER_VERSION, "1.2.0-intake-v2")
        self.assertEqual(versions.DIRECT_REFERENCE_COMPILER_VERSION, "1.4.1-direct-reference")
        self.assertEqual(versions.SCIENCE_RUNTIME_COMPAT_PROTOCOL, "science-runtime-compat-v0.1")
        current = json.loads((SKILL_ROOT / "assets/schemas/knowledge-object.schema.json").read_text())
        self.assertFalse(current["additionalProperties"])
        self.assertEqual(current["properties"]["schema_version"]["const"], "tkc.expert-skill/v0.1")
        archive = os.environ.get("FAKE_EXPERT_SCIENCE_LEGACY_ARCHIVE")
        if not archive:
            self.skipTest("private historical schema comparison not supplied")
        with zipfile.ZipFile(archive) as source:
            old = json.loads(source.read("fake-expert/assets/schemas/knowledge-object.schema.json"))
        self.assertEqual(current["required"], old["required"])
        self.assertEqual(set(current["properties"]) - set(old["properties"]), {"figure_refs", "value_basis"})
        self.assertEqual(set(current["properties"]["type"]["enum"]) - set(old["properties"]["type"]["enum"]),
                         {"Figure", "Table", "DataPoint"})
        for name, rule in old["properties"].items():
            if name != "type":
                self.assertEqual(current["properties"][name], rule, name)

    def test_original_legacy_package_still_passes_current_original_contract(self):
        base = ROOT / "tests/fixtures/legacy-fake-heat-ref-1.1.0"
        self.assertEqual([i for i in validate_expert_skill(base) if i.severity == "error"], [])
        self.assertFalse(load_package(base)["manifest"]["capabilities"]["executable"])

    def test_current_workpack_accepts_old_valid_proposal_but_cannot_self_promote(self):
        import test_direct_reference_skill as old
        fixture = old.DirectReferenceSkillTests(); fixture.setUp(); fixture.root = fixture.root.resolve()
        try:
            issues, _ = validate_semantic_workpack(fixture.source, fixture.workpack)
            self.assertEqual([i for i in issues if i.severity == "error"], [])
            issues, _ = validate_reviewed_workpack(fixture.source, fixture.workpack)
            self.assertTrue([i for i in issues if i.severity == "error"])
            target = fixture.root / "forbidden-promotion"
            with self.assertRaisesRegex(SemanticPromotionError, "reviewed_gate_failed"):
                promote_reviewed_workpack(fixture.source, fixture.workpack, target,
                    name="fixture", display_name="Fixture", domain="fixture")
            self.assertFalse(target.exists())
            package = fixture.root / "candidate"; fixture._build(package)
            self.assertIn("not_ready", {i.code for i in validate_expert_skill(package, "publish")})
            manifest_path = package / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["capabilities"]["executable"] = True
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "science_base_contract_invalid"):
                load_package(package)
        finally:
            fixture.tearDown()

    def test_unreviewed_proposal_rejects_forged_review_and_execution_fields(self):
        import test_direct_reference_skill as old
        fixture = old.DirectReferenceSkillTests(); fixture.setUp(); fixture.root = fixture.root.resolve()
        try:
            entry = fixture.workpack_manifest["units"][0]
            path = fixture.workpack / "drafts" / (entry["unit_id"] + ".json")
            original = json.loads(path.read_text())
            for change in ({"executable": True}, {"review_binding": {"independent": True}},
                           {"status": "ready"}, {"science_sidecar": {"knowledge_verified": True}}):
                draft = copy.deepcopy(original); draft["objects"][0].update(change)
                path.write_text(json.dumps(draft))
                issues, _ = validate_reviewed_workpack(fixture.source, fixture.workpack)
                self.assertTrue([i for i in issues if i.severity == "error"], change)
                with self.assertRaisesRegex(SemanticPromotionError, "reviewed_gate_failed"):
                    promote_reviewed_workpack(fixture.source, fixture.workpack, fixture.root / "forbidden",
                        name="fixture", display_name="Fixture", domain="fixture")
                self.assertFalse((fixture.root / "forbidden").exists())
        finally:
            fixture.tearDown()
