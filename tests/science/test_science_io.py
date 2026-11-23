"""Filesystem/CLI checks on a newly synthesized, unreviewed legacy Draft only."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from science.support import sc, profile, record
import science_workpack as sw


class ScienceIOTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import test_direct_reference_skill as legacy
        except ImportError as error:
            raise unittest.SkipTest("Existing synthetic PDF integration dependencies unavailable") from error
        cls.legacy = legacy.DirectReferenceSkillTests()
        cls.legacy.setUp()
        cls.base = cls.legacy.root.resolve() / "science-synthetic-draft"
        cls.legacy._build(cls.base)

    @classmethod
    def tearDownClass(cls):
        cls.legacy.tearDown()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.package = sw.load_package(self.base)

    def tearDown(self):
        self.temp.cleanup()

    def candidate(self):
        r = record(); obj = self.package["objects"][0]
        anchor = next(a for a in self.package["anchors"] if a["id"] in obj["evidence_ids"])
        oref = {"package_id": self.package["fingerprint"]["package_id"], "package_version": self.package["fingerprint"]["package_version"],
                "object_id": obj["id"], "object_sha256": sc.sha256_json(obj)}
        eref = {"object_ref": oref, "evidence_id": anchor["id"], "evidence_sha256": sc.sha256_json(anchor)}
        def replace(value):
            if isinstance(value, dict):
                if "evidence_sha256" in value: return copy.deepcopy(eref)
                if "object_sha256" in value: return copy.deepcopy(oref)
                return {k: replace(v) for k, v in value.items()}
            return [replace(v) for v in value] if isinstance(value, list) else value
        r = replace(r)
        for p in r["provenance"]:
            if p["status"] != "unknown": p["status"] = "assumed"
            p["reason"] = "Synthetic test semantics; not facts from the generated source"
        return sc.bind_record(r)

    def cli(self, *args):
        result = subprocess.run([sys.executable, "-B", str(Path(sw.__file__)), *map(str, args)], capture_output=True, text=True)
        self.assertTrue(result.stdout, result.stderr)
        return result, json.loads(result.stdout)

    def test_build_validate_freeze_round_trip_does_not_modify_base(self):
        before = {str(p.relative_to(self.base)): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.base.rglob("*") if p.is_file()}
        records, config, output, frozen = [self.root / f for f in ("records.json", "profile.json", "science-sidecar.json", "frozen.json")]
        records.write_text(json.dumps([self.candidate()])); config.write_text(json.dumps(profile()))
        run, result = self.cli("build", "--package", self.base, "--profile", config, "--records", records,
                               "--data-origin", "synthetic", "--output", output)
        self.assertEqual(run.returncode, 0, result)
        self.assertFalse(result["executable"])
        run, result = self.cli("validate", "--package", self.base, "--profile", config, "--sidecar", self.root)
        self.assertEqual(run.returncode, 0, result)
        run, result = self.cli("freeze", "--package", self.base, "--profile", config, "--sidecar", output,
                               "--proposer-instance", "synthetic-test-proposer", "--output", frozen)
        self.assertEqual(run.returncode, 0, result)
        self.assertEqual(result["status"], "paused-independent-review")
        self.assertIsNone(sc.load_json(frozen)["review_plan"])
        self.assertEqual(before, {str(p.relative_to(self.base)): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.base.rglob("*") if p.is_file()})

    def test_existing_output_never_overwritten(self):
        output = self.root / "kept.json"; output.write_bytes(b"preserve me")
        with self.assertRaises(FileExistsError): sw._write_new(output, {"example": True})
        self.assertEqual(output.read_bytes(), b"preserve me")
        self.assertEqual(list(self.root.glob(".science-*")), [])

    def test_absent_optional_sidecar_is_unavailable(self):
        config = self.root / "profile.json"; config.write_text(json.dumps(profile()))
        run, result = self.cli("validate", "--package", self.base, "--profile", config)
        self.assertEqual(run.returncode, 0, result)
        self.assertEqual(result["status"], "unavailable")

    def test_duplicate_json_keys_rejected(self):
        p = self.root / "duplicate.json"; p.write_text('{"executable":false,"executable":true}')
        with self.assertRaisesRegex(ValueError, "duplicate_json_key"): sc.load_json(p)

    def test_input_symlink_rejected(self):
        p = self.root / "real.json"; p.write_text('{}')
        link = self.root / "link.json"; link.symlink_to(p)
        with self.assertRaises(ValueError): sc.load_json(link)

    def test_parent_symlink_rejected(self):
        directory = self.root / "directory"; directory.mkdir(); (directory / "data.json").write_text('{}')
        link = self.root / "link"; link.symlink_to(directory, target_is_directory=True)
        with self.assertRaises(ValueError): sc.load_json(link / "data.json")

    def test_package_relative_path_escape_rejected(self):
        for path in ("../manifest.json", "/etc/passwd", "references/../../manifest.json"):
            with self.subTest(path=path), self.assertRaises(ValueError): sw._safe_path(self.base, path)

    def test_tampered_base_rejected_before_any_sidecar(self):
        copied = self.root / "copy"; shutil.copytree(self.base, copied)
        (copied / "SKILL.md").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "integrity_drift"): sw.load_package(copied)

    def test_output_inside_base_via_dotdot_rejected(self):
        records, config = self.root / "records.json", self.root / "profile.json"
        records.write_text(json.dumps([self.candidate()])); config.write_text(json.dumps(profile()))
        # Lexical path starts outside base; normalized destination lies inside it.
        output = self.root / ".." / self.base.relative_to(self.root.parent) / "science-must-not-exist.json" if self.base.is_relative_to(self.root.parent) else self.base.parent / "." / self.base.name / "science-must-not-exist.json"
        try:
            run, result = self.cli("build", "--package", self.base, "--profile", config, "--records", records,
                                   "--data-origin", "synthetic", "--output", output)
            self.assertEqual(run.returncode, 2, result)
            self.assertIn("science_output_inside_base_forbidden", result["reason_codes"])
            self.assertFalse(output.exists())
        finally:
            if output.exists(): output.unlink()
