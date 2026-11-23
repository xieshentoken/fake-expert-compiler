"""Real compiler archives and standalone negative verification, all synthetic."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from science.paths import SKILL_ROOT
import compiler_version as versions
import package_fake_expert_skill as packaging
import reference_search
import verify_fake_expert_release as verifier


class ScienceReleaseCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name).resolve()
        cls.a = cls.root / "a"; cls.b = cls.root / "b"
        for target in (cls.a, cls.b):
            packaging.package_fake_expert_skill(SKILL_ROOT, target, created_at="2026-11-23T00:00:00Z",
                version=versions.SCIENCE_LIFECYCLE_COMPILER_VERSION)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def standalone(self, directory):
        return subprocess.run([sys.executable, "-I", "-B", str(directory / verifier.VERIFIER_NAME), str(directory)],
            cwd=self.root, text=True, capture_output=True)

    def mutation(self, label, changes):
        target = self.root / label
        shutil.copytree(self.a, target)
        manifest = json.loads((target / "release-manifest.json").read_text())
        archive = target / manifest["archive"]["filename"]
        with zipfile.ZipFile(archive) as source:
            contents = {item.filename: source.read(item) for item in source.infolist()}
        for path, value in changes.items():
            name = "fake-expert/" + path
            if value is None: contents.pop(name)
            else: contents[name] = value
        with zipfile.ZipFile(archive, "w") as output:
            for name, data in sorted(contents.items()):
                output.writestr(packaging._zip_info(name), data, compress_type=zipfile.ZIP_DEFLATED)
        files = [{"path": name.removeprefix("fake-expert/"), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                 for name, data in sorted(contents.items())]
        manifest["compiler"].update(files=files, file_count=len(files), uncompressed_bytes=sum(f["bytes"] for f in files))
        manifest["archive"].update(sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), file_count=len(files),
                                   uncompressed_bytes=sum(f["bytes"] for f in files))
        (target / "release-manifest.json").write_text(json.dumps(manifest))
        checksum = target / "SHA256SUMS"
        names = [line.split("  ", 1)[1] for line in checksum.read_text().splitlines()]
        checksum.write_text("".join(hashlib.sha256((target / name).read_bytes()).hexdigest() + "  " + name + "\n" for name in names))
        return target

    def test_ab_candidate_and_standalone_verification_allow_only_pinned_terms(self):
        first = json.loads((self.a / "release-manifest.json").read_text())
        second = json.loads((self.b / "release-manifest.json").read_text())
        self.assertEqual(first, second)
        self.assertEqual((self.a / first["archive"]["filename"]).read_bytes(), (self.b / second["archive"]["filename"]).read_bytes())
        self.assertTrue(reference_search.load_term_aliases(SKILL_ROOT / "assets/reference-terms.json"))
        for target in (self.a, self.b):
            result = self.standalone(target)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "candidate-verified")

    def test_packaging_rejects_changed_terms_and_runtime_json(self):
        source = self.root / "copied-source" / "fake-expert"
        shutil.copytree(SKILL_ROOT, source)
        terms = source / "assets/reference-terms.json"; original = terms.read_bytes()
        malformed = {"schema_version": "tkc.reference-term-aliases/v0.1", "terms": [{"term": "x", "aliases": ["hidden answer"]}]}
        terms.write_text(json.dumps(malformed))
        with self.assertRaisesRegex(packaging.CompilerReleasePackagingError, "science_reference_terms_hash_mismatch"):
            packaging.package_fake_expert_skill(source, self.root / "bad-terms", created_at="2026-11-23T00:00:00Z",
                version=versions.SCIENCE_LIFECYCLE_COMPILER_VERSION)
        terms.write_bytes(original)
        (source / "assets/operator-private.json").write_text('{"expected_answer":"hidden synthetic payload"}')
        with self.assertRaisesRegex(packaging.CompilerReleasePackagingError, "science_runtime_payload_forbidden"):
            packaging.package_fake_expert_skill(source, self.root / "bad-runtime", created_at="2026-11-23T00:00:00Z",
                version=versions.SCIENCE_LIFECYCLE_COMPILER_VERSION)

    def test_standalone_rejects_rehashed_runtime_terms_and_incomplete_adapter(self):
        compiler = (SKILL_ROOT / "scripts/compiler_version.py").read_text()
        marker = 'SCIENCE_RUNTIME_COMPAT_PROTOCOL = "science-runtime-compat-v0.1"'
        cases = [
            ("terms", {"assets/reference-terms.json": b'{"schema_version":"tkc.reference-term-aliases/v0.1","terms":[]}'}, "science_reference_terms_hash_mismatch"),
            ("payload", {"assets/operator-private.json": b'{"expected_answer":"synthetic hidden"}'}, "science_runtime_payload_forbidden"),
            ("adapter", {"scripts/science_runtime_compat.py": None}, "science_runtime_compat_path_missing"),
            ("marker", {"scripts/compiler_version.py": compiler.replace(marker, "").encode()}, "science_runtime_compat_marker_missing"),
            ("unknown", {"scripts/compiler_version.py": compiler.replace(marker, marker.replace("v0.1", "v99")).encode()}, "science_runtime_compat_protocol_invalid"),
            ("duplicate", {"scripts/compiler_version.py": (compiler + "\n" + marker + "\n").encode()}, "science_runtime_compat_protocol_invalid"),
            ("nested", {"scripts/compiler_version.py": compiler.replace(marker, "def hidden_protocol():\n    " + marker).encode()}, "science_runtime_compat_protocol_invalid"),
            ("conditional", {"scripts/compiler_version.py": compiler.replace(marker, "if False:\n    " + marker).encode()}, "science_runtime_compat_protocol_invalid"),
            ("nonliteral", {"scripts/compiler_version.py": compiler.replace(marker, 'SCIENCE_RUNTIME_COMPAT_PROTOCOL = str("science-runtime-compat-v0.1")').encode()}, "science_runtime_compat_protocol_invalid"),
        ]
        for label, changes, reason in cases:
            with self.subTest(label=label):
                result = self.standalone(self.mutation("mutated-" + label, changes))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(reason, result.stdout + result.stderr)

    def test_historical_fixed_m4_archive_is_readable_by_current_verifier(self):
        path = os.environ.get("FAKE_EXPERT_SCIENCE_LEGACY_ARCHIVE")
        if not path: self.skipTest("private historical M4 archive not supplied")
        result = verifier.verify_compiler_release(Path(path).parent)
        self.assertEqual(result["archive_sha256"], verifier.HISTORICAL_SCIENCE_M4_ARCHIVE_SHA256)
        self.assertEqual(result["status"], "candidate-verified")
