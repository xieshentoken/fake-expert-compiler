#!/usr/bin/env python3
"""Build or validate a local semantic workpack for born-digital PDF knowledge promotion."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from semantic_promotion import (
    SemanticPromotionError,
    build_semantic_workpack,
    merge_review_fragments,
    prepare_review_plan,
    promote_reviewed_workpack,
    validate_reviewed_workpack,
    validate_semantic_workpack,
)


SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "assets" / "schemas"
SCHEMA_HELP_FILES = {
    "draft": "semantic-draft.schema.json",
    "semantic-review": "semantic-review.schema.json",
    "visual-receipt": "visual-receipt.schema.json",
    "semantic-assertion": "semantic-assertion.schema.json",
    "support-matrix": "support-matrix.schema.json",
    "review-attestation": "review-attestation.schema.json",
    "coverage-ledger": "coverage-ledger.schema.json",
    "visual-object": "visual-object.schema.json",
    "visual-relation": "visual-relation.schema.json",
    "table-grid": "table-grid.schema.json",
    "visual-conflict": "visual-conflict.schema.json",
    "visual-review-attestation": "visual-review-attestation.schema.json",
    "visual-assurance-manifest": "visual-assurance-manifest.schema.json",
}


def _schema_help(kind: str) -> dict[str, object]:
    schema = json.loads((SCHEMA_ROOT / SCHEMA_HELP_FILES[kind]).read_text(encoding="utf-8"))
    properties = schema.get("properties", {})
    summary: dict[str, object] = {
        "kind": kind,
        "schema_version": next(
            (
                value.get("const")
                for value in properties.values()
                if isinstance(value, dict) and value.get("const", "").startswith("tkc.")
            ),
            None,
        ),
        "required": schema.get("required", []),
        "properties": {},
    }
    for name, value in properties.items():
        if not isinstance(value, dict):
            continue
        entry: dict[str, object] = {}
        for field in ("type", "const", "enum", "minItems", "minProperties"):
            if field in value:
                entry[field] = value[field]
        if name == "confidence":
            entry["shape"] = {
                "extraction": "number 0..1",
                "interpretation": "number 0..1",
            }
        if name == "applicability":
            entry["shape"] = {
                "physical_pages": "[start_page, end_page]",
                "excluded_conclusions": "array of strings",
            }
        summary["properties"][name] = entry
    examples: dict[str, object] = {
        "draft": {
            "schema_version": "tkc.semantic-draft/v0.1",
            "unit_id": "<unit-id>",
            "proposer_instance": "<proposal-session>",
            "candidate_disposition": {"action": "context-only", "reason": "<reason>"},
            "objects": [],
            "relations": [],
            "conflicts": [],
            "gaps": [],
        },
        "semantic-review": {
            "schema_version": "tkc.semantic-review/v0.1",
            "item_ref": "<unit-id>:<local-id>",
            "item_kind": "object",
            "reviewer_instance": "<review-session>",
            "proposer_instance": "<proposal-session>",
            "verdict": "accepted",
            "evidence_support": "full",
            "issue_codes": [],
            "rationale": "<source-grounded rationale>",
            "confidence": {"extraction": 0.0, "interpretation": 0.0},
            "review_session_id": "rws-<20 lowercase hex chars>",
            "review_method": "fresh-source-inspection",
            "review_checks": {
                "evidence_span_checked": True,
                "support_completeness_checked": True,
                "scope_and_applicability_checked": True,
                "conflict_checked": True,
            },
        },
        "visual-receipt": {
            "schema_version": "tkc.visual-receipt/v0.1",
            "page": 1,
            "reviewer_instance": "<review-session>",
            "verdict": "verified",
            "task_resolutions": [
                {"task_id": "<task-id>", "verdict": "supported", "observation": "<page-specific observation>"}
            ],
            "review_session_id": "rws-<20 lowercase hex chars>",
            "review_method": "fresh-render-inspection",
            "review_checks": {"page_asset_opened": True, "task_resolutions_checked": True},
        },
        "semantic-assertion": {
            "schema_version": "tkc.semantic-assertion/v0.2",
            "assertion_id": "sa-<20 lowercase hex chars>",
            "parent_item_ref": "<unit-id>:<local-id>",
            "kind": "numeric",
            "origin": "source-explicit",
            "payload": {"field": "statement", "token": "2", "start": 4, "end": 5},
            "risk_tier": 2,
            "assertion_sha256": "<64 lowercase hex chars>",
        },
        "support-matrix": {
            "schema_version": "tkc.support-matrix/v0.1",
            "assertion_id": "sa-<20 lowercase hex chars>",
            "support_kind": "explicit",
            "status": "full",
            "support_sha256": "<64 lowercase hex chars>",
        },
        "review-attestation": {
            "schema_version": "tkc.review-attestation/v0.1",
            "attestation_id": "rat-<20 lowercase hex chars>",
            "attestation_level": "host-orchestrator-recorded-not-cryptographic",
            "item_ref": "<unit-id>:<local-id>",
            "reviewer_instance": "<independent-review-session>",
            "differences": [
                {"assertion_id": "sa-<20 lowercase hex chars>", "status": "confirmed", "observation": "<source-specific observation>"}
            ],
        },
        "coverage-ledger": {
            "schema_version": "tkc.coverage-ledger/v0.1",
            "coverage_id": "cov-<20 lowercase hex chars>",
            "unit_id": "swu-<20 lowercase hex chars>",
            "disposition": "promoted",
            "rationale": "<selected-scope disposition rationale>",
        },
    }
    summary["example"] = examples[kind]
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Create a new ephemeral workpack.")
    build.add_argument("--source", required=True, type=Path)
    build.add_argument("--ir", required=True, type=Path, help="Phase 1 IR directory.")
    build.add_argument("--output", required=True, type=Path, help="Empty output directory.")
    build.add_argument("--context-characters", type=int, default=800)
    build.add_argument("--max-focus-characters", type=int, default=20000)
    build.add_argument("--pdftoppm", type=Path, help="Render all routed visual pages with this binary.")
    build.add_argument("--render-dpi", type=int, default=150)
    build.add_argument(
        "--allow-scan-candidates",
        action="store_true",
        help="Explicitly project hash-locked OCR candidates into a proposal-only workpack; never promotes them directly.",
    )
    build.add_argument(
        "--legacy-semantic-review",
        action="store_true",
        help="Compatibility-only: build the v0.4 single-record review workpack. It cannot claim v0.5 semantic assurance.",
    )
    build.add_argument("--json", action="store_true", dest="json_output")

    validate = subparsers.add_parser("validate", help="Recompute source spans and validate drafts.")
    validate.add_argument("--source", required=True, type=Path)
    validate.add_argument("--workpack", required=True, type=Path)
    validate.add_argument("--level", choices=("proposal", "reviewed"), default="proposal")
    validate.add_argument(
        "--allow-partial-drafts",
        action="store_true",
        help="Validate present drafts without requiring one per unit.",
    )
    validate.add_argument("--json", action="store_true", dest="json_output")

    review = subparsers.add_parser("prepare-review", help="Freeze proposal fingerprints and issue a review plan.")
    review.add_argument("--source", required=True, type=Path)
    review.add_argument("--workpack", required=True, type=Path)
    review.add_argument("--reviewer-instance", action="append", required=True)
    review.add_argument(
        "--visual-review-scope",
        choices=("all", "referenced"),
        default="all",
        help="Review every detected visual task, or only tasks referenced by proposed knowledge objects.",
    )
    review.add_argument("--json", action="store_true", dest="json_output")

    merge = subparsers.add_parser(
        "merge-review",
        help="Merge disjoint reviewer fragments only when coverage and reviewed gates pass.",
    )
    merge.add_argument("--source", required=True, type=Path)
    merge.add_argument("--workpack", required=True, type=Path)
    merge.add_argument("--semantic-fragment", action="append", required=True, type=Path)
    merge.add_argument("--visual-fragment", action="append", default=[], type=Path)
    merge.add_argument("--json", action="store_true", dest="json_output")

    promote = subparsers.add_parser(
        "promote",
        help="Deterministically write accepted reviewed items into a draft Expert Skill.",
    )
    promote.add_argument("--source", required=True, type=Path)
    promote.add_argument("--workpack", required=True, type=Path)
    promote.add_argument("--output", required=True, type=Path)
    promote.add_argument("--name", required=True)
    promote.add_argument("--display-name", required=True)
    promote.add_argument("--domain", required=True)
    promote.add_argument("--source-title")
    promote.add_argument("--json", action="store_true", dest="json_output")

    help_parser = subparsers.add_parser(
        "schema-help",
        help="Show copy-ready fields, enums, and current review trace requirements.",
    )
    help_parser.add_argument(
        "--kind",
        choices=tuple(SCHEMA_HELP_FILES),
        default="draft",
        help="Contract to explain.",
    )
    help_parser.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "schema-help":
        payload = _schema_help(args.kind)
        if args.json_output:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(f"{payload['kind']} schema={payload['schema_version']}")
            print("required:", ", ".join(payload["required"]))
            for name, value in payload["properties"].items():
                details = ", ".join(f"{key}={item!r}" for key, item in value.items())
                print(f"- {name}: {details}")
            print("example:")
            print(json.dumps(payload["example"], ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if args.command == "build":
        try:
            manifest = build_semantic_workpack(
                args.source,
                args.ir,
                args.output,
                context_characters=args.context_characters,
                max_focus_characters=args.max_focus_characters,
                pdftoppm=args.pdftoppm,
                render_dpi=args.render_dpi,
                allow_scan_candidates=args.allow_scan_candidates,
                semantic_assurance=not args.legacy_semantic_review,
            )
        except SemanticPromotionError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        payload = {
            "passed": True,
            "workpack": str(args.output.expanduser().resolve()),
            "workpack_id": manifest["workpack_id"],
            "source_id": manifest["source"]["source_id"],
            "scope": manifest["source"]["scope"],
            "unit_count": len(manifest["units"]),
            "rendered": manifest["render_profile"] is not None,
            "shareable": manifest["policy"]["shareable"],
            "scan_candidate_mode": bool(manifest.get("scan_candidate")),
            "semantic_assurance_protocol": (
                manifest.get("semantic_assurance", {}).get("protocol")
                if isinstance(manifest.get("semantic_assurance"), dict)
                else "legacy"
            ),
        }
        if args.json_output:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(
                "PASS "
                f"workpack={payload['workpack_id']} units={payload['unit_count']} "
                f"rendered={payload['rendered']} shareable={payload['shareable']}"
            )
        return 0

    if args.command == "prepare-review":
        try:
            plan = prepare_review_plan(
                args.source,
                args.workpack,
                args.reviewer_instance,
                visual_review_scope=args.visual_review_scope,
            )
        except SemanticPromotionError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        if args.json_output:
            print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(
                "PASS "
                f"bundle={plan['bundle_sha256']} semantic_items={plan['semantic_item_count']} "
                f"visual_pages={plan['visual_page_count']}"
            )
        return 0

    if args.command == "merge-review":
        try:
            summary = merge_review_fragments(
                args.source,
                args.workpack,
                args.semantic_fragment,
                args.visual_fragment,
            )
        except SemanticPromotionError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        payload = {
            "passed": True,
            "workpack": str(args.workpack.expanduser().resolve()),
            "summary": summary,
        }
        if args.json_output:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(
                "PASS "
                f"review_items={summary['review_item_count']} "
                f"visual_receipts={summary['visual_receipt_count']}"
            )
        return 0

    if args.command == "promote":
        try:
            result = promote_reviewed_workpack(
                args.source,
                args.workpack,
                args.output,
                name=args.name,
                display_name=args.display_name,
                domain=args.domain,
                source_title=args.source_title,
            )
        except SemanticPromotionError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        if args.json_output:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(
                "PASS "
                f"package={result['package']} objects={result['counts']['knowledge_objects']} "
                f"status={result['status']} executable={result['capabilities']['executable']}"
            )
        return 0

    if args.level == "reviewed":
        issues, summary = validate_reviewed_workpack(args.source, args.workpack)
    else:
        issues, summary = validate_semantic_workpack(
            args.source,
            args.workpack,
            require_complete_drafts=not args.allow_partial_drafts,
        )
    errors = sum(issue.severity == "error" for issue in issues)
    warnings = sum(issue.severity == "warning" for issue in issues)
    payload = {
        "passed": errors == 0,
        "workpack": str(args.workpack.expanduser().resolve()),
        "summary": {**summary, "errors": errors, "warnings": warnings},
        "issues": [issue.to_dict() for issue in issues],
    }
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        state = "PASS" if payload["passed"] else "FAIL"
        print(f"{state} workpack={payload['workpack']}")
        for issue in issues:
            print(f"{issue.severity.upper()} {issue.code} {issue.path}: {issue.message}")
        print(f"errors={errors} warnings={warnings}")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
