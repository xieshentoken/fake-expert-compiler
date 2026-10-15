from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "fake-expert/scripts/fake_expert.py"
PREFLIGHT = ROOT / "fake-expert/scripts/environment_preflight.py"
sys.path.insert(0, str(SCRIPT.parent))
import fake_expert as fake_expert_module  # noqa: E402
import reviewer_workbench  # noqa: E402


SCAN_WORKBENCH = ROOT / "tmp/workspace/output/phase7d6e-ceramic-structure-review-r5"
SCAN_SUBMISSION = ROOT / "tmp/workspace/output/phase7d6e-ceramic-structure-review-inputs-r1/scan-structure-review-submission.json"


class FakeExpertUxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="fake-expert-ux-test-"))
        self.source = self.tmp / "source.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        with self.source.open("wb") as stream:
            writer.write(stream)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    @staticmethod
    def write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    def make_text_source(self, name: str = "native-text.pdf") -> Path:
        path = self.tmp / name
        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({
                NameObject("/F1"): writer._add_object(font),
            }),
        })
        content = DecodedStreamObject()
        content.set_data(
            b"BT /F1 12 Tf 72 720 Td "
            b"(Native text source contains enough stable characters for this profile.) Tj ET"
        )
        page[NameObject("/Contents")] = writer._add_object(content)
        with path.open("wb") as stream:
            writer.write(stream)
        return path

    def make_image_source(self, name: str = "image-only.pdf") -> Path:
        path = self.tmp / name
        writer = PdfWriter()
        page = writer.add_blank_page(width=72, height=72)
        image = DecodedStreamObject()
        image.set_data(b"\x00\x00\x00")
        image.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(1),
            NameObject("/Height"): NumberObject(1),
            NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
            NameObject("/BitsPerComponent"): NumberObject(8),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/XObject"): DictionaryObject({
                NameObject("/Im0"): writer._add_object(image),
            }),
        })
        content = DecodedStreamObject()
        content.set_data(b"q 72 0 0 72 0 0 cm /Im0 Do Q")
        page[NameObject("/Contents")] = writer._add_object(content)
        with path.open("wb") as stream:
            writer.write(stream)
        return path

    def make_complete_workpack(self, source: Path, name: str = "workpack") -> Path:
        workpack = self.tmp / name
        (workpack / "drafts").mkdir(parents=True)
        self.write_json(
            workpack / "workpack.json",
            {
                "source": {"sha256": fake_expert_module._sha256_file(source)},
                "units": [{"id": "unit-1"}],
                "drafts_directory": "drafts",
            },
        )
        return workpack

    def test_plan_is_closed_and_dry_run_does_not_write(self) -> None:
        source = self.make_text_source("job-source.pdf")
        workspace = self.tmp / "job"
        planned = self.run_cli(
            "plan",
            "--source",
            str(source),
            "--workspace",
            str(workspace),
            "--profile",
            "native-text",
            "--name",
            "test-reference",
            "--domain",
            "test-domain",
            "--display-name",
            "Test Reference",
            "--version",
            "0.1.0",
            "--created-at",
            "2026-08-31T00:00:00+08:00",
            "--json",
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)
        job_path = workspace / "fake-expert-job.json"
        job = json.loads(job_path.read_text(encoding="utf-8"))
        self.assertEqual(plan["compiler_version"], "1.2.0")
        self.assertEqual(set(job), {
            "schema_version", "protocol", "compiler_version", "job_id", "created_at",
            "profile", "backend", "target_stage", "inputs", "package", "workspace", "gold",
            "required_reading", "state", "outputs", "tool_bindings", "runtime", "policy", "next_action",
            "job_sha256",
        })
        self.assertEqual(job["schema_version"], "tkc.fake-expert-job/v0.2")
        self.assertEqual(job["protocol"], "fake-expert-job-v0.2")
        self.assertEqual(job["compiler_version"], "1.2.0")
        self.assertEqual(plan["required_reading"], job["required_reading"])
        self.assertTrue(all(record["path"].startswith("references/") for record in job["required_reading"]))
        self.assertEqual(job["backend"], {"name": "pypdf-native", "runtime_receipt": None})
        self.assertEqual(plan["job_sha256"], job["job_sha256"])
        before = job_path.read_bytes()
        dry_run = self.run_cli("compile", "--job", str(job_path), "--dry-run", "--json")
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        result = json.loads(dry_run.stdout)
        self.assertEqual(result["status"], "paused")
        self.assertIn("semantic_authoring_required", result["required_gates"])
        self.assertEqual(result["would_write"], [str(Path(job["workspace"])/part) for part in ("extraction/ir", "extraction/workpack", "extraction/workpack/authoring")])
        self.assertEqual(job_path.read_bytes(), before)

    def test_progress_is_jsonl_and_does_not_leak_input_paths(self) -> None:
        source = self.make_text_source("progress-source.pdf")
        workspace = self.tmp / "progress-job"
        planned = self.run_cli(
            "plan", "--source", str(source), "--workspace", str(workspace),
            "--profile", "native-text", "--json",
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        job_path = workspace / "fake-expert-job.json"
        progress = self.run_cli("compile", "--job", str(job_path), "--progress", "--json")
        self.assertEqual(progress.returncode, 0, progress.stderr)
        events = [json.loads(line) for line in progress.stderr.splitlines() if line.strip()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["protocol"], "fake-expert-ux-cli-v0.2")
        self.assertEqual(events[0]["status"], "paused")
        self.assertNotIn(str(source), progress.stderr)
        self.assertNotIn(str(workspace), progress.stderr)

    def test_tampered_job_is_reported_without_repair(self) -> None:
        source = self.make_text_source("tamper-source.pdf")
        workspace = self.tmp / "job"
        planned = self.run_cli(
            "plan", "--source", str(source), "--workspace", str(workspace),
            "--profile", "native-text", "--json",
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        job_path = workspace / "fake-expert-job.json"
        value = json.loads(job_path.read_text(encoding="utf-8"))
        value["package"]["display_name"] = "tampered"
        job_path.write_text(json.dumps(value), encoding="utf-8")
        status = self.run_cli("status", "--job", str(job_path), "--json")
        self.assertEqual(status.returncode, 1)
        report = json.loads(status.stdout)
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(report["code"], "job_hash_mismatch")

    def test_job_schema_is_closed_and_declares_backend(self) -> None:
        schema = json.loads((ROOT / "fake-expert/assets/schemas/fake-expert-job.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["$id"], "tkc.fake-expert-job/v0.2")
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("backend", schema["required"])
        self.assertIn("required_reading", schema["required"])
        self.assertIn("job_sha256", schema["required"])
        self.assertFalse(schema["properties"]["state"]["additionalProperties"])
        self.assertFalse(schema["properties"]["policy"]["additionalProperties"])
        self.assertFalse(schema["properties"]["tool_bindings"]["additionalProperties"])
        self.assertFalse(schema["$defs"]["file"]["additionalProperties"])

    def test_required_reading_is_profile_specific_and_hash_bound(self) -> None:
        source = self.make_text_source("reading-source.pdf")
        native_workspace = self.tmp / "native-reading"
        native = self.run_cli(
            "plan", "--source", str(source), "--workspace", str(native_workspace),
            "--profile", "native-text", "--json",
        )
        self.assertEqual(native.returncode, 0, native.stderr)
        native_job = json.loads((native_workspace / "fake-expert-job.json").read_text(encoding="utf-8"))
        native_paths = [row["path"] for row in native_job["required_reading"]]
        self.assertIn("references/pdf-intermediate-representation.md", native_paths)
        self.assertIn("references/expert-skill-contract.md", native_paths)
        self.assertNotIn("references/phase-7a-visual-semantics-implementation-outline.md", native_paths)

        visual_workspace = self.tmp / "visual-reading"
        visual = self.run_cli(
            "plan", "--source", str(self.source), "--workspace", str(visual_workspace),
            "--profile", "visual", "--backend", "pdftoppm", "--json",
        )
        self.assertEqual(visual.returncode, 0, visual.stderr)
        visual_job = json.loads((visual_workspace / "fake-expert-job.json").read_text(encoding="utf-8"))
        visual_paths = [row["path"] for row in visual_job["required_reading"]]
        self.assertIn("references/phase-7a-visual-semantics-implementation-outline.md", visual_paths)
        self.assertNotIn("references/expert-skill-contract.md", visual_paths)

        visual_job["required_reading"][0]["sha256"] = "0" * 64
        visual_job["job_sha256"] = fake_expert_module._hash_without(visual_job, "job_sha256")
        visual_job_path = visual_workspace / "fake-expert-job.json"
        visual_job_path.write_text(json.dumps(visual_job), encoding="utf-8")
        status = self.run_cli("status", "--job", str(visual_job_path), "--json")
        self.assertEqual(status.returncode, 1)
        report = json.loads(status.stdout)
        self.assertEqual(report["code"], "job_drift")
        self.assertTrue(any(code.startswith("required_reference_hash_mismatch:") for code in report["drift"]))

    def test_old_job_requires_explicit_replan_without_rewrite(self) -> None:
        source = self.make_text_source("old-job-source.pdf")
        workspace = self.tmp / "old-job"
        planned = self.run_cli(
            "plan", "--source", str(source), "--workspace", str(workspace),
            "--profile", "native-text", "--json",
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        job_path = workspace / "fake-expert-job.json"
        old = json.loads(job_path.read_text(encoding="utf-8"))
        old["schema_version"] = "tkc.fake-expert-job/v0.1"
        old["protocol"] = "fake-expert-job-v0.1"
        old["compiler_version"] = "1.2.0-ux"
        old.pop("required_reading")
        old["job_sha256"] = fake_expert_module._hash_without(old, "job_sha256")
        job_path.write_text(json.dumps(old), encoding="utf-8")
        before = job_path.read_bytes()
        status = self.run_cli("status", "--job", str(job_path), "--json")
        self.assertEqual(status.returncode, 1)
        self.assertEqual(json.loads(status.stdout)["code"], "job_schema_upgrade_required")
        self.assertEqual(job_path.read_bytes(), before)

    def test_document_contract_and_doctor_expose_required_route(self) -> None:
        contract = fake_expert_module._document_contract_report()
        self.assertTrue(contract["passed"], contract)
        self.assertGreater(contract["required_reference_count"], 0)
        with mock.patch.dict(
            fake_expert_module.PROFILE_READING,
            {"native-text": fake_expert_module.PROFILE_READING["native-text"] + ("references/missing-contract.md",)},
        ):
            missing = fake_expert_module._document_contract_report()
        self.assertFalse(missing["passed"])
        self.assertIn("required_reference_missing:references/missing-contract.md", missing["issues"])

        doctor = self.run_cli("doctor", "--profile", "native-text", "--json")
        self.assertIn(doctor.returncode, (0, 1), doctor.stderr)
        report = json.loads(doctor.stdout)
        checks = {row["name"]: row for row in report["checks"]}
        self.assertTrue(checks["document_contract"]["passed"])
        self.assertEqual(report["compiler_version"], "1.2.0")
        self.assertTrue(report["required_reading"])

    def test_higher_stage_and_authorship_requests_remain_fail_closed(self) -> None:
        source = self.make_text_source("reviewed-source.pdf")
        workpack = self.make_complete_workpack(source, "reviewed-workpack")
        workspace = self.tmp / "reviewed-job"
        planned = self.run_cli(
            "plan", "--source", str(source), "--workspace", str(workspace),
            "--profile", "native-text", "--workpack", str(workpack),
            "--target-stage", "executable", "--json",
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        job_path = workspace / "fake-expert-job.json"
        job = json.loads(job_path.read_text(encoding="utf-8"))
        self.assertIn("references/safe-formula-execution.md", [row["path"] for row in job["required_reading"]])
        self.assertTrue(all(value is False for value in job["policy"].values()))
        compiled = self.run_cli("compile", "--job", str(job_path), "--json")
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        result = json.loads(compiled.stdout)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["code"], "independent_review_required")
        self.assertIn("seal_required", result["required_gates"])

    def test_backend_selection_and_runtime_gate_are_explicit(self) -> None:
        missing_backend = self.run_cli(
            "plan", "--source", str(self.source), "--workspace", str(self.tmp / "missing-backend"),
            "--profile", "visual", "--json",
        )
        self.assertEqual(missing_backend.returncode, 2)
        self.assertIn("backend_required_for_profile", missing_backend.stderr)

        workspace = self.tmp / "scan-job"
        planned = self.run_cli(
            "plan", "--source", str(self.source), "--workspace", str(workspace),
            "--profile", "scan-text", "--backend", "paddleocr-ppocrv6", "--json",
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        job = json.loads((workspace / "fake-expert-job.json").read_text(encoding="utf-8"))
        self.assertEqual(job["backend"]["name"], "paddleocr-ppocrv6")
        self.assertIsNone(job["backend"]["runtime_receipt"])
        compiled = self.run_cli("compile", "--job", str(workspace / "fake-expert-job.json"), "--json")
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        result = json.loads(compiled.stdout)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["code"], "runtime_qualification_required")

    def test_native_profile_rejects_image_only_source_even_with_matching_workpack(self) -> None:
        source = self.make_image_source()
        workpack = self.make_complete_workpack(source, "image-workpack")
        workspace = self.tmp / "image-job"
        planned = self.run_cli(
            "plan", "--source", str(source), "--workspace", str(workspace),
            "--profile", "native-text", "--workpack", str(workpack), "--json",
        )
        self.assertEqual(planned.returncode, 1, planned.stderr)
        plan = json.loads(planned.stdout)
        self.assertEqual(plan["status"], "paused")
        self.assertEqual(plan["code"], "source_profile_mismatch")
        self.assertIn("select_explicit_scan_profile", plan["required_gates"])
        job_path = workspace / "fake-expert-job.json"
        job = json.loads(job_path.read_text(encoding="utf-8"))
        self.assertEqual(job["state"]["status"], "paused")
        self.assertEqual(job["state"]["code"], "source_profile_mismatch")
        dry_run = self.run_cli("compile", "--job", str(job_path), "--dry-run", "--json")
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        result = json.loads(dry_run.stdout)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["code"], "source_profile_mismatch")
        self.assertNotIn("direct_reference_build_ready", result.get("planned_commands", []))

    def test_native_profile_accepts_text_layer_for_direct_route(self) -> None:
        source = self.make_text_source()
        workpack = self.make_complete_workpack(source, "text-workpack")
        workspace = self.tmp / "text-job"
        planned = self.run_cli(
            "plan", "--source", str(source), "--workspace", str(workspace),
            "--profile", "native-text", "--workpack", str(workpack), "--json",
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        job_path = workspace / "fake-expert-job.json"
        import direct_reference_skill

        with mock.patch.object(direct_reference_skill, "build_direct_reference", return_value={
            "status": "draft", "artifact_stage": "draft-distributable", "object_count": 1, "anchor_count": 1,
        }) as builder:
            result = fake_expert_module._run_job(
                type("Args", (), {"job": str(job_path), "dry_run": False, "progress": False})(),
                resume=False,
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["code"], "draft_compiled")
        self.assertEqual(builder.call_args.args[0], source.resolve())
        self.assertEqual(builder.call_args.args[1], workpack.resolve())

    def test_gold_update_is_rejected_and_bound_mode_reaches_direct_builder(self) -> None:
        source = self.make_text_source("gold-source.pdf")
        revision = self.tmp / "gold-revision.json"
        self.write_json(revision, {"schema_version": "test.gold/v0.1"})
        rejected = self.run_cli(
            "plan", "--source", str(source), "--workspace", str(self.tmp / "update"),
            "--profile", "native-text", "--gold-mode", "update", "--gold-revision", str(revision), "--json",
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("gold_update_requires_existing_base", rejected.stderr)

        workpack = self.tmp / "workpack"
        workpack.mkdir()
        workspace = self.tmp / "reuse-job"
        planned = self.run_cli(
            "plan", "--source", str(source), "--workspace", str(workspace),
            "--profile", "native-text", "--workpack", str(workpack),
            "--gold-mode", "reuse", "--gold-revision", str(revision), "--json",
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        job_path = workspace / "fake-expert-job.json"
        ready = {
            "status": "ready-to-compile",
            "code": "direct_reference_build_ready",
            "job_id": json.loads(job_path.read_text(encoding="utf-8"))["job_id"],
            "workpack": str(workpack),
            "next_action": {"code": "build_direct_reference_draft", "meaning": "test"},
            "required_gates": [],
        }
        import direct_reference_skill

        with mock.patch.object(fake_expert_module, "_evaluate_job", return_value=ready), mock.patch.object(
            direct_reference_skill,
            "build_direct_reference",
            return_value={"status": "draft", "artifact_stage": "draft-distributable", "object_count": 1, "anchor_count": 1},
        ) as builder:
            result = fake_expert_module._run_job(
                type("Args", (), {"job": str(job_path), "dry_run": False, "progress": False})(),
                resume=False,
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(builder.call_args.kwargs["gold_mode"], "reuse")
        self.assertEqual(builder.call_args.kwargs["gold_revision"], revision.resolve())
        updated = json.loads(job_path.read_text(encoding="utf-8"))
        self.assertEqual(result["job_sha256"], updated["job_sha256"])
        self.assertEqual(result["job_file_sha256"], fake_expert_module._sha256_file(job_path))

    def test_existing_output_is_authoritatively_validated(self) -> None:
        source = self.make_text_source("output-source.pdf")
        workpack = self.tmp / "workpack"
        workpack.mkdir()
        workspace = self.tmp / "output-check"
        planned = self.run_cli(
            "plan", "--source", str(source), "--workspace", str(workspace),
            "--profile", "native-text", "--workpack", str(workpack), "--json",
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        job_path = workspace / "fake-expert-job.json"
        job = json.loads(job_path.read_text(encoding="utf-8"))
        Path(job["outputs"]["skill"]).mkdir()
        with mock.patch.object(fake_expert_module, "_workpack_summary", return_value={"kind": "semantic", "complete": True}):
            result = fake_expert_module._evaluate_job(job_path.resolve(), job)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["code"], "draft_output_invalid", result)

    def test_empty_runtime_and_structure_hints_do_not_clear_gates(self) -> None:
        runtime_result = fake_expert_module._runtime_qualification_gate({
            "profile": "scan-text",
            "backend": {"name": "paddleocr-ppocrv6", "runtime_receipt": None},
        })
        self.assertEqual(runtime_result["code"], "runtime_qualification_required")

        workpack = self.tmp / "structure-workpack"
        workpack.mkdir()
        (workpack / "external-scan-structure-review-fragment.json").write_text("", encoding="utf-8")
        empty = fake_expert_module._scan_structure_review_gate(workpack, "a" * 64)
        self.assertEqual(empty["code"], "structure_review_required")
        (workpack / "external-scan-structure-review-fragment.json").write_text("{}\n", encoding="utf-8")
        hint_only = fake_expert_module._scan_structure_review_gate(workpack, "a" * 64)
        self.assertEqual(hint_only["code"], "structure_review_validator_required")

    def test_semantic_submission_requires_exact_declarations_and_hides_predictions(self) -> None:
        import semantic_review_workbench

        root = self.tmp / "semantic-workbench"
        root.mkdir()
        queue = {
            "item_ref": "assertion-1",
            "review_input_sha256": "b" * 64,
            "assertion_bundle_sha256": "c" * 64,
            "assertion_ids": [],
            "requires_formula_check": False,
            "item_kind": "assertion",
        }
        manifest = {
            "schema_version": "tkc.semantic-review-workbench/v0.1",
            "workbench_id": "semantic-workbench-1",
            "reviewer_instance": "reviewer-a",
            "review_plan_sha256": "a" * 64,
            "assigned_item_refs": ["assertion-1"],
        }
        self.write_json(root / "data.json", {"items": [{"queue": queue}]})
        declarations = {name: True for name in semantic_review_workbench.DECLARATIONS}
        row = {
            "item_ref": "assertion-1",
            "review_input_sha256": queue["review_input_sha256"],
            "assertion_bundle_sha256": queue["assertion_bundle_sha256"],
            "verdict": "accepted",
            "evidence_support": "full",
            "issue_codes": [],
            "rationale": "The reviewer checked the bound source evidence for this assertion.",
            "confidence": {"extraction": 0.9, "interpretation": 0.9},
            "review_checks": {name: True for name in semantic_review_workbench.REVIEW_CHECKS},
            "differences": [],
        }
        submission = {
            "schema_version": "tkc.semantic-review-workbench-submission/v0.1",
            "workbench_id": manifest["workbench_id"],
            "reviewer_instance": manifest["reviewer_instance"],
            "review_plan_sha256": manifest["review_plan_sha256"],
            "exported_at": "2026-08-31T00:00:00+08:00",
            "declarations": declarations,
            "items": [row],
        }
        submission_path = root / "review-submission.json"
        self.write_json(submission_path, submission)
        valid = fake_expert_module._validate_semantic_submission(root, submission_path, manifest)
        self.assertTrue(valid["accepted"])

        missing_declaration = json.loads(submission_path.read_text(encoding="utf-8"))
        missing_declaration["declarations"] = {}
        self.write_json(submission_path, missing_declaration)
        with self.assertRaisesRegex(fake_expert_module.FakeExpertError, "semantic_submission_declarations_invalid"):
            fake_expert_module._validate_semantic_submission(root, submission_path, manifest)

        hidden = json.loads(submission_path.read_text(encoding="utf-8"))
        hidden["declarations"] = declarations
        hidden["items"][0]["prediction"] = "must-not-be-imported"
        self.write_json(submission_path, hidden)
        with self.assertRaisesRegex(fake_expert_module.FakeExpertError, "semantic_submission_hidden_field:prediction"):
            fake_expert_module._validate_semantic_submission(root, submission_path, manifest)

    def test_visual_submission_requires_exact_declarations_and_rejects_hidden_fields(self) -> None:
        root = self.tmp / "visual-workbench"
        (root / "data").mkdir(parents=True)
        identity = {
            "workpack_sha256": "f" * 64,
            "phase7d5a_manifest_sha256": "a" * 64,
            "candidate_items_file_sha256": "b" * 64,
            "split_commitment_sha256": "c" * 64,
            "review_plan_sha256": "d" * 64,
        }
        reviewer_id = "reviewer-a"
        session_id = "session-a"
        item = {
            "item_id": "visual-1",
            "item_sha256": "1" * 64,
            "review_task_id": "task-1",
            "candidate_type": "equation",
            "source_sha256": "2" * 64,
            "bbox_sha256": "3" * 64,
            "context": {"sha256": "4" * 64},
            "crop": {"sha256": "5" * 64},
            "geometry": {
                "page_width_points": 612,
                "page_height_points": 792,
                "candidate_bbox_pdf": [10, 10, 100, 100],
            },
        }
        data_path = root / "data" / "reviewer-workpack.json"
        self.write_json(data_path, {"items": [item]})
        instance_id = reviewer_workbench._reviewer_instance_id(identity["workpack_sha256"], reviewer_id, session_id)
        manifest = {
            "schema_version": "tkc.visual-review-workbench-manifest/v0.1",
            "protocol": "visual-review-workbench-v0.1",
            "compiler_version": "test",
            "workbench_instance_id": instance_id,
            "workpack_identity": identity,
            "data_file_sha256": fake_expert_module._sha256_file(data_path),
        }
        declarations = {name: True for name in reviewer_workbench.DECLARATION_KEYS}
        review = {
            "review_status": "complete",
            "presence": "present",
            "type_verdict": "equation",
            "bbox_verdict": "correct",
            "reviewer_confidence": "high",
            "notes": "The reviewer inspected the bound context and crop.",
            "difference_observation": "No difference observed after inspecting the specified evidence.",
            "equation_type": "display",
            "formula_number": "none",
            "crosses_lines": "no",
            "subscripts": "none",
            "superscripts": "none",
            "special_symbols": "checked",
            "transcription_completeness": "complete",
            "transcription_latex": "x = y",
        }
        submission = {
            "schema_version": "tkc.visual-review-workbench-submission/v0.1",
            "protocol": "visual-review-workbench-v0.1",
            "workbench_instance_id": instance_id,
            "reviewer_instance": reviewer_id,
            "review_session_id": session_id,
            "workpack_identity": identity,
            "declarations": declarations,
            "items": [{
                "item_id": item["item_id"],
                "item_sha256": item["item_sha256"],
                "review_task_id": item["review_task_id"],
                "input_hashes": {
                    "source_sha256": item["source_sha256"],
                    "render_sha256": item["context"]["sha256"],
                    "crop_sha256": item["crop"]["sha256"],
                    "bbox_sha256": item["bbox_sha256"],
                },
                "review": review,
            }],
            "exported_at": "2026-08-31T00:00:00+08:00",
        }
        submission_path = root / "review-submission.json"
        self.write_json(submission_path, submission)
        valid = fake_expert_module._validate_visual_submission(root, submission_path, manifest)
        self.assertTrue(valid["accepted"], valid)

        missing_declaration = json.loads(submission_path.read_text(encoding="utf-8"))
        missing_declaration["declarations"] = {}
        self.write_json(submission_path, missing_declaration)
        with self.assertRaisesRegex(fake_expert_module.FakeExpertError, "visual_submission_declarations_invalid"):
            fake_expert_module._validate_visual_submission(root, submission_path, manifest)

        hidden = json.loads(submission_path.read_text(encoding="utf-8"))
        hidden["declarations"] = declarations
        hidden["items"][0]["model_output"] = "must-not-be-imported"
        self.write_json(submission_path, hidden)
        with self.assertRaisesRegex(fake_expert_module.FakeExpertError, "visual_submission_hidden_field:model_output"):
            fake_expert_module._validate_visual_submission(root, submission_path, manifest)

    def test_status_all_requires_narrow_explicit_workspace(self) -> None:
        broad = self.run_cli("status", "--workspace", str(ROOT), "--all", "--json")
        self.assertEqual(broad.returncode, 2)
        self.assertIn("workspace_too_broad", broad.stderr)

        workspace = self.tmp / "jobs"
        planned = self.run_cli(
            "plan", "--source", str(self.source), "--workspace", str(workspace),
            "--profile", "visual", "--backend", "pdftoppm", "--json",
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        all_status = self.run_cli("status", "--workspace", str(workspace), "--all", "--json")
        self.assertEqual(all_status.returncode, 0, all_status.stderr)
        report = json.loads(all_status.stdout)
        self.assertEqual(report["count"], 1)
        self.assertEqual(report["jobs"][0]["profile"], "visual")

    def test_profile_doctor_keeps_backend_advice_separate(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [sys.executable, str(PREFLIGHT), "--profile", "scan-structure", "--json"],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertIn(result.returncode, (0, 1))
        report = json.loads(result.stdout)
        self.assertEqual(report["profile"], "scan-structure")
        self.assertFalse(report["passed"])
        self.assertFalse(report["backend"]["selection_valid"])
        self.assertEqual(report["backend"]["code"], "backend_selection_required")
        self.assertEqual(report["compiler_policy"]["silent_backend_fallback"], False)
        optional = [row for row in report["checks"] if row.get("optional")]
        self.assertTrue(optional)
        self.assertTrue(all("applies_to" in row and "recommendation" in row for row in optional))

        renderer = self.tmp / "pdftoppm-fixture"
        renderer.write_text("#!/bin/sh\necho 'fixture pdftoppm 1.0' >&2\nexit 0\n", encoding="utf-8")
        renderer.chmod(0o700)
        selected = subprocess.run(
            [
                sys.executable, str(PREFLIGHT), "--pdftoppm", str(renderer),
                "--profile", "visual", "--backend", "pdftoppm", "--json",
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(selected.returncode, 1)
        selected_report = json.loads(selected.stdout)
        self.assertTrue(selected_report["backend"]["prerequisites_passed"])
        self.assertFalse(selected_report["passed"])
        self.assertIn("runtime_qualification_required", selected_report["blocking"])

        source_checked = subprocess.run(
            [
                sys.executable, str(PREFLIGHT), "--source", str(self.source),
                "--profile", "native-text", "--json",
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(source_checked.returncode, 1)
        source_report = json.loads(source_checked.stdout)["source"]
        self.assertEqual(source_report["code"], "source_requires_ocr")
        for key in ("code", "meaning", "next_action", "remediation"):
            self.assertIn(key, source_report)

    @unittest.skipUnless(SCAN_WORKBENCH.is_dir() and SCAN_SUBMISSION.is_file(), "private workbench fixture unavailable")
    def test_review_route_validates_existing_scan_submission_read_only(self) -> None:
        listed_root = self.tmp / "workbenches"
        for name, manifest_name, schema in (
            ("semantic", "manifest.json", "tkc.semantic-review-workbench/v0.1"),
            ("visual", "workbench-manifest.json", "tkc.visual-review-workbench-manifest/v0.1"),
            ("scan", "workbench.json", "tkc.scan-structure-review-workbench/v0.1"),
        ):
            workbench = listed_root / name
            workbench.mkdir(parents=True)
            (workbench / manifest_name).write_text(
                json.dumps({"schema_version": schema, "protocol": "test", "compiler_version": "test", "workbench_id": name, "item_count": 0}),
                encoding="utf-8",
            )
        listed = self.run_cli("review", "list", "--workspace", str(listed_root), "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        kinds = {row["kind"] for row in json.loads(listed.stdout)["workbenches"]}
        self.assertTrue({"semantic", "visual", "scan-structure"}.issubset(kinds))

        valid = self.run_cli(
            "review", "validate-submission", "--workbench", str(SCAN_WORKBENCH),
            "--submission", str(SCAN_SUBMISSION), "--json",
        )
        self.assertEqual(valid.returncode, 0, valid.stderr)
        result = json.loads(valid.stdout)
        self.assertEqual(result["status"], "valid")
        self.assertTrue(result["accepted"])
        self.assertFalse(result["writes"])

        tampered = self.tmp / "tampered-submission.json"
        value = json.loads(SCAN_SUBMISSION.read_text(encoding="utf-8"))
        value["items"][0]["input_sha256"] = "0" * 64
        tampered.write_text(json.dumps(value), encoding="utf-8")
        rejected = self.run_cli(
            "review", "validate-submission", "--workbench", str(SCAN_WORKBENCH),
            "--submission", str(tampered), "--json",
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("submission_item_hash_mismatch", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
