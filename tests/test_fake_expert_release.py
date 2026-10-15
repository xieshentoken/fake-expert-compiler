from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HISTORY_ROOT = Path(os.environ.get("FAKE_EXPERT_HISTORY_ROOT", PROJECT_ROOT))
SCRIPT_ROOT = PROJECT_ROOT / "fake-expert/scripts"
SKILL_ROOT = PROJECT_ROOT / "fake-expert"
sys.path.insert(0, str(SCRIPT_ROOT))

from package_fake_expert_skill import (
    ARCHIVE_NAME,
    CompilerReleasePackagingError,
    package_fake_expert_skill,
)
import package_fake_expert_skill as package_module
from verify_fake_expert_release import (
    CompilerReleaseVerificationError,
    verify_compiler_release,
)


class CompilerSkillReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _skill_copy(self, container: str | None = None) -> Path:
        copied = self.root / "fake-expert"
        if container is not None:
            copied = self.root / container / "fake-expert"
            copied.parent.mkdir(parents=True)
        shutil.copytree(
            SKILL_ROOT,
            copied,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
        )
        return copied

    def _refresh_manifest_checksum(self, release: Path) -> None:
        manifest_path = release / "release-manifest.json"
        sums_path = release / "SHA256SUMS"
        refreshed = []
        for line in sums_path.read_text(encoding="utf-8").splitlines():
            name = line[66:]
            digest = (
                hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                if name == "release-manifest.json"
                else line[:64]
            )
            refreshed.append(f"{digest}  {name}")
        sums_path.write_text("\n".join(refreshed) + "\n", encoding="utf-8")

    def _codex_certification(self, skill: Path, label: str) -> Path:
        candidate = self.root / f"{label}-candidate"
        package_fake_expert_skill(
            skill,
            candidate,
            created_at="2026-09-02T00:00:00+00:00",
        )
        manifest = json.loads((candidate / "release-manifest.json").read_text(encoding="utf-8"))
        archive = manifest["archive"]
        challenge_ids = [
            "document-contract",
            "required-reading",
            "native-negative",
            "scan-negative",
            "review-boundary",
            "old-job",
        ]
        challenge_sha = hashlib.sha256(
            json.dumps(challenge_ids, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        observations = []
        for challenge_id in challenge_ids:
            observed = f"unit contract fixture observed {challenge_id}"
            observations.append({
                "challenge_id": challenge_id,
                "status": "passed",
                "observed": observed,
                "evidence_sha256": hashlib.sha256(observed.encode("utf-8")).hexdigest(),
            })
        seed = archive["sha256"] + challenge_sha
        certification = {
            "schema_version": package_module.CODEX_CERTIFICATION_SCHEMA,
            "protocol": package_module.CODEX_CERTIFICATION_PROTOCOL,
            "certification_id": "fxcert-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20],
            "issued_at": "2026-09-02T00:01:00+00:00",
            "compiler_version": "1.2.0",
            "archive": {"filename": archive["filename"], "sha256": archive["sha256"]},
            "host": {"kind": "codex-isolated-subagent", "agent_id": "unit-fixture", "model": "unit-fixture"},
            "challenge_set": {"sha256": challenge_sha, "count": len(challenge_ids)},
            "results": {
                "status": "passed",
                "attempted": len(challenge_ids),
                "passed": len(challenge_ids),
                "failed": 0,
                "observations": observations,
            },
            "claims": {
                "document_contract_enforced": True,
                "required_reading_hash_bound": True,
                "native_text_negative_routing": True,
                "scanned_pdf_negative_routing": True,
                "review_authority_preserved": True,
                "old_job_not_silently_rewritten": True,
            },
            "limitations": ["Unit contract fixture; not a product certification."],
            "attestation": {
                "evaluator_independent_of_implementation": True,
                "hidden_expectations_not_sent": True,
                "source_pdf_sent": False,
                "network_used": False,
                "original_project_modified": False,
            },
            "certification_sha256": "",
        }
        certification["certification_sha256"] = package_module._certification_hash(certification)
        path = self.root / f"{label}-codex-certification.json"
        path.write_text(json.dumps(certification, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def _package_formal(self, skill: Path, output: Path, *, created_at: str) -> None:
        certification = self._codex_certification(skill, output.name)
        package_fake_expert_skill(
            skill,
            output,
            created_at=created_at,
            artifact_stage="release-verified",
            codex_certification=certification,
        )

    def test_release_is_deterministic_source_free_and_safely_extractable(self) -> None:
        skill = self._skill_copy()
        first = self.root / "first"
        second = self.root / "second"
        created_at = "2026-08-14T00:00:00+00:00"
        package_fake_expert_skill(skill, first, created_at=created_at)
        package_fake_expert_skill(skill, second, created_at=created_at)
        first_files = {
            path.relative_to(first).as_posix(): path.read_bytes()
            for path in sorted(first.iterdir())
        }
        second_files = {
            path.relative_to(second).as_posix(): path.read_bytes()
            for path in sorted(second.iterdir())
        }
        self.assertEqual(first_files, second_files)
        verified = verify_compiler_release(first)
        self.assertEqual(verified["status"], "candidate-verified")
        release_manifest = json.loads((first / "release-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(release_manifest["artifact_stage"], "candidate-verified")
        self.assertEqual(
            release_manifest["source_candidate_identity"],
            release_manifest["compiler"]["version"],
        )
        self.assertIn(
            "not a formal MVP 1.2 release",
            (first / "RECEIVER.md").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            verified["file_count"],
            len(
                [
                    path
                    for path in skill.rglob("*")
                    if path.is_file()
                    and "__pycache__" not in path.parts
                    and path.suffix != ".pyc"
                    and path.name != ".DS_Store"
                ]
            ),
        )
        extracted = self.root / "extracted"
        result = verify_compiler_release(first, extract=extracted)
        root = Path(result["extracted_root"])
        self.assertTrue((root / "SKILL.md").is_file())
        self.assertTrue((root / "references/private-host-distribution.md").is_file())
        for required in (
            "assets/schemas/direct-reference.schema.json",
            "assets/schemas/skill-release.schema.json",
            "scripts/direct_reference_runtime.py",
            "scripts/direct_reference_skill.py",
            "scripts/package_skill_release.py",
            "scripts/portable_reference_runtime.py",
            "scripts/verify_skill_release.py",
        ):
            self.assertTrue((root / required).is_file(), required)
        self.assertEqual(
            (first / "verify_fake_expert_release.py").read_bytes(),
            (root / "scripts/verify_fake_expert_release.py").read_bytes(),
        )
        self.assertFalse(any(path.suffix.casefold() == ".pdf" for path in root.rglob("*")))

    def test_direct_reference_release_paths_are_required_before_packaging(self) -> None:
        for relative in (
            "scripts/direct_reference_skill.py",
            "assets/schemas/direct-reference.schema.json",
        ):
            with self.subTest(relative=relative):
                skill = self._skill_copy(relative.replace("/", "-"))
                (skill / relative).unlink()
                with self.assertRaisesRegex(
                    CompilerReleasePackagingError, "required_file_missing"
                ):
                    package_fake_expert_skill(
                        skill,
                        self.root / f"release-{Path(relative).stem}",
                        created_at="2026-08-31T00:00:00+00:00",
                    )

    def test_tampered_archive_is_rejected(self) -> None:
        release = self.root / "release"
        package_fake_expert_skill(
            self._skill_copy(), release, created_at="2026-08-14T00:00:00+00:00"
        )
        archive = release / ARCHIVE_NAME
        archive.write_bytes(archive.read_bytes() + b"tamper")
        with self.assertRaises(CompilerReleaseVerificationError):
            verify_compiler_release(release)

    def test_formal_release_requires_explicit_stage_and_binds_product_scope(self) -> None:
        formal = self.root / "formal"
        self._package_formal(
            self._skill_copy(), formal, created_at="2026-08-30T00:00:00+00:00"
        )
        verified = verify_compiler_release(formal, require_release_verified=True)
        self.assertEqual(verified["status"], "release-verified")
        manifest = json.loads((formal / "release-manifest.json").read_text(encoding="utf-8"))
        acceptance = manifest["mvp_acceptance"]
        self.assertEqual(acceptance["product_scope"]["version"], "1.2.0")
        self.assertEqual(
            acceptance["product_scope"]["accepted_input_profiles"],
            ["native-text-technical-pdf", "scanned-technical-pdf"],
        )
        self.assertTrue(acceptance["product_scope"]["knowledge_contract"]["codex_certified"])
        self.assertEqual(acceptance["unreviewed_output_max_stage"], "draft")
        self.assertTrue(acceptance["unreviewed_output_usable"])
        self.assertTrue(acceptance["draft_private_distribution_allowed"])
        direct = acceptance["direct_reference_contract"]
        self.assertEqual(direct["package_prefix"], "fake-")
        self.assertEqual(direct["gold_modes"], ["none", "reuse", "create", "update"])
        self.assertFalse(direct["gold_required"])
        self.assertFalse(direct["gold_promotion_authority"])
        self.assertEqual(direct["distribution_stage"], "draft-distributable")
        self.assertTrue(direct["queryable"])
        self.assertFalse(direct["executable_capability"])
        self.assertEqual(
            acceptance["review_deferred_boundary"]["review_state"], "deferred"
        )
        self.assertFalse(acceptance["review_deferred_boundary"]["knowledge_acceptance"])
        self.assertEqual(
            acceptance["codex_evidence_scope"]["status"],
            "exact-compiler-package-certified",
        )
        self.assertEqual(acceptance["codex_evidence_scope"]["exact_compiler_package_certification"], "certified")
        self.assertIsNotNone(verified["codex_certification_id"])
        self.assertTrue(acceptance["known_uncovered_items"])
        self.assertNotIn("Ceramic", json.dumps(acceptance, ensure_ascii=False))
        self.assertIn(
            "verify_fake_expert_release.py . --require-release-verified",
            (formal / "RECEIVER.md").read_text(encoding="utf-8"),
        )

        candidate = self.root / "candidate"
        candidate_parent = self.root / "candidate-input"
        candidate_parent.mkdir()
        candidate_skill = candidate_parent / "fake-expert"
        shutil.copytree(
            SKILL_ROOT,
            candidate_skill,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
        )
        package_fake_expert_skill(
            candidate_skill,
            candidate,
            created_at="2026-08-30T00:00:00+00:00",
        )
        self.assertEqual(verify_compiler_release(candidate)["status"], "candidate-verified")
        with self.assertRaisesRegex(CompilerReleaseVerificationError, "release_stage_required"):
            verify_compiler_release(candidate, require_release_verified=True)

    def test_release_schema_closes_candidate_and_formal_stage_shapes(self) -> None:
        schema = json.loads(
            (SKILL_ROOT / "assets/schemas/fake-expert-release.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["$id"], "tkc.fake-expert-release/v0.4")
        self.assertIn("artifact_stage", schema["required"])
        branches = {
            branch["properties"]["artifact_stage"]["const"]: branch
            for branch in schema["oneOf"]
        }
        self.assertEqual(set(branches), {"candidate-verified", "release-verified"})
        self.assertIn("source_candidate_identity", branches["candidate-verified"]["required"])
        self.assertIn("anyOf", branches["candidate-verified"]["not"])
        self.assertIn("mvp_acceptance", branches["release-verified"]["required"])
        self.assertIn("codex_certification", branches["release-verified"]["required"])
        self.assertEqual(
            branches["release-verified"]["not"],
            {"required": ["source_candidate_identity"]},
        )
        runtime = schema["properties"]["runtime"]
        self.assertIn("direct_reference", runtime["required"])
        direct = runtime["properties"]["direct_reference"]
        self.assertFalse(direct["additionalProperties"])
        self.assertEqual(direct["properties"]["package_prefix"]["const"], "fake-")
        self.assertEqual(
            direct["properties"]["distribution_stage"]["const"],
            "draft-distributable",
        )
        self.assertTrue(direct["properties"]["reference_capability"]["const"])
        self.assertFalse(direct["properties"]["gold_required"]["const"])
        self.assertFalse(direct["properties"]["executable_capability"]["const"])

    def test_release_stage_cannot_be_used_for_a_source_candidate(self) -> None:
        with self.assertRaisesRegex(
            CompilerReleasePackagingError, "release_stage_requires_current_version"
        ):
            package_fake_expert_skill(
                self._skill_copy(),
                self.root / "release",
                created_at="2026-08-30T00:00:00+00:00",
                version="0.14.4-scan-mvp-contract-hardening",
                artifact_stage="release-verified",
            )

    def test_formal_release_requires_exact_archive_codex_certification(self) -> None:
        skill = self._skill_copy()
        with self.assertRaisesRegex(CompilerReleasePackagingError, "codex_certification_required"):
            package_fake_expert_skill(
                skill,
                self.root / "missing-cert",
                created_at="2026-09-02T00:00:00+00:00",
                artifact_stage="release-verified",
            )

        certification = self._codex_certification(skill, "wrong-archive")
        value = json.loads(certification.read_text(encoding="utf-8"))
        value["archive"]["sha256"] = "0" * 64
        value["certification_sha256"] = package_module._certification_hash(value)
        certification.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(CompilerReleasePackagingError, "codex_certification_invalid"):
            package_fake_expert_skill(
                skill,
                self.root / "wrong-cert",
                created_at="2026-09-02T00:00:00+00:00",
                artifact_stage="release-verified",
                codex_certification=certification,
            )

    def test_formal_mvp_scope_tampering_is_rejected_after_checksum_refresh(self) -> None:
        release = self.root / "release"
        self._package_formal(
            self._skill_copy(), release, created_at="2026-08-30T00:00:00+00:00"
        )
        manifest_path = release / "release-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["mvp_acceptance"]["unreviewed_output_max_stage"] = "ready"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._refresh_manifest_checksum(release)
        with self.assertRaisesRegex(
            CompilerReleaseVerificationError, "release_mvp_acceptance_invalid"
        ):
            verify_compiler_release(release, require_release_verified=True)

    def test_direct_reference_runtime_and_acceptance_tampering_are_rejected(self) -> None:
        for target in ("runtime", "acceptance"):
            with self.subTest(target=target):
                release = self.root / f"tamper-{target}"
                self._package_formal(
                    self._skill_copy(f"skill-{target}"),
                    release,
                    created_at="2026-08-31T00:00:00+00:00",
                )
                manifest_path = release / "release-manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if target == "runtime":
                    manifest["runtime"]["direct_reference"]["package_prefix"] = "expert-"
                    expected = "private_distribution_contract_invalid"
                else:
                    manifest["mvp_acceptance"]["direct_reference_contract"][
                        "gold_required"
                    ] = True
                    expected = "release_mvp_acceptance_invalid"
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n",
                    encoding="utf-8",
                )
                self._refresh_manifest_checksum(release)
                with self.assertRaisesRegex(CompilerReleaseVerificationError, expected):
                    verify_compiler_release(release, require_release_verified=True)

    def test_current_verifier_limits_legacy_compatibility_to_current_source_line(self) -> None:
        historical_candidate = (
            HISTORY_ROOT
            / "output/private-releases/candidates/fake-expert-v0.14.1-scan-mvp-20260827-a"
        )
        if not historical_candidate.is_dir():
            self.skipTest("historical release fixture unavailable; set FAKE_EXPERT_HISTORY_ROOT for read-only replay")
        legacy_result = verify_compiler_release(historical_candidate)
        self.assertEqual(legacy_result["status"], "legacy-verified")
        self.assertEqual(legacy_result["version"], "0.14.1")

        formal_release = HISTORY_ROOT / "output/private-releases/fake-expert/v0.14.0"
        with self.assertRaisesRegex(
            CompilerReleaseVerificationError, "legacy_artifact_identity_invalid"
        ):
            verify_compiler_release(formal_release)

    def test_source_pdf_in_skill_tree_is_rejected_before_packaging(self) -> None:
        skill = self._skill_copy()
        (skill / "references" / "unauthorized-source.pdf").write_bytes(b"%PDF-1.4")
        with self.assertRaises(CompilerReleasePackagingError) as raised:
            package_fake_expert_skill(
                skill,
                self.root / "release",
                created_at="2026-08-14T00:00:00+00:00",
            )
        self.assertIn("embedded_source_or_render_forbidden", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
