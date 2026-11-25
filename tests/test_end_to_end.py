"""Unified CLI, packaging, compatibility, and pin checks (HANDOFF steps 3, 5, 6)."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from _support import (
    CREATED_AT, FAKE_MODEL, HEAT_BOOK, LEGACY_PACKAGE, SCRIPTS, TOY_EMBED, TempDirTestCase,
    query_package, requires_renderer, run_cli, run_json,
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@requires_renderer
class UnifiedCliTests(unittest.TestCase):
    """plan -> compile (extract) -> explicit authoring command -> compile -> query."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="fake-expert-e2e-")
        cls.workspace = Path(cls._tmp.name) / "job"
        cls.plan = run_json(
            "fake_expert.py", "plan", "--source", str(HEAT_BOOK), "--workspace", str(cls.workspace),
            "--profile", "native-text", "--name", "heat-ref", "--display-name", "Heat Reference",
            "--domain", "heat-transfer", "--version", "0.1.0", "--json",
        )
        cls.job = cls.workspace / "fake-expert-job.json"
        cls.dry_run = run_json("fake_expert.py", "compile", "--job", str(cls.job), "--dry-run", "--json")
        cls.files_after_dry_run = sorted(path.name for path in cls.workspace.rglob("*") if path.is_file())
        cls.first = run_json("fake_expert.py", "compile", "--job", str(cls.job), "--json")
        # ``status`` exits 1 for a paused job by design; the JSON is still emitted.
        cls.status = json.loads(run_cli("fake_expert.py", "status", "--job", str(cls.job), "--json", check=False).stdout)
        cls.authoring = run_json(
            "semantic_authoring.py", "run", "--workpack", str(cls.workspace / "extraction/workpack"),
            "--command", shlex.join([sys.executable, str(FAKE_MODEL)]), "--proposer-instance", "fake-model-1",
            "--source", str(HEAT_BOOK), "--json",
        )
        cls.pending = run_json("fake_expert.py", "compile", "--job", str(cls.job), "--json")
        run_json("semantic_authoring.py", "run", "--workpack", str(cls.workspace / "extraction/workpack"),
                 "--command", shlex.join([sys.executable, str(FAKE_MODEL), "--text-only-values"]),
                 "--proposer-instance", "fake-model-1", "--rerun", "--replace-drafts", "--source", str(HEAT_BOOK), "--json")
        cls.second = run_json("fake_expert.py", "compile", "--job", str(cls.job), "--json")
        cls.package = cls.workspace / "fake-heat-ref"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_dry_run_writes_nothing(self) -> None:
        self.assertEqual(self.dry_run["code"], "semantic_authoring_required")
        self.assertEqual(self.dry_run["extraction"]["state"], "pending")
        self.assertEqual(len(self.dry_run["would_write"]), 3)
        self.assertEqual(self.files_after_dry_run, ["fake-expert-job.json"])

    def test_first_compile_extracts_and_pauses_for_authoring(self) -> None:
        self.assertEqual(self.first["status"], "paused")
        self.assertEqual(self.first["extraction"]["state"], "awaiting-drafts")
        self.assertEqual(self.first["extraction_run"]["unit_count"], 5)
        self.assertEqual(self.first["extraction_run"]["figure_count"], 4)
        self.assertIn("semantic_authoring.py", self.first["next_action"]["meaning"])
        self.assertEqual(self.status["code"], "semantic_authoring_required")
        self.assertEqual(self.status["drift"], [])
        crops = list((self.workspace / "extraction/workpack/crops").glob("*.png"))
        self.assertEqual(len(crops), 4)

    def test_authoring_ingest_drops_and_validates(self) -> None:
        ingest = self.authoring["ingest"]
        self.assertTrue(ingest["validation"]["passed"], ingest["validation"]["errors"])
        codes = {problem["code"] for unit in ingest["units"] for problem in unit["problems"]}
        self.assertTrue({"object_dropped_no_evidence", "object_pending_numeric_evidence"} <= codes)
        warnings = {row["code"] for row in ingest["validation"]["warnings"]}
        self.assertIn("numeric_review_required", warnings)

    def test_second_compile_builds_valid_draft_with_figures(self) -> None:
        self.assertEqual(self.second["code"], "draft_compiled")
        self.assertEqual(self.second["direct_reference"]["figure_count"], 4)
        result = run_json("direct_reference_skill.py", "validate", str(self.package))
        self.assertTrue(result["valid"])
        manifest = json.loads((self.package / "manifest.json").read_text())
        self.assertEqual(manifest["build"]["compiler_version"], "1.4.2-direct-reference")
        self.assertEqual(manifest["distribution"]["source_derived_content"], ["figure-captions", "figure-images", "figure-text"])
        self.assertFalse((self.package / "scripts/portable_reference_runtime.py").exists())

    def test_queries_return_facts_figures_and_warnings(self) -> None:
        response = query_package(self.package, "copper conductivity peak")
        top = response["results"][0]
        self.assertEqual(self.pending["code"], "numeric_review_required")
        self.assertEqual(top["type"], "Figure")
        self.assertIsNone(top["value_basis"])
        figure = response["figures"][0]
        self.assertEqual(figure["label"], "Figure 1.1")
        self.assertTrue((self.package / figure["image"]).is_file())
        self.assertEqual(query_package(self.package, "换热系数")["results"][0]["title"], "对流换热系数")
        self.assertEqual(query_package(self.package, "Design a heat exchanger for me")["behavior"], "abstain")

    def test_figure_package_is_not_release_distributable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completed = run_cli(
                "package_skill_release.py", str(self.package), "--output", str(Path(temporary) / "rel"),
                "--created-at", CREATED_AT, "--artifact-stage", "draft-distributable", check=False,
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("not_distributable", completed.stderr + completed.stdout)


@requires_renderer
class NumericGateTests(TempDirTestCase):
    def test_untraced_number_needs_figure_binding(self) -> None:
        import semantic_authoring
        from semantic_promotion import validate_semantic_workpack

        ir, workpack = self.tmp / "ir", self.tmp / "wp"
        run_cli("reconstruct_pdf.py", str(HEAT_BOOK), "--output", str(ir))
        run_cli("semantic_workpack.py", "build", "--source", str(HEAT_BOOK), "--ir", str(ir), "--output", str(workpack), "--pdftoppm", "pdftoppm")
        units = {json.loads(path.read_text())["title"]: json.loads(path.read_text()) for path in (workpack / "units").glob("*.json")}
        tasks = {row["id"]: row for row in map(json.loads, (workpack / "visual-tasks.jsonl").read_text().splitlines())}
        figures = {row["label"]: row["visual_object_id"] for row in map(json.loads, (workpack / "visual-objects.jsonl").read_text().splitlines()) if row["type"] == "figure"}
        unit = units["1.2 Temperature Dependence of Conductivity"]
        caption = semantic_authoring._resolve_quote("Figure 1.1: Thermal conductivity of copper versus temperature.", unit["focus_spans"])
        figure_task = [task_id for task_id in unit["visual_task_ids"] if figures["Figure 1.1"] in tasks[task_id].get("visual_object_ids", [])]
        obj = {
            "local_id": "peak", "type": "Claim", "origin": "source-paraphrase", "title": "Copper peak",
            "statement": "Copper conductivity peaks near 52 W/(m K).", "evidence_spans": [caption],
            "assumptions": [], "valid_when": [], "fails_when": [],
            "applicability": {"physical_pages": [2, 2], "excluded_conclusions": []},
            "visual_task_ids": figure_task, "formula": None,
        }
        (workpack / "drafts").mkdir(exist_ok=True)

        def codes(draft_object: dict) -> dict[str, str]:
            (workpack / "drafts" / f"{unit['unit_id']}.json").write_text(json.dumps({
                "schema_version": "tkc.semantic-draft/v0.1", "unit_id": unit["unit_id"], "proposer_instance": "t",
                "candidate_disposition": {"action": "promote", "reason": "r"}, "objects": [draft_object],
                "relations": [], "conflicts": [], "gaps": [],
            }))
            issues, _ = validate_semantic_workpack(HEAT_BOOK, workpack, require_complete_drafts=False)
            return {issue.code: issue.severity for issue in issues if "numeric" in issue.code or "figure" in issue.code}

        self.assertEqual(codes(obj), {"numeric_token_untraced": "error"})
        self.assertEqual(codes({**obj, "figure_object_ids": [figures["Figure 1.1"]]}), {"numeric_review_required": "warning"})
        self.assertEqual(codes({**obj, "figure_object_ids": ["vo-ffffffffffffffffffff"]}).get("figure_object_unresolved"), "error")


class CompatibilityTests(TempDirTestCase):
    def test_legacy_package_still_validates_and_answers(self) -> None:
        package = self.tmp / "legacy"
        shutil.copytree(LEGACY_PACKAGE, package)
        self.assertTrue(run_json("direct_reference_skill.py", "validate", str(package))["valid"])
        response = query_package(package, "Fourier's law")
        self.assertEqual((response["schema_version"], response["behavior"]), ("tkc.runtime-response/v0.1", "answer"))

    @requires_renderer
    def test_legacy_package_releases_and_extends_to_current_generation(self) -> None:
        package = self.tmp / "legacy"
        shutil.copytree(LEGACY_PACKAGE, package)
        run_cli("package_skill_release.py", str(package), "--output", str(self.tmp / "rel"), "--created-at", CREATED_AT, "--artifact-stage", "draft-distributable")
        run_cli("verify_skill_release.py", str(self.tmp / "rel"))
        ir, workpack = self.tmp / "ir", self.tmp / "wp"
        run_cli("reconstruct_pdf.py", str(HEAT_BOOK), "--output", str(ir))
        run_cli("semantic_workpack.py", "build", "--source", str(HEAT_BOOK), "--ir", str(ir), "--output", str(workpack), "--pdftoppm", "pdftoppm")
        # The old fixture contains an untraced figure-only peak. Reading stays
        # compatible, but upgrading must not launder it through the new gate.
        rejected = run_cli("direct_reference_skill.py", "extend", "--base", str(package),
            "--source", str(HEAT_BOOK), "--workpack", str(workpack), "--output", str(self.tmp/"rejected"),
            "--version", "0.2.0", "--created-at", CREATED_AT, check=False)
        # Populate the workpack before testing the actual legacy numeric gate.
        run_cli("semantic_authoring.py", "run", "--workpack", str(workpack),
            "--command", shlex.join([sys.executable,str(FAKE_MODEL),"--text-only-values"]), "--proposer-instance", "m2")
        rejected = run_cli("direct_reference_skill.py", "extend", "--base", str(package),
            "--source", str(HEAT_BOOK), "--workpack", str(workpack), "--output", str(self.tmp/"rejected"),
            "--version", "0.2.0", "--created-at", CREATED_AT, check=False)
        self.assertIn("legacy_numeric_revalidation_required", rejected.stderr)
        package = self.tmp/"current"/"fake-heat-ref"
        run_cli("direct_reference_skill.py", "build", "--source", str(HEAT_BOOK), "--workpack", str(workpack),
            "--output", str(package), "--name", "heat-ref", "--display-name", "Heat Reference", "--domain", "heat-transfer",
            "--version", "0.1.0", "--created-at", CREATED_AT, "--figures", "none")
        extra = self.tmp / "extra_model.py"
        extra.write_text(
            "import json, os, sys\nsys.stdin.read()\npacket = json.load(open(os.environ['FAKE_EXPERT_PACKET']))\n"
            "objects = [{'id': 'alpha', 'type': 'Definition', 'title': 'Thermal diffusivity', "
            "'statement': 'Thermal diffusivity alpha equals k divided by rho cp and governs transient response.', "
            "'evidence': ['The thermal diffusivity alpha = k/(rho cp) controls transient response.']}] if packet['title'].startswith('1.1') else []\n"
            "print(json.dumps({'unit_id': packet['unit_id'], 'disposition': 'promote' if objects else 'context-only', 'reason': 'x', 'objects': objects}))\n"
        )
        run_cli("semantic_authoring.py", "run", "--workpack", str(workpack), "--command", shlex.join([sys.executable, str(extra)]), "--proposer-instance", "m2", "--rerun", "--replace-drafts")
        output = self.tmp / "extended" / "fake-heat-ref"
        run_cli(
            "direct_reference_skill.py", "extend", "--base", str(package), "--source", str(HEAT_BOOK), "--workpack", str(workpack),
            "--output", str(output), "--version", "0.2.0", "--created-at", CREATED_AT,
            "--embedding-command", shlex.join([sys.executable, str(TOY_EMBED)]),
        )
        self.assertTrue(run_json("direct_reference_skill.py", "validate", str(output))["valid"])
        response = query_package(output, "thermal diffusivity", "--embedding-command", shlex.join([sys.executable, str(TOY_EMBED)]))
        self.assertEqual(response["results"][0]["title"], "Thermal diffusivity")
        self.assertEqual(response["retrieval"]["method"], "bm25+embedding-rrf")
        run_cli("package_skill_release.py", str(output), "--output", str(self.tmp / "rel2"), "--created-at", CREATED_AT, "--artifact-stage", "draft-distributable")
        run_cli("verify_skill_release.py", str(self.tmp / "rel2"))


class PinTests(unittest.TestCase):
    def test_runtime_pins_and_standalone_copies_agree(self) -> None:
        import compiler_version as cv

        runtime_hash = sha256_file(SCRIPTS / "reference_search.py")
        self.assertEqual(runtime_hash, cv.REFERENCE_SEARCH_RUNTIME_SHA256)
        verifier = (SCRIPTS / "verify_skill_release.py").read_text()
        self.assertIn(f'REFERENCE_SEARCH_RUNTIME_SHA256 = "{runtime_hash}"', verifier)
        self.assertIn(f'SEARCH_DIRECT_REFERENCE_COMPILER_VERSION = "{cv.DIRECT_REFERENCE_COMPILER_VERSION}"', verifier)
        self.assertIn(f'REFERENCE_SEARCH_RUNTIME_VERSION = "{cv.REFERENCE_SEARCH_RUNTIME_VERSION}"', verifier)
        # Legacy runtimes are byte-identical to the pinned 1.1.0 generation.
        self.assertEqual(sha256_file(SCRIPTS / "direct_reference_runtime.py"), cv.DIRECT_REFERENCE_RUNTIME_SHA256)
        self.assertEqual(sha256_file(SCRIPTS / "portable_reference_runtime.py"), cv.PORTABLE_REFERENCE_RUNTIME_SHA256)
        fallback = re.search(r'DIRECT_REFERENCE_COMPILER_VERSION = "([^"]+)"  # standalone-release fallback', (SCRIPTS / "verify_fake_expert_release.py").read_text())
        self.assertEqual(fallback.group(1), cv.DIRECT_REFERENCE_COMPILER_VERSION)

    def test_document_contract_passes(self) -> None:
        import fake_expert

        report = fake_expert._document_contract_report()
        self.assertTrue(report["passed"], report["issues"])


if __name__ == "__main__":
    unittest.main()
