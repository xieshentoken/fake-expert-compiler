#!/usr/bin/env python3
"""Deterministic contract checks for generated Expert Skills."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from compiler_version import (
    EXECUTION_COMPILER_VERSION,
    PHASE3_COMPILER_VERSION,
    SEMANTIC_ASSURANCE_COMPILER_VERSION,
    SEMANTIC_ASSURANCE_PROTOCOL,
    VISUAL_ASSURANCE_MANIFEST_SCHEMA,
    VISUAL_CONFLICT_SCHEMA,
    VISUAL_OBJECT_SCHEMA,
    VISUAL_RELATION_SCHEMA,
    TABLE_GRID_SCHEMA,
    VISUAL_REVIEW_ATTESTATION_SCHEMA,
    VISUAL_REVIEW_PROTOCOL,
    VISUAL_SEMANTICS_PROTOCOL,
    VISUAL_SEMANTICS_COMPILER_VERSION,
)
from semantic_assurance import validate_promoted_assurance
from visual_semantics import validate_visual_assurance_manifest, validate_visual_bundle, validate_visual_review_attestations
from portable_reference_runtime import (
    CATALOG_SCHEMA,
    EVALUATOR_VERSION,
    RESPONSE_SCHEMA,
    RUNTIME_VERSION,
    conflict_weighted_terms,
    evaluate_response,
    gap_weighted_terms,
    object_weighted_terms,
    query_catalog,
)
from formula_execution import (
    FORMULA_AST_SCHEMA,
    FormulaContractError,
    evaluate_formula_ast,
    parse_unit,
    sha256_json as formula_sha256_json,
    validate_formula_ast,
    verify_generated_module,
)
from pdf_structure import stable_id


SCHEMA_ID = "tkc.expert-skill/v0.1"
PACKAGE_STATUSES = {"draft", "ready", "deprecated", "revoked"}
SOURCE_MODES = {
    "source-required": "external-source-required",
    "evidence-pack": "evidence-pack-verifiable",
    "full": "self-contained-source-verifiable",
}
REQUIRED_COMPETENCY_CATEGORIES = {
    "definition",
    "comparison",
    "equation",
    "decision",
    "failure-condition",
    "trace",
    "out-of-scope",
}


def object_has_equation(obj: dict[str, Any]) -> bool:
    """True when a knowledge object is an equation or carries a formula.

    The mandatory ``equation`` competency category applies only to packages
    that actually contain such objects; pure-concept packages omit it rather
    than fabricating a meaningless equation test.
    """
    return (
        str(obj.get("type", "")).casefold() == "equation"
        or bool(obj.get("formula"))
    )
KNOWLEDGE_TYPES = {
    "Definition",
    "Concept",
    "Claim",
    "Equation",
    "Assumption",
    "Exception",
    "Method",
    "Procedure",
    "DecisionRule",
}
RELATION_TYPES = {
    "is_a",
    "part_of",
    "depends_on",
    "derived_from",
    "causes",
    "supports",
    "contradicts",
    "valid_when",
    "fails_when",
    "used_for",
    "equivalent_to",
    "example_of",
    "related_to",
}
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ISSUE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
EXECUTION_BUNDLE_SCHEMA = "tkc.execution-bundle/v0.1"
EXECUTION_TEST_SUITE_SCHEMA = "tkc.execution-test-suite/v0.1"
EXECUTION_RECEIPT_SCHEMA = "tkc.execution-receipt/v0.1"
EXECUTION_POLICY_SCHEMA = "tkc.execution-policy/v0.1"
COMPILER_IDENTITIES = {"technical-knowledge-compiler", "fake-expert"}
PHASE3_TEST_SCHEMA = "tkc.competency-test/v0.2"
PHASE3_PLAN_SCHEMA = "tkc.competency-evaluation-plan/v0.1"
PHASE3_RECEIPT_SCHEMA = "tkc.competency-receipt/v0.1"
REVIEW_PROTOCOL = "separate-source-render-review-v1"
ASSURANCE_REVIEW_PROTOCOL = SEMANTIC_ASSURANCE_PROTOCOL
SUPPORTED_PROMOTION_COMPILER_VERSIONS = {
    "0.2.0-phase2",
    PHASE3_COMPILER_VERSION,
    EXECUTION_COMPILER_VERSION,
    SEMANTIC_ASSURANCE_COMPILER_VERSION,
    VISUAL_SEMANTICS_COMPILER_VERSION,
}
SEMANTIC_REVIEW_METHOD = "fresh-source-inspection"
VISUAL_REVIEW_METHOD = "fresh-render-inspection"
SEMANTIC_REVIEW_CHECKS = {
    "evidence_span_checked",
    "support_completeness_checked",
    "scope_and_applicability_checked",
    "conflict_checked",
}
VISUAL_REVIEW_CHECKS = {"page_asset_opened", "task_resolutions_checked"}


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _review_authorization(plan: dict[str, Any]) -> str:
    """Public tamper-evidence checksum; never an identity credential."""

    payload = {
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
    if "review_protocol" in plan:
        payload["review_protocol"] = plan.get("review_protocol")
        payload["review_session_id"] = plan.get("review_session_id")
    if "visual_review_protocol" in plan:
        payload["visual_review_protocol"] = plan.get("visual_review_protocol")
    if "semantic_assurance" in plan:
        payload["semantic_assurance"] = plan.get("semantic_assurance")
    if "visual_review_scope" in plan:
        payload["visual_review_scope"] = plan.get("visual_review_scope")
        payload["visual_candidate_task_count"] = plan.get(
            "visual_candidate_task_count"
        )
        payload["visual_queued_task_count"] = plan.get(
            "visual_queued_task_count"
        )
        payload["visual_deferred_task_count"] = plan.get(
            "visual_deferred_task_count"
        )
        payload["visual_deferred_task_ids_sha256"] = plan.get(
            "visual_deferred_task_ids_sha256"
        )
    return sha256_json(payload)


def _stable_review_session_id(
    bundle_sha256: Any,
    issued_at: Any,
    reviewer_instances: Any,
) -> str:
    reviewers = sorted(reviewer_instances) if isinstance(reviewer_instances, list) else []
    return stable_id("rws", bundle_sha256, issued_at, ",".join(reviewers))


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_jsonl(path: Path) -> list[Any]:
    rows: list[Any] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {line_number}: {exc.msg}") from exc
    return rows


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def safe_package_path(root: Path, relative: str) -> Path | None:
    candidate = PurePosixPath(relative)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        return None
    resolved = (root / Path(*candidate.parts)).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return None
    return resolved


def tracked_files(root: Path, *, include_nested_manifests: bool = False) -> dict[str, str]:
    ignored_parts = {"__pycache__", ".pytest_cache"}
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path == root / "manifest.json":
            continue
        if path.name == "manifest.json" and not include_nested_manifests:
            continue
        relative_path = path.relative_to(root)
        if any(part in ignored_parts or part.startswith(".") for part in relative_path.parts):
            continue
        result[relative_path.as_posix()] = sha256_file(path)
    return result


def _issue(
    issues: list[Issue], severity: str, code: str, path: str, message: str
) -> None:
    issues.append(Issue(severity=severity, code=code, path=path, message=message))


def _required_dict(
    parent: dict[str, Any], key: str, issues: list[Issue], path: str
) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        _issue(issues, "error", "required_object", f"{path}.{key}", "Expected an object.")
        return {}
    return value


def _required_list(
    parent: dict[str, Any], key: str, issues: list[Issue], path: str
) -> list[Any]:
    value = parent.get(key)
    if not isinstance(value, list):
        _issue(issues, "error", "required_array", f"{path}.{key}", "Expected an array.")
        return []
    return value


def _load_required_json(root: Path, relative: str, issues: list[Issue]) -> Any | None:
    path = safe_package_path(root, relative)
    if path is None:
        _issue(issues, "error", "unsafe_path", relative, "Path escapes the package root.")
        return None
    if not path.is_file():
        _issue(issues, "error", "missing_file", relative, "Required file is missing.")
        return None
    try:
        return load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        _issue(issues, "error", "invalid_json", relative, str(exc))
        return None


def _load_required_jsonl(root: Path, relative: str, issues: list[Issue]) -> list[Any]:
    path = safe_package_path(root, relative)
    if path is None:
        _issue(issues, "error", "unsafe_path", relative, "Path escapes the package root.")
        return []
    if not path.is_file():
        _issue(issues, "error", "missing_file", relative, "Required file is missing.")
        return []
    try:
        return load_jsonl(path)
    except (OSError, ValueError) as exc:
        _issue(issues, "error", "invalid_jsonl", relative, str(exc))
        return []


def _validate_skill_frontmatter(root: Path, issues: list[Issue]) -> None:
    path = root / "SKILL.md"
    if not path.is_file():
        _issue(issues, "error", "missing_skill", "SKILL.md", "Canonical Skill entrypoint is missing.")
        return
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if len(lines) < 4 or lines[0].strip() != "---":
        _issue(issues, "error", "invalid_frontmatter", "SKILL.md", "Missing YAML frontmatter.")
        return
    try:
        end = lines[1:].index("---") + 1
    except ValueError:
        _issue(issues, "error", "invalid_frontmatter", "SKILL.md", "Frontmatter is not closed.")
        return
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    if not fields.get("name") or not SLUG_RE.fullmatch(fields["name"]):
        _issue(issues, "error", "invalid_skill_name", "SKILL.md", "Frontmatter name must be a hyphen-case slug.")
    if not fields.get("description"):
        _issue(issues, "error", "missing_skill_description", "SKILL.md", "Frontmatter description is required.")
    unexpected = set(fields) - {"name", "description"}
    if unexpected:
        _issue(
            issues,
            "error",
            "unexpected_frontmatter_field",
            "SKILL.md",
            f"Only name and description are allowed; found {sorted(unexpected)}.",
        )


def _valid_confidence(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(value.get(field), (int, float))
        and not isinstance(value.get(field), bool)
        and 0 <= value[field] <= 1
        for field in ("extraction", "interpretation")
    )


def _validate_scan_locator_v2(
    anchor: dict[str, Any],
    anchor_path: str,
    declared_sources: dict[str, dict[str, Any]],
    issues: list[Issue],
) -> None:
    """Validate the source-free, object-scoped scan commitment shape.

    This is intentionally structural.  Source/render replay remains the job of
    ``verify_source_anchors.py``; the package contract still rejects a locator
    that silently drops a row, observation, geometry, runtime, or reviewer
    binding before that verifier is called.
    """

    locator = anchor.get("scan_locator")
    if not isinstance(locator, dict) or locator.get("locator_protocol") != "scan-anchor-v0.2":
        return
    source = declared_sources.get(str(anchor.get("source_id")), {})
    source_hash = source.get("sha256")
    if locator.get("source_sha256") != source_hash or anchor.get("pages") != locator.get("pages"):
        _issue(issues, "error", "scan_locator_source_or_page_mismatch", anchor_path, "Object scan locator is not bound to its anchor source/pages.")
    required = (
        "source_sha256", "pages", "page_locators", "evidence_spans", "evidence_spans_sha256",
        "excerpt_sha256", "scan_excerpt_sha256", "evidence_commitment_sha256",
        "proposer_instance", "reviewer_instance", "review_session_id", "review_plan_sha256",
        "review_input_sha256", "candidate_sha256", "external_fragment_sha256",
        "review_attestation_sha256", "review_independence_basis",
    )
    if any(key not in locator for key in required):
        _issue(issues, "error", "scan_locator_v2_incomplete", anchor_path, "Hardened scan locator fields are incomplete.")
        return
    for key in (
        "source_sha256", "evidence_spans_sha256", "excerpt_sha256", "scan_excerpt_sha256",
        "evidence_commitment_sha256", "review_plan_sha256", "external_fragment_sha256",
        "review_attestation_sha256",
    ):
        if not isinstance(locator.get(key), str) or not SHA256_RE.fullmatch(locator[key]):
            _issue(issues, "error", "scan_locator_v2_hash_invalid", f"{anchor_path}.scan_locator.{key}", "Expected lowercase SHA-256.")
    if (
        not isinstance(locator.get("proposer_instance"), str)
        or not isinstance(locator.get("reviewer_instance"), str)
        or not locator.get("proposer_instance")
        or not locator.get("reviewer_instance")
        or locator.get("proposer_instance") == locator.get("reviewer_instance")
        or locator.get("review_independence_basis") != "host-plan-proposer-reviewer-separation-v1"
    ):
        _issue(issues, "error", "scan_reviewer_independence_invalid", anchor_path, "Reviewer independence requires host-bound proposer/reviewer separation.")
    evidence_spans = locator.get("evidence_spans")
    text_spans = anchor.get("text_spans")
    if (
        not isinstance(evidence_spans, list)
        or not isinstance(text_spans, list)
        or len(evidence_spans) != len(text_spans)
        or locator.get("evidence_spans_sha256") != sha256_json(evidence_spans)
        or locator.get("evidence_commitment_sha256")
        != sha256_json({key: value for key, value in locator.items() if key != "evidence_commitment_sha256"})
    ):
        _issue(issues, "error", "scan_locator_v2_commitment_invalid", anchor_path, "Evidence span or locator commitment does not close.")
        return
    pages = set(locator.get("pages", [])) if isinstance(locator.get("pages"), list) else set()
    for index, (evidence, text_span) in enumerate(zip(evidence_spans, text_spans)):
        path = f"{anchor_path}.scan_locator.evidence_spans[{index}]"
        if not isinstance(evidence, dict) or not isinstance(text_span, dict):
            _issue(issues, "error", "scan_evidence_span_invalid", path, "Expected an evidence span and matching text span.")
            continue
        if (
            evidence.get("page") not in pages
            or not isinstance(evidence.get("transcript_span_ids"), list)
            or not evidence.get("transcript_span_ids")
            or not isinstance(evidence.get("observation_ids"), list)
            or not evidence.get("observation_ids")
            or not isinstance(evidence.get("bbox_pdf_points"), list)
            or len(evidence["bbox_pdf_points"]) != 4
            or evidence.get("bbox_sha256") != sha256_json(evidence.get("bbox_pdf_points"))
            or (
                any(
                    text_span.get(key) != evidence.get(key)
                    for key in (
                        "page", "start", "end", "page_text_sha256", "span_sha256",
                        "transcript_span_ids", "observation_ids", "bbox_pdf_points", "bbox_sha256",
                    )
                )
                or text_span.get("scan_evidence_span_id") != evidence.get("id")
            )
        ):
            _issue(issues, "error", "scan_evidence_span_binding_invalid", path, "Object evidence and text span bindings differ.")
    page_locators = locator.get("page_locators")
    if not isinstance(page_locators, list) or {row.get("physical_page") for row in page_locators if isinstance(row, dict)} != pages:
        _issue(issues, "error", "scan_page_locator_set_invalid", f"{anchor_path}.scan_locator.page_locators", "Each evidence page needs its own exact locator.")
        return
    for index, page_locator in enumerate(page_locators):
        path = f"{anchor_path}.scan_locator.page_locators[{index}]"
        if not isinstance(page_locator, dict):
            _issue(issues, "error", "scan_page_locator_invalid", path, "Expected a page locator.")
            continue
        required_page = (
            "source_sha256", "physical_page", "render_sha256", "render_receipt", "render_receipt_sha256",
            "bbox_pdf_points", "bbox_sha256", "coordinate_transform_id", "coordinate_transform",
            "coordinate_transform_sha256", "ocr_observation_ids", "observation_commitments",
            "transcript_span_ids", "transcript_row_commitments", "transcript_sha256",
            "transcript_commitment_sha256", "evidence_span_ids", "backend_id",
            "model_identity_sha256", "configuration_sha256", "backend_receipt_id",
            "backend_receipt_sha256", "backend_receipt_binding_sha256", "runtime_contract_sha256",
            "worker_sha256", "qualification_receipt_sha256", "runtime_binding_kind",
        )
        if any(key not in page_locator for key in required_page):
            _issue(issues, "error", "scan_page_locator_incomplete", path, "Page locator commitments are incomplete.")
            continue
        if page_locator.get("source_sha256") != source_hash:
            _issue(issues, "error", "scan_page_source_binding_invalid", path, "Page locator source differs.")
        for key in (
            "render_sha256", "render_receipt_sha256", "bbox_sha256", "coordinate_transform_sha256",
            "transcript_sha256", "transcript_commitment_sha256", "model_identity_sha256",
            "configuration_sha256", "backend_receipt_sha256", "backend_receipt_binding_sha256",
            "runtime_contract_sha256", "worker_sha256",
        ):
            if not isinstance(page_locator.get(key), str) or not SHA256_RE.fullmatch(page_locator[key]):
                _issue(issues, "error", "scan_page_commitment_hash_invalid", f"{path}.{key}", "Expected lowercase SHA-256.")
        if page_locator.get("bbox_sha256") != sha256_json(page_locator.get("bbox_pdf_points")):
            _issue(issues, "error", "scan_page_bbox_commitment_invalid", path, "Page body BBox hash does not close.")
        if page_locator.get("render_receipt_sha256") != sha256_json(page_locator.get("render_receipt")):
            _issue(issues, "error", "scan_page_render_commitment_invalid", path, "Render receipt hash does not close.")
        if page_locator.get("coordinate_transform_sha256") != sha256_json(page_locator.get("coordinate_transform")):
            _issue(issues, "error", "scan_page_transform_commitment_invalid", path, "Coordinate transform hash does not close.")
        if page_locator.get("backend_receipt_binding_sha256") != sha256_json({
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
        }):
            _issue(issues, "error", "scan_backend_commitment_invalid", path, "Backend/runtime receipt binding does not close.")
        rows = page_locator.get("transcript_row_commitments")
        observations = page_locator.get("observation_commitments")
        if not isinstance(rows, list) or not isinstance(observations, list):
            _issue(issues, "error", "scan_page_commitments_missing", path, "Transcript and observation commitments are required.")
            continue
        row_ids = [row.get("id") for row in rows if isinstance(row, dict)]
        observation_ids = sorted(row.get("id") for row in observations if isinstance(row, dict))
        expected_row_ids = [
            row.get("id")
            for row in sorted(
                (row for row in rows if isinstance(row, dict)),
                key=lambda value: (int(value.get("span", {}).get("start", 0)), str(value.get("id"))),
            )
        ]
        if page_locator.get("transcript_span_ids") != expected_row_ids or page_locator.get("ocr_observation_ids") != observation_ids:
            _issue(issues, "error", "scan_page_commitment_ids_invalid", path, "Commitment IDs do not match their records.")
        for row in rows:
            if not isinstance(row, dict) or row.get("row_binding_sha256") != sha256_json({
                "id": row.get("id"), "page_transcript_id": row.get("page_transcript_id"),
                "source_sha256": row.get("source_sha256"), "physical_page": row.get("physical_page"),
                "backend_id": row.get("backend_id"), "span": row.get("span"),
                "raw_text_sha256": row.get("raw_text_sha256"),
                "normalized_text_sha256": row.get("normalized_text_sha256"),
                "observation_ids": row.get("observation_ids"),
            }):
                _issue(issues, "error", "scan_transcript_row_binding_invalid", path, "Transcript row binding does not close.")
        for observation in observations:
            if not isinstance(observation, dict) or observation.get("observation_binding_sha256") != sha256_json({
                "id": observation.get("id"), "source_sha256": observation.get("source_sha256"),
                "physical_page": observation.get("physical_page"),
                "canonical_render_sha256": observation.get("canonical_render_sha256"),
                "backend": observation.get("backend"), "model": observation.get("model"),
                "configuration_sha256": observation.get("configuration_sha256"),
                "coordinate_transform_id": observation.get("coordinate_transform_id"),
                "bbox_image_px": observation.get("bbox_image_px"),
                "polygon_image_px": observation.get("polygon_image_px"),
                "bbox_pdf_points": observation.get("bbox_pdf_points"),
                "polygon_pdf_points": observation.get("polygon_pdf_points"),
                "confidence": observation.get("confidence"), "kind": observation.get("kind"),
                "transcript_span_id": observation.get("transcript_span_id"),
                "backend_receipt_id": observation.get("backend_receipt_id"),
            }):
                _issue(issues, "error", "scan_observation_binding_invalid", path, "Observation binding does not close.")


def _validate_promotion_audit(
    root: Path,
    manifest: dict[str, Any],
    knowledge_objects: dict[str, dict[str, Any]],
    anchors: list[Any],
    issues: list[Issue],
) -> None:
    report_relative = "references/evidence/promotion-report.json"
    build = manifest.get("build")
    is_promoted_package = (
        isinstance(build, dict)
        and build.get("compiler") in COMPILER_IDENTITIES
        and build.get("compiler_version") in SUPPORTED_PROMOTION_COMPILER_VERSIONS
    )
    if not (root / report_relative).is_file():
        if is_promoted_package:
            _issue(
                issues,
                "error",
                "promotion_report_missing",
                report_relative,
                "Phase 2 packages require their promotion audit artifacts.",
            )
        return
    report = _load_required_json(root, report_relative, issues)
    if not isinstance(report, dict) or report.get("schema_version") != "tkc.promotion-report/v0.1":
        _issue(issues, "error", "promotion_report_invalid", report_relative, "Unexpected report schema.")
        return
    report_items_raw = report.get("items")
    report_items_for_lookup = report_items_raw if isinstance(report_items_raw, list) else []
    if not isinstance(build, dict) or build.get("compiler_version") not in SUPPORTED_PROMOTION_COMPILER_VERSIONS:
        _issue(issues, "error", "promotion_compiler_version_invalid", "manifest.build", "Promotion audit requires a supported Phase 2, Phase 3, or execution compiler version.")
    package = manifest.get("package")
    allowed_statuses = {"draft", "ready"} if isinstance(build, dict) and build.get("compiler_version") in {PHASE3_COMPILER_VERSION, EXECUTION_COMPILER_VERSION} else {"draft"}
    if not isinstance(package, dict) or package.get("status") not in allowed_statuses:
        _issue(issues, "error", "promotion_lifecycle_invalid", "manifest.package.status", "Promoted package lifecycle is incompatible with its compiler phase.")
    artifacts = report.get("review_artifacts")
    if not isinstance(artifacts, dict):
        _issue(issues, "error", "promotion_review_artifacts_missing", report_relative, "Review artifact paths are required.")
        return
    required_paths = {
        "plan": "references/reviews/review-plan.json",
        "semantic_records": "references/reviews/semantic-records.jsonl",
        "visual_receipts": "references/reviews/visual-receipts.jsonl",
    }
    if any(artifacts.get(key) != value for key, value in required_paths.items()):
        _issue(issues, "error", "promotion_review_path_invalid", report_relative, "Review artifacts must use canonical paths.")
        return
    plan = _load_required_json(root, required_paths["plan"], issues)
    records = _load_required_jsonl(root, required_paths["semantic_records"], issues)
    visual_receipts = _load_required_jsonl(root, required_paths["visual_receipts"], issues)
    if not isinstance(plan, dict) or plan.get("schema_version") != "tkc.review-plan/v0.1":
        _issue(issues, "error", "promotion_review_plan_invalid", required_paths["plan"], "Unexpected review plan.")
        return
    bundle_hash = report.get("review_bundle_sha256")
    source_hashes = {
        source.get("sha256")
        for source in manifest.get("sources", [])
        if isinstance(source, dict)
    }
    phase2_sources = [source for source in manifest.get("sources", []) if isinstance(source, dict)]
    if not phase2_sources or any(not isinstance(source.get("scope"), dict) for source in phase2_sources):
        _issue(issues, "error", "promotion_source_scope_missing", "manifest.sources", "Phase 2 packages require explicit source scope.")
    if not isinstance(bundle_hash, str) or not SHA256_RE.fullmatch(bundle_hash):
        _issue(issues, "error", "promotion_bundle_invalid", report_relative, "Review bundle SHA-256 is required.")
    if plan.get("bundle_sha256") != bundle_hash:
        _issue(issues, "error", "promotion_bundle_mismatch", required_paths["plan"], "Plan and report bundle hashes differ.")
    if plan.get("source_sha256") not in source_hashes or report.get("source_sha256") not in source_hashes:
        _issue(issues, "error", "promotion_source_mismatch", report_relative, "Promotion source hash is not declared.")
    if plan.get("attestation_level") != "host-orchestrator-recorded-not-cryptographic":
        _issue(issues, "error", "promotion_attestation_overstated", required_paths["plan"], "Unexpected attestation level.")
    if (
        plan.get("issued_by") != "host-orchestrator"
        or not isinstance(plan.get("issued_at"), str)
        or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
            plan.get("issued_at", ""),
        )
        or plan.get("gap_resolution_policy") != "explicit-package-scope-v1"
        or plan.get("authorization_sha256") != _review_authorization(plan)
    ):
        _issue(
            issues,
            "error",
            "promotion_review_authorization_invalid",
            required_paths["plan"],
            "The public review-plan fingerprint or bounded orchestration metadata is invalid.",
        )
    review_protocol = plan.get("review_protocol")
    protocol_enabled = review_protocol in {REVIEW_PROTOCOL, ASSURANCE_REVIEW_PROTOCOL}
    if review_protocol is not None and review_protocol not in {REVIEW_PROTOCOL, ASSURANCE_REVIEW_PROTOCOL}:
        _issue(
            issues,
            "error",
            "promotion_review_protocol_invalid",
            required_paths["plan"],
            f"Unsupported review protocol: {review_protocol!r}.",
        )
    review_session_id = plan.get("review_session_id")
    if protocol_enabled and (
        not isinstance(review_session_id, str)
        or not review_session_id
        or review_session_id
        != _stable_review_session_id(
            plan.get("bundle_sha256"),
            plan.get("issued_at"),
            plan.get("reviewer_instances"),
        )
    ):
        _issue(
            issues,
            "error",
            "promotion_review_protocol_invalid",
            required_paths["plan"],
            "Review session is not bound to the frozen plan.",
        )
    reviewers = set(plan.get("reviewer_instances", [])) if isinstance(plan.get("reviewer_instances"), list) else set()
    proposers = set(plan.get("proposer_instances", [])) if isinstance(plan.get("proposer_instances"), list) else set()
    if not reviewers or reviewers & proposers:
        _issue(issues, "error", "promotion_reviewer_not_independent", required_paths["plan"], "Reviewer registry is empty or overlaps proposers.")

    records_by_ref: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        path = f"{required_paths['semantic_records']}:{index + 1}"
        if not isinstance(record, dict) or record.get("schema_version") != "tkc.semantic-review/v0.1":
            _issue(issues, "error", "promotion_review_record_invalid", path, "Unexpected semantic review record.")
            continue
        item_ref = record.get("item_ref")
        if not isinstance(item_ref, str) or not item_ref or item_ref in records_by_ref:
            _issue(issues, "error", "promotion_review_record_duplicate", path, str(item_ref))
            continue
        records_by_ref[item_ref] = record
        if record.get("reviewer_instance") not in reviewers or record.get("reviewer_instance") == record.get("proposer_instance"):
            _issue(issues, "error", "promotion_reviewer_identity_invalid", path, str(record.get("reviewer_instance")))
        for field in ("item_sha256", "review_input_sha256"):
            value = record.get(field)
            if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
                _issue(issues, "error", "promotion_review_hash_invalid", f"{path}.{field}", "Expected SHA-256.")
        verdict = record.get("verdict")
        issue_codes = record.get("issue_codes")
        if record.get("item_kind") not in {"object", "relation", "conflict", "gap"}:
            _issue(issues, "error", "promotion_review_kind_invalid", path, str(record.get("item_kind")))
        if verdict not in {"accepted", "rejected", "quarantined"}:
            _issue(issues, "error", "promotion_review_verdict_invalid", path, str(verdict))
        if (
            not isinstance(issue_codes, list)
            or any(not isinstance(code, str) or not ISSUE_CODE_RE.fullmatch(code) for code in issue_codes or [])
            or len(issue_codes) != len(set(issue_codes))
        ):
            _issue(issues, "error", "promotion_review_issues_invalid", path, "issue_codes must be unique stable snake_case identifiers.")
            issue_codes = []
        if not isinstance(record.get("rationale"), str) or not record.get("rationale"):
            _issue(issues, "error", "promotion_review_rationale_missing", path, "Review rationale is required.")
        if protocol_enabled:
            if record.get("review_session_id") != review_session_id:
                _issue(issues, "error", "promotion_review_protocol_invalid", path, "Review session differs from the frozen plan.")
            if record.get("review_method") != SEMANTIC_REVIEW_METHOD:
                _issue(issues, "error", "promotion_review_protocol_invalid", path, f"Expected {SEMANTIC_REVIEW_METHOD!r}.")
            required_checks = set(SEMANTIC_REVIEW_CHECKS)
            if record.get("item_kind") == "object" and record.get("formula_ast_sha256"):
                required_checks.add("formula_checked")
            checks = record.get("review_checks")
            if not isinstance(checks, dict) or any(checks.get(name) is not True for name in required_checks):
                _issue(issues, "error", "promotion_review_checks_missing", path, f"Required host-recorded checks: {sorted(required_checks)}.")
        if verdict == "accepted":
            if record.get("evidence_support") != "full" or issue_codes or not _valid_confidence(record.get("confidence")):
                _issue(issues, "error", "promotion_acceptance_invalid", path, "Accepted review must be fully supported, issue-free, and confident.")
        elif not issue_codes:
            _issue(issues, "error", "promotion_rejection_unexplained", path, "Rejected/quarantined review needs issue codes.")
        if record.get("item_kind") == "gap":
            resolution = record.get("gap_resolution")
            if not isinstance(resolution, dict) or resolution.get("status") not in {
                "unresolved_in_package",
                "resolved_elsewhere",
            } or not isinstance(resolution.get("resolved_by_refs"), list):
                _issue(issues, "error", "promotion_gap_resolution_invalid", path, "Explicit gap resolution is required.")
        elif record.get("gap_resolution") is not None:
            _issue(issues, "error", "promotion_gap_resolution_invalid", path, "Only gap reviews may carry gap_resolution.")
    if plan.get("semantic_item_count") != len(records_by_ref):
        _issue(issues, "error", "promotion_review_count_mismatch", required_paths["plan"], "Semantic record count differs from plan.")

    receipts_by_page: dict[int, dict[str, Any]] = {}
    for index, receipt in enumerate(visual_receipts):
        path = f"{required_paths['visual_receipts']}:{index + 1}"
        if not isinstance(receipt, dict) or receipt.get("schema_version") != "tkc.visual-receipt/v0.1":
            _issue(issues, "error", "promotion_visual_receipt_invalid", path, "Unexpected visual receipt.")
            continue
        page = receipt.get("page")
        if not isinstance(page, int) or page < 1 or page in receipts_by_page:
            _issue(issues, "error", "promotion_visual_receipt_duplicate", path, str(page))
            continue
        receipts_by_page[page] = receipt
        if receipt.get("reviewer_instance") not in reviewers or receipt.get("reviewer_instance") in proposers:
            _issue(issues, "error", "promotion_reviewer_identity_invalid", path, str(receipt.get("reviewer_instance")))
        for field in ("review_input_sha256", "page_asset_sha256"):
            value = receipt.get(field)
            if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
                _issue(issues, "error", "promotion_visual_hash_invalid", f"{path}.{field}", "Expected SHA-256.")
        if receipt.get("verdict") not in {"verified", "contradicted", "unresolved", "not-needed"}:
            _issue(issues, "error", "promotion_visual_verdict_invalid", path, str(receipt.get("verdict")))
        if protocol_enabled:
            if receipt.get("review_session_id") != review_session_id:
                _issue(issues, "error", "promotion_review_protocol_invalid", path, "Review session differs from the frozen plan.")
            if receipt.get("review_method") != VISUAL_REVIEW_METHOD:
                _issue(issues, "error", "promotion_review_protocol_invalid", path, f"Expected {VISUAL_REVIEW_METHOD!r}.")
            checks = receipt.get("review_checks")
            if not isinstance(checks, dict) or any(checks.get(name) is not True for name in VISUAL_REVIEW_CHECKS):
                _issue(issues, "error", "promotion_review_checks_missing", path, f"Required host-recorded checks: {sorted(VISUAL_REVIEW_CHECKS)}.")
        resolutions = receipt.get("task_resolutions")
        if not isinstance(resolutions, list) or not resolutions:
            _issue(issues, "error", "promotion_visual_tasks_missing", path, "Visual task resolutions are required.")
        else:
            task_ids: set[str] = set()
            for resolution in resolutions:
                if not isinstance(resolution, dict) or not isinstance(resolution.get("task_id"), str) or resolution["task_id"] in task_ids:
                    _issue(issues, "error", "promotion_visual_task_invalid", path, str(resolution))
                    continue
                task_ids.add(resolution["task_id"])
                if resolution.get("verdict") not in {"supported", "not-present", "contradicted", "unresolved"} or not isinstance(resolution.get("observation"), str) or not resolution.get("observation"):
                    _issue(issues, "error", "promotion_visual_task_invalid", path, resolution["task_id"])
    visual_protocol = plan.get("visual_review_protocol")
    # Object-level v0.2 attestations intentionally do not emit legacy
    # page-receipt rows.  Their queue count remains authorization metadata, so
    # compare the receipt count only for the legacy visual-receipt route.
    if visual_protocol != VISUAL_REVIEW_PROTOCOL and plan.get("visual_page_count") != len(receipts_by_page):
        _issue(issues, "error", "promotion_visual_count_mismatch", required_paths["plan"], "Visual receipt count differs from plan.")

    if visual_protocol == VISUAL_REVIEW_PROTOCOL:
        visual_paths = {
            "visual_objects": "references/visual/visual-objects.jsonl",
            "visual_relations": "references/visual/visual-relations.jsonl",
            "table_grids": "references/visual/table-grids.jsonl",
            "visual_conflicts": "references/visual/visual-conflicts.jsonl",
            "review_attestations": "references/visual/review-attestations.jsonl",
            "assurance_manifest": "references/visual/assurance-manifest.json",
        }
        declared = {
            "visual_objects": artifacts.get("visual_objects"),
            "visual_relations": artifacts.get("visual_relations"),
            "table_grids": artifacts.get("table_grids"),
            "visual_conflicts": artifacts.get("visual_conflicts"),
            "review_attestations": artifacts.get("visual_review_attestations"),
            "assurance_manifest": artifacts.get("visual_assurance_manifest"),
        }
        for key, expected in visual_paths.items():
            if declared.get(key) != expected:
                _issue(issues, "error", "visual_assurance_path_invalid", report_relative, f"Unexpected visual artifact path for {key}.")
        visual_rows: dict[str, list[Any]] = {}
        for key in ("visual_objects", "visual_relations", "table_grids", "visual_conflicts", "review_attestations"):
            path = declared.get(key) or visual_paths[key]
            visual_rows[key] = _load_required_jsonl(root, path, issues)
        assurance_path = declared.get("assurance_manifest") or visual_paths["assurance_manifest"]
        assurance = _load_required_json(root, assurance_path, issues)
        source_hash = str(plan.get("source_sha256"))
        visual_bundle = {
            "schema_version": VISUAL_SEMANTICS_PROTOCOL,
            "source_sha256": source_hash,
            "objects": visual_rows["visual_objects"],
            "relations": visual_rows["visual_relations"],
            "table_grids": visual_rows["table_grids"],
            "conflicts": visual_rows["visual_conflicts"],
        }
        for visual_issue in validate_visual_bundle(visual_bundle, source_sha256=source_hash, require_candidate_only=False):
            _issue(issues, visual_issue.severity, visual_issue.code, visual_issue.path, visual_issue.message)
        if isinstance(assurance, dict):
            binding = assurance.get("review_binding") if isinstance(assurance.get("review_binding"), dict) else {}
            frozen_requirements = binding.get("required_reviewers") if isinstance(binding.get("required_reviewers"), dict) else {}
            frozen_registered = binding.get("registered_reviewer_instances") if isinstance(binding.get("registered_reviewer_instances"), list) else []
            plan_requirements = plan.get("visual_reviewer_requirements")
            if not isinstance(plan_requirements, dict) or not plan_requirements:
                _issue(issues, "error", "visual_reviewer_requirement_missing", required_paths["plan"], "Promoted v0.2 visual review must carry the frozen reviewer requirements from the review plan.")
                plan_requirements = {}
            plan_registered = plan.get("reviewer_instances") if isinstance(plan.get("reviewer_instances"), list) else frozen_registered
            for visual_issue in validate_visual_assurance_manifest(
                assurance,
                source_sha256=source_hash,
                objects=visual_rows["visual_objects"],
                relations=visual_rows["visual_relations"],
                grids=visual_rows["table_grids"],
                conflicts=visual_rows["visual_conflicts"],
                attestations=visual_rows["review_attestations"],
                review_plan_sha256=plan.get("visual_review_plan_sha256"),
                review_session_id=plan.get("review_session_id"),
                required_reviewers=plan_requirements,
                registered_reviewers=plan_registered,
            ):
                _issue(issues, visual_issue.severity, visual_issue.code, visual_issue.path, visual_issue.message)
        object_rows = [row for row in visual_rows["visual_objects"] if isinstance(row, dict)]
        object_ids = {str(row.get("visual_object_id")) for row in object_rows}
        if any(row.get("status") != "promoted" for row in object_rows):
            _issue(issues, "error", "visual_candidate_promotion_forbidden", visual_paths["visual_objects"], "Runtime visual objects must be explicitly promoted by an external attestation.")
        attestation_rows = [row for row in visual_rows["review_attestations"] if isinstance(row, dict)]
        accepted_by_object: dict[str, list[dict[str, Any]]] = {}
        for row in attestation_rows:
            if row.get("verdict") == "accepted":
                for identifier in row.get("visual_object_ids", []):
                    accepted_by_object.setdefault(str(identifier), []).append(row)
        plan_hash = plan.get("visual_review_plan_sha256")
        if not isinstance(plan_hash, str) or not SHA256_RE.fullmatch(plan_hash):
            _issue(issues, "error", "visual_review_plan_hash_invalid", required_paths["plan"], "Original visual review plan hash is required.")
        else:
            binding = assurance.get("review_binding") if isinstance(assurance, dict) and isinstance(assurance.get("review_binding"), dict) else {}
            required_counts = plan.get("visual_reviewer_requirements") if isinstance(plan.get("visual_reviewer_requirements"), dict) else {}
            registered_visual_reviewers = plan.get("reviewer_instances") if isinstance(plan.get("reviewer_instances"), list) else []
            frozen_visual_session = binding.get("review_session_id")
            for visual_issue in validate_visual_review_attestations(
                attestation_rows,
                object_rows,
                review_plan_sha256=plan_hash,
                source_sha256=source_hash,
                proposer_instances=proposers,
                required_reviewers=required_counts,
                table_grids=visual_rows["table_grids"],
                visual_relations=visual_rows["visual_relations"],
                expected_review_session_id=frozen_visual_session,
                registered_reviewers=registered_visual_reviewers,
            ):
                _issue(issues, visual_issue.severity, visual_issue.code, visual_issue.path, visual_issue.message)
        grid_cell_ids = {
            str(cell.get("cell_id"))
            for grid in visual_rows["table_grids"]
            if isinstance(grid, dict)
            for cell in grid.get("cells", [])
            if isinstance(cell, dict)
        }
        series_ids = {str(row.get("visual_object_id")) for row in object_rows if row.get("type") == "series"}
        relation_ids = {str(row.get("visual_relation_id")): str(row.get("relation_sha256")) for row in visual_rows["visual_relations"] if isinstance(row, dict)}
        grid_ids = {str(row.get("table_grid_id")): str(row.get("grid_sha256")) for row in visual_rows["table_grids"] if isinstance(row, dict)}
        grid_by_cell = {
            str(cell.get("cell_id")): grid
            for grid in visual_rows["table_grids"]
            if isinstance(grid, dict)
            for cell in grid.get("cells", [])
            if isinstance(cell, dict) and isinstance(cell.get("cell_id"), str)
        }
        object_by_visual_id = {
            str(row.get("visual_object_id")): row
            for row in object_rows
            if isinstance(row, dict) and isinstance(row.get("visual_object_id"), str)
        }
        for object_id, obj in knowledge_objects.items():
            visual_ids = obj.get("visual_object_ids", [])
            visual_tasks = obj.get("visual_task_ids", [])
            if visual_tasks and not visual_ids:
                _issue(issues, "error", "visual_fact_object_support_missing", f"knowledge-object:{object_id}", "New visual-semantics facts require exact visual object/cell/series bindings; page-only support is insufficient.")
            if not visual_ids and not obj.get("table_cell_ids") and not obj.get("series_ids"):
                continue
            path = f"knowledge-object:{object_id}"
            if any(identifier not in object_ids for identifier in visual_ids):
                _issue(issues, "error", "visual_object_binding_unresolved", path, "Visual fact references an absent visual object.")
            if any(identifier not in grid_cell_ids for identifier in obj.get("table_cell_ids", [])):
                _issue(issues, "error", "table_cell_unresolved", path, "Visual fact references an absent table cell.")
            for cell_id in obj.get("table_cell_ids", []):
                grid = grid_by_cell.get(str(cell_id))
                if not isinstance(grid, dict):
                    continue
                grid_id = str(grid.get("table_grid_id"))
                if grid_id not in {str(value) for value in obj.get("table_grid_ids", [])} or str(grid.get("table_visual_object_id")) not in {str(value) for value in visual_ids}:
                    _issue(issues, "error", "table_cell_binding_invalid", path, "Visual fact table cells must belong to a bound same-page table/grid.")
            if any(identifier not in series_ids for identifier in obj.get("series_ids", [])):
                _issue(issues, "error", "visual_series_unresolved", path, "Visual fact references an absent series.")
            for series_id in obj.get("series_ids", []):
                series = object_by_visual_id.get(str(series_id))
                if not isinstance(series, dict):
                    continue
                bound_ids = {str(value) for value in visual_ids}
                has_series_relation = any(
                    isinstance(relation, dict)
                    and relation.get("relation_type") == "series_of"
                    and str(relation.get("source_visual_object_id")) == str(series_id)
                    and str(relation.get("target_visual_object_id")) in bound_ids
                    for relation in visual_rows["visual_relations"]
                )
                if str(series.get("parent")) not in bound_ids and not has_series_relation:
                    _issue(issues, "error", "visual_series_binding_invalid", path, "Visual fact series must belong to a bound plot/object.")
            if any(identifier not in relation_ids for identifier in obj.get("visual_relation_ids", [])):
                _issue(issues, "error", "visual_relation_unresolved", path, "Visual fact references an absent visual relation.")
            expected_relation_hashes = sorted(relation_ids.get(str(identifier), "") for identifier in obj.get("visual_relation_ids", []))
            actual_relation_hashes = sorted(str(value) for value in obj.get("relation_sha256s", [])) if isinstance(obj.get("relation_sha256s"), list) else []
            if expected_relation_hashes != actual_relation_hashes:
                _issue(issues, "error", "visual_relation_hash_mismatch", path, "Visual fact relation hashes do not match the bound relation rows.")
            if any(identifier not in grid_ids for identifier in obj.get("table_grid_ids", [])):
                _issue(issues, "error", "table_grid_unresolved", path, "Visual fact references an absent table grid.")
            expected_grid_hashes = sorted(grid_ids.get(str(identifier), "") for identifier in obj.get("table_grid_ids", []))
            actual_grid_hashes = sorted(str(value) for value in obj.get("table_grid_sha256s", [])) if isinstance(obj.get("table_grid_sha256s"), list) else []
            if expected_grid_hashes != actual_grid_hashes:
                _issue(issues, "error", "table_grid_hash_mismatch", path, "Visual fact table-grid hashes do not match the bound grid rows.")
            if not all(identifier in accepted_by_object for identifier in visual_ids):
                _issue(issues, "error", "visual_fact_review_missing", path, "Visual fact lacks accepted object-level attestation.")

    accepted_records = {
        item_ref: record
        for item_ref, record in records_by_ref.items()
        if record.get("verdict") == "accepted"
    }
    relation_rows = _load_required_jsonl(root, "references/knowledge/relations.jsonl", issues)
    conflict_rows = _load_required_jsonl(root, "references/conflicts.jsonl", issues)
    gap_rows = _load_required_jsonl(root, "references/knowledge-gaps.jsonl", issues)

    def entities_by_id(kind: str, rows: list[Any], relative: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for index, row in enumerate(rows):
            entity_id = row.get("id") if isinstance(row, dict) else None
            if not isinstance(entity_id, str) or not entity_id or entity_id in result:
                _issue(
                    issues,
                    "error",
                    "promotion_entity_id_invalid",
                    f"{relative}:{index + 1}",
                    f"{kind} entities require unique IDs.",
                )
                continue
            result[entity_id] = row
        return result

    relation_entities = entities_by_id("relation", relation_rows, "references/knowledge/relations.jsonl")
    conflict_entities = entities_by_id("conflict", conflict_rows, "references/conflicts.jsonl")
    gap_entities = entities_by_id("gap", gap_rows, "references/knowledge-gaps.jsonl")
    entity_maps: dict[str, dict[str, dict[str, Any]]] = {
        "object": knowledge_objects,
        "relation": relation_entities,
        "conflict": conflict_entities,
        "gap": gap_entities,
    }

    for object_id, obj in knowledge_objects.items():
        binding = obj.get("review_binding")
        path = f"knowledge-object:{object_id}"
        if not isinstance(binding, dict):
            _issue(issues, "error", "promotion_object_review_missing", path, "Promoted object needs a review binding.")
            continue
        matches = [
            (item_ref, record)
            for item_ref, record in accepted_records.items()
            if record.get("item_sha256") == binding.get("item_sha256")
            and record.get("review_input_sha256") == binding.get("review_input_sha256")
            and record.get("reviewer_instance") == binding.get("reviewer_instance")
        ]
        if len(matches) != 1 or binding.get("review_bundle_sha256") != bundle_hash:
            _issue(issues, "error", "promotion_object_review_unresolved", path, "Object review binding does not resolve uniquely.")
        else:
            matched_ref, matched_record = matches[0]
            if matched_record.get("item_kind") != "object":
                _issue(issues, "error", "promotion_object_review_unresolved", path, "Object bound to a non-object review record.")
            report_match = next(
                (
                    item
                    for item in report_items_for_lookup
                    if isinstance(item, dict) and item.get("item_ref") == matched_ref
                ),
                None,
            )
            if not isinstance(report_match, dict) or report_match.get("promoted_id") != object_id:
                _issue(issues, "error", "promotion_object_mapping_mismatch", path, "Promotion report does not map this accepted object review to this object ID.")

    for kind, entities in (
        ("relation", relation_entities),
        ("conflict", conflict_entities),
        ("gap", gap_entities),
    ):
        for entity_id, entity in entities.items():
            path = f"{kind}:{entity_id}"
            if kind == "gap":
                spans = entity.get("evidence_spans")
                gap_source_id = entity.get("source_id")
                gap_source = next(
                    (
                        source
                        for source in manifest.get("sources", [])
                        if isinstance(source, dict) and source.get("source_id") == gap_source_id
                    ),
                    None,
                )
                if (
                    entity.get("scope") != "package"
                    or entity.get("status") != "unresolved"
                    or not isinstance(gap_source, dict)
                    or not isinstance(spans, list)
                    or not spans
                    or not isinstance(entity.get("excerpt_sha256"), str)
                    or not SHA256_RE.fullmatch(entity["excerpt_sha256"])
                ):
                    _issue(issues, "error", "promotion_gap_invalid", path, "Canonical gaps must be source-bound unresolved package gaps.")
                else:
                    gap_scope = gap_source.get("scope")
                    scope_pages = gap_scope.get("physical_pages") if isinstance(gap_scope, dict) else None
                    for span in spans:
                        if (
                            not isinstance(span, dict)
                            or not isinstance(span.get("page"), int)
                            or not isinstance(span.get("start"), int)
                            or not isinstance(span.get("end"), int)
                            or not 0 <= span["start"] < span["end"]
                            or any(not isinstance(span.get(field), str) or not SHA256_RE.fullmatch(span[field]) for field in ("page_text_sha256", "span_sha256"))
                            or (
                                isinstance(scope_pages, list)
                                and len(scope_pages) == 2
                                and (span.get("page") < scope_pages[0] or span.get("page") > scope_pages[1])
                            )
                        ):
                            _issue(issues, "error", "promotion_gap_invalid", path, "Gap evidence spans are invalid.")
                            break
            binding = entity.get("review_binding")
            if not isinstance(binding, dict):
                _issue(issues, "error", "promotion_entity_review_missing", path, "Promoted entity needs a review binding.")
                continue
            matches = [
                (item_ref, record)
                for item_ref, record in accepted_records.items()
                if record.get("item_kind") == kind
                and record.get("item_sha256") == binding.get("item_sha256")
                and record.get("review_input_sha256") == binding.get("review_input_sha256")
                and record.get("reviewer_instance") == binding.get("reviewer_instance")
            ]
            if (
                len(matches) != 1
                or binding.get("attestation_level")
                != "host-orchestrator-recorded-not-cryptographic"
                or binding.get("review_bundle_sha256") != bundle_hash
            ):
                _issue(issues, "error", "promotion_entity_review_unresolved", path, "Entity review binding does not resolve uniquely.")
                continue
            matched_ref, _ = matches[0]
            report_match = next(
                (
                    item
                    for item in report_items_for_lookup
                    if isinstance(item, dict) and item.get("item_ref") == matched_ref
                ),
                None,
            )
            if not isinstance(report_match, dict) or report_match.get("promoted_id") != entity_id:
                _issue(issues, "error", "promotion_entity_mapping_mismatch", path, "Promotion report does not map this review to this entity ID.")
            if kind == "gap":
                unit_id = entity.get("unit_id")
                source_item_ref = entity.get("source_item_ref")
                source_ref_matches_unit = (
                    isinstance(unit_id, str)
                    and re.fullmatch(r"swu-[0-9a-f]{20}", unit_id) is not None
                    and re.fullmatch(rf"gap:{re.escape(unit_id)}:[0-9]+", matched_ref)
                    is not None
                )
                if (
                    source_item_ref != matched_ref
                    or not source_ref_matches_unit
                    or not isinstance(report_match, dict)
                    or report_match.get("item_ref") != source_item_ref
                ):
                    _issue(
                        issues,
                        "error",
                        "promotion_gap_source_binding_mismatch",
                        path,
                        "Canonical gap source_item_ref and unit_id must match the accepted gap review and promotion-report item.",
                    )

    for index, anchor in enumerate(anchors):
        if not isinstance(anchor, dict):
            continue
        locator = anchor.get("locator")
        if not isinstance(locator, dict) or locator.get("review_bundle_sha256") != bundle_hash:
            _issue(issues, "error", "promotion_anchor_bundle_mismatch", f"anchors:{index + 1}", "Anchor is not bound to the review bundle.")
            continue
        visual_hash = locator.get("visual_receipts_sha256")
        visual_pages = anchor.get("visual_receipt_pages")
        if visual_hash is not None:
            if not isinstance(visual_pages, list) or not visual_pages or any(page not in receipts_by_page for page in visual_pages):
                _issue(issues, "error", "promotion_anchor_visual_unresolved", f"anchors:{index + 1}", "Visual receipt pages do not resolve.")
            else:
                actual = sha256_json(
                    sorted({sha256_json(receipts_by_page[page]) for page in visual_pages})
                )
                if visual_hash != actual:
                    _issue(issues, "error", "promotion_anchor_visual_mismatch", f"anchors:{index + 1}", "Visual receipt fingerprint differs.")

    report_items = report_items_raw
    if not isinstance(report_items, list) or len(report_items) != len(records_by_ref):
        _issue(issues, "error", "promotion_report_item_count_mismatch", report_relative, "Report item coverage differs from review records.")
        report_items = []
    report_refs: set[str] = set()
    mapped_entity_ids: set[str] = set()
    for index, item in enumerate(report_items):
        path = f"{report_relative}.items[{index}]"
        if not isinstance(item, dict) or item.get("item_ref") not in records_by_ref or item.get("item_ref") in report_refs:
            _issue(issues, "error", "promotion_report_item_invalid", path, str(item))
            continue
        item_ref = item["item_ref"]
        report_refs.add(item_ref)
        record = records_by_ref[item_ref]
        if (
            item.get("item_kind") != record.get("item_kind")
            or item.get("verdict") != record.get("verdict")
            or item.get("review_record_sha256") != sha256_json(record)
        ):
            _issue(issues, "error", "promotion_report_item_mismatch", path, "Report entry differs from its review record.")
        promoted_id = item.get("promoted_id")
        kind = record.get("item_kind")
        if record.get("verdict") == "accepted" and kind in entity_maps:
            if promoted_id not in entity_maps[kind] or promoted_id in mapped_entity_ids:
                _issue(issues, "error", "promotion_entity_mapping_mismatch", path, "Accepted review must map uniquely to an entity of the same kind.")
            elif isinstance(promoted_id, str):
                mapped_entity_ids.add(promoted_id)
        elif promoted_id is not None:
            _issue(issues, "error", "promotion_entity_mapping_mismatch", path, "Only accepted reviews may map to promoted entities.")
        if kind == "gap":
            resolution = record.get("gap_resolution")
            report_resolution = item.get("gap_resolution")
            resolved_ids = item.get("resolved_by_ids")
            if report_resolution != resolution or not isinstance(resolved_ids, list):
                _issue(issues, "error", "promotion_gap_mapping_mismatch", path, "Gap resolution differs from its review record.")
            elif isinstance(resolution, dict):
                status = resolution.get("status")
                resolver_refs = resolution.get("resolved_by_refs")
                if status == "resolved_elsewhere":
                    expected_ids = []
                    if isinstance(resolver_refs, list):
                        for resolver_ref in resolver_refs:
                            resolver_item = next(
                                (
                                    report_row
                                    for report_row in report_items_for_lookup
                                    if isinstance(report_row, dict)
                                    and report_row.get("item_ref") == resolver_ref
                                ),
                                None,
                            )
                            if not isinstance(resolver_item, dict) or resolver_item.get("verdict") != "accepted" or resolver_item.get("item_kind") != "object":
                                _issue(issues, "error", "promotion_gap_mapping_mismatch", path, f"Resolver is not an accepted object: {resolver_ref}")
                                continue
                            expected_ids.append(resolver_item.get("promoted_id"))
                    if resolved_ids != expected_ids or record.get("verdict") != "rejected" or "knowledge_gap_resolved_elsewhere" not in record.get("issue_codes", []):
                        _issue(issues, "error", "promotion_gap_mapping_mismatch", path, "Resolved gap mapping or disposition is inconsistent.")
                elif status == "unresolved_in_package":
                    if resolved_ids or record.get("verdict") != "accepted" or promoted_id not in gap_entities:
                        _issue(issues, "error", "promotion_gap_mapping_mismatch", path, "Unresolved package gap must map to one canonical gap.")
                else:
                    _issue(issues, "error", "promotion_gap_mapping_mismatch", path, "Unknown gap resolution status.")
    expected_entity_ids = {
        entity_id
        for entities in entity_maps.values()
        for entity_id in entities
    }
    if mapped_entity_ids != expected_entity_ids:
        _issue(
            issues,
            "error",
            "promotion_entity_coverage_mismatch",
            report_relative,
            "Accepted review mappings do not exactly cover promoted entities.",
        )
    counts = report.get("counts")
    if isinstance(counts, dict):
        resolved_elsewhere_count = sum(
            record.get("item_kind") == "gap"
            and isinstance(record.get("gap_resolution"), dict)
            and record["gap_resolution"].get("status") == "resolved_elsewhere"
            for record in records_by_ref.values()
        )
        expected_counts = {
            "review_items": len(records_by_ref),
            "accepted_items": len(accepted_records),
            "knowledge_objects": len(knowledge_objects),
            "anchors": len(anchors),
            "relations": len(relation_entities),
            "conflicts": len(conflict_entities),
            "knowledge_gaps": len(gap_entities),
            "resolved_elsewhere_gaps": resolved_elsewhere_count,
        }
        if any(counts.get(key) != value for key, value in expected_counts.items()):
            _issue(issues, "error", "promotion_report_count_mismatch", report_relative, f"Expected counts {expected_counts}.")
    else:
        _issue(issues, "error", "promotion_report_count_mismatch", report_relative, "counts is required.")
    policy = report.get("policy")
    capabilities = manifest.get("capabilities")
    distribution = manifest.get("distribution")
    is_execution_package = isinstance(build, dict) and build.get("compiler_version") == EXECUTION_COMPILER_VERSION
    if (
        not isinstance(policy, dict)
        or policy.get("promoter") != "deterministic"
        or policy.get("source_content_copied") is not False
        or policy.get("source_required") is not True
        or policy.get("decision_support") is not False
        or policy.get("executable") is not False
        or policy.get("publication_ready") is not False
        or (not is_execution_package and (
            not isinstance(capabilities, dict)
            or capabilities.get("reference") is not True
            or capabilities.get("decision_support") is not False
            or capabilities.get("executable") is not False
        ))
        or not isinstance(distribution, dict)
        or distribution.get("source_mode") != "source-required"
        or distribution.get("verification") != "external-source-required"
    ):
        _issue(
            issues,
            "error",
            "promotion_policy_invalid",
            report_relative,
            "The retained Phase 2 promotion record must remain source-required, reference-only, draft, and non-executable.",
        )


def _validate_phase3_runtime(
    root: Path,
    manifest: dict[str, Any],
    knowledge_objects: dict[str, dict[str, Any]],
    anchor_ids: set[str],
    tests: list[Any],
    issues: list[Issue],
) -> None:
    build = manifest.get("build")
    if not isinstance(build, dict) or build.get("compiler_version") != PHASE3_COMPILER_VERSION:
        return
    catalog_relative = "references/runtime/catalog.json"
    plan_relative = "references/evaluations/evaluation-plan.json"
    responses_relative = "references/evaluations/runtime-responses.jsonl"
    receipts_relative = "references/evaluations/competency-receipts.jsonl"
    runtime_relative = "scripts/query_reference.py"
    catalog = _load_required_json(root, catalog_relative, issues)
    plan = _load_required_json(root, plan_relative, issues)
    responses = _load_required_jsonl(root, responses_relative, issues)
    receipts = _load_required_jsonl(root, receipts_relative, issues)
    runtime_path = safe_package_path(root, runtime_relative)
    canonical_runtime = Path(__file__).with_name("portable_reference_runtime.py")
    if runtime_path is None or not runtime_path.is_file():
        _issue(issues, "error", "runtime_missing", runtime_relative, "Portable reference runtime is required.")
        return
    runtime_hash = sha256_file(runtime_path)
    if not canonical_runtime.is_file() or runtime_path.read_bytes() != canonical_runtime.read_bytes():
        _issue(issues, "error", "runtime_implementation_mismatch", runtime_relative, "Runtime must match the validated compiler implementation.")

    if not isinstance(catalog, dict) or catalog.get("schema_version") != CATALOG_SCHEMA or catalog.get("runtime_version") != RUNTIME_VERSION:
        _issue(issues, "error", "runtime_catalog_invalid", catalog_relative, "Unexpected runtime catalog contract.")
        return
    package = manifest.get("package")
    modules = manifest.get("modules")
    module_id = modules[0].get("module_id") if isinstance(modules, list) and modules and isinstance(modules[0], dict) else None
    if (
        not isinstance(package, dict)
        or catalog.get("package_id") != package.get("id")
        or catalog.get("module_id") != module_id
        or catalog.get("capabilities") != manifest.get("capabilities")
    ):
        _issue(issues, "error", "runtime_catalog_identity_mismatch", catalog_relative, "Catalog package, module, or capabilities differ from the manifest.")
    policy = catalog.get("policy")
    if not isinstance(policy, dict):
        _issue(issues, "error", "runtime_policy_invalid", catalog_relative, "Runtime policy is required.")
    else:
        for field in ("decision_guard_terms", "out_of_scope_terms"):
            values = policy.get(field)
            if (
                not isinstance(values, list)
                or any(not isinstance(value, str) or not value for value in values)
                or values != sorted(set(values))
            ):
                _issue(issues, "error", "runtime_policy_invalid", f"{catalog_relative}.{field}", "Terms must be unique sorted strings.")

    catalog_objects = catalog.get("objects")
    catalog_object_by_id: dict[str, dict[str, Any]] = {}
    if not isinstance(catalog_objects, list):
        _issue(issues, "error", "runtime_catalog_objects_invalid", catalog_relative, "objects must be an array.")
        catalog_objects = []
    index = _load_required_json(root, "references/knowledge/index.json", issues)
    path_by_id = {
        row.get("id"): row.get("path")
        for row in index.get("objects", [])
        if isinstance(index, dict) and isinstance(row, dict)
    } if isinstance(index, dict) else {}
    for row in catalog_objects:
        object_id = row.get("id") if isinstance(row, dict) else None
        if not isinstance(object_id, str) or object_id in catalog_object_by_id or object_id not in knowledge_objects:
            _issue(issues, "error", "runtime_catalog_object_invalid", catalog_relative, str(object_id))
            continue
        obj = knowledge_objects[object_id]
        expected = {
            "id": object_id,
            "type": obj.get("type"),
            "title": obj.get("title"),
            "path": path_by_id.get(object_id),
            "evidence_ids": sorted(obj.get("evidence_ids", [])),
            "source_ids": sorted(obj.get("source_ids", [])),
            "weighted_terms": object_weighted_terms(obj),
        }
        if row != expected:
            _issue(issues, "error", "runtime_catalog_object_mismatch", object_id, "Runtime object metadata is not a deterministic projection of its canonical object.")
        catalog_object_by_id[object_id] = row
    if set(catalog_object_by_id) != set(knowledge_objects):
        _issue(issues, "error", "runtime_catalog_coverage_mismatch", catalog_relative, "Runtime catalog must cover every canonical knowledge object exactly once.")

    conflicts = _load_required_jsonl(root, "references/conflicts.jsonl", issues)
    expected_conflicts: dict[str, dict[str, Any]] = {}
    for conflict in conflicts:
        if not isinstance(conflict, dict) or not isinstance(conflict.get("id"), str):
            continue
        expected_conflicts[conflict["id"]] = {
            "id": conflict["id"],
            "title": conflict.get("topic"),
            "status": conflict.get("status"),
            "claim_ids": sorted(
                claim.get("claim_id")
                for claim in conflict.get("claims", [])
                if isinstance(claim, dict) and isinstance(claim.get("claim_id"), str)
            ),
            "weighted_terms": conflict_weighted_terms(conflict),
        }
    actual_conflicts = {
        row.get("id"): row
        for row in catalog.get("conflicts", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    } if isinstance(catalog.get("conflicts"), list) else {}
    if actual_conflicts != expected_conflicts:
        _issue(issues, "error", "runtime_conflict_projection_mismatch", catalog_relative, "Runtime conflicts differ from canonical conflict records.")

    gaps = _load_required_jsonl(root, "references/knowledge-gaps.jsonl", issues)
    expected_gaps: dict[str, dict[str, Any]] = {}
    for gap in gaps:
        if not isinstance(gap, dict) or not isinstance(gap.get("id"), str):
            continue
        expected_gaps[gap["id"]] = {
            "id": gap["id"],
            "title": gap.get("statement"),
            "status": gap.get("status"),
            "weighted_terms": gap_weighted_terms(gap),
        }
    actual_gaps = {
        row.get("id"): row
        for row in catalog.get("gaps", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    } if isinstance(catalog.get("gaps"), list) else {}
    if actual_gaps != expected_gaps:
        _issue(issues, "error", "runtime_gap_projection_mismatch", catalog_relative, "Runtime gaps differ from canonical package gaps.")

    if not isinstance(plan, dict) or plan.get("schema_version") != PHASE3_PLAN_SCHEMA:
        _issue(issues, "error", "competency_plan_invalid", plan_relative, "Unexpected evaluation plan.")
        return
    canonical_paths = {
        "catalog": catalog_relative,
        "runtime": runtime_relative,
        "test_suite": "references/competency-tests.jsonl",
        "responses": responses_relative,
        "receipts": receipts_relative,
    }
    if any(plan.get(field) != value for field, value in canonical_paths.items()):
        _issue(issues, "error", "competency_plan_path_invalid", plan_relative, "Evaluation artifacts must use canonical package paths.")
    catalog_hash = sha256_json(catalog)
    test_suite_hash = sha256_json(tests)
    expected_run_id = "cer-" + hashlib.sha256(
        (catalog_hash + test_suite_hash + runtime_hash + EVALUATOR_VERSION).encode("utf-8")
    ).hexdigest()[:20]
    if (
        plan.get("run_id") != expected_run_id
        or plan.get("evaluator") != "deterministic-runtime"
        or plan.get("evaluator_version") != EVALUATOR_VERSION
        or plan.get("catalog_sha256") != catalog_hash
        or plan.get("runtime_sha256") != runtime_hash
        or plan.get("test_suite_sha256") != test_suite_hash
        or plan.get("test_count") != len(tests)
        or plan.get("attestation_level") != "deterministic-recomputed-not-human"
        or plan.get("created_at") != build.get("created_at")
    ):
        _issue(issues, "error", "competency_plan_stale", plan_relative, "Evaluation plan does not bind the current runtime and test suite.")

    test_by_id: dict[str, dict[str, Any]] = {}
    for test in tests:
        test_id = test.get("id") if isinstance(test, dict) else None
        if not isinstance(test_id, str) or test_id in test_by_id or test.get("schema_version") != PHASE3_TEST_SCHEMA or test.get("result") is not None:
            _issue(issues, "error", "competency_test_contract_invalid", "references/competency-tests.jsonl", str(test_id))
            continue
        test_by_id[test_id] = test

    response_by_test: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(responses):
        path = f"{responses_relative}:{index + 1}"
        test_id = row.get("test_id") if isinstance(row, dict) else None
        response = row.get("response") if isinstance(row, dict) else None
        if test_id not in test_by_id or test_id in response_by_test or not isinstance(response, dict) or response.get("schema_version") != RESPONSE_SCHEMA:
            _issue(issues, "error", "competency_response_invalid", path, str(test_id))
            continue
        test = test_by_id[test_id]
        try:
            expected_response = query_catalog(catalog, test["prompt"], limit=test["runtime"]["limit"])
        except (KeyError, TypeError, ValueError) as error:
            _issue(issues, "error", "competency_response_invalid", path, str(error))
            continue
        if response != expected_response:
            _issue(issues, "error", "competency_response_stale", path, "Stored response differs from deterministic runtime recomputation.")
        response_by_test[test_id] = row
    if set(response_by_test) != set(test_by_id):
        _issue(issues, "error", "competency_response_coverage_mismatch", responses_relative, "Every test requires exactly one deterministic response.")

    receipt_by_test: dict[str, dict[str, Any]] = {}
    for index, receipt in enumerate(receipts):
        path = f"{receipts_relative}:{index + 1}"
        test_id = receipt.get("test_id") if isinstance(receipt, dict) else None
        if test_id not in test_by_id or test_id in receipt_by_test or receipt.get("schema_version") != PHASE3_RECEIPT_SCHEMA:
            _issue(issues, "error", "competency_receipt_invalid", path, str(test_id))
            continue
        test = test_by_id[test_id]
        response_row = response_by_test.get(test_id)
        if not isinstance(response_row, dict):
            continue
        expected_checks = evaluate_response(test, response_row["response"])
        expected_receipt_id = "cr-" + hashlib.sha256(
            (expected_run_id + test_id).encode("utf-8")
        ).hexdigest()[:20]
        if (
            receipt.get("receipt_id") != expected_receipt_id
            or receipt.get("run_id") != expected_run_id
            or receipt.get("evaluator") != "deterministic-runtime"
            or receipt.get("evaluator_version") != EVALUATOR_VERSION
            or receipt.get("test_sha256") != sha256_json(test)
            or receipt.get("catalog_sha256") != catalog_hash
            or receipt.get("runtime_sha256") != runtime_hash
            or receipt.get("response_sha256") != sha256_json(response_row)
            or receipt.get("checks") != expected_checks
            or receipt.get("verdict") != ("passed" if all(check["passed"] for check in expected_checks) else "failed")
        ):
            _issue(issues, "error", "competency_receipt_stale", path, "Receipt does not match deterministic recomputation.")
        if receipt.get("verdict") != "passed":
            _issue(issues, "error", "competency_not_passed", path, "Reference-tier runtime competency failed.")
        receipt_by_test[test_id] = receipt
    if set(receipt_by_test) != set(test_by_id):
        _issue(issues, "error", "competency_receipt_coverage_mismatch", receipts_relative, "Every test requires exactly one recomputable receipt.")

    promotion = _load_required_json(root, "references/evidence/promotion-report.json", issues)
    phase2_bundle = promotion.get("review_bundle_sha256") if isinstance(promotion, dict) else None
    expected_fingerprint = sha256_json(
        {
            "phase2_review_bundle_sha256": phase2_bundle,
            "runtime_sha256": runtime_hash,
            "catalog_sha256": catalog_hash,
            "competency_suite_sha256": test_suite_hash,
        }
    )
    if build.get("input_fingerprint") != expected_fingerprint:
        _issue(issues, "error", "phase3_input_fingerprint_mismatch", "manifest.build.input_fingerprint", "Phase 3 build fingerprint is stale.")


def _validate_execution_bundle(
    root: Path,
    manifest: dict[str, Any],
    knowledge_objects: dict[str, dict[str, Any]],
    anchor_ids: set[str],
    conflicts: list[Any],
    procedure_entries: list[Any],
    issues: list[Issue],
) -> None:
    """Validate the one supported route that may declare executable=true.

    The public attestation labels are intentionally not interpreted as a
    cryptographic identity proof.  This gate verifies deterministic binding and
    makes that limitation explicit in the generated policy.
    """

    build = manifest.get("build")
    capabilities = manifest.get("capabilities")
    executable = isinstance(capabilities, dict) and capabilities.get("executable") is True
    is_execution_build = isinstance(build, dict) and build.get("compiler_version") == EXECUTION_COMPILER_VERSION
    if not executable:
        if is_execution_build:
            _issue(issues, "error", "execution_capability_missing", "manifest.capabilities.executable", "Execution build must explicitly declare executable=true.")
        return
    if not is_execution_build or build.get("compiler") != "fake-expert":
        _issue(issues, "error", "executable_upgrade_contract_missing", "manifest.build", "Executable capability is reserved for fake-expert execution bundles.")
        return
    execution_build = build.get("execution")
    if not isinstance(execution_build, dict) or set(execution_build) != {"bundle", "policy", "input_manifest_sha256", "compiler_instance"}:
        _issue(issues, "error", "execution_build_invalid", "manifest.build.execution", "Execution build binding is required.")
        return
    if execution_build.get("bundle") != "references/execution/manifest.json" or execution_build.get("policy") != "references/execution/policy.json":
        _issue(issues, "error", "execution_build_invalid", "manifest.build.execution", "Execution paths must be canonical.")
    if not isinstance(execution_build.get("input_manifest_sha256"), str) or not SHA256_RE.fullmatch(execution_build["input_manifest_sha256"]):
        _issue(issues, "error", "execution_build_invalid", "manifest.build.execution.input_manifest_sha256", "Input manifest hash is required.")
    compiler_instance = execution_build.get("compiler_instance")
    if not isinstance(compiler_instance, str) or not compiler_instance:
        _issue(issues, "error", "execution_build_invalid", "manifest.build.execution.compiler_instance", "Compiler instance is required.")

    bundle_relative = "references/execution/manifest.json"
    policy_relative = "references/execution/policy.json"
    tests_relative = "references/execution/test-suite.json"
    receipts_relative = "references/execution/receipts.jsonl"
    bundle = _load_required_json(root, bundle_relative, issues)
    policy = _load_required_json(root, policy_relative, issues)
    suite = _load_required_json(root, tests_relative, issues)
    receipts = _load_required_jsonl(root, receipts_relative, issues)
    if not isinstance(bundle, dict) or bundle.get("schema_version") != EXECUTION_BUNDLE_SCHEMA:
        _issue(issues, "error", "execution_bundle_invalid", bundle_relative, "Unexpected execution bundle schema.")
        return
    if not isinstance(policy, dict) or policy.get("schema_version") != EXECUTION_POLICY_SCHEMA:
        _issue(issues, "error", "execution_policy_invalid", policy_relative, "Unexpected execution policy schema.")
        return
    expected_policy_keys = {
        "schema_version", "execution_model", "isolation", "network", "filesystem", "subprocess",
        "allowed_imports", "timeout_seconds", "memory_limit_mib", "memory_guard", "requires_sealed_input", "requires_independent_test_author",
    }
    if (
        set(policy) != expected_policy_keys
        or policy.get("execution_model") != "formula-ast-only"
        or policy.get("isolation") != "process-resource-limits-not-kernel-sandbox"
        or policy.get("network") != "denied-by-generated-runtime-surface"
        or policy.get("filesystem") != "generated-runtime-has-no-file-api; child-file-size-limit-zero"
        or policy.get("subprocess") != "denied-by-generated-runtime-surface; child-process-limit-zero-when-supported"
        or policy.get("allowed_imports") != ["decimal", "json", "re", "sys"]
        or policy.get("memory_guard") != "bounded-ast-and-stdin-not-os-rss"
        or policy.get("requires_sealed_input") is not True
        or policy.get("requires_independent_test_author") is not True
        or not isinstance(policy.get("timeout_seconds"), int)
        or not 1 <= policy["timeout_seconds"] <= 10
        or not isinstance(policy.get("memory_limit_mib"), int)
        or not 64 <= policy["memory_limit_mib"] <= 512
    ):
        _issue(issues, "error", "execution_policy_invalid", policy_relative, "Execution policy is incomplete or overstates isolation.")
    if not isinstance(suite, dict) or suite.get("schema_version") != EXECUTION_TEST_SUITE_SCHEMA:
        _issue(issues, "error", "execution_test_suite_invalid", tests_relative, "Unexpected independent test-suite schema.")
        return
    author = suite.get("author")
    cases = suite.get("cases")
    if (
        not isinstance(author, dict)
        or set(author) != {"instance", "attestation_level"}
        or not isinstance(author.get("instance"), str)
        or not author.get("instance")
        or author.get("instance") == compiler_instance
        or author.get("attestation_level") != "host-orchestrator-recorded-not-cryptographic"
        or not isinstance(cases, list)
        or not cases
    ):
        _issue(issues, "error", "execution_test_author_invalid", tests_relative, "Independent test author and cases are required.")
        return

    input_binding = bundle.get("input")
    if (
        not isinstance(input_binding, dict)
        or set(input_binding) != {"package_id", "package_version", "manifest_sha256", "integrity_sha256"}
        or input_binding.get("manifest_sha256") != execution_build.get("input_manifest_sha256")
        or not isinstance(input_binding.get("integrity_sha256"), str)
        or not SHA256_RE.fullmatch(input_binding["integrity_sha256"])
        or bundle.get("policy") != policy_relative
        or bundle.get("policy_sha256") != sha256_json(policy)
        or bundle.get("test_suite") != tests_relative
        or bundle.get("test_suite_sha256") != sha256_json(suite)
        or bundle.get("receipts") != receipts_relative
        or bundle.get("compiler_instance") != compiler_instance
    ):
        _issue(issues, "error", "execution_bundle_binding_invalid", bundle_relative, "Bundle paths or input/test bindings are stale.")
        return

    runner = bundle.get("runner")
    runner_relative = "scripts/run_formula_sandbox.py"
    runner_path = safe_package_path(root, runner_relative)
    canonical_runner = Path(__file__).with_name("formula_sandbox_runner.py")
    if (
        not isinstance(runner, dict)
        or set(runner) != {"entrypoint", "sha256"}
        or runner.get("entrypoint") != runner_relative
        or not isinstance(runner.get("sha256"), str)
        or not SHA256_RE.fullmatch(runner["sha256"])
        or runner_path is None
        or not runner_path.is_file()
        or sha256_file(runner_path) != runner.get("sha256")
        or not canonical_runner.is_file()
        or runner_path.read_bytes() != canonical_runner.read_bytes()
    ):
        _issue(issues, "error", "execution_runner_invalid", bundle_relative, "Canonical constrained execution runner is required.")

    promotion = _load_required_json(root, "references/evidence/promotion-report.json", issues)
    promotion_rows = promotion.get("items") if isinstance(promotion, dict) else []
    records = _load_required_jsonl(root, "references/reviews/semantic-records.jsonl", issues)
    records_by_hash = {sha256_json(record): record for record in records if isinstance(record, dict)}
    review_by_object: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    if isinstance(promotion_rows, list):
        for row in promotion_rows:
            if not isinstance(row, dict) or row.get("item_kind") != "object" or row.get("verdict") != "accepted":
                continue
            object_id = row.get("promoted_id")
            record_hash = row.get("review_record_sha256")
            record = records_by_hash.get(record_hash) if isinstance(record_hash, str) else None
            if isinstance(object_id, str) and isinstance(record, dict):
                review_by_object[object_id] = (row, record)

    unresolved_claim_ids: set[str] = set()
    for conflict in conflicts:
        if isinstance(conflict, dict) and conflict.get("status") == "unresolved":
            unresolved_claim_ids.update(
                claim.get("claim_id") for claim in conflict.get("claims", [])
                if isinstance(claim, dict) and isinstance(claim.get("claim_id"), str)
            )
    formulas = bundle.get("formulas")
    if not isinstance(formulas, list) or not formulas:
        _issue(issues, "error", "execution_formula_missing", bundle_relative, "At least one formula execution record is required.")
        return
    procedure_by_id = {
        entry.get("id"): entry for entry in procedure_entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }
    formula_ids: set[str] = set()
    expected_cases_by_object: dict[str, set[str]] = {}
    expected_receipt_ids: set[str] = set()
    reviewer_instances: set[str] = set()
    for index, formula_record in enumerate(formulas):
        path = f"{bundle_relative}.formulas[{index}]"
        if not isinstance(formula_record, dict) or set(formula_record) != {
            "object_id", "procedure_id", "entrypoint", "module_sha256", "formula_ast_sha256",
            "source_evidence_ids", "review_binding_sha256", "review_item_ref", "test_case_ids", "receipt_ids",
        }:
            _issue(issues, "error", "execution_formula_invalid", path, "Formula record has an invalid shape.")
            continue
        object_id = formula_record.get("object_id")
        if not isinstance(object_id, str) or object_id in formula_ids or object_id not in knowledge_objects:
            _issue(issues, "error", "execution_formula_invalid", path, "Formula object must resolve exactly once.")
            continue
        formula_ids.add(object_id)
        obj = knowledge_objects[object_id]
        formula = obj.get("formula")
        binding = obj.get("review_binding")
        if (
            obj.get("type") != "Equation"
            or obj.get("origin") not in {"source-explicit", "source-paraphrase"}
            or obj.get("status") != "independently-reviewed-draft"
            or object_id in unresolved_claim_ids
            or not isinstance(formula, dict)
            or formula.get("representation_relation") not in {"identical", "notation-normalized"}
            or formula.get("dimension_check") not in {"consistent", "consistent_with_stated_units", "dimensionless", "length-versus-length"}
            or not isinstance(formula.get("source_notation"), str)
            or not isinstance(formula.get("normalized_notation"), str)
            or formula.get("ast_schema_version") != FORMULA_AST_SCHEMA
            or not isinstance(formula.get("ast"), dict)
            or formula.get("ast_sha256") != sha256_json(formula.get("ast"))
            or formula_record.get("formula_ast_sha256") != formula.get("ast_sha256")
            or formula_record.get("source_evidence_ids") != sorted(obj.get("evidence_ids", []))
            or set(obj.get("evidence_ids", [])) - anchor_ids
            or not isinstance(binding, dict)
            or formula_record.get("review_binding_sha256") != sha256_json(binding)
        ):
            _issue(issues, "error", "execution_formula_source_binding_invalid", path, "Formula lacks a reviewed, source-bound, conflict-free AST.")
            continue
        try:
            contract = validate_formula_ast(formula["ast"])
        except FormulaContractError as error:
            _issue(issues, "error", "execution_formula_dimension_invalid", path, str(error))
            continue
        review = review_by_object.get(object_id)
        if review is None:
            _issue(issues, "error", "execution_formula_review_missing", path, "Accepted promotion record is missing.")
        else:
            review_row, record = review
            if (
                formula_record.get("review_item_ref") != review_row.get("item_ref")
                or record.get("verdict") != "accepted"
                or record.get("evidence_support") != "full"
                or record.get("formula_ast_sha256") != formula.get("ast_sha256")
                or any(binding.get(field) != record.get(field) for field in ("item_sha256", "review_input_sha256", "reviewer_instance"))
            ):
                _issue(issues, "error", "execution_formula_review_binding_invalid", path, "Formula does not bind its accepted independent review.")
            reviewer_instances.add(record.get("reviewer_instance"))
        procedure_id = formula_record.get("procedure_id")
        procedure_entry = procedure_by_id.get(procedure_id)
        procedure = _load_required_json(root, procedure_entry.get("path"), issues) if isinstance(procedure_entry, dict) and isinstance(procedure_entry.get("path"), str) else None
        entrypoint = formula_record.get("entrypoint")
        code_path = safe_package_path(root, entrypoint) if isinstance(entrypoint, str) else None
        if (
            not isinstance(procedure, dict)
            or procedure.get("source_object_id") != object_id
            or procedure.get("formula_ast_sha256") != formula.get("ast_sha256")
            or procedure.get("review_binding") != binding
            or procedure.get("evidence_ids") != sorted(obj.get("evidence_ids", []))
            or procedure.get("formula_module") != entrypoint
            or procedure.get("execution", {}).get("entrypoint") != runner_relative
            or procedure.get("execution", {}).get("arguments") != ["--formula", object_id]
            or not isinstance(code_path, Path)
            or not code_path.is_file()
            or not isinstance(formula_record.get("module_sha256"), str)
            or sha256_file(code_path) != formula_record.get("module_sha256")
        ):
            _issue(issues, "error", "execution_procedure_binding_invalid", path, "Generated procedure or entrypoint is stale.")
        elif code_path is not None:
            try:
                verify_generated_module(code_path.read_text(encoding="utf-8"))
            except (OSError, FormulaContractError) as error:
                _issue(issues, "error", "execution_generated_code_invalid", entrypoint, str(error))
        case_ids = formula_record.get("test_case_ids")
        receipt_ids = formula_record.get("receipt_ids")
        if not isinstance(case_ids, list) or not case_ids or case_ids != sorted(set(case_ids)) or not isinstance(receipt_ids, list) or not receipt_ids or receipt_ids != sorted(set(receipt_ids)):
            _issue(issues, "error", "execution_test_binding_invalid", path, "Formula requires unique test and receipt IDs.")
        else:
            expected_cases_by_object[object_id] = set(case_ids)
            expected_receipt_ids.update(receipt_ids)

    if author.get("instance") in reviewer_instances:
        _issue(issues, "error", "execution_test_author_not_independent", tests_relative, "Test author must differ from every formula reviewer.")
    cases_by_id: dict[str, dict[str, Any]] = {}
    for index, case in enumerate(cases):
        path = f"{tests_relative}.cases[{index}]"
        case_id = case.get("id") if isinstance(case, dict) else None
        object_id = case.get("object_id") if isinstance(case, dict) else None
        if not isinstance(case, dict) or set(case) != {"id", "object_id", "inputs", "expected", "tolerance"} or not isinstance(case_id, str) or case_id in cases_by_id or object_id not in formula_ids:
            _issue(issues, "error", "execution_test_case_invalid", path, "Test case must bind one generated formula.")
            continue
        cases_by_id[case_id] = case
        if case_id not in expected_cases_by_object.get(object_id, set()):
            _issue(issues, "error", "execution_test_binding_invalid", path, "Case is absent from formula execution record.")
            continue
        formula = knowledge_objects[object_id].get("formula")
        try:
            contract = validate_formula_ast(formula["ast"])
            actual = evaluate_formula_ast(contract, case.get("inputs"))
            expected = case.get("expected")
            tolerance = case.get("tolerance")
            if not isinstance(expected, dict) or not isinstance(tolerance, dict) or set(expected) != {"value", "unit"} or set(tolerance) != {"absolute", "relative"} or expected.get("unit") != actual.get("unit"):
                raise FormulaContractError("expected_shape_invalid")
            actual_value = Decimal(actual["value"])
            expected_value = Decimal(str(expected["value"]))
            absolute = Decimal(str(tolerance["absolute"]))
            relative = Decimal(str(tolerance["relative"]))
            if not all(value.is_finite() for value in (actual_value, expected_value)) or not all(value.is_finite() and value >= 0 for value in (absolute, relative)) or (absolute == 0 and relative == 0) or abs(actual_value - expected_value) > max(absolute, abs(expected_value) * relative):
                raise FormulaContractError("expected_value_mismatch")
        except (FormulaContractError, InvalidOperation, ValueError, KeyError) as error:
            _issue(issues, "error", "execution_independent_test_invalid", path, str(error))
    expected_case_ids = set().union(*expected_cases_by_object.values()) if expected_cases_by_object else set()
    if set(cases_by_id) != expected_case_ids:
        _issue(issues, "error", "execution_test_coverage_mismatch", tests_relative, "Cases must exactly cover formula execution records.")
    for object_id, case_ids in expected_cases_by_object.items():
        if len(case_ids) < 2:
            _issue(issues, "error", "execution_test_coverage_insufficient", object_id, "At least two independent cases are required per formula.")

    receipt_by_case: dict[str, dict[str, Any]] = {}
    for index, receipt in enumerate(receipts):
        path = f"{receipts_relative}:{index + 1}"
        if not isinstance(receipt, dict):
            _issue(issues, "error", "execution_receipt_invalid", path, "Expected an object.")
            continue
        case_id = receipt.get("case_id")
        object_id = receipt.get("object_id")
        if case_id not in cases_by_id or case_id in receipt_by_case or object_id != cases_by_id[case_id].get("object_id") or receipt.get("schema_version") != EXECUTION_RECEIPT_SCHEMA:
            _issue(issues, "error", "execution_receipt_invalid", path, "Receipt must bind exactly one known case.")
            continue
        record = next((row for row in formulas if isinstance(row, dict) and row.get("object_id") == object_id), None)
        expected_module_hash = record.get("module_sha256") if isinstance(record, dict) else None
        expected_receipt_id = "xer-" + hashlib.sha256((object_id + case_id + str(expected_module_hash)).encode("utf-8")).hexdigest()[:20]
        if (
            receipt.get("receipt_id") != expected_receipt_id
            or receipt.get("module_sha256") != expected_module_hash
            or receipt.get("test_case_sha256") != sha256_json(cases_by_id[case_id])
            or receipt.get("policy_sha256") != sha256_json(policy)
            or receipt.get("isolation") != "process-resource-limits-not-kernel-sandbox"
            or receipt.get("timeout_seconds") != policy.get("timeout_seconds")
            or receipt.get("memory_limit_mib") != policy.get("memory_limit_mib")
            or receipt.get("verdict") != "passed"
            or receipt.get("issue_code") is not None
            or not isinstance(receipt.get("output_sha256"), str)
            or not SHA256_RE.fullmatch(receipt["output_sha256"])
        ):
            _issue(issues, "error", "execution_receipt_stale", path, "Receipt does not bind the current module, policy, and test case.")
        receipt_by_case[case_id] = receipt
    if set(receipt_by_case) != set(cases_by_id) or {row.get("receipt_id") for row in receipts if isinstance(row, dict)} != expected_receipt_ids:
        _issue(issues, "error", "execution_receipt_coverage_mismatch", receipts_relative, "Every test requires exactly one listed passing receipt.")


def validate_expert_skill(root: Path, level: str = "structure") -> list[Issue]:
    if level not in {"structure", "publish"}:
        raise ValueError("level must be 'structure' or 'publish'")

    root = root.resolve()
    issues: list[Issue] = []
    if not root.is_dir():
        return [Issue("error", "missing_package", str(root), "Package directory does not exist.")]

    _validate_skill_frontmatter(root, issues)
    manifest = _load_required_json(root, "manifest.json", issues)
    if not isinstance(manifest, dict):
        return issues

    if manifest.get("schema_version") != SCHEMA_ID:
        _issue(issues, "error", "schema_version", "manifest.json", f"Expected {SCHEMA_ID}.")

    package = _required_dict(manifest, "package", issues, "manifest")
    package_id = package.get("id")
    if not isinstance(package_id, str) or not SLUG_RE.fullmatch(package_id):
        _issue(issues, "error", "invalid_package_id", "manifest.package.id", "Expected a hyphen-case slug.")
    if package.get("status") not in PACKAGE_STATUSES:
        _issue(issues, "error", "invalid_status", "manifest.package.status", "Unknown lifecycle status.")
    if level == "publish" and package.get("status") != "ready":
        _issue(issues, "error", "not_ready", "manifest.package.status", "Publication requires status ready.")
    if not isinstance(package.get("version"), str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", package.get("version", "")
    ):
        _issue(issues, "error", "invalid_package_version", "manifest.package.version", "Expected a semantic version.")

    distribution = _required_dict(manifest, "distribution", issues, "manifest")
    source_mode = distribution.get("source_mode")
    if source_mode not in SOURCE_MODES:
        _issue(issues, "error", "invalid_source_mode", "manifest.distribution.source_mode", "Unknown source mode.")
    elif distribution.get("verification") != SOURCE_MODES[source_mode]:
        _issue(issues, "error", "verification_mismatch", "manifest.distribution.verification", "Verification label does not match source mode.")

    source_rows = _required_list(manifest, "sources", issues, "manifest")
    declared_sources: dict[str, dict[str, Any]] = {}
    for index, source in enumerate(source_rows):
        source_path = f"manifest.sources[{index}]"
        if not isinstance(source, dict):
            _issue(issues, "error", "invalid_source", source_path, "Expected an object.")
            continue
        source_id = source.get("source_id")
        source_hash = source.get("sha256")
        if not isinstance(source_id, str) or not source_id:
            _issue(issues, "error", "missing_source_id", source_path, "source_id is required.")
            continue
        if source_id in declared_sources:
            _issue(issues, "error", "duplicate_source_id", source_path, source_id)
        declared_sources[source_id] = source
        if not isinstance(source_hash, str) or not SHA256_RE.fullmatch(source_hash):
            _issue(issues, "error", "invalid_source_hash", f"{source_path}.sha256", "Expected lowercase SHA-256.")
        if source_mode == "source-required" and source.get("included_path"):
            _issue(issues, "error", "source_mode_violation", source_path, "source-required must not claim an included source.")
        declared_scope = source.get("scope")
        if declared_scope is not None:
            if not isinstance(declared_scope, dict):
                _issue(issues, "error", "invalid_source_scope", source_path, "scope must be an object.")
            else:
                physical_pages = declared_scope.get("physical_pages")
                printed_labels = declared_scope.get("printed_page_labels")
                if (
                    not isinstance(physical_pages, list)
                    or len(physical_pages) != 2
                    or any(not isinstance(page, int) or page < 1 for page in physical_pages)
                    or physical_pages[0] > physical_pages[1]
                    or not isinstance(printed_labels, list)
                    or not printed_labels
                    or any(not isinstance(label, str) or not label for label in printed_labels)
                ):
                    _issue(issues, "error", "invalid_source_scope", source_path, "Expected ordered physical pages and printed labels.")
        if source_mode == "full":
            included_path = source.get("included_path")
            if not isinstance(included_path, str):
                _issue(issues, "error", "missing_full_source", source_path, "Full mode requires included_path.")
            else:
                included = safe_package_path(root, included_path)
                if included is None or not included.is_file():
                    _issue(issues, "error", "missing_full_source", included_path, "Included source file is missing.")
                elif isinstance(source_hash, str) and sha256_file(included) != source_hash:
                    _issue(issues, "error", "full_source_hash_mismatch", included_path, "Included source does not match the declared SHA-256.")
    if not declared_sources:
        _issue(issues, "error", "no_sources", "manifest.sources", "At least one source is required.")

    source_manifest = _load_required_json(root, "references/evidence/source-manifest.json", issues)
    if isinstance(source_manifest, dict):
        mirrored = source_manifest.get("sources")
        if not isinstance(mirrored, list):
            _issue(issues, "error", "invalid_source_manifest", "references/evidence/source-manifest.json", "sources must be an array.")
        else:
            mirrored_ids = {row.get("source_id") for row in mirrored if isinstance(row, dict)}
            if mirrored_ids != set(declared_sources):
                _issue(issues, "error", "source_manifest_mismatch", "references/evidence/source-manifest.json", "Source IDs do not match manifest.json.")
            for row in mirrored:
                if not isinstance(row, dict) or row.get("source_id") not in declared_sources:
                    continue
                declared = declared_sources[row["source_id"]]
                if row.get("sha256") != declared.get("sha256"):
                    _issue(issues, "error", "source_manifest_hash_mismatch", "references/evidence/source-manifest.json", f"Hash differs for {row['source_id']}.")

    build = _required_dict(manifest, "build", issues, "manifest")
    if build.get("schema_version") != SCHEMA_ID:
        _issue(issues, "error", "build_schema_version", "manifest.build.schema_version", f"Expected {SCHEMA_ID}.")
    if not isinstance(build.get("compiler"), str) or not build.get("compiler"):
        _issue(issues, "error", "missing_compiler", "manifest.build.compiler", "Compiler identity is required.")
    if not isinstance(build.get("compiler_version"), str) or not build.get("compiler_version"):
        _issue(issues, "error", "missing_compiler_version", "manifest.build.compiler_version", "Compiler version is required.")
    fingerprint = build.get("input_fingerprint")
    if not isinstance(fingerprint, str) or not SHA256_RE.fullmatch(fingerprint):
        _issue(issues, "error", "invalid_input_fingerprint", "manifest.build.input_fingerprint", "Expected lowercase SHA-256.")
    for field in ("prompt_versions", "model_ids"):
        if not isinstance(build.get(field), list):
            _issue(issues, "error", "invalid_build_array", f"manifest.build.{field}", "Expected an array.")

    modules = _required_list(manifest, "modules", issues, "manifest")
    exports: set[str] = set()
    for index, module in enumerate(modules):
        module_path = f"manifest.modules[{index}]"
        if not isinstance(module, dict):
            _issue(issues, "error", "invalid_module", module_path, "Expected an object.")
            continue
        module_id = module.get("module_id")
        if not isinstance(module_id, str) or not SLUG_RE.fullmatch(module_id):
            _issue(issues, "error", "invalid_module_id", module_path, "Expected a hyphen-case module ID.")
        index_path = module.get("knowledge_index")
        if not isinstance(index_path, str):
            _issue(issues, "error", "missing_knowledge_index", module_path, "knowledge_index is required.")
        elif index_path != "references/knowledge/index.json":
            _issue(issues, "error", "unsupported_knowledge_index", module_path, "ABI v0.1 uses the canonical knowledge index path.")
        module_exports = module.get("exports")
        if not isinstance(module_exports, list):
            _issue(issues, "error", "invalid_exports", module_path, "exports must be an array.")
        else:
            exports.update(item for item in module_exports if isinstance(item, str))
            if level == "publish" and not module_exports:
                _issue(issues, "error", "empty_exports", module_path, "Published modules must export knowledge.")
        dependencies = module.get("dependencies")
        if not isinstance(dependencies, list):
            _issue(issues, "error", "invalid_dependencies", module_path, "dependencies must be an array.")
        else:
            for dependency in dependencies:
                if not isinstance(dependency, dict) or not isinstance(dependency.get("module_id"), str) or not isinstance(dependency.get("version_range"), str):
                    _issue(issues, "error", "invalid_dependency", module_path, "Each dependency requires module_id and version_range.")
    if not modules:
        _issue(issues, "error", "no_modules", "manifest.modules", "At least one module is required.")

    knowledge_index = _load_required_json(root, "references/knowledge/index.json", issues)
    knowledge_objects: dict[str, dict[str, Any]] = {}
    object_evidence: dict[str, set[str]] = {}
    if isinstance(knowledge_index, dict):
        if knowledge_index.get("schema_version") != SCHEMA_ID:
            _issue(issues, "error", "knowledge_index_schema", "references/knowledge/index.json", f"Expected {SCHEMA_ID}.")
        entries = knowledge_index.get("objects")
        if not isinstance(entries, list):
            _issue(issues, "error", "invalid_knowledge_index", "references/knowledge/index.json", "objects must be an array.")
            entries = []
        for index, entry in enumerate(entries):
            entry_path = f"references/knowledge/index.json.objects[{index}]"
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not isinstance(entry.get("path"), str):
                _issue(issues, "error", "invalid_object_entry", entry_path, "Expected id and path strings.")
                continue
            object_id = entry["id"]
            if not entry["path"].startswith("references/knowledge/objects/"):
                _issue(issues, "error", "object_path_scope", entry_path, "Object files must remain under references/knowledge/objects/.")
            obj = _load_required_json(root, entry["path"], issues)
            if not isinstance(obj, dict):
                continue
            if obj.get("id") != object_id:
                _issue(issues, "error", "object_id_mismatch", entry["path"], "Index and object IDs differ.")
            if object_id in knowledge_objects:
                _issue(issues, "error", "duplicate_object_id", entry_path, object_id)
            knowledge_objects[object_id] = obj
            if obj.get("schema_version") != SCHEMA_ID:
                _issue(issues, "error", "object_schema_version", entry["path"], f"Expected {SCHEMA_ID}.")
            if obj.get("type") not in KNOWLEDGE_TYPES:
                _issue(issues, "error", "invalid_object_type", entry["path"], f"Unknown type {obj.get('type')!r}.")
            for field in ("title", "statement"):
                if not isinstance(obj.get(field), str) or not obj.get(field):
                    _issue(issues, "error", "missing_object_field", entry["path"], f"{field} is required.")
            for field in ("assumptions", "valid_when", "fails_when"):
                if not isinstance(obj.get(field), list) or any(not isinstance(item, str) for item in obj.get(field, [])):
                    _issue(issues, "error", "invalid_object_array", entry["path"], f"{field} must be an array of strings.")
            if "origin" in obj and obj.get("origin") not in {
                "source-explicit",
                "source-paraphrase",
                "compiler-derived",
            }:
                _issue(issues, "error", "invalid_object_origin", entry["path"], "Unknown proposal origin.")
            applicability = obj.get("applicability")
            if applicability is not None:
                if not isinstance(applicability, dict):
                    _issue(issues, "error", "invalid_applicability", entry["path"], "applicability must be an object.")
                else:
                    pages = applicability.get("physical_pages")
                    excluded = applicability.get("excluded_conclusions")
                    if not isinstance(pages, list) or len(pages) != 2 or any(not isinstance(page, int) for page in pages) or pages[0] > pages[1]:
                        _issue(issues, "error", "invalid_applicability", entry["path"], "physical_pages must be an ordered pair.")
                    if not isinstance(excluded, list) or any(not isinstance(value, str) for value in excluded):
                        _issue(issues, "error", "invalid_applicability", entry["path"], "excluded_conclusions must be strings.")
            review_binding = obj.get("review_binding")
            if review_binding is not None:
                if not isinstance(review_binding, dict) or review_binding.get("attestation_level") != "host-orchestrator-recorded-not-cryptographic":
                    _issue(issues, "error", "invalid_review_binding", entry["path"], "Review attestation level is missing or overstated.")
                else:
                    if not isinstance(review_binding.get("reviewer_instance"), str) or not review_binding.get("reviewer_instance"):
                        _issue(issues, "error", "invalid_review_binding", entry["path"], "reviewer_instance is required.")
                    for hash_field in ("item_sha256", "review_input_sha256", "review_bundle_sha256"):
                        value = review_binding.get(hash_field)
                        if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
                            _issue(issues, "error", "invalid_review_binding", entry["path"], f"{hash_field} must be SHA-256.")
            confidence = obj.get("confidence")
            if not isinstance(confidence, dict):
                _issue(issues, "error", "invalid_confidence", entry["path"], "confidence is required.")
            else:
                for field in ("extraction", "interpretation"):
                    value = confidence.get(field)
                    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
                        _issue(issues, "error", "invalid_confidence", entry["path"], f"confidence.{field} must be between 0 and 1.")
            evidence_ids = obj.get("evidence_ids")
            if not isinstance(evidence_ids, list) or any(not isinstance(item, str) for item in evidence_ids):
                _issue(issues, "error", "invalid_evidence_ids", entry["path"], "evidence_ids must be an array of strings.")
                evidence_ids = []
            object_evidence[object_id] = set(evidence_ids)
            source_ids = obj.get("source_ids")
            if not isinstance(source_ids, list) or not source_ids:
                _issue(issues, "error", "missing_object_sources", entry["path"], "source_ids must be non-empty.")
            else:
                for source_id in source_ids:
                    if source_id not in declared_sources:
                        _issue(issues, "error", "unknown_object_source", entry["path"], str(source_id))
            if level == "publish" and not evidence_ids:
                _issue(issues, "error", "unanchored_object", entry["path"], "Published objects require evidence.")

    if exports - set(knowledge_objects):
        _issue(issues, "error", "unknown_export", "manifest.modules", f"Unknown exports: {sorted(exports - set(knowledge_objects))}.")
    if level == "publish" and not knowledge_objects:
        _issue(issues, "error", "no_knowledge", "references/knowledge/index.json", "Published packages require knowledge objects.")

    anchors = _load_required_jsonl(root, "references/evidence/anchors.jsonl", issues)
    anchor_ids: set[str] = set()
    anchor_supports: dict[str, set[str]] = {}
    for index, anchor in enumerate(anchors):
        anchor_path = f"references/evidence/anchors.jsonl:{index + 1}"
        if not isinstance(anchor, dict):
            _issue(issues, "error", "invalid_anchor", anchor_path, "Expected an object.")
            continue
        anchor_id = anchor.get("id")
        if not isinstance(anchor_id, str) or not anchor_id:
            _issue(issues, "error", "missing_anchor_id", anchor_path, "id is required.")
            continue
        if anchor_id in anchor_ids:
            _issue(issues, "error", "duplicate_anchor_id", anchor_path, anchor_id)
        anchor_ids.add(anchor_id)
        if anchor.get("source_id") not in declared_sources:
            _issue(issues, "error", "unknown_anchor_source", anchor_path, str(anchor.get("source_id")))
        pages = anchor.get("pages")
        if not isinstance(pages, list) or not pages or any(not isinstance(page, int) or page < 1 for page in pages):
            _issue(issues, "error", "invalid_anchor_pages", anchor_path, "pages must contain positive PDF page indices.")
        else:
            declared_source = declared_sources.get(anchor.get("source_id"))
            declared_scope = declared_source.get("scope") if isinstance(declared_source, dict) else None
            scope_pages = declared_scope.get("physical_pages") if isinstance(declared_scope, dict) else None
            if (
                isinstance(scope_pages, list)
                and len(scope_pages) == 2
                and any(page < scope_pages[0] or page > scope_pages[1] for page in pages)
            ):
                _issue(issues, "error", "anchor_scope_escape", anchor_path, "Anchor pages escape the declared source scope.")
        if not anchor.get("segment_id") or not isinstance(anchor.get("locator"), dict):
            _issue(issues, "error", "incomplete_anchor_locator", anchor_path, "segment_id and locator are required.")
        excerpt_hash = anchor.get("excerpt_sha256")
        if not isinstance(excerpt_hash, str) or not SHA256_RE.fullmatch(excerpt_hash):
            _issue(issues, "error", "invalid_evidence_hash", anchor_path, "excerpt_sha256 is required.")
        text_spans = anchor.get("text_spans")
        if text_spans is not None:
            if not isinstance(text_spans, list) or not text_spans:
                _issue(issues, "error", "invalid_evidence_spans", anchor_path, "text_spans must be a non-empty array.")
            else:
                anchor_pages = set(pages) if isinstance(pages, list) else set()
                for span_index, span in enumerate(text_spans):
                    span_path = f"{anchor_path}.text_spans[{span_index}]"
                    if not isinstance(span, dict):
                        _issue(issues, "error", "invalid_evidence_span", span_path, "Expected an object.")
                        continue
                    page = span.get("page")
                    start = span.get("start")
                    end = span.get("end")
                    if page not in anchor_pages:
                        _issue(issues, "error", "evidence_span_page_mismatch", span_path, "Span page must be listed in anchor.pages.")
                    if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end:
                        _issue(issues, "error", "invalid_evidence_span", span_path, "Expected a valid [start,end) interval.")
                    for hash_field in ("page_text_sha256", "span_sha256"):
                        value = span.get(hash_field)
                        if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
                            _issue(issues, "error", "invalid_evidence_span_hash", f"{span_path}.{hash_field}", "Expected lowercase SHA-256.")
        _validate_scan_locator_v2(anchor, anchor_path, declared_sources, issues)
        if source_mode == "evidence-pack" and level == "publish":
            asset_path = anchor.get("asset_path")
            if not isinstance(asset_path, str):
                _issue(issues, "error", "missing_evidence_asset", anchor_path, "Evidence-pack anchors require asset_path.")
            else:
                asset = safe_package_path(root, asset_path)
                if asset is None or not asset.is_file():
                    _issue(issues, "error", "missing_evidence_asset", asset_path, "Evidence asset is missing.")
                elif isinstance(excerpt_hash, str) and sha256_file(asset) != excerpt_hash:
                    _issue(issues, "error", "evidence_asset_hash_mismatch", asset_path, "Evidence asset does not match excerpt_sha256.")
        supports = anchor.get("supports")
        if not isinstance(supports, list) or any(not isinstance(item, str) for item in supports):
            _issue(issues, "error", "invalid_anchor_supports", anchor_path, "supports must be an array of strings.")
            supports = []
        anchor_supports[anchor_id] = set(supports)
        unknown = set(supports) - set(knowledge_objects)
        if unknown:
            _issue(issues, "error", "unknown_supported_object", anchor_path, f"Unknown objects: {sorted(unknown)}.")

    for object_id, evidence_ids in object_evidence.items():
        missing = evidence_ids - anchor_ids
        if missing:
            _issue(issues, "error", "unknown_evidence", object_id, f"Unknown anchors: {sorted(missing)}.")
        for anchor_id in evidence_ids & anchor_ids:
            if object_id not in anchor_supports.get(anchor_id, set()):
                _issue(issues, "error", "non_bidirectional_evidence", object_id, f"Anchor {anchor_id} does not support this object.")
    for anchor_id, supported_ids in anchor_supports.items():
        for object_id in supported_ids & set(knowledge_objects):
            if anchor_id not in object_evidence.get(object_id, set()):
                _issue(
                    issues,
                    "error",
                    "non_bidirectional_evidence",
                    anchor_id,
                    f"Object {object_id} does not cite this anchor.",
                )

    conflicts_path = root / "references/conflicts.jsonl"
    conflicts: list[Any] = []
    if conflicts_path.is_file():
        conflicts = _load_required_jsonl(root, "references/conflicts.jsonl", issues)
        for index, conflict in enumerate(conflicts):
            conflict_path = f"references/conflicts.jsonl:{index + 1}"
            if not isinstance(conflict, dict) or not isinstance(conflict.get("id"), str) or not conflict.get("id"):
                _issue(issues, "error", "invalid_conflict", conflict_path, "A conflict ID is required.")
                continue
            if not isinstance(conflict.get("topic"), str) or not conflict.get("topic"):
                _issue(issues, "error", "invalid_conflict_topic", conflict_path, "A conflict topic is required.")
            claims = conflict.get("claims")
            if not isinstance(claims, list) or len(claims) < 2:
                _issue(issues, "error", "insufficient_conflict_claims", conflict_path, "At least two claims are required.")
                claims = []
            for claim in claims:
                if not isinstance(claim, dict) or not isinstance(claim.get("statement"), str) or not claim.get("statement"):
                    _issue(issues, "error", "invalid_conflict_claim", conflict_path, "Each claim requires a statement.")
                    continue
                evidence_ids = claim.get("evidence_ids")
                if not isinstance(evidence_ids, list) or not evidence_ids:
                    _issue(issues, "error", "unanchored_conflict_claim", conflict_path, "Each claim requires evidence IDs.")
                elif set(evidence_ids) - anchor_ids:
                    _issue(issues, "error", "unknown_conflict_evidence", conflict_path, f"Unknown anchors: {sorted(set(evidence_ids) - anchor_ids)}.")
            if conflict.get("status") not in {"unresolved", "mitigated", "resolved"}:
                _issue(issues, "error", "invalid_conflict_status", conflict_path, "Unknown conflict status.")
            if not isinstance(conflict.get("requires_review"), bool):
                _issue(issues, "error", "missing_conflict_review_policy", conflict_path, "requires_review must be explicit.")
            if conflict.get("status") in {"mitigated", "resolved"} and not isinstance(conflict.get("resolution"), dict):
                _issue(issues, "error", "missing_conflict_resolution", conflict_path, "Mitigated/resolved conflicts require a resolution record.")

    relations = _load_required_jsonl(root, "references/knowledge/relations.jsonl", issues)
    for index, relation in enumerate(relations):
        relation_path = f"references/knowledge/relations.jsonl:{index + 1}"
        if not isinstance(relation, dict):
            _issue(issues, "error", "invalid_relation", relation_path, "Expected an object.")
            continue
        source_object = relation.get("source_id")
        target_object = relation.get("target_id")
        if source_object not in knowledge_objects or target_object not in knowledge_objects:
            _issue(issues, "error", "unknown_relation_object", relation_path, "source_id and target_id must resolve to knowledge objects.")
        if relation.get("type") not in RELATION_TYPES:
            _issue(issues, "error", "invalid_relation_type", relation_path, f"Unknown type {relation.get('type')!r}.")

    tests = _load_required_jsonl(root, "references/competency-tests.jsonl", issues)
    categories: set[str] = set()
    competency_ids: set[str] = set()
    is_phase3 = build.get("compiler_version") in {PHASE3_COMPILER_VERSION, EXECUTION_COMPILER_VERSION}
    for index, test in enumerate(tests):
        test_path = f"references/competency-tests.jsonl:{index + 1}"
        if not isinstance(test, dict):
            _issue(issues, "error", "invalid_competency_test", test_path, "Expected an object.")
            continue
        test_id = test.get("id")
        if not isinstance(test_id, str) or not test_id or test_id in competency_ids:
            _issue(issues, "error", "invalid_competency_test_id", test_path, str(test_id))
        else:
            competency_ids.add(test_id)
        category = test.get("category")
        if isinstance(category, str):
            categories.add(category)
        if category not in REQUIRED_COMPETENCY_CATEGORIES:
            _issue(issues, "error", "invalid_competency_category", test_path, str(category))
        if not test.get("id") or not test.get("prompt") or not isinstance(test.get("expected"), dict):
            _issue(issues, "error", "incomplete_competency_test", test_path, "id, prompt, and expected are required.")
        expected = test.get("expected")
        if isinstance(expected, dict):
            behavior = expected.get("behavior")
            required_evidence = expected.get("required_evidence_ids")
            if behavior not in {"answer", "abstain", "escalate"}:
                _issue(issues, "error", "invalid_expected_behavior", test_path, "Unknown expected behavior.")
            if not isinstance(required_evidence, list) or any(not isinstance(item, str) for item in required_evidence):
                _issue(issues, "error", "invalid_test_evidence", test_path, "required_evidence_ids must be an array of strings.")
                required_evidence = []
            unknown_test_evidence = set(required_evidence) - anchor_ids
            if unknown_test_evidence:
                _issue(issues, "error", "unknown_test_evidence", test_path, f"Unknown anchors: {sorted(unknown_test_evidence)}.")
            if level == "publish" and behavior == "answer" and not required_evidence:
                _issue(issues, "error", "uncited_competency_answer", test_path, "In-scope answer tests require evidence IDs.")
            if is_phase3:
                for field, known in (
                    ("required_object_ids", set(knowledge_objects)),
                    ("forbidden_object_ids", set(knowledge_objects)),
                    ("required_conflict_ids", {
                        conflict.get("id") for conflict in conflicts
                        if isinstance(conflict, dict) and isinstance(conflict.get("id"), str)
                    }),
                    ("required_reason_codes", None),
                ):
                    values = expected.get(field)
                    if (
                        not isinstance(values, list)
                        or any(not isinstance(value, str) for value in values)
                        or values != sorted(set(values))
                        or (known is not None and set(values) - known)
                    ):
                        _issue(issues, "error", "invalid_competency_reference", f"{test_path}.{field}", "Expected unique sorted known identifiers.")
                runtime = test.get("runtime")
                if not isinstance(runtime, dict) or not isinstance(runtime.get("limit"), int) or not 1 <= runtime["limit"] <= 25:
                    _issue(issues, "error", "invalid_competency_runtime", test_path, "Phase 3 tests require a bounded runtime limit.")
                if test.get("schema_version") != PHASE3_TEST_SCHEMA or test.get("result") is not None:
                    _issue(issues, "error", "competency_test_contract_invalid", test_path, "Phase 3 tests use v0.2 and may not self-declare results.")
                if category == "decision" and behavior == "answer":
                    _issue(issues, "error", "capability_premature", test_path, "Reference-tier decision tests must abstain or escalate.")
                if category == "out-of-scope" and behavior != "abstain":
                    _issue(issues, "error", "out_of_scope_behavior_invalid", test_path, "Out-of-scope tests must abstain.")
        if level == "publish" and not is_phase3:
            result = test.get("result")
            if not isinstance(result, dict) or result.get("status") != "passed":
                _issue(issues, "error", "competency_not_passed", test_path, "A recorded passing result is required.")
            elif result.get("evaluator") not in {"human", "independent-agent", "deterministic"}:
                _issue(issues, "error", "invalid_evaluator", test_path, "Use a human, independent-agent, or deterministic evaluator.")
            elif not isinstance(result.get("run_id"), str) or not result.get("run_id"):
                _issue(issues, "error", "missing_test_run", test_path, "Passing results require a run_id.")
    if level == "publish":
        required_categories = set(REQUIRED_COMPETENCY_CATEGORIES)
        if not any(
            object_has_equation(obj) for obj in knowledge_objects.values()
        ):
            required_categories.discard("equation")
        missing_categories = required_categories - categories
        if missing_categories:
            _issue(issues, "error", "missing_competency_categories", "references/competency-tests.jsonl", f"Missing: {sorted(missing_categories)}.")

    _validate_phase3_runtime(root, manifest, knowledge_objects, anchor_ids, tests, issues)

    capabilities = _required_dict(manifest, "capabilities", issues, "manifest")
    for capability in ("reference", "decision_support", "executable"):
        if not isinstance(capabilities.get(capability), bool):
            _issue(issues, "error", "invalid_capability", f"manifest.capabilities.{capability}", "Expected true or false.")
    procedures = _load_required_json(root, "references/procedures/index.json", issues)
    decisions = _load_required_json(root, "references/decisions/index.json", issues)
    procedure_entries = procedures.get("procedures", []) if isinstance(procedures, dict) else []
    decision_entries = decisions.get("decisions", []) if isinstance(decisions, dict) else []
    if not isinstance(procedure_entries, list):
        _issue(issues, "error", "invalid_procedure_index", "references/procedures/index.json", "procedures must be an array.")
        procedure_entries = []
    if not isinstance(decision_entries, list):
        _issue(issues, "error", "invalid_decision_index", "references/decisions/index.json", "decisions must be an array.")
        decision_entries = []

    for entry_index, entry in enumerate(procedure_entries):
        entry_path = f"references/procedures/index.json.procedures[{entry_index}]"
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not isinstance(entry.get("path"), str):
            _issue(issues, "error", "invalid_procedure_entry", entry_path, "Expected id and path strings.")
            continue
        procedure = _load_required_json(root, entry["path"], issues)
        if not isinstance(procedure, dict):
            continue
        if procedure.get("id") != entry["id"]:
            _issue(issues, "error", "procedure_id_mismatch", entry["path"], "Index and procedure IDs differ.")
        evidence_ids = procedure.get("evidence_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            _issue(issues, "error", "unanchored_procedure", entry["path"], "Procedure evidence_ids must be non-empty.")
        elif set(evidence_ids) - anchor_ids:
            _issue(issues, "error", "unknown_procedure_evidence", entry["path"], f"Unknown anchors: {sorted(set(evidence_ids) - anchor_ids)}.")
        execution = procedure.get("execution")
        if not isinstance(execution, dict):
            _issue(issues, "error", "missing_execution_contract", entry["path"], "execution is required.")
            continue
        if execution.get("risk_tier") not in {"read-only", "reversible-write", "high-impact"}:
            _issue(issues, "error", "invalid_risk_tier", entry["path"], "Unknown execution risk tier.")
        if not isinstance(execution.get("requires_human_review"), bool):
            _issue(issues, "error", "missing_review_policy", entry["path"], "requires_human_review must be explicit.")
        entrypoint = execution.get("entrypoint")
        if not isinstance(entrypoint, str):
            _issue(issues, "error", "missing_entrypoint", entry["path"], "A deterministic entrypoint is required.")
        else:
            executable = safe_package_path(root, entrypoint)
            if executable is None or not executable.is_file() or not entrypoint.startswith("scripts/"):
                _issue(issues, "error", "invalid_entrypoint", entry["path"], "Entrypoint must resolve under scripts/.")

    for entry_index, entry in enumerate(decision_entries):
        entry_path = f"references/decisions/index.json.decisions[{entry_index}]"
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not isinstance(entry.get("path"), str):
            _issue(issues, "error", "invalid_decision_entry", entry_path, "Expected id and path strings.")
            continue
        decision = _load_required_json(root, entry["path"], issues)
        if not isinstance(decision, dict):
            continue
        if decision.get("id") != entry["id"]:
            _issue(issues, "error", "decision_id_mismatch", entry["path"], "Index and decision IDs differ.")
        for field in ("inputs", "decision_rules", "constraints", "outputs"):
            if not isinstance(decision.get(field), list):
                _issue(issues, "error", "incomplete_decision_contract", entry["path"], f"{field} must be an array.")
        evidence_ids = decision.get("evidence_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            _issue(issues, "error", "unanchored_decision", entry["path"], "Decision evidence_ids must be non-empty.")
        elif set(evidence_ids) - anchor_ids:
            _issue(issues, "error", "unknown_decision_evidence", entry["path"], f"Unknown anchors: {sorted(set(evidence_ids) - anchor_ids)}.")

    if capabilities.get("decision_support") and not decision_entries:
        _issue(issues, "error", "missing_decisions", "manifest.capabilities.decision_support", "Decision capability requires decision contracts.")
    if capabilities.get("executable") and not procedure_entries:
        _issue(issues, "error", "missing_procedures", "manifest.capabilities.executable", "Executable capability requires procedure contracts.")
    if level == "publish" and decision_entries and not capabilities.get("decision_support"):
        _issue(issues, "error", "hidden_decision_capability", "manifest.capabilities.decision_support", "Decision contracts require declared capability.")
    if level == "publish" and procedure_entries and not capabilities.get("executable"):
        _issue(issues, "error", "hidden_executable_capability", "manifest.capabilities.executable", "Procedure contracts require declared capability.")
    if level == "publish" and knowledge_objects and not capabilities.get("reference"):
        _issue(issues, "error", "missing_reference_capability", "manifest.capabilities.reference", "Published knowledge requires reference capability.")

    _validate_execution_bundle(
        root,
        manifest,
        knowledge_objects,
        anchor_ids,
        conflicts,
        procedure_entries,
        issues,
    )
    _validate_promotion_audit(root, manifest, knowledge_objects, anchors, issues)
    assurance_issues, _ = validate_promoted_assurance(root)
    for assurance_issue in assurance_issues:
        _issue(
            issues,
            assurance_issue.severity,
            assurance_issue.code,
            assurance_issue.path,
            assurance_issue.message,
        )
    if (
        isinstance(build, dict)
        and build.get("compiler_version") == SEMANTIC_ASSURANCE_COMPILER_VERSION
        and not (root / "references/semantic/assurance-manifest.json").is_file()
    ):
        _issue(
            issues,
            "error",
            "legacy_semantic_protocol_not_promotable",
            "manifest.build.compiler_version",
            "A v0.5 semantic-assurance compiler identity requires the complete assurance artifact graph.",
        )

    integrity = manifest.get("integrity")
    actual_files = tracked_files(
        root,
        include_nested_manifests=isinstance(build, dict) and build.get("compiler_version") == EXECUTION_COMPILER_VERSION,
    )
    if not isinstance(integrity, dict):
        severity = "error" if level == "publish" else "warning"
        _issue(issues, severity, "unsealed_package", "manifest.integrity", "Run seal_expert_skill.py after final edits.")
    else:
        recorded = integrity.get("files")
        if not isinstance(recorded, dict):
            _issue(issues, "error", "invalid_integrity", "manifest.integrity.files", "Expected a path-to-hash object.")
        else:
            for relative, expected_hash in recorded.items():
                if relative not in actual_files:
                    _issue(issues, "error", "missing_integrity_file", str(relative), "Sealed file is missing.")
                elif expected_hash != actual_files[relative]:
                    _issue(issues, "error", "integrity_mismatch", str(relative), "File hash differs from the seal.")
            unsealed = set(actual_files) - set(recorded)
            if unsealed:
                severity = "error" if level == "publish" else "warning"
                _issue(issues, severity, "unsealed_files", "manifest.integrity.files", f"Unsealed files: {sorted(unsealed)}.")

    return sorted(issues, key=lambda item: (item.severity != "error", item.code, item.path))


def issue_summary(issues: Iterable[Issue]) -> dict[str, int]:
    counts = {"errors": 0, "warnings": 0}
    for issue in issues:
        if issue.severity == "error":
            counts["errors"] += 1
        elif issue.severity == "warning":
            counts["warnings"] += 1
    return counts
