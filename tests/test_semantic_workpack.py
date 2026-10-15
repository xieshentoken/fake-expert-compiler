from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

from reportlab.pdfgen import canvas


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = PROJECT_ROOT / "fake-expert/scripts"
VERIFY_ANCHORS = SCRIPT_ROOT / "verify_source_anchors.py"
sys.path.insert(0, str(SCRIPT_ROOT))

from pdf_structure import compile_pdf_intermediate_representation, write_intermediate_representation
from semantic_promotion import (
    DRAFT_SCHEMA,
    REVIEW_SCHEMA,
    SemanticPromotionError,
    VISUAL_RECEIPT_SCHEMA,
    _locate_headings,
    _verbatim_window,
    build_semantic_workpack,
    merge_review_fragments,
    prepare_review_plan,
    promote_reviewed_workpack,
    sha256_json,
    sha256_text,
    validate_semantic_workpack,
    validate_reviewed_workpack,
)
from expert_skill_contract import validate_expert_skill


def create_semantic_pdf(path: Path, *, duplicate_heading: bool = False) -> None:
    pdf = canvas.Canvas(str(path))
    pdf.bookmarkPage("chapter")
    pdf.bookmarkPage("child-a")
    pdf.addOutlineEntry("1 Parent Chapter", "chapter", level=0, closed=False)
    pdf.addOutlineEntry("1.1 Child A", "child-a", level=1, closed=False)
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(72, 760, "1 Parent Chapter")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(72, 730, "Parent introduction defines the bounded system before its child sections.")
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(72, 690, "1.1 Child A")
    if duplicate_heading:
        pdf.drawString(72, 665, "1.1 Child A")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(72, 630, "The source states the synthetic equation x = 2. (1.1)")
    pdf.drawString(72, 600, "Figure 1.1 illustrates the same synthetic bounded system.")
    pdf.drawCentredString(306, 36, "11")
    pdf.showPage()

    pdf.bookmarkPage("child-b")
    pdf.addOutlineEntry("1.2 Child B", "child-b", level=1, closed=False)
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(72, 760, "1.2 Child B")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(72, 720, "A second child provides an explicit operating boundary and failure condition.")
    pdf.drawString(72, 690, "The second synthetic equation is y = 3. (1.2)")
    pdf.drawCentredString(306, 36, "12")
    pdf.showPage()

    pdf.bookmarkPage("bibliography")
    pdf.addOutlineEntry("Bibliography", "bibliography", level=1, closed=False)
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(72, 760, "Bibliography")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(72, 720, "Synthetic Reference. 2026. Used only for deterministic tests.")
    pdf.drawCentredString(306, 36, "13")
    pdf.showPage()
    pdf.save()


def create_out_of_order_outline_pdf(path: Path) -> None:
    pdf = canvas.Canvas(str(path))
    pages = (
        ("first", "1.1 First section", "Opening text on physical page one."),
        ("second", "1.2 Second section", "Middle text on physical page two."),
        ("third", "1.3 Third section", "Closing text on physical page three."),
    )
    for bookmark, heading, body in pages:
        pdf.bookmarkPage(bookmark)
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(72, 750, heading)
        pdf.setFont("Helvetica", 11)
        text = pdf.beginText(72, 700)
        for chunk in (body[:85], body[85:]):
            if chunk:
                text.textLine(chunk)
        pdf.drawText(text)
        pdf.showPage()
    pdf.addOutlineEntry("1.3 Third section", "third", level=0, closed=False)
    pdf.addOutlineEntry("1.1 First section", "first", level=1, closed=False)
    pdf.addOutlineEntry("1.2 Second section", "second", level=1, closed=False)
    pdf.save()


def create_rename_offset_pdf(path: Path) -> None:
    pdf = canvas.Canvas(str(path))
    pdf.bookmarkPage("intro")
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(72, 750, "1.1 Intro")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(72, 720, "Introductory text for the rename-offset ordering regression test.")
    pdf.showPage()

    pdf.bookmarkPage("alpha")
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(72, 750, "1.2 Alpha original")
    pdf.bookmarkPage("beta")
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(72, 700, "1.3 Beta")
    pdf.setFont("Helvetica", 11)
    text = pdf.beginText(72, 650)
    text.textLine("The corrected body heading reads 1.2 Alpha renamed and sits below the beta section.")
    pdf.drawText(text)
    pdf.showPage()

    pdf.addOutlineEntry("1.1 Intro", "intro", level=0, closed=False)
    pdf.addOutlineEntry("1.2 Alpha original", "alpha", level=1, closed=False)
    pdf.addOutlineEntry("1.3 Beta", "beta", level=1, closed=False)
    pdf.save()


def write_draft(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class SemanticWorkpackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "semantic.pdf"
        create_semantic_pdf(self.source)
        self.ir = self.root / "ir"
        result = compile_pdf_intermediate_representation(self.source, 1, 3)
        write_intermediate_representation(self.ir, result)
        self.workpack = self.root / "workpack"
        self.manifest = build_semantic_workpack(
            self.source, self.ir, self.workpack, semantic_assurance=False
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _units(self) -> list[dict[str, object]]:
        return [
            json.loads((self.workpack / entry["path"]).read_text(encoding="utf-8"))
            for entry in self.manifest["units"]
        ]

    def _write_complete_drafts(self) -> dict[str, tuple[Path, dict[str, object]]]:
        units = self._units()
        tasks = [
            json.loads(line)
            for line in (self.workpack / "visual-tasks.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        equation_task = next(task for task in tasks if task["kind"] == "equation")
        written: dict[str, tuple[Path, dict[str, object]]] = {}
        for unit in units:
            title = unit["title"]
            draft: dict[str, object] = {
                "schema_version": DRAFT_SCHEMA,
                "unit_id": unit["unit_id"],
                "proposer_instance": "test-proposer",
                "candidate_disposition": {
                    "action": "non-knowledge",
                    "reason": "Synthetic fixture unit is not selected for this assertion.",
                },
                "objects": [],
                "relations": [],
                "conflicts": [],
                "gaps": [],
            }
            focus = unit["focus_spans"]
            if title == "1 Parent Chapter":
                span = focus[0]
                draft["candidate_disposition"] = {
                    "action": "promote",
                    "reason": "Parent introduction contains a source-backed concept.",
                }
                draft["objects"] = [
                    {
                        "local_id": "parent-concept",
                        "type": "Concept",
                        "origin": "source-paraphrase",
                        "title": "Bounded synthetic system",
                        "statement": "The parent introduction defines a bounded system before its child sections.",
                        "evidence_spans": [
                            {
                                "page": span["page"],
                                "start": span["start"],
                                "end": span["end"],
                                "span_sha256": span["span_sha256"],
                            }
                        ],
                        "assumptions": [],
                        "valid_when": ["The synthetic fixture is being tested."],
                        "fails_when": ["Used as real technical evidence."],
                        "applicability": {
                            "physical_pages": [1, 1],
                            "excluded_conclusions": ["Real-world engineering conclusions"],
                        },
                        "visual_task_ids": [],
                        "formula": None,
                    }
                ]
            elif title == "1.1 Child A":
                span = focus[0]
                draft["candidate_disposition"] = {
                    "action": "promote",
                    "reason": "The child contains a source equation.",
                }
                draft["objects"] = [
                    {
                        "local_id": "synthetic-equation",
                        "type": "Equation",
                        "origin": "source-explicit",
                        "title": "Synthetic equation",
                        "statement": "The source states x = 2 in the synthetic model.",
                        "evidence_spans": [
                            {
                                "page": span["page"],
                                "start": span["start"],
                                "end": span["end"],
                                "span_sha256": span["span_sha256"],
                            }
                        ],
                        "assumptions": ["This is a synthetic fixture."],
                        "valid_when": ["Testing the equation path."],
                        "fails_when": ["Treated as a real equation."],
                        "applicability": {
                            "physical_pages": [1, 1],
                            "excluded_conclusions": ["External use"],
                        },
                        "visual_task_ids": [equation_task["id"]],
                        "formula": {
                            "source_notation": "x = 2",
                            "normalized_notation": "x = 2",
                            "derived_notation": None,
                            "representation_relation": "identical",
                            "dimension_check": "not-run",
                        },
                    }
                ]
            path = self.workpack / "drafts" / f"{unit['unit_id']}.json"
            write_draft(path, draft)
            written[title] = (path, draft)
        return written

    def _bind_synthetic_render_assets(self) -> None:
        tasks_path = self.workpack / "visual-tasks.jsonl"
        tasks = [
            json.loads(line)
            for line in tasks_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        renders = self.workpack / "renders"
        renders.mkdir()
        assets: dict[int, tuple[str, str]] = {}
        for page in sorted({task["page"] for task in tasks}):
            relative = f"renders/page-{page:04d}.png"
            def chunk(kind: bytes, data: bytes) -> bytes:
                return (
                    len(data).to_bytes(4, "big")
                    + kind
                    + data
                    + zlib.crc32(kind + data).to_bytes(4, "big")
                )

            rgba = bytes((page % 256, 80, 160, 255))
            payload = (
                b"\x89PNG\r\n\x1a\n"
                + chunk(
                    b"IHDR",
                    (1).to_bytes(4, "big")
                    + (1).to_bytes(4, "big")
                    + bytes((8, 6, 0, 0, 0)),
                )
                + chunk(b"IDAT", zlib.compress(b"\x00" + rgba))
                + chunk(b"IEND", b"")
            )
            (self.workpack / relative).write_bytes(payload)
            assets[page] = (relative, hashlib.sha256(payload).hexdigest())
        for task in tasks:
            task["page_asset"], task["page_asset_sha256"] = assets[task["page"]]
        tasks_path.write_text(
            "".join(json.dumps(task, sort_keys=True) + "\n" for task in tasks),
            encoding="utf-8",
        )
        manifest_path = self.workpack / "workpack.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["render_profile"] = {
            "renderer": "synthetic-test-renderer",
            "renderer_version": "1",
            "dpi": 150,
            "format": "png",
        }
        write_draft(manifest_path, manifest)
        self.manifest = manifest

    def _accepting_semantic_records(
        self,
        plan: dict[str, object],
        reviewer: str = "test-reviewer",
        *,
        gap_overrides: dict[str, dict[str, object]] | None = None,
    ) -> list[dict[str, object]]:
        semantic_queue = [
            json.loads(line)
            for line in (self.workpack / "review-queue.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        records = []
        for row in semantic_queue:
            record: dict[str, object] = {
                "schema_version": REVIEW_SCHEMA,
                "item_ref": row["item_ref"],
                "item_kind": row["item_kind"],
                "reviewer_instance": reviewer,
                "proposer_instance": row["proposer_instance"],
                "item_sha256": row["item_sha256"],
                "review_input_sha256": row["review_input_sha256"],
                "verdict": "accepted",
                "evidence_support": "full",
                "issue_codes": [],
                "rationale": (
                    f"The synthetic proposal {row['item_ref']} is fully supported "
                    "by its bound fixture span."
                ),
                "confidence": {"extraction": 1.0, "interpretation": 1.0},
                "review_session_id": plan["review_session_id"],
                "review_method": "fresh-source-inspection",
                "review_checks": {
                    "evidence_span_checked": True,
                    "support_completeness_checked": True,
                    "scope_and_applicability_checked": True,
                    "conflict_checked": True,
                    "formula_checked": True,
                },
            }
            override = (gap_overrides or {}).get(row["item_ref"])
            if override is not None:
                record.update(override)
            records.append(record)
        return records

    def _accepting_visual_receipts(
        self, plan: dict[str, object], reviewer: str = "test-reviewer"
    ) -> list[dict[str, object]]:
        visual_queue = [
            json.loads(line)
            for line in (self.workpack / "visual-review-queue.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return [
            {
                "schema_version": VISUAL_RECEIPT_SCHEMA,
                "page": row["page"],
                "reviewer_instance": reviewer,
                "review_input_sha256": row["review_input_sha256"],
                "page_asset_sha256": row["page_asset_sha256"],
                "verdict": "verified",
                "task_resolutions": [
                    {
                        "task_id": task_id,
                        "verdict": "supported",
                        "observation": (
                            f"Synthetic receipt for task {task_id} exercises "
                            "deterministic binding only."
                        ),
                    }
                    for task_id in row["visual_task_ids"]
                ],
                "review_session_id": plan["review_session_id"],
                "review_method": "fresh-render-inspection",
                "review_checks": {
                    "page_asset_opened": True,
                    "task_resolutions_checked": True,
                },
            }
            for row in visual_queue
        ]

    def _merge_review_files(
        self,
        records: list[dict[str, object]],
        receipts: list[dict[str, object]],
    ) -> None:
        fragments = self.root / "review-fragments-external"
        fragments.mkdir(exist_ok=True)
        semantic_fragment = fragments / "semantic.jsonl"
        visual_fragment = fragments / "visual.jsonl"
        semantic_fragment.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in records),
            encoding="utf-8",
        )
        visual_fragment.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in receipts),
            encoding="utf-8",
        )
        merge_review_fragments(
            self.source,
            self.workpack,
            [semantic_fragment],
            [visual_fragment],
        )
        self._semantic_fragment = semantic_fragment
        self._visual_fragment = visual_fragment

    def _write_accepting_reviews(self, reviewer: str = "test-reviewer") -> None:
        plan = prepare_review_plan(self.source, self.workpack, [reviewer])
        self._merge_review_files(
            self._accepting_semantic_records(plan, reviewer),
            self._accepting_visual_receipts(plan, reviewer),
        )

    def _add_gap(self, *, resolved: bool) -> tuple[str, str]:
        written = self._write_complete_drafts()
        parent_path, parent = written["1 Parent Chapter"]
        child_path, child = written["1.1 Child A"]
        parent = copy.deepcopy(parent)
        child = copy.deepcopy(child)
        span = parent["objects"][0]["evidence_spans"][0]
        parent["gaps"] = [
            {
                "origin": "knowledge-gap",
                "statement": "The parent unit does not provide the child equation.",
                "evidence_spans": [span],
            }
        ]
        write_draft(parent_path, parent)
        write_draft(child_path, child)
        self._bind_synthetic_render_assets()
        plan = prepare_review_plan(self.source, self.workpack, ["test-reviewer"])
        queue = [
            json.loads(line)
            for line in (self.workpack / "review-queue.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        gap_ref = next(row["item_ref"] for row in queue if row["item_kind"] == "gap")
        resolver_ref = next(
            row["item_ref"]
            for row in queue
            if row["item_kind"] == "object" and row["unit_id"] == child["unit_id"]
        )
        gap_override: dict[str, object] = {
            "gap_resolution": {
                "status": "resolved_elsewhere" if resolved else "unresolved_in_package",
                "resolved_by_refs": [resolver_ref] if resolved else [],
            }
        }
        if resolved:
            gap_override["verdict"] = "rejected"
            gap_override["issue_codes"] = ["knowledge_gap_resolved_elsewhere"]
            gap_override["rationale"] = "The accepted child equation closes this unit-local gap."
        else:
            gap_override["rationale"] = "No accepted object in another unit closes this package gap."
        records = self._accepting_semantic_records(
            plan, gap_overrides={gap_ref: gap_override}
        )
        self._merge_review_files(records, self._accepting_visual_receipts(plan))
        return gap_ref, resolver_ref

    def test_build_is_deterministic_and_preserves_parent_intro(self) -> None:
        second = self.root / "workpack-two"
        second_manifest = build_semantic_workpack(
            self.source, self.ir, second, semantic_assurance=False
        )
        self.assertEqual(self.manifest["workpack_id"], second_manifest["workpack_id"])
        self.assertEqual(self.manifest["units"], second_manifest["units"])
        for first_entry, second_entry in zip(self.manifest["units"], second_manifest["units"]):
            self.assertEqual(
                (self.workpack / first_entry["path"]).read_bytes(),
                (second / second_entry["path"]).read_bytes(),
            )

        parent = next(unit for unit in self._units() if unit["title"] == "1 Parent Chapter")
        focus_text = " ".join(span["text"] for span in parent["focus_spans"])
        self.assertIn("Parent introduction defines the bounded system", focus_text)
        self.assertNotIn("The source states the synthetic equation", focus_text)

    def test_vector_figure_and_equation_create_visual_tasks(self) -> None:
        tasks = [json.loads(line) for line in (self.workpack / "visual-tasks.jsonl").read_text().splitlines()]
        kinds = {task["kind"] for task in tasks}
        self.assertIn("figure-check", kinds)
        self.assertIn("equation", kinds)
        figure = next(task for task in tasks if task["kind"] == "figure-check")
        self.assertEqual(figure["presence_classification"], "unclassified")

    def test_render_must_be_a_crc_valid_decodable_png(self) -> None:
        self._write_complete_drafts()
        self._bind_synthetic_render_assets()
        tasks_path = self.workpack / "visual-tasks.jsonl"
        tasks = [
            json.loads(line)
            for line in tasks_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        page = tasks[0]["page"]
        page_tasks = [row for row in tasks if row["page"] == page]
        asset = self.workpack / page_tasks[0]["page_asset"]
        fake = b"\x89PNG\r\n\x1a\n" + b"NOT-A-DECODABLE-PNG"
        asset.write_bytes(fake)
        fake_hash = hashlib.sha256(fake).hexdigest()
        for task in page_tasks:
            task["page_asset_sha256"] = fake_hash
        tasks_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in tasks),
            encoding="utf-8",
        )
        issues, _ = validate_semantic_workpack(self.source, self.workpack)
        self.assertIn("render_format_invalid", {issue.code for issue in issues})

    def test_all_visual_tasks_on_a_page_share_the_canonical_asset(self) -> None:
        self._write_complete_drafts()
        self._bind_synthetic_render_assets()
        tasks_path = self.workpack / "visual-tasks.jsonl"
        tasks = [
            json.loads(line)
            for line in tasks_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        task_counts = {
            page: sum(row["page"] == page for row in tasks)
            for page in {row["page"] for row in tasks}
        }
        page = next(page for page, count in task_counts.items() if count > 1)
        same_page_tasks = [row for row in tasks if row["page"] == page]
        other_page_task = next(row for row in tasks if row["page"] != page)
        same_page_tasks[1]["page_asset"] = other_page_task["page_asset"]
        same_page_tasks[1]["page_asset_sha256"] = other_page_task["page_asset_sha256"]
        tasks_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in tasks),
            encoding="utf-8",
        )
        issues, _ = validate_semantic_workpack(self.source, self.workpack)
        error_codes = {issue.code for issue in issues if issue.severity == "error"}
        self.assertIn("visual_page_asset_mismatch", error_codes)
        self.assertIn("render_path_noncanonical", error_codes)
        with self.assertRaisesRegex(
            SemanticPromotionError,
            "proposal_gate_failed: (render_path_noncanonical|visual_page_asset_mismatch)",
        ):
            prepare_review_plan(self.source, self.workpack, ["test-reviewer"])

    def test_visual_task_must_share_an_evidence_page(self) -> None:
        written = self._write_complete_drafts()
        self._bind_synthetic_render_assets()
        tasks = [
            json.loads(line)
            for line in (self.workpack / "visual-tasks.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        other_page_equation = next(
            row for row in tasks if row["kind"] == "equation" and row["page"] == 2
        )
        path, draft = written["1.1 Child A"]
        draft = copy.deepcopy(draft)
        draft["objects"][0]["visual_task_ids"] = [other_page_equation["id"]]
        write_draft(path, draft)
        issues, _ = validate_semantic_workpack(self.source, self.workpack)
        self.assertIn(
            "visual_task_evidence_page_mismatch",
            {issue.code for issue in issues},
        )

    def test_conflict_ids_and_claim_refs_must_be_distinct(self) -> None:
        written = self._write_complete_drafts()
        parent = written["1 Parent Chapter"][1]
        child_path, child = written["1.1 Child A"]
        parent_ref = f"{parent['unit_id']}:parent-concept"
        child_ref = f"{child['unit_id']}:synthetic-equation"
        child = copy.deepcopy(child)
        child["conflicts"] = [
            {
                "local_id": "duplicate-conflict",
                "topic": "Synthetic duplicate claims",
                "claim_refs": [parent_ref, parent_ref],
                "possible_causes": [],
                "recommended_status": "unresolved",
                "requires_review": True,
            },
            {
                "local_id": "duplicate-conflict",
                "topic": "Synthetic duplicate identifier",
                "claim_refs": [parent_ref, child_ref],
                "possible_causes": [],
                "recommended_status": "unresolved",
                "requires_review": True,
            },
        ]
        write_draft(child_path, child)
        issues, _ = validate_semantic_workpack(self.source, self.workpack)
        error_codes = {issue.code for issue in issues if issue.severity == "error"}
        self.assertIn("conflict_id_invalid", error_codes)
        self.assertIn("conflict_claims_invalid", error_codes)

    def test_complete_source_backed_drafts_validate(self) -> None:
        self._write_complete_drafts()
        issues, summary = validate_semantic_workpack(self.source, self.workpack)
        self.assertEqual([issue for issue in issues if issue.severity == "error"], [], issues)
        self.assertEqual(summary["draft_count"], len(self.manifest["units"]))
        self.assertEqual(summary["object_count"], 2)

    def test_review_plan_requires_fresh_render_assets_and_independent_reviewer(self) -> None:
        self._write_complete_drafts()
        with self.assertRaisesRegex(SemanticPromotionError, "visual_review_assets_required"):
            prepare_review_plan(self.source, self.workpack, ["test-reviewer"])

        self._bind_synthetic_render_assets()
        with self.assertRaisesRegex(SemanticPromotionError, "reviewer_not_independent"):
            prepare_review_plan(self.source, self.workpack, ["test-proposer"])

    def test_referenced_visual_scope_preserves_candidates_but_queues_only_claimed_tasks(self) -> None:
        self._write_complete_drafts()
        self._bind_synthetic_render_assets()
        task_path = self.workpack / "visual-tasks.jsonl"
        before = task_path.read_bytes()
        drafts = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((self.workpack / "drafts").glob("*.json"))
        ]
        referenced = {
            task_id
            for draft in drafts
            for obj in draft["objects"]
            for task_id in obj["visual_task_ids"]
        }

        plan = prepare_review_plan(
            self.source,
            self.workpack,
            ["reviewer-a"],
            visual_review_scope="referenced",
        )
        queued = [
            json.loads(line)
            for line in (self.workpack / "visual-review-queue.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        queued_ids = {
            task_id for row in queued for task_id in row["visual_task_ids"]
        }
        all_tasks = [
            json.loads(line) for line in before.decode("utf-8").splitlines() if line.strip()
        ]

        self.assertEqual(queued_ids, referenced)
        self.assertEqual(task_path.read_bytes(), before)
        self.assertEqual(plan["visual_review_scope"], "referenced")
        self.assertEqual(plan["visual_candidate_task_count"], len(all_tasks))
        self.assertEqual(plan["visual_queued_task_count"], len(referenced))
        self.assertEqual(
            plan["visual_deferred_task_count"], len(all_tasks) - len(referenced)
        )

    def test_referenced_scope_with_no_visual_claims_merges_semantic_only(self) -> None:
        written = self._write_complete_drafts()
        for path, draft in written.values():
            updated = copy.deepcopy(draft)
            if any(obj["type"] == "Equation" for obj in updated["objects"]):
                updated["objects"] = []
                updated["candidate_disposition"] = {
                    "action": "non-knowledge",
                    "reason": "This semantic-only fixture intentionally defers the equation.",
                }
            else:
                for obj in updated["objects"]:
                    obj["visual_task_ids"] = []
            write_draft(path, updated)
        plan = prepare_review_plan(
            self.source,
            self.workpack,
            ["reviewer-a"],
            visual_review_scope="referenced",
        )
        self.assertEqual(plan["visual_page_count"], 0)
        semantic_fragment = self.root / "semantic-only-review.jsonl"
        semantic_fragment.write_text(
            "".join(
                json.dumps(row, sort_keys=True) + "\n"
                for row in self._accepting_semantic_records(plan, "reviewer-a")
            ),
            encoding="utf-8",
        )
        result = merge_review_fragments(
            self.source,
            self.workpack,
            [semantic_fragment],
            [],
        )
        self.assertEqual(result["visual_receipt_count"], 0)
        issues, summary = validate_reviewed_workpack(self.source, self.workpack)
        self.assertEqual([issue for issue in issues if issue.severity == "error"], [])
        self.assertTrue(summary["reviewed"])

    def test_blank_review_scaffold_cannot_enter_v031_review_gate(self) -> None:
        self._write_complete_drafts()
        self._bind_synthetic_render_assets()
        plan = prepare_review_plan(self.source, self.workpack, ["test-reviewer"])
        semantic_queue = [
            json.loads(line)
            for line in (self.workpack / "review-queue.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        visual_queue = [
            json.loads(line)
            for line in (self.workpack / "visual-review-queue.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        records = []
        for row in semantic_queue:
            records.append({
                "schema_version": REVIEW_SCHEMA,
                "item_ref": row["item_ref"],
                "item_kind": row["item_kind"],
                "reviewer_instance": "test-reviewer",
                "proposer_instance": row["proposer_instance"],
                "item_sha256": row["item_sha256"],
                "review_input_sha256": row["review_input_sha256"],
                "verdict": "accepted",
                "evidence_support": "full",
                "issue_codes": [],
                "rationale": "accepted by an old blanket scaffold",
                "confidence": {"extraction": 1.0, "interpretation": 1.0},
            })
        receipts = [
            {
                "schema_version": VISUAL_RECEIPT_SCHEMA,
                "page": row["page"],
                "reviewer_instance": "test-reviewer",
                "review_input_sha256": row["review_input_sha256"],
                "page_asset_sha256": row["page_asset_sha256"],
                "verdict": "verified",
                "task_resolutions": [
                    {"task_id": task_id, "verdict": "supported", "observation": "generic"}
                    for task_id in row["visual_task_ids"]
                ],
            }
            for row in visual_queue
        ]
        (self.workpack / "review-records.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in records), encoding="utf-8"
        )
        (self.workpack / "visual-receipts.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in receipts), encoding="utf-8"
        )
        issues, _ = validate_reviewed_workpack(self.source, self.workpack)
        codes = {issue.code for issue in issues if issue.severity == "error"}
        self.assertIn("review_protocol_invalid", codes)
        self.assertIn("review_checks_missing", codes)
        self.assertIn("review_fragments_manifest_missing", codes)
        self.assertIn("review_blanket_pattern", codes)

    def test_complete_independent_reviews_validate_and_stale_plan_fails(self) -> None:
        written = self._write_complete_drafts()
        self._bind_synthetic_render_assets()
        self._write_accepting_reviews()
        issues, summary = validate_reviewed_workpack(self.source, self.workpack)
        self.assertEqual([issue for issue in issues if issue.severity == "error"], [], issues)
        self.assertTrue(summary["reviewed"])

        parent_path, parent = written["1 Parent Chapter"]
        parent = copy.deepcopy(parent)
        parent["objects"][0]["statement"] += " This post-plan mutation must invalidate receipts."
        write_draft(parent_path, parent)
        issues, _ = validate_reviewed_workpack(self.source, self.workpack)
        self.assertIn("review_receipt_stale", {issue.code for issue in issues})

    def test_missing_or_contradicted_visual_receipt_blocks_accepted_equation(self) -> None:
        self._write_complete_drafts()
        self._bind_synthetic_render_assets()
        self._write_accepting_reviews()
        receipts_path = self.workpack / "visual-receipts.jsonl"
        receipts = [
            json.loads(line)
            for line in receipts_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        receipts[0]["verdict"] = "contradicted"
        receipts_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in receipts),
            encoding="utf-8",
        )
        issues, _ = validate_reviewed_workpack(self.source, self.workpack)
        self.assertIn("visual_claim_support_missing", {issue.code for issue in issues})

        receipts_path.write_text("", encoding="utf-8")
        issues, _ = validate_reviewed_workpack(self.source, self.workpack)
        self.assertIn("formula_visual_receipt_missing", {issue.code for issue in issues})

    def test_duplicate_visual_task_resolution_is_rejected(self) -> None:
        self._write_complete_drafts()
        self._bind_synthetic_render_assets()
        self._write_accepting_reviews()
        receipts_path = self.workpack / "visual-receipts.jsonl"
        receipts = [
            json.loads(line)
            for line in receipts_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        receipts[0]["task_resolutions"].append(
            {
                **receipts[0]["task_resolutions"][0],
                "verdict": "contradicted",
                "observation": "A duplicate disposition must not override the first one.",
            }
        )
        receipts_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in receipts),
            encoding="utf-8",
        )
        issues, _ = validate_reviewed_workpack(self.source, self.workpack)
        self.assertIn("visual_task_duplicate", {issue.code for issue in issues})

    def test_rejected_conflict_blocks_accepting_its_conflicted_formula(self) -> None:
        written = self._write_complete_drafts()
        self._bind_synthetic_render_assets()
        equation_path, equation = written["1.1 Child A"]
        equation = copy.deepcopy(equation)
        unit_id = equation["unit_id"]
        equation_ref = f"{unit_id}:synthetic-equation"
        parent = written["1 Parent Chapter"][1]
        parent_ref = f"{parent['unit_id']}:parent-concept"
        equation["objects"][0]["formula"]["representation_relation"] = "conflicted"
        equation["objects"][0]["formula"]["dimension_check"] = "inconsistent"
        equation["conflicts"] = [
            {
                "local_id": "synthetic-conflict",
                "topic": "Synthetic formula inconsistency",
                "claim_refs": [equation_ref, parent_ref],
                "recommended_status": "unresolved",
                "requires_review": True,
            }
        ]
        write_draft(equation_path, equation)
        self._write_accepting_reviews()
        records_path = self.workpack / "review-records.jsonl"
        records = [
            json.loads(line)
            for line in records_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        conflict_record = next(
            row for row in records if row["item_ref"].startswith("conf:")
        )
        conflict_record["verdict"] = "rejected"
        conflict_record["evidence_support"] = "none"
        conflict_record["issue_codes"] = ["conflict_unsupported"]
        conflict_record["rationale"] = "Synthetic rejection exercises conflict closure."
        records_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in records),
            encoding="utf-8",
        )
        issues, _ = validate_reviewed_workpack(self.source, self.workpack)
        self.assertIn("conflict_omitted", {issue.code for issue in issues})

    def test_deterministic_promoter_writes_reference_only_draft(self) -> None:
        self._write_complete_drafts()
        self._bind_synthetic_render_assets()
        self._write_accepting_reviews()
        first = self.root / "promoted-one"
        second = self.root / "promoted-two"
        for output in (first, second):
            result = promote_reviewed_workpack(
                self.source,
                self.workpack,
                output,
                name="synthetic-reviewed-expert",
                display_name="Synthetic Reviewed Expert",
                domain="synthetic-domain",
            )
            self.assertEqual(result["status"], "draft")
            self.assertFalse(result["publication_ready"])
            self.assertEqual(
                result["capabilities"],
                {"reference": True, "decision_support": False, "executable": False},
            )
            validation = validate_expert_skill(output, level="structure")
            self.assertEqual(
                [issue for issue in validation if issue.severity == "error"], [], validation
            )

        first_files = {
            path.relative_to(first).as_posix(): path.read_bytes()
            for path in first.rglob("*")
            if path.is_file()
        }
        second_files = {
            path.relative_to(second).as_posix(): path.read_bytes()
            for path in second.rglob("*")
            if path.is_file()
        }
        self.assertEqual(first_files, second_files)
        self.assertFalse(any(path.suffix == ".pdf" for path in first.rglob("*")))
        manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["capabilities"]["reference"])
        self.assertFalse(manifest["capabilities"]["executable"])
        self.assertFalse(manifest["capabilities"]["decision_support"])
        anchors = [
            json.loads(line)
            for line in (first / "references/evidence/anchors.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        self.assertIn("11", {label for anchor in anchors for label in anchor["printed_page_labels"]})

        completed = subprocess.run(
            [sys.executable, str(VERIFY_ANCHORS), str(first), "--source", str(self.source)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

        second_manifest_path = second / "manifest.json"
        second_manifest = json.loads(second_manifest_path.read_text(encoding="utf-8"))
        for field, bad_value in (
            ("reference", False),
            ("decision_support", True),
            ("executable", True),
        ):
            with self.subTest(manifest_capability=field):
                mutated_manifest = copy.deepcopy(second_manifest)
                mutated_manifest["capabilities"][field] = bad_value
                write_draft(second_manifest_path, mutated_manifest)
                policy_issues = validate_expert_skill(second, level="structure")
                self.assertIn(
                    "promotion_policy_invalid",
                    {issue.code for issue in policy_issues if issue.severity == "error"},
                    policy_issues,
                )
        mutated_manifest = copy.deepcopy(second_manifest)
        mutated_manifest["distribution"]["source_mode"] = "bundled"
        write_draft(second_manifest_path, mutated_manifest)
        policy_issues = validate_expert_skill(second, level="structure")
        self.assertIn(
            "promotion_policy_invalid",
            {issue.code for issue in policy_issues if issue.severity == "error"},
            policy_issues,
        )
        write_draft(second_manifest_path, second_manifest)

        second_report_path = second / "references/evidence/promotion-report.json"
        second_report = json.loads(second_report_path.read_text(encoding="utf-8"))
        for field, bad_value in (
            ("source_required", False),
            ("decision_support", True),
            ("executable", True),
        ):
            with self.subTest(report_policy=field):
                mutated_report = copy.deepcopy(second_report)
                mutated_report["policy"][field] = bad_value
                write_draft(second_report_path, mutated_report)
                policy_issues = validate_expert_skill(second, level="structure")
                self.assertIn(
                    "promotion_policy_invalid",
                    {issue.code for issue in policy_issues if issue.severity == "error"},
                    policy_issues,
                )
        write_draft(second_report_path, second_report)

        review_path = first / "references/reviews/semantic-records.jsonl"
        review_rows = [
            json.loads(line)
            for line in review_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        review_rows[0]["rationale"] += " Tampered after deterministic promotion."
        review_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in review_rows),
            encoding="utf-8",
        )
        tampered_issues = validate_expert_skill(first, level="structure")
        self.assertIn(
            "promotion_report_item_mismatch",
            {issue.code for issue in tampered_issues},
        )

    def test_reverse_evidence_edge_and_non_object_review_deletion_are_rejected(self) -> None:
        written = self._write_complete_drafts()
        parent_path, parent = written["1 Parent Chapter"]
        _, child = written["1.1 Child A"]
        parent = copy.deepcopy(parent)
        parent["relations"] = [
            {
                "source_ref": f"{parent['unit_id']}:parent-concept",
                "type": "related_to",
                "target_ref": f"{child['unit_id']}:synthetic-equation",
            }
        ]
        write_draft(parent_path, parent)
        self._bind_synthetic_render_assets()
        self._write_accepting_reviews()
        output = self.root / "audit-tamper"
        promote_reviewed_workpack(
            self.source,
            self.workpack,
            output,
            name="synthetic-reviewed-expert",
            display_name="Synthetic Reviewed Expert",
            domain="synthetic-domain",
        )
        anchors_path = output / "references/evidence/anchors.jsonl"
        anchors = [
            json.loads(line)
            for line in anchors_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        objects = json.loads(
            (output / "references/knowledge/index.json").read_text(encoding="utf-8")
        )["objects"]
        extra_object = next(
            row["id"] for row in objects if row["id"] not in anchors[0]["supports"]
        )
        anchors[0]["supports"].append(extra_object)
        anchors_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in anchors),
            encoding="utf-8",
        )
        issues = validate_expert_skill(output, level="structure")
        self.assertIn("non_bidirectional_evidence", {issue.code for issue in issues})

        # Restore anchors, then remove one accepted relation review and rewrite the
        # publicly recomputable plan fingerprint. The entity binding must still fail.
        anchors[0]["supports"].remove(extra_object)
        anchors_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in anchors),
            encoding="utf-8",
        )
        records_path = output / "references/reviews/semantic-records.jsonl"
        report_path = output / "references/evidence/promotion-report.json"
        plan_path = output / "references/reviews/review-plan.json"
        records = [
            json.loads(line)
            for line in records_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        removed = next(row for row in records if row["item_kind"] == "relation")
        records = [row for row in records if row["item_ref"] != removed["item_ref"]]
        records_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in records),
            encoding="utf-8",
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["items"] = [
            row for row in report["items"] if row["item_ref"] != removed["item_ref"]
        ]
        report["counts"]["review_items"] -= 1
        report["counts"]["accepted_items"] -= 1
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["semantic_item_count"] -= 1
        plan["authorization_sha256"] = sha256_json(
            {
                "attestation_level": plan.get("attestation_level"),
                "issued_by": plan.get("issued_by"),
                "issued_at": plan.get("issued_at"),
                "source_sha256": plan.get("source_sha256"),
                "workpack_id": plan.get("workpack_id"),
                "bundle_sha256": plan.get("bundle_sha256"),
                "proposer_instances": plan.get("proposer_instances"),
                "reviewer_instances": plan.get("reviewer_instances"),
                "semantic_item_count": plan.get("semantic_item_count"),
                "visual_page_count": plan.get("visual_page_count"),
                "gap_resolution_policy": plan.get("gap_resolution_policy"),
            }
        )
        plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        issues = validate_expert_skill(output, level="structure")
        self.assertTrue(
            {"promotion_entity_review_unresolved", "promotion_entity_coverage_mismatch"}
            & {issue.code for issue in issues},
            issues,
        )

    def test_gap_resolution_is_explicit_and_promoted_deterministically(self) -> None:
        gap_ref, resolver_ref = self._add_gap(resolved=True)
        issues, summary = validate_reviewed_workpack(self.source, self.workpack)
        self.assertEqual([issue for issue in issues if issue.severity == "error"], [], issues)
        self.assertTrue(summary["reviewed"])
        output = self.root / "resolved-gap"
        promote_reviewed_workpack(
            self.source,
            self.workpack,
            output,
            name="synthetic-reviewed-expert",
            display_name="Synthetic Reviewed Expert",
            domain="synthetic-domain",
        )
        self.assertEqual(
            (output / "references/knowledge-gaps.jsonl").read_text(encoding="utf-8"),
            "",
        )
        report = json.loads(
            (output / "references/evidence/promotion-report.json").read_text(encoding="utf-8")
        )
        gap_item = next(row for row in report["items"] if row["item_ref"] == gap_ref)
        resolver_item = next(row for row in report["items"] if row["item_ref"] == resolver_ref)
        self.assertEqual(gap_item["promoted_id"], None)
        self.assertEqual(gap_item["resolved_by_ids"], [resolver_item["promoted_id"]])
        self.assertEqual(report["counts"]["resolved_elsewhere_gaps"], 1)
        self.assertEqual(
            [issue for issue in validate_expert_skill(output, level="structure") if issue.severity == "error"],
            [],
        )
    def test_unresolved_gap_requires_a_package_scope_canonical_record(self) -> None:
        gap_ref, _ = self._add_gap(resolved=False)
        issues, _ = validate_reviewed_workpack(self.source, self.workpack)
        self.assertEqual([issue for issue in issues if issue.severity == "error"], [], issues)
        output = self.root / "unresolved-gap"
        promote_reviewed_workpack(
            self.source,
            self.workpack,
            output,
            name="synthetic-reviewed-expert",
            display_name="Synthetic Reviewed Expert",
            domain="synthetic-domain",
        )
        gaps = [
            json.loads(line)
            for line in (output / "references/knowledge-gaps.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["scope"], "package")
        self.assertEqual(gaps[0]["status"], "unresolved")
        self.assertEqual(gaps[0]["source_item_ref"], gap_ref)
        self.assertTrue(gaps[0]["evidence_spans"])
        self.assertEqual(
            [issue for issue in validate_expert_skill(output, level="structure") if issue.severity == "error"],
            [],
        )
        gap_path = output / "references/knowledge-gaps.jsonl"
        original_gap = dict(gaps[0])
        for field, forged_value in (
            ("source_item_ref", "gap:forged:0"),
            ("unit_id", "swu-00000000000000000000"),
        ):
            forged_gap = dict(original_gap)
            forged_gap[field] = forged_value
            gap_path.write_text(
                json.dumps(forged_gap, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            attack_issues = validate_expert_skill(output, level="structure")
            self.assertIn(
                "promotion_gap_source_binding_mismatch",
                {issue.code for issue in attack_issues if issue.severity == "error"},
                attack_issues,
            )
        gap_path.write_text(
            json.dumps(original_gap, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [sys.executable, str(VERIFY_ANCHORS), str(output), "--source", str(self.source)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("gaps=1", completed.stdout)

    def test_schema_contract_tracks_review_authorization_and_item_kind(self) -> None:
        schema_root = PROJECT_ROOT / "fake-expert/assets/schemas"
        plan_schema = json.loads((schema_root / "review-plan.schema.json").read_text(encoding="utf-8"))
        review_schema = json.loads((schema_root / "semantic-review.schema.json").read_text(encoding="utf-8"))
        self.assertIn("authorization_sha256", plan_schema["required"])
        self.assertIn("gap_resolution_policy", plan_schema["required"])
        self.assertFalse(plan_schema["additionalProperties"])
        self.assertIn("item_kind", review_schema["required"])

    def test_cjk_verbatim_windows_and_freeform_issue_codes_are_blocked(self) -> None:
        source = "这是用于验证中文连续原文泄漏检测能力的一段足够长的技术知识来源文本"
        self.assertIsNotNone(
            _verbatim_window(
                [source],
                [("review.rationale", source)],
                cjk_window=12,
            )
        )

        self._write_complete_drafts()
        self._bind_synthetic_render_assets()
        self._write_accepting_reviews()
        records_path = self.workpack / "review-records.jsonl"
        records = [
            json.loads(line)
            for line in records_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        records[0]["verdict"] = "rejected"
        records[0]["issue_codes"] = [
            "Parent introduction defines the bounded system before its child sections"
        ]
        records_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in records),
            encoding="utf-8",
        )
        issues, _ = validate_reviewed_workpack(self.source, self.workpack)
        self.assertIn("review_issue_code_invalid", {issue.code for issue in issues})

    def test_review_fragment_merge_requires_exact_coverage(self) -> None:
        self._write_complete_drafts()
        self._bind_synthetic_render_assets()
        self._write_accepting_reviews()
        semantic_fragment = self.root / "semantic-fragment.jsonl"
        visual_fragment = self.root / "visual-fragment.jsonl"
        semantic_fragment.write_bytes((self.workpack / "review-records.jsonl").read_bytes())
        visual_fragment.write_bytes((self.workpack / "visual-receipts.jsonl").read_bytes())
        (self.workpack / "review-records.jsonl").write_text("", encoding="utf-8")
        (self.workpack / "visual-receipts.jsonl").write_text("", encoding="utf-8")

        summary = merge_review_fragments(
            self.source,
            self.workpack,
            [semantic_fragment],
            [visual_fragment],
        )
        self.assertTrue(summary["reviewed"])

        rows = semantic_fragment.read_text(encoding="utf-8").splitlines()
        semantic_fragment.write_text("\n".join(rows[:-1]) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(SemanticPromotionError, "review_receipt_missing"):
            merge_review_fragments(
                self.source,
                self.workpack,
                [semantic_fragment],
                [visual_fragment],
            )

    def test_review_fragments_must_reside_outside_the_workpack(self) -> None:
        self._write_complete_drafts()
        self._bind_synthetic_render_assets()
        prepare_review_plan(self.source, self.workpack, ["test-reviewer"])
        inside = self.workpack / "review-records.jsonl"
        with self.assertRaisesRegex(SemanticPromotionError, "review_fragment_inside_workpack"):
            merge_review_fragments(
                self.source,
                self.workpack,
                [inside],
                [inside],
            )

    def test_direct_review_writes_require_fragment_provenance(self) -> None:
        self._write_complete_drafts()
        self._bind_synthetic_render_assets()
        plan = prepare_review_plan(self.source, self.workpack, ["test-reviewer"])
        semantic_queue = [
            json.loads(line)
            for line in (self.workpack / "review-queue.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        records = [
            {
                "schema_version": REVIEW_SCHEMA,
                "item_ref": row["item_ref"],
                "item_kind": row["item_kind"],
                "reviewer_instance": "test-reviewer",
                "proposer_instance": row["proposer_instance"],
                "item_sha256": row["item_sha256"],
                "review_input_sha256": row["review_input_sha256"],
                "verdict": "accepted",
                "evidence_support": "full",
                "issue_codes": [],
                "rationale": f"Item-specific rationale for {row['item_ref']}.",
                "confidence": {"extraction": 1.0, "interpretation": 1.0},
                "review_session_id": plan["review_session_id"],
                "review_method": "fresh-source-inspection",
                "review_checks": {
                    "evidence_span_checked": True,
                    "support_completeness_checked": True,
                    "scope_and_applicability_checked": True,
                    "conflict_checked": True,
                    "formula_checked": True,
                },
            }
            for row in semantic_queue
        ]
        (self.workpack / "review-records.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in records),
            encoding="utf-8",
        )
        issues, _ = validate_reviewed_workpack(self.source, self.workpack)
        codes = {issue.code for issue in issues if issue.severity == "error"}
        self.assertIn("review_fragments_manifest_missing", codes)
        self.assertNotIn("review_blanket_pattern", codes)

    def test_merged_review_edit_breaks_fragment_attribution(self) -> None:
        self._write_complete_drafts()
        self._bind_synthetic_render_assets()
        self._write_accepting_reviews()
        records_path = self.workpack / "review-records.jsonl"
        records = [
            json.loads(line)
            for line in records_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        records[0]["rationale"] += " Edited after the deterministic merge."
        records_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in records),
            encoding="utf-8",
        )
        issues, _ = validate_reviewed_workpack(self.source, self.workpack)
        codes = {issue.code for issue in issues if issue.severity == "error"}
        self.assertIn("review_fragments_manifest_mismatch", codes)

    def test_blanket_merge_never_writes_merged_material(self) -> None:
        self._write_complete_drafts()
        self._bind_synthetic_render_assets()
        plan = prepare_review_plan(self.source, self.workpack, ["test-reviewer"])
        semantic_queue = [
            json.loads(line)
            for line in (self.workpack / "review-queue.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        visual_queue = [
            json.loads(line)
            for line in (self.workpack / "visual-review-queue.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        records = [
            {
                "schema_version": REVIEW_SCHEMA,
                "item_ref": row["item_ref"],
                "item_kind": row["item_kind"],
                "reviewer_instance": "test-reviewer",
                "proposer_instance": row["proposer_instance"],
                "item_sha256": row["item_sha256"],
                "review_input_sha256": row["review_input_sha256"],
                "verdict": "accepted",
                "evidence_support": "full",
                "issue_codes": [],
                "rationale": "identical blanket rationale reused for every item",
                "confidence": {"extraction": 0.98, "interpretation": 0.97},
                "review_session_id": plan["review_session_id"],
                "review_method": "fresh-source-inspection",
                "review_checks": {
                    "evidence_span_checked": True,
                    "support_completeness_checked": True,
                    "scope_and_applicability_checked": True,
                    "conflict_checked": True,
                    "formula_checked": True,
                },
            }
            for row in semantic_queue
        ]
        receipts = [
            {
                "schema_version": VISUAL_RECEIPT_SCHEMA,
                "page": row["page"],
                "reviewer_instance": "test-reviewer",
                "review_input_sha256": row["review_input_sha256"],
                "page_asset_sha256": row["page_asset_sha256"],
                "verdict": "verified",
                "task_resolutions": [
                    {
                        "task_id": task_id,
                        "verdict": "supported",
                        "observation": "generic",
                    }
                    for task_id in row["visual_task_ids"]
                ],
                "review_session_id": plan["review_session_id"],
                "review_method": "fresh-render-inspection",
                "review_checks": {
                    "page_asset_opened": True,
                    "task_resolutions_checked": True,
                },
            }
            for row in visual_queue
        ]
        semantic_fragment = self.root / "blanket-semantic.jsonl"
        visual_fragment = self.root / "blanket-visual.jsonl"
        semantic_fragment.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in records),
            encoding="utf-8",
        )
        visual_fragment.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in receipts),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SemanticPromotionError, "review_blanket_pattern"):
            merge_review_fragments(
                self.source,
                self.workpack,
                [semantic_fragment],
                [visual_fragment],
            )
        self.assertEqual(
            (self.workpack / "review-records.jsonl").read_text(encoding="utf-8"),
            "",
        )
        self.assertFalse((self.workpack / "review-fragments.json").is_file())

    def test_direct_blanket_writes_are_quarantined_fail_closed(self) -> None:
        self._write_complete_drafts()
        self._bind_synthetic_render_assets()
        prepare_review_plan(self.source, self.workpack, ["test-reviewer"])
        semantic_queue = [
            json.loads(line)
            for line in (self.workpack / "review-queue.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        records = [
            {
                "schema_version": REVIEW_SCHEMA,
                "item_ref": row["item_ref"],
                "item_kind": row["item_kind"],
                "reviewer_instance": "test-reviewer",
                "proposer_instance": row["proposer_instance"],
                "item_sha256": row["item_sha256"],
                "review_input_sha256": row["review_input_sha256"],
                "verdict": "accepted",
                "evidence_support": "full",
                "issue_codes": [],
                "rationale": "the same boilerplate rationale for every accepted item",
                "confidence": {"extraction": 0.99, "interpretation": 0.99},
                "review_session_id": "rws-" + "a" * 20,
                "review_method": "fresh-source-inspection",
                "review_checks": {
                    "evidence_span_checked": True,
                    "support_completeness_checked": True,
                    "scope_and_applicability_checked": True,
                    "conflict_checked": True,
                    "formula_checked": True,
                },
            }
            for row in semantic_queue
        ]
        (self.workpack / "review-records.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in records),
            encoding="utf-8",
        )
        issues, _ = validate_reviewed_workpack(self.source, self.workpack)
        codes = {issue.code for issue in issues if issue.severity == "error"}
        self.assertIn("review_blanket_pattern", codes)
        quarantine = self.workpack / "quarantine"
        self.assertTrue(quarantine.is_dir())
        quarantine_dir = next(path for path in quarantine.iterdir() if path.is_dir())
        receipt = json.loads(
            (quarantine_dir / "quarantine-receipt.json").read_text(encoding="utf-8")
        )
        self.assertEqual(receipt["code"], "review_blanket_pattern")
        self.assertIn("review-records.jsonl", receipt["moved"])
        self.assertFalse((self.workpack / "review-records.jsonl").is_file())

    def test_tampered_hash_and_context_escape_are_rejected(self) -> None:
        written = self._write_complete_drafts()
        path, draft = written["1 Parent Chapter"]
        draft = copy.deepcopy(draft)
        draft["objects"][0]["evidence_spans"][0]["span_sha256"] = "0" * 64
        write_draft(path, draft)
        issues, _ = validate_semantic_workpack(self.source, self.workpack)
        self.assertIn("evidence_hash_mismatch", {issue.code for issue in issues})

        unit = next(unit for unit in self._units() if unit["title"] == "1 Parent Chapter")
        context = unit["context_spans"][0]
        draft["objects"][0]["evidence_spans"][0] = {
            "page": context["page"],
            "start": context["start"],
            "end": context["end"],
            "span_sha256": context["span_sha256"],
        }
        write_draft(path, draft)
        issues, _ = validate_semantic_workpack(self.source, self.workpack)
        self.assertIn("evidence_scope_escape", {issue.code for issue in issues})

    def test_source_text_leak_and_missing_formula_task_are_rejected(self) -> None:
        written = self._write_complete_drafts()
        parent_path, parent = written["1 Parent Chapter"]
        parent = copy.deepcopy(parent)
        parent["objects"][0]["quote"] = "Forbidden copied source text"
        write_draft(parent_path, parent)
        equation_path, equation = written["1.1 Child A"]
        equation = copy.deepcopy(equation)
        equation["objects"][0]["visual_task_ids"] = []
        write_draft(equation_path, equation)
        issues, _ = validate_semantic_workpack(self.source, self.workpack)
        codes = {issue.code for issue in issues}
        self.assertIn("draft_source_text_leak", codes)
        self.assertIn("formula_visual_receipt_missing", codes)

    def test_source_mismatch_and_ambiguous_heading_fail_closed(self) -> None:
        other = self.root / "other.pdf"
        create_semantic_pdf(other)
        other.write_bytes(other.read_bytes() + b"\n% changed\n")
        issues, _ = validate_semantic_workpack(other, self.workpack, require_complete_drafts=False)
        self.assertIn("source_hash_mismatch", {issue.code for issue in issues})

        ambiguous = self.root / "ambiguous.pdf"
        create_semantic_pdf(ambiguous, duplicate_heading=True)
        ambiguous_ir = self.root / "ambiguous-ir"
        result = compile_pdf_intermediate_representation(ambiguous, 1, 3)
        write_intermediate_representation(ambiguous_ir, result)
        with self.assertRaisesRegex(SemanticPromotionError, "heading_locator_ambiguous"):
            build_semantic_workpack(
                ambiguous,
                ambiguous_ir,
                self.root / "ambiguous-workpack",
                semantic_assurance=False,
            )

    def test_parser_receipt_tamper_blocks_workpack_construction(self) -> None:
        receipt_path = self.ir / "parser-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["pages"][0]["page_dimensions_points"]["width"] += 1
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(SemanticPromotionError, "ir_receipt_hash_mismatch"):
            build_semantic_workpack(
                self.source,
                self.ir,
                self.root / "tampered-workpack",
                semantic_assurance=False,
            )

    def test_complete_legacy_v01_ir_remains_usable_but_is_labeled_unbound(self) -> None:
        legacy = self.root / "legacy-ir"
        shutil.copytree(self.ir, legacy)
        for filename in (
            "parser-receipt.json",
            "layout-candidates.jsonl",
            "render-receipts.jsonl",
        ):
            (legacy / filename).unlink()
        for filename in ("source.json", "preflight.json", "document-map.json", "visual-review.json"):
            path = legacy / filename
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["schema_version"] = "tkc.pdf-ir/v0.1"
            if filename == "source.json":
                payload.pop("pdf_ir_receipts", None)
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        manifest = build_semantic_workpack(
            self.source,
            legacy,
            self.root / "legacy-workpack",
            semantic_assurance=False,
        )
        self.assertEqual(manifest["pdf_ir"]["receipt_status"], "legacy-unbound")

    def test_page_top_running_head_is_not_mistaken_for_body_heading(self) -> None:
        title = "1.1 Child A"
        page_text = f"{title} 11 Earlier section text. {title} Body text."
        locations = _locate_headings(
            [{"id": "child-a", "title": title, "physical_pages": [1]}],
            {1: page_text},
        )
        self.assertEqual(locations[0]["start"], page_text.rindex(title))

    def test_exact_span_anchor_can_be_reverified_without_storing_text(self) -> None:
        unit = next(unit for unit in self._units() if unit["title"] == "1 Parent Chapter")
        span = unit["focus_spans"][0]
        package = self.root / "span-package"
        (package / "references/evidence").mkdir(parents=True)
        source_record = self.manifest["source"]
        (package / "manifest.json").write_text(
            json.dumps({
                "sources": [{
                    "source_id": source_record["source_id"],
                    "sha256": source_record["sha256"],
                }]
            }),
            encoding="utf-8",
        )
        anchor = {
            "id": "ev-span-fixture",
            "source_id": source_record["source_id"],
            "pages": [span["page"]],
            "segment_id": unit["segment_id"],
            "locator": {
                "hash_scope": "normalized native text spans",
                "extractor": self.manifest["hash_profile"]["extractor"],
            },
            "text_spans": [{
                "page": span["page"],
                "start": span["start"],
                "end": span["end"],
                "page_text_sha256": span["page_text_sha256"],
                "span_sha256": span["span_sha256"],
            }],
            "excerpt_sha256": span["span_sha256"],
        }
        anchor_path = package / "references/evidence/anchors.jsonl"
        anchor_path.write_text(json.dumps(anchor) + "\n", encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, str(VERIFY_ANCHORS), str(package), "--source", str(self.source)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("anchors=1", completed.stdout)

        anchor["text_spans"][0]["span_sha256"] = "0" * 64
        anchor_path.write_text(json.dumps(anchor) + "\n", encoding="utf-8")
        tampered = subprocess.run(
            [sys.executable, str(VERIFY_ANCHORS), str(package), "--source", str(self.source)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(tampered.returncode, 1)
        self.assertIn("span_hash_mismatch", tampered.stdout)


    def test_out_of_order_outline_ir_builds_workpack(self) -> None:
        source = self.root / "out-of-order.pdf"
        create_out_of_order_outline_pdf(source)
        ir = self.root / "out-of-order-ir"
        result = compile_pdf_intermediate_representation(source, 1, 3)
        write_intermediate_representation(ir, result)
        document_map = json.loads((ir / "document-map.json").read_text(encoding="utf-8"))
        titles = [row["title"] for row in document_map["segments"]]
        self.assertEqual(
            titles,
            ["1.1 First section", "1.2 Second section", "1.3 Third section"],
        )
        manifest = build_semantic_workpack(
            source,
            ir,
            self.root / "out-of-order-workpack",
            semantic_assurance=False,
        )
        self.assertTrue(manifest["units"])
        unit_titles = [
            json.loads(
                (self.root / "out-of-order-workpack" / entry["path"]).read_text(encoding="utf-8")
            )["title"]
            for entry in manifest["units"]
        ]
        self.assertEqual(
            unit_titles,
            ["1.1 First section", "1.2 Second section", "1.3 Third section"],
        )


    def test_override_renames_keep_physical_order_for_workpack(self) -> None:
        source = self.root / "rename-offset.pdf"
        create_rename_offset_pdf(source)
        overrides = self.root / "rename-overrides.jsonl"
        overrides.write_text(
            json.dumps(
                {
                    "schema_version": "tkc.heading-override/v0.1",
                    "before_title": "1.2 Alpha original",
                    "new_title": "1.2 Alpha renamed",
                    "physical_page": 2,
                    "rationale": "The body heading reads '1.2 Alpha renamed'.",
                    "reviewer_instance": "ir-corrector",
                    "before_sha256": hashlib.sha256(
                        "1.2 Alpha original".encode("utf-8")
                    ).hexdigest(),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        ir = self.root / "rename-ir"
        result = compile_pdf_intermediate_representation(
            source, 1, 2, heading_overrides=overrides
        )
        write_intermediate_representation(ir, result)
        manifest = build_semantic_workpack(
            source,
            ir,
            self.root / "rename-workpack",
            semantic_assurance=False,
        )
        unit_titles = [
            json.loads(
                (self.root / "rename-workpack" / entry["path"]).read_text(encoding="utf-8")
            )["title"]
            for entry in manifest["units"]
        ]
        self.assertEqual(
            unit_titles,
            ["1.1 Intro", "1.3 Beta", "1.2 Alpha renamed"],
        )


if __name__ == "__main__":
    unittest.main()
