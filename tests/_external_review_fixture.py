"""Synthetic test-only Review fragments; never use with real workpacks."""
import json
from semantic_assurance import ATTESTATION_SCHEMA, ATTESTATION_LEVEL, expected_attestation_id, review_plan_sha256
from semantic_promotion import prepare_review_plan, merge_review_fragments, sha256_json
from visual_semantics import build_visual_review_attestation, _visual_review_input_payload

def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

def accepting_attestation(
    plan: dict[str, object],
    row: dict[str, object],
    reviewer: str,
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": ATTESTATION_SCHEMA,
        "attestation_id": expected_attestation_id(plan, row, reviewer),
        "attestation_level": ATTESTATION_LEVEL,
        "item_ref": row["item_ref"],
        "item_kind": row["item_kind"],
        "reviewer_instance": reviewer,
        "proposer_instance": row["proposer_instance"],
        "review_session_id": plan["review_session_id"],
        "review_plan_sha256": review_plan_sha256(plan),
        "item_sha256": row["item_sha256"],
        "review_input_sha256": row["review_input_sha256"],
        "assertion_ids": row["assertion_ids"],
        "assertion_bundle_sha256": row["assertion_bundle_sha256"],
        "verdict": "accepted",
        "evidence_support": "full",
        "issue_codes": [],
        "rationale": f"{reviewer} independently confirmed {row['item_ref']} against its bound synthetic evidence.",
        "confidence": {"extraction": 1.0, "interpretation": 0.95},
        "review_method": "fresh-source-inspection",
        "review_checks": {
            "evidence_span_checked": True,
            "support_completeness_checked": True,
            "scope_and_applicability_checked": True,
            "conflict_checked": True,
            "atomic_assertions_checked": True,
            "differences_recorded": True,
            "formula_checked": True,
        },
        "inspected_artifacts": {
            "source_sha256": plan["source_sha256"],
            "evidence_span_sha256s": row["evidence_span_sha256s"],
            "render_sha256s": row["render_sha256s"],
        },
        "differences": [
            {
                "assertion_id": assertion_id,
                "status": "confirmed",
                "observation": f"{reviewer} confirmed atom {index + 1} for {row['item_ref']}.",
            }
            for index, assertion_id in enumerate(row["assertion_ids"])
        ],
    }
    if row["item_kind"] == "gap":
        result["gap_resolution"] = {
            "status": "unresolved_in_package",
            "resolved_by_refs": [],
        }
    return result


def install_synthetic_review(source, workpack, external):
    plan = prepare_review_plan(source, workpack, ["test-reviewer-a", "test-reviewer-b"])
    external.mkdir()
    semantic = [accepting_attestation(plan, row, reviewer)
                for row in rows(workpack / "review-queue.jsonl")
                for reviewer in plan["reviewer_instances"]]
    objects = {row["visual_object_id"]: row for row in rows(workpack / "visual-objects.jsonl")}
    visual = []
    for row in rows(workpack / "visual-review-queue.jsonl"):
        bound = [objects[key] for key in row["visual_object_ids"]]
        payload = {"review_plan_sha256": sha256_json(plan), "source_sha256": plan["source_sha256"],
            "visual_object_ids": row["visual_object_ids"], "table_cell_ids": row["table_cell_ids"],
            "series_ids": row["series_ids"], "visual_relation_ids": row["visual_relation_ids"],
            "relation_sha256s": row["relation_sha256s"], "table_grid_ids": row["table_grid_ids"],
            "table_grid_sha256s": row["table_grid_sha256s"],
            "full_page_sha256": bound[0]["canonical_render_sha256"],
            "crop_sha256s": sorted({obj["crop_sha256"] for obj in bound}),
            "bbox_sha256s": sorted({sha256_json(obj["bbox"]) for obj in bound}),
            "render_sha256": bound[0]["canonical_render_sha256"],
            "render_dpi": bound[0]["canonical_render_dpi"],
            "render_rotation": bound[0]["canonical_render_rotation"]}
        for reviewer in plan["reviewer_instances"]:
            visual.append(build_visual_review_attestation(
                reviewer_instance=reviewer, proposer_instance="test-proposer",
                review_session_id=plan["review_session_id"], **payload,
                input_sha256=sha256_json(_visual_review_input_payload(payload)), verdict="accepted",
                differences=[{"visual_object_id": obj["visual_object_id"], "status": "confirmed",
                              "observation": f"Synthetic fixture checked {obj['visual_object_id']}"} for obj in bound],
                rationale=f"TEST ONLY: {reviewer} accepted synthetic geometry and values.",
                confidence={"extraction": 0.9, "interpretation": 0.9},
                reviewer_independence={"independent": True, "basis": "synthetic contract fixture only"}))
    for name, records in [("semantic", semantic), ("visual", visual)]:
        (external / f"{name}.jsonl").write_text("".join(json.dumps(row)+"\n" for row in records))
    merge_review_fragments(source, workpack, [external/"semantic.jsonl"], [external/"visual.jsonl"])
    return plan
