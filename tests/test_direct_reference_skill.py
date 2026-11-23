from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from reportlab.pdfgen import canvas


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = PROJECT_ROOT / "fake-expert/scripts"
SCHEMA_ROOT = PROJECT_ROOT / "fake-expert/assets/schemas"
sys.path.insert(0, str(SCRIPT_ROOT))

from direct_reference_skill import (
    DIRECT_REFERENCE_PATH,
    DirectReferenceError,
    bind_gold,
    build_direct_reference,
    extend_direct_reference,
    validate_direct_reference,
)
from composite_contract import CompositionError, validate_module_inputs
from expert_skill_contract import (
    sha256_file,
    tracked_files,
    validate_expert_skill,
    write_json,
)
from package_skill_release import ReleasePackagingError, package_release
from pdf_structure import (
    compile_pdf_intermediate_representation,
    write_intermediate_representation,
)
from semantic_promotion import DRAFT_SCHEMA, build_semantic_workpack
from verify_skill_release import verify_release


def create_reference_pdf(path: Path) -> None:
    pdf = canvas.Canvas(str(path))
    pdf.bookmarkPage("candidate")
    pdf.addOutlineEntry("1 Candidate Domain", "candidate", level=0, closed=False)
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(72, 760, "1 Candidate Domain")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(
        72,
        720,
        "A bounded candidate system uses a controlled input and reports a stable output.",
    )
    pdf.drawString(
        72,
        690,
        "Its stated operating boundary excludes safety certification and design approval.",
    )
    pdf.drawCentredString(306, 36, "1")
    pdf.showPage()
    pdf.save()


class DirectReferenceSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "candidate.pdf"
        create_reference_pdf(self.source)
        self.ir = self.root / "ir"
        write_intermediate_representation(
            self.ir,
            compile_pdf_intermediate_representation(self.source, 1, 1),
        )
        self.workpack = self.root / "workpack"
        self.workpack_manifest = build_semantic_workpack(
            self.source,
            self.ir,
            self.workpack,
            semantic_assurance=False,
        )
        self._write_candidate_drafts()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_candidate_drafts(self) -> None:
        selected = False
        for entry in self.workpack_manifest["units"]:
            unit = json.loads(
                (self.workpack / entry["path"]).read_text(encoding="utf-8")
            )
            draft: dict[str, object] = {
                "schema_version": DRAFT_SCHEMA,
                "unit_id": unit["unit_id"],
                "proposer_instance": "fixture-proposer",
                "candidate_disposition": {
                    "action": "non-knowledge",
                    "reason": "This unit is outside the selected fixture object.",
                },
                "objects": [],
                "relations": [],
                "conflicts": [],
                "gaps": [],
            }
            if not selected and unit.get("focus_spans"):
                span = unit["focus_spans"][0]
                draft["candidate_disposition"] = {
                    "action": "promote",
                    "reason": "The unit contains a bounded source-backed concept.",
                }
                draft["objects"] = [
                    {
                        "local_id": "bounded-candidate",
                        "type": "Concept",
                        "origin": "source-paraphrase",
                        "title": "Bounded candidate system",
                        "statement": "A controlled input belongs to a bounded candidate system with a reported output.",
                        "evidence_spans": [
                            {
                                "page": span["page"],
                                "start": span["start"],
                                "end": span["end"],
                                "span_sha256": span["span_sha256"],
                            }
                        ],
                        "assumptions": [],
                        "valid_when": ["Using the declared bounded fixture scope."],
                        "fails_when": ["Used for safety certification or design approval."],
                        "applicability": {
                            "physical_pages": [1, 1],
                            "excluded_conclusions": ["Real engineering approval"],
                        },
                        "visual_task_ids": [],
                        "formula": None,
                    }
                ]
                selected = True
            write_json(
                self.workpack / "drafts" / f"{unit['unit_id']}.json",
                draft,
            )
        self.assertTrue(selected)

    def _build(self, output: Path, *, version: str = "1.0.0") -> dict[str, object]:
        return build_direct_reference(
            self.source,
            self.workpack,
            output,
            name="candidate-domain",
            display_name="Candidate Domain Reference",
            domain="candidate-domain",
            version=version,
            created_at="2026-08-31T04:00:00+00:00",
        )

    def _refresh_integrity(self, package: Path) -> None:
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        manifest["integrity"]["files"] = tracked_files(package)
        write_json(package / "manifest.json", manifest)

    def test_no_gold_no_review_build_is_deterministic_queryable_and_honest(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        result = self._build(first)
        self._build(second)
        self.assertEqual(result["package_id"], "fake-candidate-domain")
        first_files = {
            path.relative_to(first).as_posix(): path.read_bytes()
            for path in sorted(first.rglob("*"))
            if path.is_file()
        }
        second_files = {
            path.relative_to(second).as_posix(): path.read_bytes()
            for path in sorted(second.rglob("*"))
            if path.is_file()
        }
        self.assertEqual(first_files, second_files)
        self.assertEqual(validate_direct_reference(first), [])
        manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["package"]["status"], "draft")
        self.assertEqual(manifest["modules"][0]["exports"], sorted(manifest["modules"][0]["exports"]))
        obj_path = next((first / "references/knowledge/objects").glob("*.json"))
        obj = json.loads(obj_path.read_text(encoding="utf-8"))
        self.assertEqual(obj["status"], "unreviewed-candidate")
        self.assertNotIn("review_binding", obj)
        control = json.loads((first / DIRECT_REFERENCE_PATH).read_text(encoding="utf-8"))
        self.assertEqual(control["gold"]["mode"], "none")
        self.assertFalse(control["usage"]["knowledge_verified"])
        schema = json.loads(
            (SCHEMA_ROOT / "direct-reference.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(schema["$id"], control["schema_version"])
        query = subprocess.run(
            [
                sys.executable,
                str(first / "scripts/query_reference.py"),
                "--query",
                "bounded candidate system",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(query.returncode, 0, query.stderr)
        response = json.loads(query.stdout)
        self.assertEqual(response["behavior"], "answer")
        self.assertTrue(response["matched_object_ids"])
        self.assertTrue(response["evidence_ids"])
        self.assertEqual(response["assurance"]["review_state"], "deferred")
        self.assertIn("unreviewed-candidate", response["warnings"])
        self.assertTrue((first / "references/runtime/response.schema.json").is_file())
        skill_text = (first / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("python3 -B scripts/query_reference.py", skill_text)
        self.assertIn("untrusted data", skill_text)
        guarded = subprocess.run(
            [
                sys.executable,
                str(first / "scripts/query_reference.py"),
                "--query",
                "推荐一个安全设计并批准执行",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(guarded.returncode, 0, guarded.stderr)
        self.assertEqual(json.loads(guarded.stdout)["behavior"], "abstain")
        publish_codes = {
            issue.code
            for issue in validate_expert_skill(first, level="publish")
            if issue.severity == "error"
        }
        self.assertIn("not_ready", publish_codes)

    def test_explicit_draft_distribution_is_source_free_and_verifiable(self) -> None:
        package = self.root / "package"
        self._build(package)
        with self.assertRaises(ReleasePackagingError):
            package_release(
                package,
                self.root / "ready-default",
                created_at="2026-08-31T04:10:00+00:00",
            )
        release = self.root / "draft-release"
        manifest = package_release(
            package,
            release,
            created_at="2026-08-31T04:10:00+00:00",
            artifact_stage="draft-distributable",
        )
        self.assertEqual(manifest["artifact_stage"], "draft-distributable")
        self.assertFalse(manifest["candidate_assurance"]["knowledge_verified"])
        extracted = self.root / "extracted"
        verified = verify_release(release, extract=extracted)
        self.assertEqual(verified["status"], "draft-distributable")
        self.assertEqual(verified["artifact_stage"], "draft-distributable")
        self.assertTrue(verified["integrity_verified"])
        self.assertFalse(verified["knowledge_verified"])
        self.assertEqual(manifest["adapter"]["working_directory"], "package-root")
        self.assertEqual(
            manifest["adapter"]["response_schema_path"],
            "references/runtime/response.schema.json",
        )
        archive = release / manifest["archive"]["filename"]
        with zipfile.ZipFile(archive) as handle:
            names = handle.namelist()
        self.assertFalse(
            any(
                Path(name).suffix.casefold()
                in {".bmp", ".gif", ".jpeg", ".jpg", ".pdf", ".png", ".tif", ".tiff", ".webp"}
                for name in names
            )
        )
        self.assertFalse(any("gold-review" in name for name in names))
        write_json(
            package / "private-gold.json",
            {"source_sha256": sha256_file(self.source), "secret": "must-not-ship"},
        )
        with self.assertRaisesRegex(
            ReleasePackagingError, "candidate_file_inventory_invalid"
        ):
            package_release(
                package,
                self.root / "leaky-release",
                created_at="2026-08-31T04:11:00+00:00",
                artifact_stage="draft-distributable",
            )

    def test_gold_can_be_bound_and_revised_without_changing_knowledge(self) -> None:
        base = self.root / "base"
        self._build(base)
        base_manifest_hash = sha256_file(base / "manifest.json")
        base_object_hashes = {
            path.name: sha256_file(path)
            for path in (base / "references/knowledge/objects").glob("*.json")
        }
        source_hash = sha256_file(self.source)
        pending = self.root / "pending"
        bind_gold(
            base,
            pending,
            version="1.0.1",
            created_at="2026-08-31T04:15:00+00:00",
            mode="create",
            revision=None,
        )
        pending_control = json.loads(
            (pending / DIRECT_REFERENCE_PATH).read_text(encoding="utf-8")
        )
        self.assertEqual(pending_control["gold"]["status"], "pending")
        self.assertEqual(pending_control["gold"]["coverage_state"], "pending")
        self.assertIsNone(pending_control["gold"]["revision_sha256"])
        self.assertFalse(pending_control["gold"]["promotion_authority"])
        gold_v1 = self.root / "gold-v1.json"
        write_json(
            gold_v1,
            {
                "schema_version": "fixture.gold/v1",
                "source_sha256": source_hash,
                "items": [],
            },
        )
        bound = self.root / "bound"
        bind_gold(
            base,
            bound,
            version="1.0.1",
            created_at="2026-08-31T04:20:00+00:00",
            mode="reuse",
            revision=gold_v1,
        )
        self.assertEqual(sha256_file(base / "manifest.json"), base_manifest_hash)
        control = json.loads((bound / DIRECT_REFERENCE_PATH).read_text(encoding="utf-8"))
        self.assertEqual(control["gold"]["status"], "bound")
        self.assertEqual(control["gold"]["coverage_state"], "not-evaluated")
        self.assertFalse(control["gold"]["object_coverage_verified"])
        self.assertFalse(control["gold"]["promotion_authority"])
        self.assertEqual(
            base_object_hashes,
            {
                path.name: sha256_file(path)
                for path in (bound / "references/knowledge/objects").glob("*.json")
            },
        )
        gold_v2 = self.root / "gold-v2.json"
        write_json(
            gold_v2,
            {
                "schema_version": "fixture.gold/v2",
                "source_sha256": source_hash,
                "items": [{"id": "revised"}],
            },
        )
        revised = self.root / "revised"
        bind_gold(
            bound,
            revised,
            version="1.0.2",
            created_at="2026-08-31T04:30:00+00:00",
            mode="update",
            revision=gold_v2,
        )
        revised_control = json.loads(
            (revised / DIRECT_REFERENCE_PATH).read_text(encoding="utf-8")
        )
        self.assertEqual(revised_control["gold"]["mode"], "update")
        self.assertNotEqual(
            revised_control["gold"]["revision_sha256"],
            control["gold"]["revision_sha256"],
        )
        self.assertEqual(revised_control["usage"]["review_state"], "deferred")
        mismatched = self.root / "mismatched.json"
        write_json(
            mismatched,
            {"schema_version": "fixture.gold/v1", "source_sha256": "0" * 64},
        )
        with self.assertRaisesRegex(DirectReferenceError, "gold_source_scope_mismatch"):
            bind_gold(
                base,
                self.root / "rejected",
                version="1.0.3",
                created_at="2026-08-31T04:40:00+00:00",
                mode="reuse",
                revision=mismatched,
            )
        invalid_schema = self.root / "invalid-schema.json"
        write_json(
            invalid_schema,
            {
                "schema_version": "/example/invalid-schema.json",
                "source_sha256": source_hash,
            },
        )
        with self.assertRaisesRegex(
            DirectReferenceError, "gold_revision_schema_invalid"
        ):
            bind_gold(
                base,
                self.root / "invalid-schema-output",
                version="1.0.4",
                created_at="2026-08-31T04:50:00+00:00",
                mode="reuse",
                revision=invalid_schema,
            )

    def test_unreviewed_direct_candidate_cannot_enter_reviewed_draft_composer(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        self._build(first)
        build_direct_reference(
            self.source,
            self.workpack,
            second,
            name="candidate-domain-two",
            display_name="Candidate Domain Reference Two",
            domain="candidate-domain-two",
            version="1.0.0",
            created_at="2026-08-31T04:55:00+00:00",
        )
        with self.assertRaisesRegex(
            CompositionError, "unreviewed_direct_candidate_requires_extend"
        ):
            validate_module_inputs([first, second], level="structure")

    def test_additive_extension_preserves_ids_and_records_new_draft_bundle(self) -> None:
        base = self.root / "base"
        self._build(base)
        base_snapshot = {
            path.relative_to(base).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in base.rglob("*")
            if path.is_file()
        }
        selected_path = next(
            path
            for path in sorted((self.workpack / "drafts").glob("*.json"))
            if json.loads(path.read_text(encoding="utf-8"))["objects"]
        )
        draft = json.loads(selected_path.read_text(encoding="utf-8"))
        second = dict(draft["objects"][0])
        second["local_id"] = "bounded-candidate-extension"
        second["title"] = "Candidate operating boundary"
        second["statement"] = "The candidate operating boundary excludes approval and certification conclusions."
        draft["objects"].append(second)
        write_json(selected_path, draft)
        extended = self.root / "extended"
        result = extend_direct_reference(
            base,
            self.source,
            self.workpack,
            extended,
            version="1.1.0",
            created_at="2026-08-31T05:00:00+00:00",
        )
        self.assertEqual(len(result["update"]["added_object_ids"]), 1)
        self.assertEqual(len(result["update"]["unchanged_object_ids"]), 1)
        self.assertEqual(validate_direct_reference(extended), [])
        extended_control = json.loads(
            (extended / DIRECT_REFERENCE_PATH).read_text(encoding="utf-8")
        )
        self.assertEqual(len(extended_control["workpack_bindings"]), 2)
        self.assertEqual(
            len(
                {
                    row["draft_bundle_sha256"]
                    for row in extended_control["workpack_bindings"]
                }
            ),
            2,
        )
        self.assertEqual(
            base_snapshot,
            {
                path.relative_to(base).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in base.rglob("*")
                if path.is_file()
            },
        )

    def test_tampered_runtime_and_provenance_fail_closed(self) -> None:
        runtime_package = self.root / "runtime-package"
        self._build(runtime_package)
        (runtime_package / "scripts/query_reference.py").write_text(
            'print("{}")\n', encoding="utf-8"
        )
        self._refresh_integrity(runtime_package)
        self.assertIn(
            "runtime_implementation_mismatch",
            validate_direct_reference(runtime_package),
        )
        with self.assertRaisesRegex(
            ReleasePackagingError, "input_candidate_invalid"
        ):
            package_release(
                runtime_package,
                self.root / "runtime-release",
                created_at="2026-08-31T05:10:00+00:00",
                artifact_stage="draft-distributable",
            )

        provenance_package = self.root / "provenance-package"
        self._build(provenance_package)
        control_path = provenance_package / DIRECT_REFERENCE_PATH
        control = json.loads(control_path.read_text(encoding="utf-8"))
        control["workpack_bindings"] = []
        write_json(control_path, control)
        self._refresh_integrity(provenance_package)
        self.assertIn(
            "workpack_bindings_missing",
            validate_direct_reference(provenance_package),
        )

    def test_input_output_overlap_is_rejected(self) -> None:
        with self.assertRaisesRegex(DirectReferenceError, "input_output_overlap"):
            self._build(self.workpack / "nested-output")
        package = self.root / "package"
        self._build(package)
        with self.assertRaisesRegex(ReleasePackagingError, "input_output_overlap"):
            package_release(
                package,
                package / "release",
                created_at="2026-08-31T05:20:00+00:00",
                artifact_stage="draft-distributable",
            )


if __name__ == "__main__":
    unittest.main()
