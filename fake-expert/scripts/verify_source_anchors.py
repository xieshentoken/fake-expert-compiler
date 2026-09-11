#!/usr/bin/env python3
"""Verify source-required evidence anchors against the matching external PDF."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import tempfile
import unicodedata
from pathlib import Path

from expert_skill_contract import load_json, load_jsonl, sha256_file
from paddle_runtime_contract import (
    PaddleRuntimeContractError,
    build_crop_coordinate_transform,
    build_crop_mapping,
    crop_render_png,
    run_external_worker_batch,
    sha256_json,
    validate_runtime_contract,
)
from scanned_pdf_ocr import (
    OCR_TRANSCRIPT_SCHEMA,
    ScanOcrError,
    _backend_role,
    _normalize_observations,
    _page_geometry_receipts,
    _png_dimensions,
    _render_pages,
    build_default_coordinate_transform,
    sha256_text,
    transform_polygon,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="Expert Skill directory.")
    parser.add_argument("--source", required=True, type=Path, help="External PDF to verify.")
    parser.add_argument("--pdftoppm", default="pdftoppm", help="Local pdftoppm renderer for scan anchors.")
    parser.add_argument(
        "--scan-runtime-contract",
        type=Path,
        help="Optional matching local OCR runtime contract for full scan-anchor replay.",
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _scan_replay_error(error: Exception) -> str:
    token = str(error).split(":", 1)[0]
    if re.fullmatch(r"[A-Za-z0-9_-]+", token):
        return token
    return "scan_replay_failed"


def _verify_scan_anchor_legacy(
    anchor: dict[str, object],
    *,
    source: Path,
    source_sha256: str,
    page_count: int,
    renderer: Path | str,
    runtime_contract_path: Path | None,
) -> dict[str, object]:
    """Verify scan geometry/review bindings, optionally replaying the OCR crop.

    A missing runtime is an intentional, useful verification level: source,
    render, geometry, review and hash integrity can be checked without
    pretending that an OCR transcript was re-executed.
    """

    result: dict[str, object] = {
        "id": anchor.get("id"),
        "passed": False,
        "verification_level": "scan-integrity",
        "full_replay": False,
        "replay_status": "missing-runtime",
    }
    locator = anchor.get("scan_locator")
    if not isinstance(locator, dict):
        result["error"] = "scan_locator_missing"
        return result
    required = (
        "source_sha256",
        "physical_page",
        "render_sha256",
        "render_dpi",
        "bbox_pdf_points",
        "coordinate_transform_id",
        "ocr_observation_ids",
        "transcript_span_ids",
        "transcript_sha256",
        "backend_id",
        "model_identity_sha256",
        "configuration_sha256",
        "review_attestation_sha256",
        "reviewer_instance",
        "review_session_id",
    )
    if any(key not in locator for key in required):
        result["error"] = "scan_locator_incomplete"
        return result
    if locator.get("source_sha256") != source_sha256:
        result["error"] = "scan_source_sha256_mismatch"
        return result
    page = locator.get("physical_page")
    if not isinstance(page, int) or page < 1 or page > page_count:
        result["error"] = "scan_page_out_of_range"
        return result
    if not isinstance(anchor.get("pages"), list) or page not in anchor["pages"]:
        result["error"] = "scan_page_anchor_binding_invalid"
        return result
    if not isinstance(locator.get("backend_id"), str) or not locator["backend_id"]:
        result["error"] = "scan_backend_binding_invalid"
        return result
    for key in (
        "source_sha256",
        "render_sha256",
        "transcript_sha256",
        "model_identity_sha256",
        "configuration_sha256",
        "review_attestation_sha256",
    ):
        if not _sha256(locator.get(key)):
            result["error"] = f"scan_{key}_invalid"
            return result
    if (
        not isinstance(locator.get("coordinate_transform_id"), str)
        or not re.fullmatch(r"pct-[0-9a-f]{20}", locator["coordinate_transform_id"])
        or not isinstance(locator.get("ocr_observation_ids"), list)
        or not locator["ocr_observation_ids"]
        or not isinstance(locator.get("transcript_span_ids"), list)
        or not locator["transcript_span_ids"]
        or any(not isinstance(value, str) or not value for value in locator["ocr_observation_ids"])
        or any(not isinstance(value, str) or not value for value in locator["transcript_span_ids"])
        or not isinstance(locator.get("reviewer_instance"), str)
        or not locator["reviewer_instance"].strip()
        or not isinstance(locator.get("review_session_id"), str)
        or not locator["review_session_id"].strip()
    ):
        result["error"] = "scan_locator_binding_invalid"
        return result
    if not isinstance(locator.get("render_dpi"), int) or locator["render_dpi"] < 36:
        result["error"] = "scan_render_dpi_invalid"
        return result
    bbox = locator.get("bbox_pdf_points")
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in bbox)
        or not (float(bbox[0]) < float(bbox[2]) and float(bbox[1]) < float(bbox[3]))
    ):
        result["error"] = "scan_bbox_invalid"
        return result
    if "bbox_sha256" in locator and (
        not _sha256(locator.get("bbox_sha256"))
        or locator.get("bbox_sha256") != sha256_json(bbox)
    ):
        result["error"] = "scan_bbox_binding_invalid"
        return result
    expected_excerpt = anchor.get("excerpt_sha256")
    if not _sha256(expected_excerpt):
        result["error"] = "scan_excerpt_hash_invalid"
        return result
    text_spans = anchor.get("text_spans")
    if not isinstance(text_spans, list) or not text_spans:
        result["error"] = "scan_transcript_spans_missing"
        return result
    for span in text_spans:
        if (
            not isinstance(span, dict)
            or not isinstance(span.get("page"), int)
            or span.get("page") not in anchor["pages"]
            or not isinstance(span.get("start"), int)
            or not isinstance(span.get("end"), int)
            or span["start"] < 0
            or span["end"] <= span["start"]
            or not _sha256(span.get("page_text_sha256"))
            or not _sha256(span.get("span_sha256"))
        ):
            result["error"] = "scan_transcript_span_integrity_invalid"
            return result
    try:
        geometry = _page_geometry_receipts(source, [page])[page]
        with _render_pages(
            source,
            [page],
            renderer,
            dpi=int(locator["render_dpi"]),
            pdf_dimensions={page: geometry},
        ) as (render_records, render_paths):
            render = next((row for row in render_records if row.get("physical_page") == page), None)
            render_path = render_paths.get(page)
            if not isinstance(render, dict) or render_path is None:
                raise ValueError("scan_render_receipt_missing")
            if render.get("source_sha256") != source_sha256:
                raise ValueError("scan_render_source_binding_invalid")
            if render.get("render_sha256") != locator.get("render_sha256"):
                raise ValueError("scan_render_sha256_mismatch")
            data = render_path.read_bytes()
            dimensions = {
                "width": int.from_bytes(data[16:20], "big"),
                "height": int.from_bytes(data[20:24], "big"),
            }
            mapping = build_crop_mapping(
                bbox_pdf_points=bbox,
                page_geometry=geometry,
                render_dimensions_px=dimensions,
                render_dpi=int(locator["render_dpi"]),
            )
            transform = build_crop_coordinate_transform(
                source_sha256=source_sha256,
                physical_page=page,
                render_sha256=str(render["render_sha256"]),
                render_dpi=int(locator["render_dpi"]),
                mapping=mapping,
            )
            transform_scope = locator.get("coordinate_transform_scope", "crop-local")
            if transform_scope not in {"crop-local", "page-render"}:
                raise ValueError("scan_coordinate_transform_scope_invalid")
            if transform_scope == "crop-local" and transform.get("id") != locator.get("coordinate_transform_id"):
                raise ValueError("scan_coordinate_transform_mismatch")
            crop = crop_render_png(data, mapping, expected_crop_sha256=None)
            if runtime_contract_path is None:
                result.update(
                    {
                        "passed": True,
                        "pages": [page],
                        "render_sha256": render["render_sha256"],
                        "coordinate_transform_id": locator["coordinate_transform_id"],
                        "replay_status": "missing-runtime",
                    }
                )
                return result
            contract_value = load_json(runtime_contract_path.expanduser().resolve())
            if not isinstance(contract_value, dict):
                raise PaddleRuntimeContractError("runtime_contract_invalid")
            validate_runtime_contract(contract_value)
            if contract_value.get("backend_id") != locator.get("backend_id"):
                raise PaddleRuntimeContractError("runtime_backend_mismatch")
            model = contract_value.get("model")
            if not isinstance(model, dict) or model.get("identity_sha256") != locator.get("model_identity_sha256"):
                raise PaddleRuntimeContractError("runtime_model_identity_mismatch")
            if contract_value.get("configuration_sha256") != locator.get("configuration_sha256"):
                raise PaddleRuntimeContractError("runtime_configuration_mismatch")
            workspace = Path(tempfile.mkdtemp(prefix="tkc-scan-anchor-replay-"))
            try:
                crop_path = workspace / "anchor.png"
                crop_path.write_bytes(crop["bytes"])
                crop_path.chmod(0o600)
                request = {
                    "request_id": f"scan-anchor-replay-{sha256_json([source_sha256, anchor.get('id'), crop['sha256']])[:20]}",
                    "backend_id": contract_value["backend_id"],
                    "input_kind": "raster-crop",
                    "image_path": str(crop_path),
                    "input_sha256": crop["sha256"],
                    "source_sha256": source_sha256,
                    "physical_page": page,
                    "render": {
                        "sha256": render["render_sha256"],
                        "dpi": int(locator["render_dpi"]),
                        "format": "png",
                        "image_dimensions_px": crop["dimensions_px"],
                    },
                    "coordinate_transform": transform,
                    "configuration": contract_value["configuration"],
                    "model": {
                        "model_id": f"external-{contract_value['backend_id']}",
                        "source": "external-local-preinstalled-runtime",
                        "identity_sha256": model["identity_sha256"],
                        "files_sha256": model.get("manifest_sha256"),
                        "manifest_sha256": model.get("manifest_sha256"),
                        "path": contract_value.get("model_root"),
                        "runtime_contract_sha256": contract_value["contract_sha256"],
                    },
                    "model_root": contract_value["model_root"],
                    "runtime_contract_sha256": contract_value["contract_sha256"],
                }
                batch = run_external_worker_batch(
                    contract_value,
                    [request],
                    timeout_seconds=120.0,
                    max_requests=1,
                    workspace_root=workspace,
                )
            finally:
                for path in sorted(workspace.rglob("*"), reverse=True):
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                    elif path.is_dir():
                        path.rmdir()
                workspace.rmdir()
            responses = [row for row in batch.get("responses", []) if isinstance(row, dict)]
            if len(responses) != 1:
                raise PaddleRuntimeContractError(
                    str((batch.get("errors") or [{"error_code": "scan_runtime_response_missing"}])[0].get("error_code"))
                )
            observations = responses[0].get("observations")
            if not isinstance(observations, list) or not observations:
                raise PaddleRuntimeContractError("scan_runtime_observations_missing")
            transcript = " ".join(
                normalize_text(str(row.get("text", "")))
                for row in observations
                if isinstance(row, dict) and normalize_text(str(row.get("text", "")))
            )
            actual_transcript_hash = _sha256_text(transcript)
            result.update(
                {
                    "pages": [page],
                    "render_sha256": render["render_sha256"],
                    "coordinate_transform_id": locator["coordinate_transform_id"],
                    "replay_coordinate_transform_id": transform["id"],
                    "expected_transcript_sha256": locator["transcript_sha256"],
                    "actual_transcript_sha256": actual_transcript_hash,
                    "verification_level": "scan-full-replay",
                    "full_replay": actual_transcript_hash == locator["transcript_sha256"],
                    "replay_status": "matched" if actual_transcript_hash == locator["transcript_sha256"] else "transcript-hash-mismatch",
                    "passed": actual_transcript_hash == locator["transcript_sha256"],
                }
            )
            if not result["passed"]:
                result["error"] = "scan_transcript_hash_mismatch"
            return result
    except (PaddleRuntimeContractError, OSError, ValueError, TypeError) as error:
        result["error"] = _scan_replay_error(error)
        if runtime_contract_path is not None:
            result["replay_status"] = "replay-failed"
        return result


def _scan_bbox_from_polygon(polygon: list[list[float]]) -> list[float]:
    return [
        round(min(point[0] for point in polygon), 4),
        round(min(point[1] for point in polygon), 4),
        round(max(point[0] for point in polygon), 4),
        round(max(point[1] for point in polygon), 4),
    ]


def _scan_valid_bbox(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(
            not isinstance(item, bool)
            and isinstance(item, (int, float))
            and math.isfinite(float(item))
            for item in value
        )
        and float(value[0]) < float(value[2])
        and float(value[1]) < float(value[3])
    )


def _scan_shift_polygon(polygon: object, left: int, top: int) -> list[list[float]]:
    if not isinstance(polygon, list) or len(polygon) < 3:
        raise ValueError("scan_replay_polygon_invalid")
    shifted: list[list[float]] = []
    for point in polygon:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError("scan_replay_polygon_invalid")
        shifted.append([round(float(point[0]) + left, 4), round(float(point[1]) + top, 4)])
    return shifted


def _scan_backend_binding(page_locator: dict[str, object]) -> dict[str, object]:
    return {
        "id": page_locator.get("backend_receipt_id"),
        "source_sha256": page_locator.get("source_sha256"),
        "physical_page": page_locator.get("physical_page"),
        "backend_id": page_locator.get("backend_id"),
        "model_identity_sha256": page_locator.get("model_identity_sha256"),
        "configuration_sha256": page_locator.get("configuration_sha256"),
        "render_sha256": page_locator.get("render_sha256"),
        "coordinate_transform_id": page_locator.get("coordinate_transform_id"),
        "runtime_contract_sha256": page_locator.get("runtime_contract_sha256"),
        "worker_sha256": page_locator.get("worker_sha256"),
        "qualification_receipt_sha256": page_locator.get("qualification_receipt_sha256"),
        "backend_receipt_sha256": page_locator.get("backend_receipt_sha256"),
    }


def _scan_observation_binding(observation: dict[str, object]) -> dict[str, object]:
    return {
        "id": observation.get("id"),
        "source_sha256": observation.get("source_sha256"),
        "physical_page": observation.get("physical_page"),
        "canonical_render_sha256": observation.get("canonical_render_sha256"),
        "backend": observation.get("backend"),
        "model": observation.get("model"),
        "configuration_sha256": observation.get("configuration_sha256"),
        "coordinate_transform_id": observation.get("coordinate_transform_id"),
        "bbox_image_px": observation.get("bbox_image_px"),
        "polygon_image_px": observation.get("polygon_image_px"),
        "bbox_pdf_points": observation.get("bbox_pdf_points"),
        "polygon_pdf_points": observation.get("polygon_pdf_points"),
        "confidence": observation.get("confidence"),
        "kind": observation.get("kind"),
        "transcript_span_id": observation.get("transcript_span_id"),
        "backend_receipt_id": observation.get("backend_receipt_id"),
    }


def _scan_row_binding(row: dict[str, object]) -> dict[str, object]:
    return {
        "id": row.get("id"),
        "page_transcript_id": row.get("page_transcript_id"),
        "source_sha256": row.get("source_sha256"),
        "physical_page": row.get("physical_page"),
        "backend_id": row.get("backend_id"),
        "span": row.get("span"),
        "raw_text_sha256": row.get("raw_text_sha256"),
        "normalized_text_sha256": row.get("normalized_text_sha256"),
        "observation_ids": row.get("observation_ids"),
    }


def _scan_verify_integrity_v2(
    anchor: dict[str, object],
    *,
    source: Path,
    source_sha256: str,
    page_count: int,
    renderer: Path | str,
) -> tuple[dict[str, object], dict[int, dict[str, object]], dict[int, Path], dict[int, dict[str, object]]]:
    """Verify the source-free v0.2 scan commitment before optional replay."""

    result: dict[str, object] = {
        "id": anchor.get("id"),
        "passed": False,
        "verification_level": "scan-integrity",
        "full_replay": False,
        "replay_status": "missing-runtime",
    }
    locator = anchor.get("scan_locator")
    if not isinstance(locator, dict) or locator.get("locator_protocol") != "scan-anchor-v0.2":
        result["error"] = "scan_locator_protocol_invalid"
        return result, {}, {}, {}
    required = (
        "source_sha256", "pages", "page_locators", "evidence_spans",
        "evidence_spans_sha256", "excerpt_sha256", "scan_excerpt_sha256",
        "evidence_commitment_sha256", "proposer_instance", "reviewer_instance",
        "review_session_id", "review_plan_sha256", "review_input_sha256",
        "candidate_sha256", "external_fragment_sha256", "review_attestation_sha256",
        "review_independence_basis",
    )
    if any(key not in locator for key in required):
        result["error"] = "scan_locator_incomplete"
        return result, {}, {}, {}
    if locator.get("source_sha256") != source_sha256:
        result["error"] = "scan_source_sha256_mismatch"
        return result, {}, {}, {}
    if (
        not isinstance(locator.get("pages"), list)
        or not locator["pages"]
        or locator["pages"] != sorted(set(locator["pages"]))
        or any(not isinstance(page, int) or page < 1 or page > page_count for page in locator["pages"])
        or anchor.get("pages") != locator["pages"]
    ):
        result["error"] = "scan_page_anchor_binding_invalid"
        return result, {}, {}, {}
    for key in (
        "source_sha256", "evidence_spans_sha256", "excerpt_sha256", "scan_excerpt_sha256",
        "evidence_commitment_sha256", "review_plan_sha256", "review_input_sha256",
        "candidate_sha256", "external_fragment_sha256", "review_attestation_sha256",
    ):
        if not _sha256(locator.get(key)):
            result["error"] = f"scan_{key}_invalid"
            return result, {}, {}, {}
    if (
        not isinstance(locator.get("proposer_instance"), str)
        or not locator["proposer_instance"].strip()
        or not isinstance(locator.get("reviewer_instance"), str)
        or not locator["reviewer_instance"].strip()
        or locator["proposer_instance"] == locator["reviewer_instance"]
        or locator.get("review_independence_basis") != "host-plan-proposer-reviewer-separation-v1"
        or not isinstance(locator.get("review_session_id"), str)
        or not locator["review_session_id"].strip()
    ):
        result["error"] = "scan_reviewer_binding_invalid"
        return result, {}, {}, {}
    if locator.get("excerpt_sha256") != anchor.get("excerpt_sha256"):
        result["error"] = "scan_excerpt_commitment_mismatch"
        return result, {}, {}, {}
    if locator.get("evidence_commitment_sha256") != sha256_json(
        {key: value for key, value in locator.items() if key != "evidence_commitment_sha256"}
    ):
        result["error"] = "scan_evidence_commitment_mismatch"
        return result, {}, {}, {}

    evidence_spans = locator.get("evidence_spans")
    text_spans = anchor.get("text_spans")
    if not isinstance(evidence_spans, list) or not evidence_spans or not isinstance(text_spans, list) or len(text_spans) != len(evidence_spans):
        result["error"] = "scan_evidence_spans_missing"
        return result, {}, {}, {}
    if locator.get("evidence_spans_sha256") != sha256_json(evidence_spans):
        result["error"] = "scan_evidence_spans_commitment_mismatch"
        return result, {}, {}, {}
    for evidence, text_span in zip(evidence_spans, text_spans):
        if not isinstance(evidence, dict) or not isinstance(text_span, dict):
            result["error"] = "scan_evidence_span_integrity_invalid"
            return result, {}, {}, {}
        for key in ("id", "page", "start", "end", "page_text_sha256", "span_sha256", "transcript_span_ids", "observation_ids", "bbox_pdf_points", "bbox_sha256"):
            if key not in evidence:
                result["error"] = "scan_evidence_span_incomplete"
                return result, {}, {}, {}
        if (
            any(not _sha256(evidence.get(key)) for key in ("page_text_sha256", "span_sha256", "bbox_sha256"))
            or evidence.get("bbox_sha256") != sha256_json(evidence.get("bbox_pdf_points"))
        ):
            result["error"] = "scan_evidence_span_hash_invalid"
            return result, {}, {}, {}
        if (
            text_span.get("page") != evidence.get("page")
            or text_span.get("start") != evidence.get("start")
            or text_span.get("end") != evidence.get("end")
            or text_span.get("page_text_sha256") != evidence.get("page_text_sha256")
            or text_span.get("span_sha256") != evidence.get("span_sha256")
            or text_span.get("scan_evidence_span_id") != evidence.get("id")
            or text_span.get("transcript_span_ids") != evidence.get("transcript_span_ids")
            or text_span.get("observation_ids") != evidence.get("observation_ids")
            or text_span.get("bbox_pdf_points") != evidence.get("bbox_pdf_points")
            or text_span.get("bbox_sha256") != evidence.get("bbox_sha256")
        ):
            result["error"] = "scan_anchor_text_span_binding_invalid"
            return result, {}, {}, {}
        if (
            not isinstance(evidence.get("page"), int)
            or evidence["page"] not in locator["pages"]
            or not isinstance(evidence.get("start"), int)
            or not isinstance(evidence.get("end"), int)
            or evidence["start"] < 0
            or evidence["end"] <= evidence["start"]
            or not isinstance(evidence.get("transcript_span_ids"), list)
            or not evidence["transcript_span_ids"]
            or not isinstance(evidence.get("observation_ids"), list)
            or not evidence["observation_ids"]
            or not _scan_valid_bbox(evidence.get("bbox_pdf_points"))
            or len(set(evidence["transcript_span_ids"])) != len(evidence["transcript_span_ids"])
            or len(set(evidence["observation_ids"])) != len(evidence["observation_ids"])
        ):
            result["error"] = "scan_evidence_span_binding_invalid"
            return result, {}, {}, {}

    page_locators = locator.get("page_locators")
    if not isinstance(page_locators, list) or {row.get("physical_page") for row in page_locators if isinstance(row, dict)} != set(locator["pages"]):
        result["error"] = "scan_page_locator_set_invalid"
        return result, {}, {}, {}
    page_by_number: dict[int, dict[str, object]] = {}
    for page_locator in page_locators:
        if not isinstance(page_locator, dict) or not isinstance(page_locator.get("physical_page"), int) or page_locator["physical_page"] in page_by_number:
            result["error"] = "scan_page_locator_invalid"
            return result, {}, {}, {}
        page_by_number[page_locator["physical_page"]] = page_locator
        required_page = (
            "source_sha256", "physical_page", "render_sha256", "render_dpi", "render_receipt", "render_receipt_sha256",
            "bbox_pdf_points", "bbox_sha256", "coordinate_transform_id", "coordinate_transform",
            "coordinate_transform_sha256", "coordinate_transform_scope", "ocr_observation_ids",
            "observation_commitments", "transcript_span_ids", "transcript_row_commitments",
            "transcript_sha256", "transcript_commitment_sha256", "evidence_span_ids", "backend_id",
            "model_identity_sha256", "configuration_sha256", "backend_receipt_id", "backend_receipt_sha256",
            "backend_receipt_binding_sha256", "runtime_contract_sha256", "worker_sha256",
            "qualification_receipt_sha256", "runtime_binding_kind",
        )
        if any(key not in page_locator for key in required_page):
            result["error"] = "scan_page_locator_incomplete"
            return result, {}, {}, {}
        if page_locator.get("source_sha256") != source_sha256:
            result["error"] = "scan_page_source_binding_invalid"
            return result, {}, {}, {}
        for key in ("render_sha256", "render_receipt_sha256", "bbox_sha256", "coordinate_transform_sha256", "transcript_sha256", "transcript_commitment_sha256", "model_identity_sha256", "configuration_sha256", "backend_receipt_sha256", "backend_receipt_binding_sha256", "runtime_contract_sha256", "worker_sha256"):
            if not _sha256(page_locator.get(key)):
                result["error"] = f"scan_page_{key}_invalid"
                return result, {}, {}, {}
        qualification = page_locator.get("qualification_receipt_sha256")
        if qualification is not None and not _sha256(qualification):
            result["error"] = "scan_page_qualification_hash_invalid"
            return result, {}, {}, {}
        if page_locator.get("runtime_binding_kind") not in {"external-local-runtime", "synthetic-test-only"}:
            result["error"] = "scan_page_runtime_binding_invalid"
            return result, {}, {}, {}
        if not _scan_valid_bbox(page_locator.get("bbox_pdf_points")) or page_locator.get("bbox_sha256") != sha256_json(page_locator["bbox_pdf_points"]):
            result["error"] = "scan_page_bbox_binding_invalid"
            return result, {}, {}, {}
        if page_locator.get("coordinate_transform_scope") != "page-render" or not isinstance(page_locator.get("coordinate_transform"), dict) or page_locator.get("coordinate_transform_id") != page_locator["coordinate_transform"].get("id"):
            result["error"] = "scan_page_transform_binding_invalid"
            return result, {}, {}, {}
        if page_locator.get("render_receipt_sha256") != sha256_json(page_locator["render_receipt"]):
            result["error"] = "scan_page_render_receipt_commitment_mismatch"
            return result, {}, {}, {}
        if page_locator.get("coordinate_transform_sha256") != sha256_json(page_locator["coordinate_transform"]):
            result["error"] = "scan_page_transform_commitment_mismatch"
            return result, {}, {}, {}
        if page_locator.get("backend_receipt_binding_sha256") != sha256_json(_scan_backend_binding(page_locator)):
            result["error"] = "scan_page_backend_receipt_binding_mismatch"
            return result, {}, {}, {}
        if not isinstance(page_locator.get("ocr_observation_ids"), list) or not isinstance(page_locator.get("observation_commitments"), list) or not page_locator["observation_commitments"] or not isinstance(page_locator.get("transcript_span_ids"), list) or not isinstance(page_locator.get("transcript_row_commitments"), list) or not page_locator["transcript_row_commitments"]:
            result["error"] = "scan_page_commitments_missing"
            return result, {}, {}, {}
        if set(page_locator["ocr_observation_ids"]) != {row.get("id") for row in page_locator["observation_commitments"]} or set(page_locator["transcript_span_ids"]) != {row.get("id") for row in page_locator["transcript_row_commitments"]}:
            result["error"] = "scan_page_commitment_ids_mismatch"
            return result, {}, {}, {}
        row_by_id = {str(row.get("id")): row for row in page_locator["transcript_row_commitments"] if isinstance(row, dict) and isinstance(row.get("id"), str)}
        observation_by_id = {str(row.get("id")): row for row in page_locator["observation_commitments"] if isinstance(row, dict) and isinstance(row.get("id"), str)}
        if len(row_by_id) != len(page_locator["transcript_row_commitments"]) or len(observation_by_id) != len(page_locator["observation_commitments"]):
            result["error"] = "scan_page_commitment_ids_duplicate"
            return result, {}, {}, {}
        for row in page_locator["transcript_row_commitments"]:
            if not isinstance(row, dict) or any(key not in row for key in ("id", "page_transcript_id", "source_sha256", "physical_page", "backend_id", "span", "raw_text_sha256", "normalized_text_sha256", "observation_ids", "row_binding_sha256", "row_sha256")):
                result["error"] = "scan_transcript_row_commitment_incomplete"
                return result, {}, {}, {}
            if row.get("row_binding_sha256") != sha256_json(_scan_row_binding(row)) or not _sha256(row.get("row_sha256")) or row.get("source_sha256") != source_sha256 or row.get("physical_page") != page_locator["physical_page"] or row.get("backend_id") != page_locator["backend_id"]:
                result["error"] = "scan_transcript_row_commitment_invalid"
                return result, {}, {}, {}
            span = row.get("span")
            if not isinstance(span, dict) or not isinstance(span.get("start"), int) or not isinstance(span.get("end"), int) or span.get("start") < 0 or span.get("end") <= span.get("start") or span.get("offset_unit") != "unicode-code-point" or not _sha256(row.get("raw_text_sha256")) or not _sha256(row.get("normalized_text_sha256")) or not isinstance(row.get("observation_ids"), list) or len(row["observation_ids"]) != 1:
                result["error"] = "scan_transcript_row_binding_invalid"
                return result, {}, {}, {}
            if row["observation_ids"][0] not in observation_by_id:
                result["error"] = "scan_transcript_observation_binding_invalid"
                return result, {}, {}, {}
        for observation in page_locator["observation_commitments"]:
            if not isinstance(observation, dict) or any(key not in observation for key in ("id", "source_sha256", "physical_page", "canonical_render_sha256", "backend", "model", "configuration_sha256", "coordinate_transform_id", "bbox_image_px", "polygon_image_px", "bbox_pdf_points", "polygon_pdf_points", "confidence", "kind", "transcript_span_id", "backend_receipt_id", "observation_binding_sha256", "observation_sha256")):
                result["error"] = "scan_observation_commitment_incomplete"
                return result, {}, {}, {}
            if observation.get("observation_binding_sha256") != sha256_json(_scan_observation_binding(observation)) or not _sha256(observation.get("observation_sha256")):
                result["error"] = "scan_observation_commitment_invalid"
                return result, {}, {}, {}
            if observation.get("source_sha256") != source_sha256 or observation.get("physical_page") != page_locator["physical_page"] or observation.get("canonical_render_sha256") != page_locator["render_sha256"] or observation.get("coordinate_transform_id") != page_locator["coordinate_transform_id"] or observation.get("backend_receipt_id") != page_locator["backend_receipt_id"] or observation.get("transcript_span_id") not in row_by_id:
                result["error"] = "scan_observation_binding_invalid"
                return result, {}, {}, {}
            if not isinstance(observation.get("backend"), dict) or observation["backend"].get("id") != page_locator["backend_id"] or not isinstance(observation.get("model"), dict) or observation["model"].get("identity_sha256") != page_locator["model_identity_sha256"] or observation.get("configuration_sha256") != page_locator["configuration_sha256"]:
                result["error"] = "scan_observation_runtime_binding_invalid"
                return result, {}, {}, {}
            polygon_image = observation.get("polygon_image_px")
            polygon_pdf = observation.get("polygon_pdf_points")
            if not isinstance(polygon_image, list) or not isinstance(polygon_pdf, list) or len(polygon_image) < 3 or len(polygon_pdf) != len(polygon_image) or not _scan_valid_bbox(observation.get("bbox_image_px")) or not _scan_valid_bbox(observation.get("bbox_pdf_points")):
                result["error"] = "scan_observation_geometry_invalid"
                return result, {}, {}, {}
            try:
                transformed = transform_polygon(polygon_image, page_locator["coordinate_transform"]["matrix_3x3"], target_dimensions=page_locator["coordinate_transform"]["pdf_dimensions_points"])
            except Exception:
                result["error"] = "scan_observation_transform_invalid"
                return result, {}, {}, {}
            if transformed != polygon_pdf or _scan_bbox_from_polygon(polygon_image) != observation.get("bbox_image_px") or _scan_bbox_from_polygon(polygon_pdf) != observation.get("bbox_pdf_points"):
                result["error"] = "scan_observation_geometry_binding_invalid"
                return result, {}, {}, {}
        if page_locator.get("transcript_commitment_sha256") != sha256_json({"transcript_sha256": page_locator["transcript_sha256"], "transcript_row_commitments": page_locator["transcript_row_commitments"]}):
            result["error"] = "scan_transcript_commitment_mismatch"
            return result, {}, {}, {}
        expected_bbox = [
            round(min(float(row["bbox_pdf_points"][0]) for row in page_locator["observation_commitments"]), 4),
            round(min(float(row["bbox_pdf_points"][1]) for row in page_locator["observation_commitments"]), 4),
            round(max(float(row["bbox_pdf_points"][2]) for row in page_locator["observation_commitments"]), 4),
            round(max(float(row["bbox_pdf_points"][3]) for row in page_locator["observation_commitments"]), 4),
        ]
        if expected_bbox != page_locator["bbox_pdf_points"]:
            result["error"] = "scan_page_bbox_observation_union_invalid"
            return result, {}, {}, {}
        evidence_for_page = [row for row in evidence_spans if isinstance(row, dict) and row.get("page") == page_locator["physical_page"]]
        expected_evidence_ids = {str(row.get("id")) for row in evidence_for_page}
        if set(page_locator["evidence_span_ids"]) != expected_evidence_ids:
            result["error"] = "scan_page_evidence_span_binding_invalid"
            return result, {}, {}, {}
        for evidence in evidence_for_page:
            row_ids = [str(value) for value in evidence["transcript_span_ids"]]
            obs_ids = [str(value) for value in evidence["observation_ids"]]
            if any(row_id not in row_by_id for row_id in row_ids) or any(obs_id not in observation_by_id for obs_id in obs_ids):
                result["error"] = "scan_evidence_commitment_reference_invalid"
                return result, {}, {}, {}
            ordered_rows = sorted((row_by_id[row_id] for row_id in row_ids), key=lambda row: int(row["span"]["start"]))
            if int(ordered_rows[0]["span"]["start"]) != int(evidence["start"]) or int(ordered_rows[-1]["span"]["end"]) != int(evidence["end"]) or any(int(right["span"]["start"]) != int(left["span"]["end"]) + 1 for left, right in zip(ordered_rows, ordered_rows[1:])):
                result["error"] = "scan_evidence_transcript_interval_invalid"
                return result, {}, {}, {}
            if set(obs_ids) != {str(row["observation_ids"][0]) for row in ordered_rows}:
                result["error"] = "scan_evidence_observation_interval_invalid"
                return result, {}, {}, {}
            bbox_values = [observation_by_id[obs_id]["bbox_pdf_points"] for obs_id in obs_ids]
            union = [
                round(min(float(row[0]) for row in bbox_values), 4),
                round(min(float(row[1]) for row in bbox_values), 4),
                round(max(float(row[2]) for row in bbox_values), 4),
                round(max(float(row[3]) for row in bbox_values), 4),
            ]
            if union != evidence["bbox_pdf_points"]:
                result["error"] = "scan_evidence_bbox_binding_invalid"
                return result, {}, {}, {}

    geometry = _page_geometry_receipts(source, locator["pages"])
    dpis = {page_by_number[page]["render_dpi"] for page in locator["pages"]}
    if len(dpis) != 1 or any(not isinstance(dpi, int) or dpi < 36 for dpi in dpis):
        result["error"] = "scan_render_dpi_invalid"
        return result, {}, {}, {}
    render_records: dict[int, dict[str, object]] = {}
    render_paths: dict[int, Path] = {}
    with _render_pages(source, locator["pages"], renderer, dpi=next(iter(dpis)), pdf_dimensions=geometry) as (records, paths):
        render_records = {int(row["physical_page"]): row for row in records if isinstance(row, dict) and isinstance(row.get("physical_page"), int)}
        render_paths = {int(page): path for page, path in paths.items()}
        for page in locator["pages"]:
            page_locator = page_by_number[page]
            record = render_records.get(page)
            render_path = render_paths.get(page)
            if not isinstance(record, dict) or render_path is None or page_locator.get("render_receipt") != record or page_locator.get("render_sha256") != record.get("render_sha256") or record.get("source_sha256") != source_sha256:
                result["error"] = "scan_render_receipt_mismatch"
                return result, {}, {}, {}
            data = render_path.read_bytes()
            dimensions = _png_dimensions(data)
            transform = page_locator["coordinate_transform"]
            if dimensions is None or transform.get("image_dimensions_px") != dimensions:
                result["error"] = "scan_render_dimensions_mismatch"
                return result, {}, {}, {}
            try:
                expected_transform = build_default_coordinate_transform(
                    source_sha256=source_sha256,
                    physical_page=page,
                    render_sha256=str(record["render_sha256"]),
                    render_dpi=int(page_locator["render_dpi"]),
                    image_dimensions_px=dimensions,
                    pdf_dimensions_points={"width": geometry[page]["width"], "height": geometry[page]["height"]},
                    preprocessing=transform.get("preprocessing") if isinstance(transform.get("preprocessing"), dict) else None,
                )
            except Exception:
                result["error"] = "scan_coordinate_transform_recompute_failed"
                return result, {}, {}, {}
            if expected_transform != transform:
                result["error"] = "scan_coordinate_transform_mismatch"
                return result, {}, {}, {}
    result.update(
        {
            "passed": True,
            "pages": list(locator["pages"]),
            "render_sha256": {str(page): page_by_number[page]["render_sha256"] for page in locator["pages"]},
            "coordinate_transform_id": {str(page): page_by_number[page]["coordinate_transform_id"] for page in locator["pages"]},
            "evidence_commitment_sha256": locator["evidence_commitment_sha256"],
            "replay_status": "missing-runtime",
        }
    )
    # _render_pages owns its temporary files; return only its records/paths
    # while the context remains active in the caller's replay path.
    return result, page_by_number, render_paths, render_records


def _verify_scan_anchor_v2(
    anchor: dict[str, object],
    *,
    source: Path,
    source_sha256: str,
    page_count: int,
    renderer: Path | str,
    runtime_contract_path: Path | None,
) -> dict[str, object]:
    """Verify exact v0.2 commitments and, when authorized, replay each body crop."""

    # Integrity has to be checked in the same render context that is used for
    # replay, so render again below after the source-free commitment checks.
    try:
        result, page_by_number, _unused_paths, _unused_records = _scan_verify_integrity_v2(
            anchor,
            source=source,
            source_sha256=source_sha256,
            page_count=page_count,
            renderer=renderer,
        )
    except (ScanOcrError, OSError, ValueError, TypeError, KeyError) as error:
        return {
            "id": anchor.get("id"),
            "passed": False,
            "verification_level": "scan-integrity",
            "full_replay": False,
            "replay_status": "integrity-failed",
            "error": _scan_replay_error(error),
        }
    if not result.get("passed") or runtime_contract_path is None:
        return result
    locator = anchor["scan_locator"]
    try:
        contract = load_json(runtime_contract_path.expanduser().resolve())
        if not isinstance(contract, dict):
            raise PaddleRuntimeContractError("runtime_contract_invalid")
        validate_runtime_contract(contract)
        pages = [int(page) for page in locator["pages"]]
        reference_runtime = {
            "contract": locator["page_locators"][0]["runtime_contract_sha256"],
            "worker": locator["page_locators"][0]["worker_sha256"],
            "qualification": locator["page_locators"][0]["qualification_receipt_sha256"],
            "backend": locator["page_locators"][0]["backend_id"],
            "model": locator["page_locators"][0]["model_identity_sha256"],
            "configuration": locator["page_locators"][0]["configuration_sha256"],
        }
        if any(
            page_locator.get("runtime_contract_sha256") != reference_runtime["contract"]
            or page_locator.get("worker_sha256") != reference_runtime["worker"]
            or page_locator.get("qualification_receipt_sha256") != reference_runtime["qualification"]
            or page_locator.get("backend_id") != reference_runtime["backend"]
            or page_locator.get("model_identity_sha256") != reference_runtime["model"]
            or page_locator.get("configuration_sha256") != reference_runtime["configuration"]
            for page_locator in locator["page_locators"]
        ):
            raise PaddleRuntimeContractError("scan_runtime_binding_drift")
        if contract.get("contract_sha256") != reference_runtime["contract"] or contract.get("worker_sha256") != reference_runtime["worker"] or contract.get("backend_id") != reference_runtime["backend"]:
            raise PaddleRuntimeContractError("scan_runtime_contract_binding_mismatch")
        if reference_runtime["qualification"] is not None and contract.get("qualification_receipt_sha256") != reference_runtime["qualification"]:
            raise PaddleRuntimeContractError("scan_runtime_qualification_binding_mismatch")
        model_contract = contract.get("model")
        configuration = contract.get("configuration")
        if not isinstance(model_contract, dict) or not isinstance(configuration, dict) or model_contract.get("identity_sha256") != reference_runtime["model"] or contract.get("configuration_sha256") != reference_runtime["configuration"]:
            raise PaddleRuntimeContractError("scan_runtime_model_or_configuration_mismatch")
        backend = str(reference_runtime["backend"])
        model = {
            "model_id": f"external-{backend}",
            "source": "external-local-preinstalled-runtime",
            "identity_sha256": model_contract.get("identity_sha256"),
            "files_sha256": model_contract.get("manifest_sha256"),
            "manifest_sha256": model_contract.get("manifest_sha256"),
        }
        with tempfile.TemporaryDirectory(prefix="tkc-scan-anchor-replay-") as temporary:
            workspace = Path(temporary)
            geometries = _page_geometry_receipts(source, pages)
            with _render_pages(source, pages, renderer, dpi=int(locator["page_locators"][0]["render_dpi"]), pdf_dimensions=geometries, workspace_root=workspace / "renders") as (records, paths):
                records_by_page = {int(row["physical_page"]): row for row in records}
                requests: list[dict[str, object]] = []
                contexts: dict[str, dict[str, object]] = {}
                actual_text_by_expected_row: dict[str, str] = {}
                expected_row_ids: list[str] = []
                for page_locator in locator["page_locators"]:
                    for expected_row in page_locator["transcript_row_commitments"]:
                        expected_row_id = str(expected_row["id"])
                        if expected_row_id in expected_row_ids:
                            raise PaddleRuntimeContractError("scan_replay_transcript_row_id_duplicate")
                        expected_row_ids.append(expected_row_id)
                for page in pages:
                    page_locator = next(row for row in locator["page_locators"] if row.get("physical_page") == page)
                    render = records_by_page.get(page)
                    render_path = paths.get(page)
                    if not isinstance(render, dict) or render_path is None or render.get("render_sha256") != page_locator.get("render_sha256"):
                        raise PaddleRuntimeContractError("scan_replay_render_binding_mismatch")
                    data = render_path.read_bytes()
                    dimensions = _png_dimensions(data)
                    if dimensions is None:
                        raise PaddleRuntimeContractError("scan_replay_render_invalid")
                    mapping = build_crop_mapping(
                        bbox_pdf_points=page_locator["bbox_pdf_points"],
                        page_geometry=geometries[page],
                        render_dimensions_px=dimensions,
                        render_dpi=int(page_locator["render_dpi"]),
                    )
                    crop = crop_render_png(data, mapping, expected_crop_sha256=None)
                    crop_path = workspace / f"page-{page:04d}.png"
                    crop_path.write_bytes(crop["bytes"])
                    crop_path.chmod(0o600)
                    crop_transform = build_crop_coordinate_transform(
                        source_sha256=source_sha256,
                        physical_page=page,
                        render_sha256=str(render["render_sha256"]),
                        render_dpi=int(page_locator["render_dpi"]),
                        mapping=mapping,
                    )
                    request_id = f"scan-anchor-replay-{sha256_json([source_sha256, anchor.get('id'), page, crop['sha256']])[:20]}"
                    request = {
                        "request_id": request_id,
                        "backend_id": backend,
                        "input_kind": "raster-crop",
                        "image_path": str(crop_path),
                        "input_sha256": crop["sha256"],
                        "source_sha256": source_sha256,
                        "physical_page": page,
                        "render": {"sha256": render["render_sha256"], "dpi": int(page_locator["render_dpi"]), "format": "png", "image_dimensions_px": crop["dimensions_px"]},
                        "coordinate_transform": crop_transform,
                        "configuration": configuration,
                        "model": model,
                        "model_root": contract["model_root"],
                        "runtime_contract_sha256": contract["contract_sha256"],
                    }
                    requests.append(request)
                    contexts[request_id] = {"page": page, "render": render, "crop": crop, "crop_transform": crop_transform, "mapping": mapping, "page_locator": page_locator}
                batch = run_external_worker_batch(contract, requests, timeout_seconds=120.0, max_requests=len(requests), workspace_root=workspace)
                responses = {str(row.get("request_id")): row for row in batch.get("responses", []) if isinstance(row, dict)}
                if batch.get("errors") or set(responses) != set(contexts):
                    raise PaddleRuntimeContractError("scan_replay_response_set_invalid")
                for request_id, context in contexts.items():
                    response = responses.get(request_id)
                    if not isinstance(response, dict) or response.get("runtime_contract_sha256") != contract["contract_sha256"]:
                        raise PaddleRuntimeContractError("scan_replay_runtime_contract_response_mismatch")
                    worker_receipt = response.get("worker_receipt")
                    if not isinstance(worker_receipt, dict) or worker_receipt.get("worker_sha256") != reference_runtime["worker"] or worker_receipt.get("runtime_contract_sha256") != contract["contract_sha256"]:
                        raise PaddleRuntimeContractError("scan_replay_worker_response_mismatch")
                    crop = context["crop"]
                    if response.get("input_sha256") not in {None, crop["sha256"]}:
                        raise PaddleRuntimeContractError("scan_replay_input_hash_mismatch")
                    page = int(context["page"])
                    page_locator = context["page_locator"]
                    page_transform = page_locator["coordinate_transform"]
                    actual_rows, actual_transcripts = _normalize_observations(
                        source_sha256=source_sha256,
                        physical_page=page,
                        backend=backend,
                        role=_backend_role(backend),
                        response=response,
                        render_receipt=context["render"],
                        transform=context["crop_transform"],
                        model=model,
                        configuration=configuration,
                        image_dimensions=crop["dimensions_px"],
                    )
                    expected_rows = sorted(page_locator["transcript_row_commitments"], key=lambda row: int(row["span"]["start"]))
                    if len(actual_rows) != len(expected_rows) or len(actual_transcripts) != len(expected_rows):
                        raise PaddleRuntimeContractError("scan_replay_transcript_row_count_mismatch")
                    page_text_by_expected_row: dict[str, str] = {}
                    expected_observation_ids: set[str] = set()
                    for index, (actual_row, actual_transcript, expected_row) in enumerate(zip(actual_rows, actual_transcripts, expected_rows)):
                        if actual_transcript.get("raw_text_sha256") != expected_row.get("raw_text_sha256") or actual_transcript.get("normalized_text_sha256") != expected_row.get("normalized_text_sha256") or actual_transcript.get("physical_page") != page or actual_transcript.get("backend_id") != backend:
                            raise PaddleRuntimeContractError("scan_replay_transcript_row_content_mismatch")
                        expected_row_id = str(expected_row["id"])
                        normalized_text = str(actual_transcript["normalized_text"])
                        if expected_row_id in actual_text_by_expected_row:
                            if actual_text_by_expected_row[expected_row_id] != normalized_text:
                                raise PaddleRuntimeContractError("scan_replay_transcript_row_overwrite")
                            raise PaddleRuntimeContractError("scan_replay_transcript_row_id_duplicate")
                        actual_text_by_expected_row[expected_row_id] = normalized_text
                        page_text_by_expected_row[expected_row_id] = normalized_text
                        if actual_transcript.get("raw_text") is None:
                            raise PaddleRuntimeContractError("scan_replay_raw_transcript_missing")
                        reconstructed_transcript = {
                            "schema_version": OCR_TRANSCRIPT_SCHEMA,
                            "id": expected_row_id,
                            "page_transcript_id": expected_row["page_transcript_id"],
                            "source_sha256": source_sha256,
                            "physical_page": page,
                            "backend_id": backend,
                            "span": expected_row["span"],
                            "raw_text": actual_transcript["raw_text"],
                            "normalized_text": actual_transcript["normalized_text"],
                            "raw_text_sha256": actual_transcript["raw_text_sha256"],
                            "normalized_text_sha256": actual_transcript["normalized_text_sha256"],
                            "candidate_only": True,
                        }
                        reconstructed_row_sha256 = sha256_json(reconstructed_transcript)
                        if reconstructed_row_sha256 != expected_row["row_sha256"]:
                            raise PaddleRuntimeContractError("scan_replay_transcript_row_commitment_mismatch")
                        expected_observation_ids.update(str(value) for value in expected_row["observation_ids"])
                    expected_observations = sorted(page_locator["observation_commitments"], key=lambda row: str(row["id"]))
                    actual_by_id = {str(row.get("id")): row for row in actual_rows}
                    if set(actual_by_id) != expected_observation_ids or len(actual_by_id) != len(actual_rows):
                        raise PaddleRuntimeContractError("scan_replay_observation_id_set_mismatch")
                    left, top, _right, _bottom = context["mapping"]["render_pixel_bbox"]
                    for expected_observation in expected_observations:
                        observation_id = str(expected_observation["id"])
                        actual = actual_by_id.get(observation_id)
                        if not isinstance(actual, dict):
                            raise PaddleRuntimeContractError("scan_replay_observation_missing")
                        if actual.get("bbox_pdf_points") != expected_observation.get("bbox_pdf_points") or actual.get("polygon_pdf_points") != expected_observation.get("polygon_pdf_points") or actual.get("confidence") != expected_observation.get("confidence") or actual.get("kind") != expected_observation.get("kind"):
                            raise PaddleRuntimeContractError("scan_replay_observation_geometry_mismatch")
                        expected_row_id = str(expected_observation["transcript_span_id"])
                        if expected_row_id not in page_text_by_expected_row or actual.get("normalized_transcript") != page_text_by_expected_row[expected_row_id]:
                            raise PaddleRuntimeContractError("scan_replay_observation_transcript_binding_mismatch")
                        full_polygon = _scan_shift_polygon(actual["polygon_image_px"], int(left), int(top))
                        full_bbox = _scan_bbox_from_polygon(full_polygon)
                        if full_bbox != expected_observation["bbox_image_px"] or _scan_bbox_from_polygon(full_polygon) != expected_observation["bbox_image_px"]:
                            raise PaddleRuntimeContractError("scan_replay_observation_render_geometry_mismatch")
                        canonical = dict(actual)
                        canonical["coordinate_transform_id"] = page_transform["id"]
                        canonical["bbox_image_px"] = full_bbox
                        canonical["polygon_image_px"] = full_polygon
                        canonical["transcript_span_id"] = expected_row_id
                        canonical["backend_receipt_id"] = page_locator["backend_receipt_id"]
                        raw_observation = dict(actual.get("raw_observation") or {})
                        raw_observation["bbox"] = full_bbox
                        raw_observation["polygon"] = full_polygon
                        canonical["raw_observation"] = raw_observation
                        canonical["model"] = expected_observation["model"]
                        canonical["backend"] = expected_observation["backend"]
                        canonical["render"] = {
                            "renderer_id": context["render"]["renderer"]["id"],
                            "renderer_version": context["render"]["renderer"]["version"],
                            "dpi": context["render"]["renderer"]["dpi"],
                            "format": context["render"]["renderer"]["format"],
                        }
                        if sha256_json(canonical) != expected_observation["observation_sha256"]:
                            raise PaddleRuntimeContractError("scan_replay_observation_commitment_mismatch")
                    for evidence in locator["evidence_spans"]:
                        if evidence.get("page") != page:
                            continue
                        text = " ".join(page_text_by_expected_row[str(row_id)] for row_id in evidence["transcript_span_ids"])
                        if sha256_text(text) != evidence["span_sha256"]:
                            raise PaddleRuntimeContractError("scan_replay_evidence_span_hash_mismatch")
                    page_text = " ".join(page_text_by_expected_row[str(row["id"])] for row in expected_rows)
                    if sha256_text(page_text) != page_locator["transcript_sha256"]:
                        raise PaddleRuntimeContractError("scan_replay_transcript_hash_mismatch")
                expected_row_id_set = set(expected_row_ids)
                actual_row_id_set = set(actual_text_by_expected_row)
                if actual_row_id_set != expected_row_id_set:
                    if expected_row_id_set - actual_row_id_set:
                        raise PaddleRuntimeContractError("scan_replay_transcript_row_missing")
                    raise PaddleRuntimeContractError("scan_replay_transcript_row_unexpected")
                replay_evidence_row_ids: set[str] = set()
                replay_excerpt_parts: list[str] = []
                for evidence in locator["evidence_spans"]:
                    if not isinstance(evidence, dict) or not isinstance(evidence.get("transcript_span_ids"), list):
                        raise PaddleRuntimeContractError("scan_replay_evidence_span_invalid")
                    row_ids = [str(row_id) for row_id in evidence["transcript_span_ids"]]
                    if any(row_id not in actual_text_by_expected_row for row_id in row_ids):
                        raise PaddleRuntimeContractError("scan_replay_transcript_row_missing")
                    if any(row_id in replay_evidence_row_ids for row_id in row_ids):
                        raise PaddleRuntimeContractError("scan_replay_transcript_row_id_duplicate")
                    replay_evidence_row_ids.update(row_ids)
                    replay_excerpt_parts.append(
                        " ".join(actual_text_by_expected_row[row_id] for row_id in row_ids)
                    )
                replay_excerpt = "\f".join(
                    replay_excerpt_parts
                )
                if sha256_text(replay_excerpt) != locator["scan_excerpt_sha256"]:
                    raise PaddleRuntimeContractError("scan_replay_excerpt_commitment_mismatch")
        result.update(
            {
                "verification_level": "scan-full-replay",
                "full_replay": True,
                "replay_status": "matched",
                "passed": True,
            }
        )
        return result
    except (ScanOcrError, PaddleRuntimeContractError, OSError, ValueError, TypeError, KeyError) as error:
        result.update({"verification_level": "scan-full-replay", "full_replay": False, "replay_status": "replay-failed", "passed": False, "error": _scan_replay_error(error)})
        return result


def _verify_scan_anchor(
    anchor: dict[str, object],
    *,
    source: Path,
    source_sha256: str,
    page_count: int,
    renderer: Path | str,
    runtime_contract_path: Path | None,
) -> dict[str, object]:
    locator = anchor.get("scan_locator")
    if isinstance(locator, dict) and locator.get("locator_protocol") == "scan-anchor-v0.2":
        return _verify_scan_anchor_v2(
            anchor,
            source=source,
            source_sha256=source_sha256,
            page_count=page_count,
            renderer=renderer,
            runtime_contract_path=runtime_contract_path,
        )
    return _verify_scan_anchor_legacy(
        anchor,
        source=source,
        source_sha256=source_sha256,
        page_count=page_count,
        renderer=renderer,
        runtime_contract_path=runtime_contract_path,
    )


def main() -> int:
    args = parse_args()
    package = args.package.expanduser().resolve()
    source = args.source.expanduser().resolve()
    try:
        from pypdf import PdfReader, __version__ as pypdf_version
    except ModuleNotFoundError:
        print(
            "error: pypdf is required; run this script with the bundled workspace "
            "Python or install pypdf",
            file=sys.stderr,
        )
        return 2

    if not source.is_file():
        print(f"error: source does not exist: {source}", file=sys.stderr)
        return 2
    manifest_path = package / "manifest.json"
    anchors_path = package / "references/evidence/anchors.jsonl"
    if not manifest_path.is_file() or not anchors_path.is_file():
        print("error: package manifest or evidence anchors are missing", file=sys.stderr)
        return 2

    manifest = load_json(manifest_path)
    declared_sources = {
        row["sha256"]: row for row in manifest.get("sources", [])
        if isinstance(row, dict) and isinstance(row.get("sha256"), str)
    }
    actual_source_hash = sha256_file(source)
    source_record = declared_sources.get(actual_source_hash)
    results: list[dict[str, object]] = []
    if source_record is None:
        payload = {
            "passed": False,
            "source_sha256": actual_source_hash,
            "error": "source_hash_not_declared",
            "anchors": results,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    reader = PdfReader(str(source))
    anchor_rows = load_jsonl(anchors_path)
    for anchor in anchor_rows:
        anchor_id = anchor.get("id")
        result: dict[str, object] = {"id": anchor_id, "passed": False}
        if anchor.get("source_id") != source_record.get("source_id"):
            result["error"] = "source_id_mismatch"
            results.append(result)
            continue
        pages = anchor.get("pages")
        locator = anchor.get("locator")
        if not isinstance(pages, list) or not pages or not isinstance(locator, dict):
            result["error"] = "invalid_anchor"
            results.append(result)
            continue
        if any(not isinstance(page, int) or page < 1 or page > len(reader.pages) for page in pages):
            result["error"] = "page_out_of_range"
            results.append(result)
            continue
        if isinstance(anchor.get("scan_locator"), dict):
            result = _verify_scan_anchor(
                anchor,
                source=source,
                source_sha256=actual_source_hash,
                page_count=len(reader.pages),
                renderer=args.pdftoppm,
                runtime_contract_path=args.scan_runtime_contract,
            )
            results.append(result)
            continue
        hash_scope = locator.get("hash_scope")
        extractor = locator.get("extractor")
        if not isinstance(hash_scope, str) or not (
            hash_scope.startswith("normalized full native text")
            or hash_scope == "normalized native text spans"
        ):
            result["error"] = "unsupported_hash_scope"
            results.append(result)
            continue
        if not isinstance(extractor, str) or not extractor.startswith("pypdf-"):
            result["error"] = "unsupported_extractor"
            results.append(result)
            continue
        normalized_pages = {
            page: normalize_text(reader.pages[page - 1].extract_text() or "")
            for page in pages
        }
        if hash_scope == "normalized native text spans":
            spans = anchor.get("text_spans")
            if not isinstance(spans, list) or not spans:
                result["error"] = "invalid_anchor_spans"
                results.append(result)
                continue
            span_texts: list[str] = []
            span_error: str | None = None
            for span in spans:
                if not isinstance(span, dict):
                    span_error = "invalid_anchor_spans"
                    break
                page = span.get("page")
                start = span.get("start")
                end = span.get("end")
                if page not in normalized_pages or not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(normalized_pages[page]):
                    span_error = "span_out_of_range"
                    break
                page_text = normalized_pages[page]
                if span.get("page_text_sha256") != hashlib.sha256(page_text.encode("utf-8")).hexdigest():
                    span_error = "page_text_hash_mismatch"
                    break
                span_text = page_text[start:end]
                if span.get("span_sha256") != hashlib.sha256(span_text.encode("utf-8")).hexdigest():
                    span_error = "span_hash_mismatch"
                    break
                span_texts.append(span_text)
            if span_error:
                result["error"] = span_error
                results.append(result)
                continue
            actual_anchor_hash = hashlib.sha256("\f".join(span_texts).encode("utf-8")).hexdigest()
        else:
            page_texts = [normalized_pages[page] for page in pages]
            actual_anchor_hash = hashlib.sha256("\f".join(page_texts).encode("utf-8")).hexdigest()
        expected_anchor_hash = anchor.get("excerpt_sha256")
        result.update(
            {
                "pages": pages,
                "expected_sha256": expected_anchor_hash,
                "actual_sha256": actual_anchor_hash,
                "passed": actual_anchor_hash == expected_anchor_hash,
            }
        )
        if not result["passed"]:
            result["error"] = "anchor_hash_mismatch"
        results.append(result)

    gap_rows: list[object] = []
    gaps_path = package / "references/knowledge-gaps.jsonl"
    if gaps_path.is_file():
        gap_rows = load_jsonl(gaps_path)
    for gap in gap_rows:
        result = {
            "id": gap.get("id") if isinstance(gap, dict) else None,
            "kind": "knowledge-gap",
            "passed": False,
        }
        if not isinstance(gap, dict) or gap.get("source_id") != source_record.get("source_id"):
            result["error"] = "source_id_mismatch"
            results.append(result)
            continue
        spans = gap.get("evidence_spans")
        if not isinstance(spans, list) or not spans:
            result["error"] = "invalid_gap_spans"
            results.append(result)
            continue
        pages = sorted(
            {
                span.get("page")
                for span in spans
                if isinstance(span, dict) and isinstance(span.get("page"), int)
            }
        )
        if not pages or any(page < 1 or page > len(reader.pages) for page in pages):
            result["error"] = "page_out_of_range"
            results.append(result)
            continue
        normalized_pages = {
            page: normalize_text(reader.pages[page - 1].extract_text() or "")
            for page in pages
        }
        span_texts: list[str] = []
        span_error: str | None = None
        for span in spans:
            if not isinstance(span, dict):
                span_error = "invalid_gap_spans"
                break
            page = span.get("page")
            start = span.get("start")
            end = span.get("end")
            if (
                page not in normalized_pages
                or not isinstance(start, int)
                or not isinstance(end, int)
                or not 0 <= start < end <= len(normalized_pages[page])
            ):
                span_error = "span_out_of_range"
                break
            page_text = normalized_pages[page]
            if span.get("page_text_sha256") != hashlib.sha256(page_text.encode("utf-8")).hexdigest():
                span_error = "page_text_hash_mismatch"
                break
            span_text = page_text[start:end]
            if span.get("span_sha256") != hashlib.sha256(span_text.encode("utf-8")).hexdigest():
                span_error = "span_hash_mismatch"
                break
            span_texts.append(span_text)
        if span_error:
            result["error"] = span_error
            results.append(result)
            continue
        actual_gap_hash = hashlib.sha256("\f".join(span_texts).encode("utf-8")).hexdigest()
        expected_gap_hash = gap.get("excerpt_sha256")
        result.update(
            {
                "pages": pages,
                "expected_sha256": expected_gap_hash,
                "actual_sha256": actual_gap_hash,
                "passed": actual_gap_hash == expected_gap_hash,
            }
        )
        if not result["passed"]:
            result["error"] = "gap_hash_mismatch"
        results.append(result)

    passed = bool(results) and all(result["passed"] for result in results)
    payload = {
        "passed": passed,
        "source_id": source_record.get("source_id"),
        "source_sha256": actual_source_hash,
        "page_count": len(reader.pages),
        "extractor_runtime": f"pypdf-{pypdf_version}",
        "anchor_count": len(anchor_rows),
        "knowledge_gap_count": len(gap_rows),
        "verified_item_count": len(results),
        "anchors": results,
    }
    if args.json_output or not passed:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"PASS source={payload['source_id']} pages={payload['page_count']} "
            f"anchors={payload['anchor_count']} gaps={payload['knowledge_gap_count']}"
        )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
