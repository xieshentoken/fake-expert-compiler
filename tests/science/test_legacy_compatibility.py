"""Read-only compatibility guards; no science sidecar can alter legacy authority."""
import hashlib
import os
import zipfile
import json
from pathlib import Path
import subprocess
import sys
import unittest

from science.paths import ROOT, SKILL_ROOT, SCRIPTS
from compiler_version import RELEASE_VERSION, FAKE_EXPERT_JOB_SCHEMA
from portable_reference_runtime import CATALOG_SCHEMA, RESPONSE_SCHEMA, query_catalog


class LegacyCompatibilityTests(unittest.TestCase):
    def test_old_release_files_are_byte_preserved(self):
        manifest = os.environ.get("FAKE_EXPERT_SCIENCE_PRESERVATION_MANIFEST")
        root = os.environ.get("FAKE_EXPERT_SCIENCE_PRESERVATION_ROOT")
        if not manifest or not root:
            self.skipTest("private historical preservation manifest/root not supplied")
        data = json.loads(Path(manifest).read_text())
        root = Path(root)
        self.assertGreater(len(data["release_files"]), 0)
        for path, digest in data["release_files"].items():
            with self.subTest(path=path):
                self.assertEqual(hashlib.sha256((root / path).read_bytes()).hexdigest(), digest)

    def test_legacy_schemas_and_gate_implementations_are_unchanged(self):
        data = json.loads((Path(__file__).parent / "fixtures/legacy-synthetic.json").read_text())
        evolved = {"skills/fake-expert/assets/schemas/knowledge-object.schema.json",
                   "skills/fake-expert/scripts/semantic_promotion.py"}
        for path, digest in data["gate_hashes"].items():
            if path not in evolved:
                with self.subTest(current_path=path):
                    current = SKILL_ROOT / path.removeprefix("skills/fake-expert/")
                    self.assertEqual(hashlib.sha256(current.read_bytes()).hexdigest(), digest)
        self.assertEqual(RELEASE_VERSION, "1.2.0")
        self.assertEqual(FAKE_EXPERT_JOB_SCHEMA, "tkc.fake-expert-job/v0.2")
        archive_path = os.environ.get("FAKE_EXPERT_SCIENCE_LEGACY_ARCHIVE")
        if not archive_path:
            self.skipTest("private historical M4 archive not supplied; current five locks checked")
        archive = Path(archive_path)
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(),
                         "ff2d2ba9c394bb27312c98bc8db51753c6d4a4f5506f1017624f65f2d190e777")
        with zipfile.ZipFile(archive) as historical:
            for path, digest in data["gate_hashes"].items():
                with self.subTest(historical_path=path):
                    self.assertEqual(hashlib.sha256(historical.read(path.removeprefix("skills/"))).hexdigest(), digest)

    def test_old_cli_entrypoints(self):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "fake_expert.py"), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("doctor", "plan", "compile", "status", "resume", "review"):
            self.assertIn(name, result.stdout)

    def test_reference_without_sidecar_and_decision_gate(self):
        catalog = {"schema_version": CATALOG_SCHEMA, "objects": [],
                   "capabilities": {"decision_support": False, "executable": False},
                   "policy": {"decision_guard_terms": ["calculate"]}}
        result = query_catalog(catalog, "calculate")
        self.assertEqual(result["schema_version"], RESPONSE_SCHEMA)
        self.assertEqual(result["behavior"], "abstain")
        self.assertIn("decision_capability_unavailable", result["reason_codes"])
        catalog["science_sidecar"] = {"executable": True}
        self.assertEqual(query_catalog(catalog, "calculate"), result)


if __name__ == "__main__":
    unittest.main()
