from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import venv

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "fake-expert/scripts"
sys.path.insert(0, str(SCRIPTS))
import intake_workflow
from compiler_version import OCR_LOCAL_CONFIG_SCHEMA
from native_intake import inspect_source
from ocr_availability import inspect_ocr, probe_interpreter
from paddle_runtime_contract import DEFAULT_PADDLE_DISTRIBUTIONS, PROFILE_MODEL_DIRS, TECHNICAL_TEXT_PROFILE_ID, _png_encode_rgb
from paddle_runtime_qualification import run_qualification


@unittest.skipUnless(shutil.which("pdftoppm"), "canonical local renderer required")
class IntakeWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        environment_patch = patch.dict(os.environ, {"FAKE_EXPERT_OCR_CONFIG": ""})
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "mixed.pdf"
        native = self.root / "native.pdf"
        pdf = canvas.Canvas(str(native), pagesize=(200, 200))
        pdf.drawString(10, 180, "1 Native fixture")
        pdf.drawString(10, 150, "A controlled system has stable bounded output.")
        pdf.showPage()
        pdf.save()
        from pypdf import PdfReader
        writer = PdfWriter()
        writer.add_page(PdfReader(native).pages[0])
        writer.add_blank_page(width=200, height=200)
        image_page = writer.add_blank_page(width=200, height=200)
        image = DecodedStreamObject()
        image.set_data(b"\x00\x00\x00")
        image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                      NameObject("/Width"): NumberObject(1), NameObject("/Height"): NumberObject(1),
                      NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
        image_page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Im"): writer._add_object(image)})})
        content = DecodedStreamObject()
        content.set_data(b"q 200 0 0 200 0 0 cm /Im Do Q")
        image_page[NameObject("/Contents")] = writer._add_object(content)
        writer.add_page(PdfReader(native).pages[0])
        writer.write(self.source)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def cli(self, *args: object) -> dict:
        run = subprocess.run([sys.executable, str(SCRIPTS / "fake_expert.py"), *map(str, args), "--json"],
                             capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertIn(run.returncode, (0, 1), run.stderr + run.stdout)
        return json.loads(run.stdout)

    def plan(self, name: str, config: Path | None = None) -> dict:
        args = ["intake", "plan", "--source", self.source, "--workspace", self.root / name,
                "--figures", "none", "--created-at", "2026-11-23T00:00:00Z"]
        if config:
            args += ["--ocr-config", config]
        return self.cli(*args)

    def synthetic_environment(self, fail_page: int | None = None) -> tuple[Path, Path]:
        """Create metadata and inert weight bytes; never install/load a model."""
        environment = self.root / "external-venv"
        venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
        python = environment / "bin/python"
        purelib = Path(subprocess.check_output([str(python), "-I", "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"], text=True).strip())
        for name, version in DEFAULT_PADDLE_DISTRIBUTIONS.items():
            dist = purelib / f"{name.replace('-', '_')}-{version}.dist-info"
            dist.mkdir()
            (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        models = self.root / "synthetic-models"
        for directory in PROFILE_MODEL_DIRS[TECHNICAL_TEXT_PROFILE_ID].values():
            child = models / directory
            child.mkdir(parents=True)
            (child / "inference.pdmodel").write_bytes(b"synthetic inert weight bytes; no engine")
        tools = self.root / "synthetic-tools"
        tools.mkdir()
        qualification_script = tools / "paddle_runtime_qualification.py"
        shutil.copyfile(SCRIPTS / qualification_script.name, qualification_script)
        worker = tools / "ocr_backend_worker.py"
        shutil.copyfile(ROOT / "tests/fixtures/intake_synthetic_worker.py", worker)
        if fail_page is not None:
            worker.write_text(worker.read_text().replace("FAIL_PAGE = None", f"FAIL_PAGE = {fail_page}"))
        worker.with_suffix(".scripts").write_text(str(SCRIPTS))
        fixture = self.root / "qualification-crop.png"
        fixture.write_bytes(_png_encode_rgb(64, 64, b"\xff" * 64 * 64 * 3))
        runtime = self.root / "runtime"
        runtime.mkdir()
        spec = importlib.util.spec_from_file_location("synthetic_intake_qualification", qualification_script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        plan = module.build_qualification_plan(backend_id="paddleocr-ppocrv6", interpreter=python,
                                               runtime_root=runtime, model_root=models, fixture=fixture,
                                               planned_at="2026-11-23T00:00:00Z", startup_timeout_seconds=5,
                                               per_request_timeout_seconds=5, total_timeout_seconds=20)
        receipt, _private = run_qualification(plan, tested_at="2026-11-23T00:00:00Z", exclusive_heavy_model_confirmed=True)
        self.assertEqual(receipt["qualification"]["status"], "qualified", receipt["qualification"])
        plan_path, receipt_path = self.root / "qualification-plan.json", self.root / "qualification-receipt.json"
        plan_path.write_text(json.dumps(plan)); receipt_path.write_text(json.dumps(receipt))
        config = self.root / "ocr-config.json"
        config.write_text(json.dumps({"schema_version": OCR_LOCAL_CONFIG_SCHEMA, "backend": "paddleocr-ppocrv6",
                                     "python": str(python), "model_root": str(models), "runtime_root": str(runtime),
                                     "worker": str(worker), "qualification_plan": str(plan_path), "qualification_receipt": str(receipt_path)}))
        return config, worker

    def test_no_environment_choice_persists_and_partial_children_keep_original_scope(self) -> None:
        inspected = self.cli("intake", "inspect", "--source", self.source)
        self.assertEqual(inspected["page_summary"]["pages"]["needs_ocr"], [3])
        self.assertEqual(inspected["batch_advice"]["ocr_page_count"], 1)
        planned = self.plan("partial")
        self.assertEqual(planned["status"], "awaiting_choice")
        workflow = Path(planned["workflow"])
        self.cli("intake", "choose", "--workflow", workflow, "--choice", "configure_environment")
        status = self.cli("intake", "resume", "--workflow", workflow)
        self.assertEqual(status["status"], "awaiting_environment")
        self.assertEqual(status["choice"], "configure_environment")
        self.assertEqual(status["choice_request"]["status"], "resolved")
        selected = self.cli("intake", "choose", "--workflow", workflow, "--choice", "native_ranges")
        self.assertEqual(selected["native_ranges"], [[1, 2], [4, 4]])
        before = workflow.read_bytes()
        for command in (("compile", "--workflow"), ("intake", "run", "--workflow"), ("intake", "resume", "--workflow")):
            dry = self.cli(*command, workflow, "--dry-run")
            self.assertTrue(dry["dry_run"])
            self.assertEqual(workflow.read_bytes(), before)
            self.assertFalse((workflow.parent / "children").exists())
        run = self.cli("intake", "run", "--workflow", workflow)
        self.assertTrue(run["partial"])
        self.assertEqual([row["physical_page"] for row in run["omitted_pages"]], [3])
        self.assertEqual(len(run["page_ledger"]), 4)
        self.assertEqual([child["physical_pages"] for child in run["children"]], [[1, 2], [4, 4]])
        self.assertTrue(all(child["code"] == "semantic_authoring_required" for child in run["children"]), run["children"])
        for child in run["children"]:
            manifest = json.loads((Path(child["workpack"]) / "workpack.json").read_text())
            self.assertEqual(manifest["source"]["sha256"], run["source"]["sha256"])
        self.assertFalse(run["whole_book_complete"])

    def test_blank_only_and_manual_only_do_not_recommend_ocr_installation(self) -> None:
        writer = PdfWriter(); writer.add_blank_page(width=200, height=200); writer.write(self.source)
        blank = self.plan("blank-only")
        self.assertEqual(blank["code"], "no_content")
        self.assertEqual(blank["route"], "inspection_only")
        self.assertEqual(blank["ocr_availability"]["status"], "not_required")
        writer = PdfWriter(); page = writer.add_blank_page(width=200, height=200)
        stream = DecodedStreamObject(); stream.set_data(b"0.001 w 1 1 m 20 20 l S")
        page[NameObject("/Contents")] = writer._add_object(stream); writer.write(self.source)
        manual = self.plan("manual-only")
        self.assertEqual(manual["code"], "page_review_required")
        self.assertNotIn("configure_environment", [row["choice"] for row in manual["choice_request"]["options"]])

    def test_synthetic_external_qualification_scan_build_verify_and_resume(self) -> None:
        config, worker = self.synthetic_environment()
        calls = worker.with_suffix(".calls")
        before = calls.read_text().splitlines()
        available = inspect_ocr(config)
        self.assertTrue(available["runnable"], available)
        self.assertEqual(available["interpreter"]["launcher"], str(self.root / "external-venv/bin/python"))
        self.assertNotEqual(available["interpreter"]["launcher"], available["interpreter"]["real_binary"])
        self.assertEqual(calls.read_text().splitlines(), before)
        planned = self.plan("scan", config)
        workflow = Path(planned["workflow"])
        self.assertEqual(planned["route"], "ocr")
        blocked = self.cli("intake", "run", "--workflow", workflow)
        self.assertEqual(blocked["code"], "ocr_execution_authorization_required")
        self.assertEqual(calls.read_text().splitlines(), before)
        snapshot = workflow.read_bytes()
        dry = self.cli("compile", "--workflow", workflow, "--authorize-ocr", "--exclusive-heavy-model", "--dry-run")
        self.assertTrue(dry["dry_run"])
        self.assertEqual(workflow.read_bytes(), snapshot)
        self.assertEqual(calls.read_text().splitlines(), before)
        run = self.cli("intake", "run", "--workflow", workflow, "--authorize-ocr", "--exclusive-heavy-model")
        self.assertEqual(run["code"], "structure_review_required", run)
        self.assertEqual(len(calls.read_text().splitlines()), len(before) + 1)
        self.assertEqual(run["scan_attempts"][0]["checkpoint_status"], "complete")
        after = calls.read_text()
        resumed = self.cli("intake", "resume", "--workflow", workflow)
        self.assertEqual(resumed["code"], "structure_review_required")
        self.assertEqual(calls.read_text(), after)
        self.assertFalse(resumed["whole_book_complete"])

    def test_real_synthetic_worker_batches_and_checkpoint_only_resume_pending_pages(self) -> None:
        config, worker = self.synthetic_environment()
        from pypdf import PdfReader
        image_page = PdfReader(self.source).pages[2]
        writer = PdfWriter()
        for _index in range(7):
            writer.add_page(image_page)
        writer.write(self.source)
        planned = self.plan("batched", config)
        workflow = Path(planned["workflow"])
        calls = worker.with_suffix(".calls")
        before = len(calls.read_text().splitlines())
        first = self.cli("intake", "run", "--workflow", workflow, "--authorize-ocr", "--exclusive-heavy-model")
        self.assertEqual(first["code"], "ocr_checkpoint_incomplete", first)
        self.assertEqual(len(calls.read_text().splitlines()) - before, 5)
        self.assertEqual(first["ocr_progress"]["completed_pages"], [1, 2, 3, 4, 5])
        self.assertEqual(first["ocr_progress"]["remaining_pages"], [6, 7])
        self.assertEqual(first["scan_attempts"][0]["scope"], [1, 5])
        second = self.cli("intake", "resume", "--workflow", workflow)
        self.assertEqual(second["code"], "structure_review_required", second)
        self.assertEqual(len(calls.read_text().splitlines()) - before, 7)
        self.assertEqual(second["ocr_progress"]["remaining_pages"], [])
        self.assertEqual(len(second["scan_attempts"]), 2)
        self.assertEqual(second["scan_attempts"][1]["scope"], [6, 7])
        self.assertFalse(second["whole_book_complete"])

    def test_automatic_plan_rejects_legacy_options_instead_of_ignoring_them(self) -> None:
        for name, value in (("--backend", "paddleocr-ppocrv6"),
                            ("--target-stage", "ready"), ("--gold-mode", "reuse")):
            with self.subTest(option=name):
                result = subprocess.run([sys.executable, str(SCRIPTS / "fake_expert.py"), "plan", "--source", str(self.source),
                                         "--workspace", str(self.root / "unsupported"), name, value, "--json"], text=True, capture_output=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn("requires_explicit_profile", result.stderr)
                self.assertFalse((self.root / "unsupported").exists())
        result = subprocess.run([sys.executable, str(SCRIPTS / "fake_expert.py"), "plan", "--source", str(self.source),
                                 "--workspace", str(self.root / "unsupported"), "--profile", "native-text",
                                 "--ocr-config", str(self.root / "config.json"), "--json"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("automatic_intake_options_require_automatic_route", result.stderr)

    def test_binding_drift_and_target_protection(self) -> None:
        planned = self.plan("drift")
        workflow = Path(planned["workflow"])
        self.source.write_bytes(self.source.read_bytes() + b"\n% drift")
        status = self.cli("intake", "status", "--workflow", workflow)
        self.assertEqual(status["code"], "intake_binding_drift_replan")
        marker = self.root / "occupied"; marker.mkdir(); (marker / "keep").write_text("user")
        result = subprocess.run([sys.executable, str(SCRIPTS / "fake_expert.py"), "intake", "plan", "--source", str(self.source),
                                 "--workspace", str(marker), "--created-at", "2026-11-23T00:00:00Z", "--json"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual((marker / "keep").read_text(), "user")

    def test_alias_paths_environment_config_and_missing_weights_are_explicit(self) -> None:
        config, worker = self.synthetic_environment()
        alias = self.root / "directory-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        data = json.loads(config.read_text())
        for key in ("python", "model_root", "runtime_root", "worker"):
            data[key] = str(alias / Path(data[key]).relative_to(self.root))
        config.write_text(json.dumps(data))
        report = inspect_ocr(config)
        self.assertTrue(report["runnable"], report)
        self.assertIn("directory-alias", report["interpreter"]["launcher"])
        with patch.dict(os.environ, {"FAKE_EXPERT_OCR_CONFIG": str(config)}):
            selected = self.plan("environment-default")
            self.assertEqual(selected["route"], "ocr")
            overridden = self.plan("explicit-missing", self.root / "missing-config")
            self.assertEqual(overridden["status"], "awaiting_choice")
            self.assertIn("ocr_configuration_missing", [row["code"] for row in overridden["ocr_availability"]["checks"]])
        for weight in (self.root / "synthetic-models").rglob("*.pdmodel"):
            weight.rename(weight.with_name("README.txt"))
        report = inspect_ocr(config)
        self.assertFalse(report["runnable"])
        self.assertTrue(any(row["code"] == "model_missing" and row.get("details", {}).get("code") == "model_weight_files_missing" for row in report["checks"]))
        self.assertEqual(len(worker.with_suffix(".calls").read_text().splitlines()), 3)

    def test_closed_state_shape_document_gate_and_retained_structure_diagnostic(self) -> None:
        from pdf_parser_adapters import sha256_json
        record = self.plan("shape")
        path = Path(record["workflow"])
        value = json.loads(path.read_text())
        value["state"]["automatic_review"] = True
        value["workflow_sha256"] = sha256_json({key: item for key, item in value.items() if key != "workflow_sha256"})
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "intake_workflow_schema_invalid"):
            intake_workflow.load(path)
        import fake_expert
        import contextlib
        import io
        for prefix in (("plan",), ("intake", "plan")):
            with patch.object(fake_expert, "check_document_contract", return_value={"passed": False}), contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                code = fake_expert.main([*prefix, "--source", str(self.source), "--workspace", str(self.root / "doc-gated"), "--created-at", "2026-11-23T00:00:00Z", "--json"])
                self.assertEqual(code, 2)
                self.assertFalse((self.root / "doc-gated").exists())
        from pypdf import PdfReader
        writer = PdfWriter(); writer.add_page(PdfReader(self.source).pages[0]); writer.add_outline_item("Missing body title", 0); writer.write(self.source)
        plan = self.plan("structure-failure")
        result = self.cli("intake", "run", "--workflow", plan["workflow"])
        failure = result["children"][0]
        self.assertEqual(failure["code"], "native_child_requires_attention")
        audit = Path(failure["audit_path"])
        self.assertTrue((audit / "failure.json").is_file())
        self.assertTrue(list(audit.rglob("structure-resolution-required.jsonl")))
        self.assertFalse((Path(plan["workflow"]).parent / "children/pages-0001-0001").exists())

    def test_real_worker_failure_stops_for_repair_with_completed_checkpoint(self) -> None:
        config, _worker = self.synthetic_environment(fail_page=2)
        from pypdf import PdfReader
        image = PdfReader(self.source).pages[2]
        writer = PdfWriter(); writer.add_page(image); writer.add_page(image); writer.write(self.source)
        plan = self.plan("worker-failure", config)
        result = self.cli("intake", "run", "--workflow", plan["workflow"], "--authorize-ocr", "--exclusive-heavy-model")
        self.assertEqual(result["code"], "ocr_worker_repair_required", result)
        self.assertEqual(result["next_action"], "repair_worker_then_explicit_resume")
        self.assertEqual(result["ocr_progress"]["completed_pages"], [1])
        self.assertEqual(result["ocr_progress"]["remaining_pages"], [2])
        self.assertTrue((Path(result["scan_attempts"][0]["path"]) / "scan-checkpoint.json").is_file())


if __name__ == "__main__":
    unittest.main()
