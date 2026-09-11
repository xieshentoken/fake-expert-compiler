#!/usr/bin/env python3
"""Offline reviewer transport for an existing scanned-PDF structure proposal.

This script never invokes OCR.  It reuses the strict structure candidate and
external-fragment validators in ``scanned_pdf_ocr.py``, re-renders only the
candidate heading pages, and stops until a real reviewer returns both a browser
submission and a separately supplied external attestation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from compiler_version import (
    SCAN_STRUCTURE_REVIEW_ATTESTATION_SCHEMA,
    SCAN_STRUCTURE_REVIEW_REVISION_SCHEMA,
    SCAN_STRUCTURE_REVIEW_SUBMISSION_SCHEMA,
    SCAN_STRUCTURE_REVIEW_WORKBENCH_COMPILER_VERSION,
    SCAN_STRUCTURE_REVIEW_WORKBENCH_PROTOCOL,
    SCAN_STRUCTURE_REVIEW_WORKBENCH_SCHEMA,
)
from scanned_pdf_ocr import (
    ScanOcrError,
    _scan_review_authorization,
    build_scan_structure_review,
    load_scanned_pdf_ir_readonly,
    sha256_file,
    sha256_json,
)
from visual_semantics import _render_page


SHA256_RE = re.compile(r"[0-9a-f]{64}")
SESSION_RE = re.compile(r"rws-[0-9a-f]{20}")
RFC3339_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)
WORKBENCH_FILENAME = "workbench.json"
SUBMISSION_FILENAME = "scan-structure-review-submission.json"
FRAGMENT_FILENAME = "external-scan-structure-review-fragment.json"


class ScanStructureReviewError(ValueError):
    """Fail-closed workbench error."""


def _load_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ScanStructureReviewError(f"json_file_missing:{path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ScanStructureReviewError(f"json_file_invalid:{path.name}") from error


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ScanStructureReviewError(f"jsonl_file_missing:{path.name}")
    rows: list[dict[str, Any]] = []
    try:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ScanStructureReviewError(f"jsonl_row_invalid:{path.name}:{line_number}")
            rows.append(value)
    except (OSError, json.JSONDecodeError) as error:
        raise ScanStructureReviewError(f"jsonl_file_invalid:{path.name}") from error
    return rows


def _write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _write_json(path: Path, value: Any) -> None:
    _write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_bytes(path: Path, value: bytes) -> None:
    path.write_bytes(value)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _fresh_private_directory(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.exists():
        if path.is_symlink() or not path.is_dir() or any(path.iterdir()):
            raise ScanStructureReviewError("output_directory_not_empty")
    else:
        path.mkdir(parents=True)
    path.chmod(stat.S_IRWXU)
    return path


def _safe_identity(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}", value):
        raise ScanStructureReviewError(f"{label}_invalid")
    return value


def _tool_sha256() -> str:
    return sha256_file(Path(__file__).resolve())


def _proposal(path: Path) -> dict[str, Any]:
    value = _load_json(path.expanduser().resolve())
    if not isinstance(value, dict) or set(value) != {"segments"}:
        raise ScanStructureReviewError("structure_proposal_shape_invalid")
    if not isinstance(value["segments"], list) or not value["segments"]:
        raise ScanStructureReviewError("structure_proposal_segments_required")
    return value


def _render_receipts(ir_root: Path) -> list[dict[str, Any]]:
    return _load_jsonl(ir_root / "render-receipts.jsonl")


def _build_candidate_result(ir_root: Path, proposal: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    loaded = load_scanned_pdf_ir_readonly(ir_root)
    scan_ir = loaded["scan_ir"]
    render_rows = _render_receipts(ir_root)
    try:
        result = build_scan_structure_review(
            source_id=str(scan_ir["source_id"]),
            source_sha256=str(scan_ir["source_sha256"]),
            scope=scan_ir["scope"],
            primary_backend=str(scan_ir["backend_selection"]["primary"]),
            observations=loaded["observations"],
            transcripts=loaded["transcripts"],
            transforms=loaded["transforms"],
            backend_receipts=loaded["backend_receipts"],
            render_receipts=render_rows,
            request=proposal,
        )
    except ScanOcrError as error:
        raise ScanStructureReviewError(str(error)) from error
    if result["structure"]["status"] != "review-required":
        raise ScanStructureReviewError("structure_candidate_state_invalid")
    if any(not row.get("candidate_ids") for row in result["segments"]):
        raise ScanStructureReviewError("structure_heading_candidate_missing")
    return loaded, result


def _review_input_hashes(
    source_sha256: str,
    items: Sequence[Mapping[str, Any]],
    candidate_hashes: Mapping[str, str],
) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in items:
        candidate_ids = [str(candidate["candidate_id"]) for candidate in item["candidates"]]
        values[str(item["segment_id"])] = sha256_json(
            {
                "source_sha256": source_sha256,
                "segment_id": item["segment_id"],
                "candidate_ids": candidate_ids,
                "candidate_hashes": {candidate_id: candidate_hashes[candidate_id] for candidate_id in candidate_ids},
                "physical_pages": item["physical_pages"],
                "structure_order": item["structure_order"],
            }
        )
    return values


def _item_payload(segment: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    item = {
        "segment_id": segment["id"],
        "requested_title": segment["title"],
        "physical_pages": segment["physical_pages"],
        "page_range": segment["page_range"],
        "structure_order": segment["structure_order"],
        "parent_id": segment["parent_id"],
        "depth": segment["depth"],
        "candidates": list(candidates),
    }
    item["input_sha256"] = sha256_json(item)
    return item


def _manifest_hash(manifest: Mapping[str, Any]) -> str:
    return sha256_json({key: value for key, value in manifest.items() if key != "manifest_sha256"})


def _attestation_request(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "tkc.scan-structure-review-attestation-request/v0.1",
        "notice": "This is a bound request, not an attestation. The compiler must not fill or sign the external response.",
        "required_response_schema": SCAN_STRUCTURE_REVIEW_ATTESTATION_SCHEMA,
        "bound_payload": {
            "schema_version": SCAN_STRUCTURE_REVIEW_ATTESTATION_SCHEMA,
            "provenance": "external-review-fragment",
            "proposer_instance": manifest["proposer_instance"],
            "reviewer_instance": manifest["reviewer_id"],
            "review_session_id": manifest["review_session_id"],
            "review_plan_sha256": manifest["review_plan_sha256"],
            "candidate_hashes": manifest["candidate_hashes"],
            "review_input_hashes": manifest["review_input_hashes"],
            "source_render_inspected": "REQUIRED_TRUE",
            "title_page_order_bbox_evidence_checked": "REQUIRED_TRUE",
            "differences_recorded": "REQUIRED_TRUE",
            "independent_from_proposer": "REQUIRED_TRUE",
            "provided_outside_workbench": "REQUIRED_TRUE",
            "compiler_authored": "REQUIRED_FALSE",
            "issued_at": "REQUIRED_RFC3339",
        },
        "hash_rule": "attestation_sha256 = sha256(canonical JSON of the complete response excluding attestation_sha256)",
    }


def generate_workbench(
    *,
    source: Path,
    ir_root: Path,
    proposal_path: Path,
    output: Path,
    pdftoppm: Path,
    reviewer_id: str,
    review_session_id: str,
    proposer_instance: str,
    created_at: str,
) -> dict[str, Any]:
    reviewer_id = _safe_identity(reviewer_id, "reviewer_id")
    proposer_instance = _safe_identity(proposer_instance, "proposer_instance")
    if reviewer_id == proposer_instance:
        raise ScanStructureReviewError("reviewer_not_independent")
    if not SESSION_RE.fullmatch(review_session_id):
        raise ScanStructureReviewError("review_session_id_invalid")
    if not RFC3339_RE.fullmatch(created_at):
        raise ScanStructureReviewError("created_at_invalid")
    source = source.expanduser().resolve()
    ir_root = ir_root.expanduser().resolve()
    proposal_path = proposal_path.expanduser().resolve()
    if source.is_symlink() or not source.is_file():
        raise ScanStructureReviewError("source_missing")
    proposal = _proposal(proposal_path)
    loaded, result = _build_candidate_result(ir_root, proposal)
    scan_ir = loaded["scan_ir"]
    if sha256_file(source) != scan_ir["source_sha256"]:
        raise ScanStructureReviewError("source_sha256_mismatch")

    by_segment: dict[str, list[dict[str, Any]]] = {}
    for candidate in result["candidates"]:
        by_segment.setdefault(str(candidate["segment_id"]), []).append(candidate)
    items = [
        _item_payload(segment, sorted(by_segment[str(segment["id"])], key=lambda row: str(row["candidate_id"])))
        for segment in result["segments"]
    ]
    candidate_hashes = {
        str(candidate["candidate_id"]): str(candidate["candidate_sha256"])
        for candidate in result["candidates"]
    }
    input_hashes = _review_input_hashes(str(scan_ir["source_sha256"]), items, candidate_hashes)
    bundle_sha256 = sha256_json(
        {"candidate_hashes": candidate_hashes, "review_input_hashes": input_hashes}
    )
    workbench_id = "ssw-" + sha256_json(
        {
            "source_sha256": scan_ir["source_sha256"],
            "scan_ir_sha256": sha256_file(ir_root / "scan-ir.json"),
            "proposal_sha256": sha256_json(proposal),
            "reviewer_id": reviewer_id,
            "review_session_id": review_session_id,
            "proposer_instance": proposer_instance,
            "tool_sha256": _tool_sha256(),
        }
    )[:20]
    plan: dict[str, Any] = {
        "schema_version": "tkc.review-plan/v0.1",
        "attestation_level": "host-orchestrator-recorded-not-cryptographic",
        "issued_by": "host-orchestrator",
        "issued_at": created_at,
        "source_sha256": scan_ir["source_sha256"],
        "workpack_id": workbench_id,
        "bundle_sha256": bundle_sha256,
        "proposer_instances": [proposer_instance],
        "reviewer_instances": [reviewer_id],
        "semantic_item_count": 0,
        "visual_page_count": len({int(candidate["physical_page"]) for candidate in result["candidates"]}),
        "gap_resolution_policy": "explicit-package-scope-v1",
        "review_protocol": "separate-source-render-review-v1",
        "review_session_id": review_session_id,
    }
    plan["authorization_sha256"] = _scan_review_authorization(plan)
    plan_sha256 = sha256_json(plan)

    output = _fresh_private_directory(output)
    assets = output / "assets"
    assets.mkdir()
    assets.chmod(stat.S_IRWXU)
    render_by_page = {int(row["physical_page"]): row for row in _render_receipts(ir_root)}
    page_assets: dict[str, dict[str, Any]] = {}
    for page in sorted({int(candidate["physical_page"]) for candidate in result["candidates"]}):
        expected = render_by_page.get(page)
        if not isinstance(expected, Mapping):
            raise ScanStructureReviewError(f"render_receipt_missing:page={page}")
        version, payload, actual_sha256 = _render_page(
            source, page, pdftoppm.expanduser().resolve(), int(expected["renderer"]["dpi"])
        )
        if actual_sha256 != expected["render_sha256"]:
            raise ScanStructureReviewError(f"render_sha256_mismatch:page={page}")
        relative = f"assets/context-page-{page:04d}.png"
        _write_bytes(output / relative, payload)
        transform = next(
            row for row in loaded["transforms"] if int(row["physical_page"]) == page
        )
        page_assets[str(page)] = {
            "path": relative,
            "sha256": actual_sha256,
            "renderer_version": version,
            "dpi": int(expected["renderer"]["dpi"]),
            "pdf_dimensions_points": transform["pdf_dimensions_points"],
            "image_dimensions_px": transform["image_dimensions_px"],
            "coordinate_transform_id": transform["id"],
            "coordinate_transform_sha256": sha256_json(transform),
        }

    manifest: dict[str, Any] = {
        "schema_version": SCAN_STRUCTURE_REVIEW_WORKBENCH_SCHEMA,
        "protocol": SCAN_STRUCTURE_REVIEW_WORKBENCH_PROTOCOL,
        "compiler_version": SCAN_STRUCTURE_REVIEW_WORKBENCH_COMPILER_VERSION,
        "workbench_id": workbench_id,
        "created_at": created_at,
        "source_sha256": scan_ir["source_sha256"],
        "source_id": scan_ir["source_id"],
        "scan_ir_sha256": sha256_file(ir_root / "scan-ir.json"),
        "scan_ir_compiler_version": scan_ir["compiler_version"],
        "scope": scan_ir["scope"],
        "proposal_sha256": sha256_json(proposal),
        "tool_sha256": _tool_sha256(),
        "reviewer_id": reviewer_id,
        "review_session_id": review_session_id,
        "proposer_instance": proposer_instance,
        "review_plan": plan,
        "review_plan_sha256": plan_sha256,
        "candidate_hashes": candidate_hashes,
        "review_input_hashes": input_hashes,
        "bundle_sha256": bundle_sha256,
        "items": items,
        "page_assets": page_assets,
        "policy": {
            "private": True,
            "release_included": False,
            "source_pdf_included": False,
            "ocr_or_model_invoked": False,
            "network_used": False,
            "browser_authors_attestation": False,
            "compiler_authors_review": False,
            "accepted_fragment_requires_external_attestation": True,
            "candidate_bbox_is_locked": True,
            "incorrect_bbox_action": "reject-or-needs-review-and-regenerate-proposal",
            "promotion_allowed": False,
            "mvp_acceptance": False,
        },
    }
    manifest["manifest_sha256"] = _manifest_hash(manifest)
    _write_json(output / WORKBENCH_FILENAME, manifest)
    _write_json(output / "structure-proposal.json", proposal)
    _write_json(output / "external-attestation-request.json", _attestation_request(manifest))
    _write_text(output / "review.html", _review_html(manifest))
    _write_text(output / "人工结构Review使用说明.md", _instructions(manifest))
    return manifest


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != SCAN_STRUCTURE_REVIEW_WORKBENCH_SCHEMA:
        raise ScanStructureReviewError("workbench_schema_invalid")
    if manifest.get("protocol") != SCAN_STRUCTURE_REVIEW_WORKBENCH_PROTOCOL:
        raise ScanStructureReviewError("workbench_protocol_invalid")
    if manifest.get("compiler_version") != SCAN_STRUCTURE_REVIEW_WORKBENCH_COMPILER_VERSION:
        raise ScanStructureReviewError("workbench_compiler_version_stale")
    if manifest.get("tool_sha256") != _tool_sha256():
        raise ScanStructureReviewError("workbench_tool_drift")
    if manifest.get("manifest_sha256") != _manifest_hash(manifest):
        raise ScanStructureReviewError("workbench_manifest_hash_invalid")
    if manifest.get("review_plan_sha256") != sha256_json(manifest.get("review_plan")):
        raise ScanStructureReviewError("workbench_review_plan_hash_invalid")
    if manifest.get("review_plan", {}).get("authorization_sha256") != _scan_review_authorization(manifest["review_plan"]):
        raise ScanStructureReviewError("workbench_review_plan_authorization_invalid")
    if manifest.get("bundle_sha256") != sha256_json(
        {
            "candidate_hashes": manifest.get("candidate_hashes"),
            "review_input_hashes": manifest.get("review_input_hashes"),
        }
    ):
        raise ScanStructureReviewError("workbench_bundle_hash_invalid")
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        raise ScanStructureReviewError("workbench_items_invalid")
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("segment_id"), str):
            raise ScanStructureReviewError("workbench_item_invalid")
        if item["segment_id"] in seen:
            raise ScanStructureReviewError("workbench_item_duplicate")
        seen.add(str(item["segment_id"]))
        expected_item_hash = sha256_json({key: value for key, value in item.items() if key != "input_sha256"})
        if item.get("input_sha256") != expected_item_hash:
            raise ScanStructureReviewError(f"workbench_item_hash_invalid:{item['segment_id']}")
        candidates = item.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ScanStructureReviewError(f"workbench_candidate_missing:{item['segment_id']}")
        for candidate in candidates:
            if candidate.get("candidate_sha256") != sha256_json(
                {key: value for key, value in candidate.items() if key != "candidate_sha256"}
            ):
                raise ScanStructureReviewError(f"workbench_candidate_hash_invalid:{item['segment_id']}")


def _validate_transport_files(workbench: Path, manifest: Mapping[str, Any]) -> None:
    bundled_proposal_path = workbench / "structure-proposal.json"
    if (
        bundled_proposal_path.is_symlink()
        or not bundled_proposal_path.is_file()
        or sha256_json(_load_json(bundled_proposal_path)) != manifest["proposal_sha256"]
    ):
        raise ScanStructureReviewError("workbench_proposal_copy_mismatch")
    for filename, expected in (
        ("review.html", _review_html(manifest)),
        ("人工结构Review使用说明.md", _instructions(manifest)),
    ):
        path = workbench / filename
        if path.is_symlink() or not path.is_file() or path.read_text(encoding="utf-8") != expected:
            raise ScanStructureReviewError(f"workbench_transport_file_drift:{filename}")
    if _load_json(workbench / "external-attestation-request.json") != _attestation_request(manifest):
        raise ScanStructureReviewError("workbench_attestation_request_drift")


def validate_workbench(
    *, source: Path, ir_root: Path, proposal_path: Path, workbench: Path, pdftoppm: Path
) -> dict[str, Any]:
    workbench = workbench.expanduser().resolve()
    manifest = _load_json(workbench / WORKBENCH_FILENAME)
    if not isinstance(manifest, Mapping):
        raise ScanStructureReviewError("workbench_manifest_invalid")
    _validate_manifest(manifest)
    source = source.expanduser().resolve()
    ir_root = ir_root.expanduser().resolve()
    proposal_path = proposal_path.expanduser().resolve()
    if sha256_file(source) != manifest["source_sha256"]:
        raise ScanStructureReviewError("workbench_source_sha256_mismatch")
    if sha256_file(ir_root / "scan-ir.json") != manifest["scan_ir_sha256"]:
        raise ScanStructureReviewError("workbench_scan_ir_sha256_mismatch")
    if sha256_json(_proposal(proposal_path)) != manifest["proposal_sha256"]:
        raise ScanStructureReviewError("workbench_proposal_sha256_mismatch")
    _validate_transport_files(workbench, manifest)
    bundled_proposal = _load_json(workbench / "structure-proposal.json")
    if bundled_proposal != _proposal(proposal_path):
        raise ScanStructureReviewError("workbench_proposal_copy_mismatch")
    _, rebuilt = _build_candidate_result(ir_root, _proposal(proposal_path))
    rebuilt_hashes = {
        str(candidate["candidate_id"]): str(candidate["candidate_sha256"])
        for candidate in rebuilt["candidates"]
    }
    if rebuilt_hashes != manifest["candidate_hashes"]:
        raise ScanStructureReviewError("workbench_candidate_replay_mismatch")
    for page_value, asset in manifest["page_assets"].items():
        page = int(page_value)
        path = workbench / asset["path"]
        if path.is_symlink() or not path.is_file() or sha256_file(path) != asset["sha256"]:
            raise ScanStructureReviewError(f"workbench_asset_hash_invalid:page={page}")
        _, payload, render_sha256 = _render_page(source, page, pdftoppm.expanduser().resolve(), int(asset["dpi"]))
        if render_sha256 != asset["sha256"] or payload != path.read_bytes():
            raise ScanStructureReviewError(f"workbench_render_replay_mismatch:page={page}")
    return {
        "passed": True,
        "status": "paused-independent-structure-review",
        "workbench_id": manifest["workbench_id"],
        "item_count": len(manifest["items"]),
        "page_asset_count": len(manifest["page_assets"]),
        "ocr_or_model_invoked": False,
        "release_included": False,
    }


def _validate_submission(manifest: Mapping[str, Any], submission: Mapping[str, Any]) -> dict[str, Any]:
    required_top = {
        "schema_version", "protocol", "workbench_id", "manifest_sha256", "reviewer_id",
        "review_session_id", "proposer_instance", "exported_at", "items", "declarations",
    }
    if set(submission) != required_top:
        raise ScanStructureReviewError("submission_shape_invalid")
    if submission.get("schema_version") != SCAN_STRUCTURE_REVIEW_SUBMISSION_SCHEMA:
        raise ScanStructureReviewError("submission_schema_invalid")
    if submission.get("protocol") != SCAN_STRUCTURE_REVIEW_WORKBENCH_PROTOCOL:
        raise ScanStructureReviewError("submission_protocol_invalid")
    for key in ("workbench_id", "manifest_sha256", "reviewer_id", "review_session_id", "proposer_instance"):
        expected_key = "manifest_sha256" if key == "manifest_sha256" else key
        if submission.get(key) != manifest.get(expected_key):
            raise ScanStructureReviewError(f"submission_identity_mismatch:{key}")
    if not isinstance(submission.get("exported_at"), str) or not RFC3339_RE.fullmatch(str(submission["exported_at"])):
        raise ScanStructureReviewError("submission_exported_at_invalid")
    declarations = submission.get("declarations")
    declaration_keys = {
        "source_render_inspected", "no_hidden_answer_or_prediction_used",
        "independent_from_proposer", "differences_recorded",
    }
    if not isinstance(declarations, Mapping) or set(declarations) != declaration_keys:
        raise ScanStructureReviewError("submission_declarations_invalid")
    items = submission.get("items")
    if not isinstance(items, list):
        raise ScanStructureReviewError("submission_items_invalid")
    expected_by_id = {str(item["segment_id"]): item for item in manifest["items"]}
    actual_by_id: dict[str, Mapping[str, Any]] = {}
    accepted = True
    blockers: list[str] = []
    item_keys = {
        "segment_id", "input_sha256", "decision", "selected_candidate_id",
        "confirmations", "rationale", "difference_observation",
    }
    confirmation_keys = {
        "title_confirmed", "page_range_confirmed", "reading_order_confirmed",
        "bbox_confirmed", "evidence_confirmed",
    }
    for row in items:
        if not isinstance(row, Mapping) or set(row) != item_keys or not isinstance(row.get("segment_id"), str):
            raise ScanStructureReviewError("submission_item_shape_invalid")
        segment_id = str(row["segment_id"])
        if segment_id in actual_by_id or segment_id not in expected_by_id:
            raise ScanStructureReviewError("submission_item_set_invalid")
        actual_by_id[segment_id] = row
        expected = expected_by_id[segment_id]
        if row.get("input_sha256") != expected["input_sha256"]:
            raise ScanStructureReviewError(f"submission_item_hash_mismatch:{segment_id}")
        if row.get("decision") not in {"accepted", "rejected", "needs_review"}:
            raise ScanStructureReviewError(f"submission_decision_invalid:{segment_id}")
        candidate_ids = {candidate["candidate_id"] for candidate in expected["candidates"]}
        if row.get("selected_candidate_id") is not None and row.get("selected_candidate_id") not in candidate_ids:
            raise ScanStructureReviewError(f"submission_candidate_invalid:{segment_id}")
        confirmations = row.get("confirmations")
        if not isinstance(confirmations, Mapping) or set(confirmations) != confirmation_keys or any(
            not isinstance(value, bool) for value in confirmations.values()
        ):
            raise ScanStructureReviewError(f"submission_confirmations_invalid:{segment_id}")
        rationale = row.get("rationale")
        difference = row.get("difference_observation")
        if not isinstance(rationale, str) or not rationale.strip() or not isinstance(difference, str) or not difference.strip():
            blockers.append(f"review_text_incomplete:{segment_id}")
        if row["decision"] != "accepted" or row.get("selected_candidate_id") is None or not all(confirmations.values()):
            accepted = False
            blockers.append(f"not_accepted:{segment_id}")
    if set(actual_by_id) != set(expected_by_id):
        raise ScanStructureReviewError("submission_coverage_incomplete")
    if not all(declarations.values()):
        accepted = False
        blockers.append("declarations_incomplete")
    if blockers:
        accepted = False
    return {"accepted": accepted, "blockers": sorted(set(blockers)), "items": actual_by_id}


def _validate_attestation(manifest: Mapping[str, Any], attestation: Mapping[str, Any]) -> None:
    required = {
        "schema_version", "provenance", "proposer_instance", "reviewer_instance",
        "review_session_id", "review_plan_sha256", "candidate_hashes",
        "review_input_hashes", "source_render_inspected",
        "title_page_order_bbox_evidence_checked", "differences_recorded",
        "independent_from_proposer", "provided_outside_workbench", "compiler_authored",
        "issued_at", "attestation_sha256",
    }
    if set(attestation) != required:
        raise ScanStructureReviewError("external_attestation_shape_invalid")
    expected = {
        "schema_version": SCAN_STRUCTURE_REVIEW_ATTESTATION_SCHEMA,
        "provenance": "external-review-fragment",
        "proposer_instance": manifest["proposer_instance"],
        "reviewer_instance": manifest["reviewer_id"],
        "review_session_id": manifest["review_session_id"],
        "review_plan_sha256": manifest["review_plan_sha256"],
        "candidate_hashes": manifest["candidate_hashes"],
        "review_input_hashes": manifest["review_input_hashes"],
        "source_render_inspected": True,
        "title_page_order_bbox_evidence_checked": True,
        "differences_recorded": True,
        "independent_from_proposer": True,
        "provided_outside_workbench": True,
        "compiler_authored": False,
    }
    for key, value in expected.items():
        if attestation.get(key) != value:
            raise ScanStructureReviewError(f"external_attestation_binding_invalid:{key}")
    if not isinstance(attestation.get("issued_at"), str) or not RFC3339_RE.fullmatch(str(attestation["issued_at"])):
        raise ScanStructureReviewError("external_attestation_issued_at_invalid")
    if attestation.get("attestation_sha256") != sha256_json(
        {key: value for key, value in attestation.items() if key != "attestation_sha256"}
    ):
        raise ScanStructureReviewError("external_attestation_hash_invalid")


def _reviewed_segment(item: Mapping[str, Any], submission: Mapping[str, Any]) -> dict[str, Any]:
    selected = next(
        candidate for candidate in item["candidates"]
        if candidate["candidate_id"] == submission["selected_candidate_id"]
    )
    locator = selected["scan_locator"]
    return {
        "segment_id": item["segment_id"],
        "candidate_id": selected["candidate_id"],
        "title": item["requested_title"],
        "page_range": item["page_range"],
        "structure_order": item["structure_order"],
        "bbox_pdf_points": locator["bbox_pdf_points"],
        "ocr_observation_ids": locator["ocr_observation_ids"],
        "transcript_span_ids": locator["transcript_span_ids"],
        "transcript_row_hashes": [row["row_sha256"] for row in locator["transcript_row_commitments"]],
        "observation_hashes": [row["observation_sha256"] for row in locator["observation_commitments"]],
        "evidence": {
            key: locator[key]
            for key in (
                "source_sha256", "render_sha256", "render_dpi", "coordinate_transform_id",
                "transcript_sha256", "backend_id", "model_identity_sha256",
                "configuration_sha256", "render_receipt_sha256",
                "coordinate_transform_sha256", "backend_receipt_sha256",
                "runtime_contract_sha256", "worker_sha256",
            )
        },
        **submission["confirmations"],
        "decision": "accepted",
        "rationale": submission["rationale"].strip(),
        "difference_observation": submission["difference_observation"].strip(),
    }


def finalize(
    *, workbench: Path, submission_path: Path, attestation_path: Path | None, output: Path
) -> dict[str, Any]:
    workbench = workbench.expanduser().resolve()
    manifest = _load_json(workbench / WORKBENCH_FILENAME)
    submission = _load_json(submission_path.expanduser().resolve())
    if not isinstance(manifest, Mapping) or not isinstance(submission, Mapping):
        raise ScanStructureReviewError("finalize_input_invalid")
    _validate_manifest(manifest)
    _validate_transport_files(workbench, manifest)
    submission_result = _validate_submission(manifest, submission)
    output = _fresh_private_directory(output)
    revision: dict[str, Any] = {
        "schema_version": SCAN_STRUCTURE_REVIEW_REVISION_SCHEMA,
        "protocol": SCAN_STRUCTURE_REVIEW_WORKBENCH_PROTOCOL,
        "compiler_version": SCAN_STRUCTURE_REVIEW_WORKBENCH_COMPILER_VERSION,
        "workbench_id": manifest["workbench_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "submission_sha256": sha256_file(submission_path.expanduser().resolve()),
        "status": "paused",
        "pause_reason": "independent-external-attestation-required",
        "blockers": list(submission_result["blockers"]),
        "fragment_count": 0,
        "fragment_sha256": None,
        "candidate_only": True,
        "promotion_allowed": False,
        "release_included": False,
        "mvp_acceptance": False,
    }
    if not submission_result["accepted"]:
        revision["pause_reason"] = "independent-structure-review-incomplete"
    elif attestation_path is not None:
        attestation = _load_json(attestation_path.expanduser().resolve())
        if not isinstance(attestation, Mapping):
            raise ScanStructureReviewError("external_attestation_invalid")
        _validate_attestation(manifest, attestation)
        registry = {
            "reviewer_instances": [manifest["reviewer_id"]],
            "registry_sha256": sha256_json({"reviewer_instances": [manifest["reviewer_id"]]}),
        }
        segment_submissions = submission_result["items"]
        fragment = {
            "schema_version": "tkc.scanned-pdf-structure-review-fragment/v0.1",
            "provenance": "external-review-fragment",
            "review_plan": manifest["review_plan"],
            "review_plan_sha256": manifest["review_plan_sha256"],
            "reviewer_registry": registry,
            "attestation": dict(attestation),
            "candidate_hashes": manifest["candidate_hashes"],
            "review_input_hashes": manifest["review_input_hashes"],
            "segments": [
                _reviewed_segment(item, segment_submissions[str(item["segment_id"])])
                for item in manifest["items"]
            ],
        }
        fragment_path = output / FRAGMENT_FILENAME
        _write_json(fragment_path, fragment)
        revision.update(
            {
                "status": "accepted-external-fragment",
                "pause_reason": None,
                "blockers": [],
                "fragment_count": 1,
                "fragment_sha256": sha256_file(fragment_path),
                "external_attestation_sha256": attestation["attestation_sha256"],
            }
        )
    revision["revision_sha256"] = sha256_json(revision)
    _write_json(output / "revision.json", revision)
    return revision


def _review_html(manifest: Mapping[str, Any]) -> str:
    data = json.dumps(
        manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).replace("</", "<\\/")
    template = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>扫描 PDF 结构人工 Review</title>
<style>
:root{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#17202a;background:#f4f1ea}*{box-sizing:border-box}body{margin:0}header{padding:14px 20px;background:#17324d;color:white;display:flex;justify-content:space-between;gap:16px;align-items:center}.layout{display:grid;grid-template-columns:230px minmax(0,1fr) 390px;height:calc(100vh - 68px)}aside,.form{overflow:auto;background:#fff;border-right:1px solid #d7dce0;padding:12px}.form{border-right:0;border-left:1px solid #d7dce0}.item{display:block;width:100%;text-align:left;margin:0 0 8px;padding:10px;border:1px solid #ccd3d9;border-radius:8px;background:white;cursor:pointer}.item.active{border-color:#0c6e93;background:#e9f5fa}.viewer{min-width:0;display:flex;flex-direction:column}.toolbar{padding:8px 12px;background:#fafafa;border-bottom:1px solid #d7dce0}.viewport{flex:1;overflow:auto;background:#3b4045;padding:20px}.canvas{position:relative;margin:auto;transform-origin:top left}.canvas img{display:block;width:100%;height:auto;user-select:none}.bbox{position:absolute;border:3px solid #e03d2f;background:rgba(224,61,47,.12);pointer-events:none}.field{margin:0 0 12px}.field label{font-weight:650;display:block;margin-bottom:5px}.locked{padding:8px;background:#f1f3f5;border-radius:6px;font-family:ui-monospace,monospace;font-size:12px;word-break:break-all}select,textarea{width:100%;padding:8px;border:1px solid #b7c0c8;border-radius:6px;background:white}textarea{min-height:72px;resize:vertical}.checks label{font-weight:400;margin:7px 0}.buttons{display:flex;gap:8px;flex-wrap:wrap}button{padding:8px 11px;border:1px solid #78909c;border-radius:7px;background:white;cursor:pointer}button.primary{background:#0c6e93;color:white;border-color:#0c6e93}.warning{padding:9px;background:#fff1cc;border-left:4px solid #e0a100;margin-bottom:12px}.ok{color:#0d7041}.bad{color:#a22929}.small{font-size:12px;color:#59636c}h2,h3{margin:8px 0 12px}
</style></head><body>
<header><div><strong>Phase 7D.6E-C 扫描结构人工 Review</strong><div class="small" style="color:#dce8f2" id="progress"></div></div><div class="buttons"><button id="importBtn">导入已有 submission</button><button class="primary" id="exportBtn">导出 submission.json</button><input id="importFile" type="file" accept="application/json" hidden></div></header>
<div class="layout"><aside><div id="items"></div></aside><main class="viewer"><div class="toolbar buttons"><button data-zoom="-0.2">缩小</button><button data-zoom="0.2">放大</button><button id="fitBtn">适合窗口</button><button id="oneBtn">100%</button><span id="coord" class="small"></span></div><div class="viewport" id="viewport"><div class="canvas" id="canvas"><img id="pageImg"><div class="bbox" id="bbox"></div></div></div></main><section class="form"><div class="warning">这里只确认 OCR 提出的章节结构。候选 BBox 是只读锁定值；若位置错误，请选择“需重新处理”或“拒绝”，不要手改坐标。导出 submission 不是 attestation，也不是 Gold。</div><h2 id="title"></h2><div class="field"><label>候选标题位置</label><select id="candidate"></select></div><div class="field"><label>标题文本</label><div class="locked" id="rawTitle"></div></div><div class="field"><label>BBox [x0,y0,x1,y1]（PDF 点）</label><div class="locked" id="bboxText"></div></div><div class="field"><label>页段 / 顺序</label><div class="locked" id="scopeText"></div></div><div class="field"><label>结论</label><select id="decision"><option value="">请选择</option><option value="accepted">接受</option><option value="needs_review">需重新处理</option><option value="rejected">拒绝</option></select></div><div class="field checks"><label><input type="checkbox" data-check="title_confirmed"> 标题与页面可见内容一致</label><label><input type="checkbox" data-check="page_range_confirmed"> 页段范围正确</label><label><input type="checkbox" data-check="reading_order_confirmed"> 结构顺序正确</label><label><input type="checkbox" data-check="bbox_confirmed"> BBox 正确覆盖标题</label><label><input type="checkbox" data-check="evidence_confirmed"> 已查看并确认绑定的 render / transcript / observation 证据</label></div><div class="field"><label>审查理由</label><textarea id="rationale" placeholder="说明为什么接受、拒绝或要求重新处理。"></textarea></div><div class="field"><label>差异观察</label><textarea id="difference" placeholder="没有差异时明确写“未发现差异”；有差异时具体描述。"></textarea></div><div class="buttons"><button id="prevBtn">上一项</button><button id="saveBtn">保存当前项</button><button id="nextBtn">下一项</button></div><hr><h3>整批声明</h3><div class="checks"><label><input type="checkbox" data-declaration="source_render_inspected"> 我逐项查看了页面 render</label><label><input type="checkbox" data-declaration="no_hidden_answer_or_prediction_used"> 我没有使用隐藏答案或下游 prediction</label><label><input type="checkbox" data-declaration="independent_from_proposer"> 我与 proposer 角色不同</label><label><input type="checkbox" data-declaration="differences_recorded"> 我已经记录每项差异或明确无差异</label></div><p class="small">这些勾选是 reviewer 声明，不自动证明独立性；独立 external attestation 必须在本页面之外另行提供。</p></section></div>
<script id="manifest" type="application/json">__MANIFEST__</script><script>
const M=JSON.parse(document.getElementById('manifest').textContent);const key=`scan-structure-review:${M.workbench_id}:${M.reviewer_id}`;let index=0,zoom=1;const checks=['title_confirmed','page_range_confirmed','reading_order_confirmed','bbox_confirmed','evidence_confirmed'];const declarations=['source_render_inspected','no_hidden_answer_or_prediction_used','independent_from_proposer','differences_recorded'];let draft={};try{draft=JSON.parse(localStorage.getItem(key)||'{}')}catch(_){draft={}};draft.items=draft.items||{};draft.declarations=draft.declarations||{};
function empty(item){return{segment_id:item.segment_id,input_sha256:item.input_sha256,decision:'',selected_candidate_id:item.candidates[0]?.candidate_id||null,confirmations:Object.fromEntries(checks.map(k=>[k,false])),rationale:'',difference_observation:''}}
function current(){const item=M.items[index];draft.items[item.segment_id]=draft.items[item.segment_id]||empty(item);return draft.items[item.segment_id]}
function saveForm(){const d=current();d.selected_candidate_id=document.getElementById('candidate').value||null;d.decision=document.getElementById('decision').value;checks.forEach(k=>d.confirmations[k]=document.querySelector(`[data-check="${k}"]`).checked);d.rationale=document.getElementById('rationale').value;d.difference_observation=document.getElementById('difference').value;declarations.forEach(k=>draft.declarations[k]=document.querySelector(`[data-declaration="${k}"]`).checked);localStorage.setItem(key,JSON.stringify(draft));renderList()}
function selected(){const item=M.items[index],d=current();return item.candidates.find(c=>c.candidate_id===d.selected_candidate_id)||item.candidates[0]}
function renderList(){const root=document.getElementById('items');root.innerHTML='';M.items.forEach((item,i)=>{const d=draft.items[item.segment_id];const b=document.createElement('button');b.className='item'+(i===index?' active':'');b.textContent=`${i+1}. ${item.requested_title} ${d?.decision?`[${d.decision}]`:''}`;b.onclick=()=>{saveForm();index=i;render()};root.appendChild(b)});const done=M.items.filter(i=>draft.items[i.segment_id]?.decision).length;document.getElementById('progress').textContent=`${done}/${M.items.length} 已填写 · reviewer ${M.reviewer_id}`}
function render(){const item=M.items[index],d=current();document.getElementById('title').textContent=`${index+1}/${M.items.length} · ${item.segment_id}`;const sel=document.getElementById('candidate');sel.innerHTML='';item.candidates.forEach(c=>{const o=document.createElement('option');o.value=c.candidate_id;o.textContent=`页 ${c.physical_page} · ${c.raw_title}`;sel.appendChild(o)});sel.value=d.selected_candidate_id;sel.onchange=()=>{d.selected_candidate_id=sel.value;saveForm();renderCandidate()};document.getElementById('decision').value=d.decision;checks.forEach(k=>document.querySelector(`[data-check="${k}"]`).checked=!!d.confirmations[k]);document.getElementById('rationale').value=d.rationale;document.getElementById('difference').value=d.difference_observation;declarations.forEach(k=>document.querySelector(`[data-declaration="${k}"]`).checked=!!draft.declarations[k]);renderCandidate();renderList()}
function renderCandidate(){const c=selected(),asset=M.page_assets[String(c.physical_page)],dims=asset.pdf_dimensions_points,b=c.scan_locator.bbox_pdf_points;document.getElementById('rawTitle').textContent=c.raw_title;document.getElementById('bboxText').textContent=`x0=${b[0]} · y0=${b[1]} · x1=${b[2]} · y1=${b[3]}`;const item=M.items[index];document.getElementById('scopeText').textContent=`物理页 ${item.page_range[0]}–${item.page_range[1]} · structure_order=${item.structure_order}`;const img=document.getElementById('pageImg');img.src=asset.path;const canvas=document.getElementById('canvas');canvas.style.width=`${asset.image_dimensions_px.width*zoom}px`;const box=document.getElementById('bbox');box.style.left=`${100*b[0]/dims.width}%`;box.style.top=`${100*b[1]/dims.height}%`;box.style.width=`${100*(b[2]-b[0])/dims.width}%`;box.style.height=`${100*(b[3]-b[1])/dims.height}%`;canvas.onmousemove=e=>{const r=img.getBoundingClientRect();const x=(e.clientX-r.left)*dims.width/r.width,y=(e.clientY-r.top)*dims.height/r.height;document.getElementById('coord').textContent=(x>=0&&y>=0&&x<=dims.width&&y<=dims.height)?`x=${x.toFixed(2)}, y=${y.toFixed(2)} PDF 点`:''}}
document.getElementById('saveBtn').onclick=()=>{saveForm();alert('已保存到当前浏览器 localStorage')};document.getElementById('prevBtn').onclick=()=>{saveForm();index=Math.max(0,index-1);render()};document.getElementById('nextBtn').onclick=()=>{saveForm();index=Math.min(M.items.length-1,index+1);render()};document.querySelectorAll('[data-zoom]').forEach(b=>b.onclick=()=>{zoom=Math.max(.2,Math.min(2.5,zoom+Number(b.dataset.zoom)));renderCandidate()});document.getElementById('oneBtn').onclick=()=>{zoom=1;renderCandidate()};document.getElementById('fitBtn').onclick=()=>{const c=selected(),a=M.page_assets[String(c.physical_page)],v=document.getElementById('viewport');zoom=Math.max(.2,(v.clientWidth-48)/a.image_dimensions_px.width);renderCandidate()};
document.getElementById('importBtn').onclick=()=>document.getElementById('importFile').click();document.getElementById('importFile').onchange=async e=>{try{const s=JSON.parse(await e.target.files[0].text());if(s.workbench_id!==M.workbench_id||s.manifest_sha256!==M.manifest_sha256||s.reviewer_id!==M.reviewer_id||s.review_session_id!==M.review_session_id||s.proposer_instance!==M.proposer_instance)throw Error('identity mismatch');const by=Object.fromEntries(M.items.map(i=>[i.segment_id,i]));if(!Array.isArray(s.items)||s.items.length!==M.items.length)throw Error('coverage mismatch');s.items.forEach(row=>{if(!by[row.segment_id]||row.input_sha256!==by[row.segment_id].input_sha256)throw Error(`item mismatch ${row.segment_id}`)});draft.items=Object.fromEntries(s.items.map(i=>[i.segment_id,i]));draft.declarations=s.declarations||{};localStorage.setItem(key,JSON.stringify(draft));render();alert('导入成功')}catch(err){alert(`导入失败：${err.message}`)}};
document.getElementById('exportBtn').onclick=()=>{saveForm();const rows=M.items.map(i=>draft.items[i.segment_id]||empty(i));const bad=[];rows.forEach(r=>{if(!r.decision||!r.rationale.trim()||!r.difference_observation.trim())bad.push(r.segment_id);if(r.decision==='accepted'&&(!r.selected_candidate_id||!checks.every(k=>r.confirmations[k]===true)))bad.push(r.segment_id)});if(!declarations.every(k=>draft.declarations[k]===true))bad.push('整批声明');if(bad.length){alert(`尚未完成：${[...new Set(bad)].join(', ')}`);return}const out={schema_version:'tkc.scan-structure-review-submission/v0.1',protocol:M.protocol,workbench_id:M.workbench_id,manifest_sha256:M.manifest_sha256,reviewer_id:M.reviewer_id,review_session_id:M.review_session_id,proposer_instance:M.proposer_instance,exported_at:new Date().toISOString().replace('.000Z','Z'),items:rows,declarations:draft.declarations};const blob=new Blob([JSON.stringify(out,null,2)+'\n'],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='scan-structure-review-submission.json';a.click();URL.revokeObjectURL(a.href)};render();
</script></body></html>'''
    return template.replace("__MANIFEST__", data)


def _instructions(manifest: Mapping[str, Any]) -> str:
    return f"""# 扫描 PDF 结构人工 Review 使用说明

本工具只审查 **章节标题结构**，不是 OCR Gold、语义 Review 或视觉对象 Review。
当前工作台 `{manifest['workbench_id']}` 绑定 reviewer `{manifest['reviewer_id']}`、
session `{manifest['review_session_id']}` 和 proposer `{manifest['proposer_instance']}`。

## 操作步骤

1. 双击打开 `review.html`。页面只包含哈希绑定的候选标题页 render，没有原 PDF。
2. 逐项查看红色候选框、标题文本、物理页段和 `structure_order`。
3. 若存在多个标题候选，先在“候选标题位置”选择正确的一项。
4. 结论选“接受”时，五项确认必须全部勾选；审查理由必须说明判断依据；
   “差异观察”没有差异时也要明确填写“未发现差异”。
5. 如果标题、页段、顺序或 BBox 有错误，选择“需重新处理”或“拒绝”并说明。
   当前核心契约只接受 Scan IR 中已有的 exact candidate BBox，因此这里不能手改
   BBox；错误必须回到 proposal/candidate 生成步骤，不能用人工坐标绕过绑定。
6. 完成四项整批声明后，点击“导出 submission.json”。浏览器会保存在本机，
   也可以导入同一工作台的旧 submission 继续修改。

## 术语说明

- **物理页**：派生 image-only PDF 内从 1 开始的页码；本次 1--15 对应原书
  物理页 308--322。
- **BBox [x0,y0,x1,y1]**：PDF 页面左上角坐标系中的标题边界框，单位是 PDF 点。
  `x0/y0` 是左上角，`x1/y1` 是右下角。鼠标在页面上移动时会显示当前位置。
- **structure_order**：本次知识范围内章节的顺序，从 0 开始。
- **evidence bindings**：标题关联的 source/render、OCR transcript、observation、
  coordinate transform、runtime/worker 等精确哈希。勾选表示实际检查了页面和
  候选，并确认没有把其他位置的文本误当成标题。
- **submission**：人工表单导出，不是 attestation、Gold 或发布批准。
- **external attestation**：在工作台之外由独立 reviewer/host 另行提供的绑定
  记录。`external-attestation-request.json` 只是请求模板，编译器不会代填或签发。

## 本次范围

工作台共 {len(manifest['items'])} 个结构项，页面 render 共
{len(manifest['page_assets'])} 张。提案覆盖派生页 1--13；派生页 14--15 的
References 仍保留在 Scan IR，但不进入本次 Reference Skill 的知识范围。

导出后，把 `scan-structure-review-submission.json` 返回给编译流程。缺少独立
external attestation 时，Finalize 必须保持 `paused` 且输出 0 个 external fragment。
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("generate", "validate-workbench"):
        command = subparsers.add_parser(name)
        command.add_argument("--source", type=Path, required=True)
        command.add_argument("--scan-ir", type=Path, required=True)
        command.add_argument("--proposal", type=Path, required=True)
        command.add_argument("--pdftoppm", type=Path, required=True)
        command.add_argument("--workbench" if name == "validate-workbench" else "--output", type=Path, required=True)
        if name == "generate":
            command.add_argument("--reviewer-id", required=True)
            command.add_argument("--review-session-id", required=True)
            command.add_argument("--proposer-instance", required=True)
            command.add_argument("--created-at", required=True)
    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--workbench", type=Path, required=True)
    finalize_parser.add_argument("--submission", type=Path, required=True)
    finalize_parser.add_argument("--attestation", type=Path)
    finalize_parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "generate":
            result = generate_workbench(
                source=args.source,
                ir_root=args.scan_ir,
                proposal_path=args.proposal,
                output=args.output,
                pdftoppm=args.pdftoppm,
                reviewer_id=args.reviewer_id,
                review_session_id=args.review_session_id,
                proposer_instance=args.proposer_instance,
                created_at=args.created_at,
            )
            summary = {
                "passed": True,
                "status": "paused-independent-structure-review",
                "workbench_id": result["workbench_id"],
                "item_count": len(result["items"]),
                "page_asset_count": len(result["page_assets"]),
                "output": str(args.output.expanduser().resolve()),
            }
        elif args.command == "validate-workbench":
            summary = validate_workbench(
                source=args.source,
                ir_root=args.scan_ir,
                proposal_path=args.proposal,
                workbench=args.workbench,
                pdftoppm=args.pdftoppm,
            )
        else:
            summary = finalize(
                workbench=args.workbench,
                submission_path=args.submission,
                attestation_path=args.attestation,
                output=args.output,
            )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    except (ScanStructureReviewError, StopIteration, KeyError, TypeError, ValueError) as error:
        print(json.dumps({"passed": False, "error": str(error)}, ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
