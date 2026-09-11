#!/usr/bin/env python3
"""Verify and optionally safely extract a private fake-expert Skill release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from compiler_version import (
        RELEASE_VERSION as VERSION,
        UX_SOURCE_CANDIDATE_VERSION,
        DIRECT_REFERENCE_COMPILER_VERSION,
        DIRECT_REFERENCE_PROTOCOL,
        DIRECT_REFERENCE_SCHEMA,
        READONLY_COMPATIBLE_SOURCE_CANDIDATE_VERSIONS,
        SCAN_MVP_CONTRACT_HARDENING_COMPILER_VERSION as HARDENING_VERSION,
    )
except ModuleNotFoundError:
    VERSION = "1.2.0"  # standalone-release fallback; package time cross-checks this literal
    UX_SOURCE_CANDIDATE_VERSION = "1.2.0-ux"  # standalone-release fallback
    DIRECT_REFERENCE_COMPILER_VERSION = "1.1.0-direct-reference"  # standalone-release fallback
    DIRECT_REFERENCE_PROTOCOL = "direct-reference-draft-v0.1"  # standalone-release fallback
    DIRECT_REFERENCE_SCHEMA = "tkc.direct-reference/v0.1"  # standalone-release fallback
    READONLY_COMPATIBLE_SOURCE_CANDIDATE_VERSIONS = ("0.14.1",)
    HARDENING_VERSION = "0.14.4-scan-mvp-contract-hardening"


RELEASE_SCHEMA = "tkc.fake-expert-release/v0.4"
LEGACY_RELEASE_SCHEMA = "tkc.fake-expert-release/v0.1"
CANDIDATE_VERSIONS = {VERSION, HARDENING_VERSION, UX_SOURCE_CANDIDATE_VERSION}
LEGACY_COMPATIBLE_VERSIONS = {VERSION, *READONLY_COMPATIBLE_SOURCE_CANDIDATE_VERSIONS}
ARTIFACT_STAGES = {"candidate-verified", "release-verified"}
MVP_ACCEPTANCE_SCHEMA = "fake-expert-mvp-1.2-product-scope-v1"
SKILL_NAME = "fake-expert"
VERIFIER_NAME = "verify_fake_expert_release.py"
RECEIVER_GUIDE_NAME = "RECEIVER.md"
CODEX_CERTIFICATION_NAME = "codex-certification.json"
CODEX_CERTIFICATION_SCHEMA = "tkc.fake-expert-compiler-codex-certification/v0.1"
CODEX_CERTIFICATION_PROTOCOL = "fake-expert-compiler-codex-negative-v0.1"
FORBIDDEN_SUFFIXES = {
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RESTRICTION_KEYS = {
    "source_pdf_included",
    "page_renders_included",
    "compiled_knowledge_included",
    "hidden_evaluations_included",
    "review_material_included",
    "credentials_included",
    "host_transcripts_included",
}


class CompilerReleaseVerificationError(RuntimeError):
    """Stable private compiler-release verification failure."""


def _expected_mvp_acceptance_scope() -> dict[str, Any]:
    return {
        "schema_version": MVP_ACCEPTANCE_SCHEMA,
        "product_scope": {
            "artifact": "fake-expert-compiler-skill",
            "version": VERSION,
            "formal_private_release": True,
            "distribution": "private-transfer-only",
            "source_free_archive": True,
            "scope": "compiler-skill-only",
            "accepted_input_profiles": ["native-text-technical-pdf", "scanned-technical-pdf"],
            "knowledge_contract": {
                "bounded_reference_skill": True,
                "source_required": True,
                "traceable": True,
                "verifiable": True,
                "composable": True,
                "update_analysis": True,
                "offline_verifiable": True,
                "codex_certified": True,
            },
        },
        "review_deferred_boundary": {
            "review_state": "deferred",
            "scope": "per-book independent semantic and visual review",
            "decision": "operational-review-may-be-deferred",
            "knowledge_acceptance": False,
            "promotion_allowed": False,
            "ready_allowed": False,
            "executable_allowed": False,
            "review_gate_preserved": True,
        },
        "unreviewed_output_max_stage": "draft",
        "unreviewed_output_usable": True,
        "draft_private_distribution_allowed": True,
        "direct_reference_contract": {
            "compiler_version": DIRECT_REFERENCE_COMPILER_VERSION,
            "protocol": DIRECT_REFERENCE_PROTOCOL,
            "schema": DIRECT_REFERENCE_SCHEMA,
            "package_prefix": "fake-",
            "queryable": True,
            "source_free_distribution": True,
            "additive_extension": True,
            "gold_required": False,
            "gold_modes": ["none", "reuse", "create", "update"],
            "gold_promotion_authority": False,
            "review_state": "deferred",
            "output_lifecycle": "draft",
            "distribution_stage": "draft-distributable",
            "reference_capability": True,
            "decision_capability": False,
            "executable_capability": False,
        },
        "codex_evidence_scope": {
            "status": "exact-compiler-package-certified",
            "host": "Codex",
            "covers": [
                "Exact fake-expert v1.2.0 compiler ZIP negative-routing behavior",
                "Native-text and scanned-PDF profile gates, required-reading bindings, documentation contract, and reviewer-authority boundaries",
            ],
            "excludes": [
                "any unreviewed book's semantic or visual correctness",
                "per-book OCR accuracy, Gold, whole-book completeness, and executable knowledge",
                "non-Codex host certification",
            ],
            "exact_compiler_package_certification": "certified",
            "scanned_pdf_compiler_route_certification": "certified",
        },
        "known_uncovered_items": [
            {
                "id": "per-book-independent-review",
                "status": "deferred",
                "boundary": "Each book remains draft until its required independent review is complete",
            },
            {
                "id": "scanned-ocr-accuracy-and-gold",
                "status": "not-run",
                "boundary": "OCR runtime/integrity evidence is not OCR accuracy, Gold, or semantic truth",
            },
            {
                "id": "whole-book-knowledge-completeness",
                "status": "not-run",
                "boundary": "bounded candidate/workpack scopes do not establish whole-book completeness",
            },
            {
                "id": "decision-support-and-executable-knowledge",
                "status": "not-enabled",
                "boundary": "knowledge promotion, ready sealing, decision support, and execution remain gated",
            },
        ],
    }


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _certification_hash(value: dict[str, Any]) -> str:
    return sha256_bytes(_canonical_bytes({key: nested for key, nested in value.items() if key != "certification_sha256"}))


def _validate_codex_certification(
    value: dict[str, Any],
    *,
    archive_name: str,
    archive_sha256: str,
    version: str,
) -> None:
    required = {
        "schema_version", "protocol", "certification_id", "issued_at",
        "compiler_version", "archive", "host", "challenge_set", "results",
        "claims", "limitations", "attestation", "certification_sha256",
    }
    results = value.get("results")
    claims = value.get("claims")
    expected_claims = {
        "document_contract_enforced",
        "required_reading_hash_bound",
        "native_text_negative_routing",
        "scanned_pdf_negative_routing",
        "review_authority_preserved",
        "old_job_not_silently_rewritten",
    }
    expected_attestation = {
        "evaluator_independent_of_implementation": True,
        "hidden_expectations_not_sent": True,
        "source_pdf_sent": False,
        "network_used": False,
        "original_project_modified": False,
    }
    if (
        set(value) != required
        or value.get("schema_version") != CODEX_CERTIFICATION_SCHEMA
        or value.get("protocol") != CODEX_CERTIFICATION_PROTOCOL
        or value.get("compiler_version") != version
        or not isinstance(value.get("certification_id"), str)
        or not re.fullmatch(r"fxcert-[0-9a-f]{20}", value["certification_id"])
        or not isinstance(value.get("issued_at"), str)
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value["issued_at"])
        or value.get("archive") != {"filename": archive_name, "sha256": archive_sha256}
        or not isinstance(value.get("host"), dict)
        or set(value["host"]) != {"kind", "agent_id", "model"}
        or value["host"].get("kind") != "codex-isolated-subagent"
        or not all(isinstance(item, str) and item for item in value["host"].values())
        or not isinstance(value.get("challenge_set"), dict)
        or set(value["challenge_set"]) != {"sha256", "count"}
        or not isinstance(value["challenge_set"].get("count"), int)
        or value["challenge_set"]["count"] < 1
        or not SHA256_RE.fullmatch(str(value["challenge_set"].get("sha256")))
        or not isinstance(results, dict)
        or set(results) != {"status", "attempted", "passed", "failed", "observations"}
        or results.get("status") != "passed"
        or results.get("attempted") != value["challenge_set"]["count"]
        or results.get("passed") != results.get("attempted")
        or results.get("failed") != 0
        or not isinstance(results.get("observations"), list)
        or len(results["observations"]) != results["attempted"]
        or any(
            not isinstance(row, dict)
            or set(row) != {"challenge_id", "status", "observed", "evidence_sha256"}
            or row.get("status") != "passed"
            or not isinstance(row.get("challenge_id"), str)
            or not row["challenge_id"]
            or not isinstance(row.get("observed"), str)
            or not row["observed"]
            or not SHA256_RE.fullmatch(str(row.get("evidence_sha256")))
            for row in results["observations"]
        )
        or not isinstance(claims, dict)
        or set(claims) != expected_claims
        or any(claims[key] is not True for key in expected_claims)
        or not isinstance(value.get("limitations"), list)
        or not value["limitations"]
        or value.get("attestation") != expected_attestation
        or value.get("certification_sha256") != _certification_hash(value)
    ):
        raise CompilerReleaseVerificationError("codex_certification_invalid")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CompilerReleaseVerificationError(f"invalid_json: {path.name}") from error
    if not isinstance(value, dict):
        raise CompilerReleaseVerificationError(f"invalid_json_object: {path.name}")
    return value


def _checksum_lines(release_dir: Path) -> dict[str, str]:
    try:
        lines = (release_dir / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise CompilerReleaseVerificationError("checksums_missing") from error
    checksums: dict[str, str] = {}
    for line in lines:
        if len(line) < 67 or line[64:66] != "  ":
            raise CompilerReleaseVerificationError("checksums_invalid")
        digest, name = line[:64], line[66:]
        if (
            not SHA256_RE.fullmatch(digest)
            or not name
            or name in checksums
            or "/" in name
            or "\\" in name
        ):
            raise CompilerReleaseVerificationError("checksums_invalid")
        checksums[name] = digest
    return checksums


def _safe_member(name: str) -> str:
    if "\\" in name:
        raise CompilerReleaseVerificationError(f"archive_path_invalid: {name}")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise CompilerReleaseVerificationError(f"archive_path_invalid: {name}")
    if path.parts[0] != SKILL_NAME or len(path.parts) < 2:
        raise CompilerReleaseVerificationError(f"archive_root_invalid: {name}")
    return PurePosixPath(*path.parts[1:]).as_posix()


def _ensure_extract_target(path: Path) -> Path:
    target = path.expanduser().resolve()
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):
            raise CompilerReleaseVerificationError(f"extract_target_not_empty: {target}")
    else:
        target.mkdir(parents=True)
    return target


def _required_string(record: dict[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise CompilerReleaseVerificationError(f"release_manifest_invalid: {field}")
    return value


def verify_compiler_release(
    release_dir: Path, *, extract: Path | None = None, require_release_verified: bool = False
) -> dict[str, Any]:
    """Verify a local release sidecar and, optionally, safely extract its archive."""

    release_dir = release_dir.expanduser().resolve()
    if not release_dir.is_dir():
        raise CompilerReleaseVerificationError(f"release_directory_missing: {release_dir}")
    release = _load_json(release_dir / "release-manifest.json")
    manifest_schema = release.get("schema_version")
    stage_hint = release.get("artifact_stage")
    if (
        (stage_hint is None and manifest_schema != LEGACY_RELEASE_SCHEMA)
        or (stage_hint is not None and manifest_schema != RELEASE_SCHEMA)
    ):
        raise CompilerReleaseVerificationError("release_schema_invalid")
    archive_record = release.get("archive")
    compiler = release.get("compiler")
    verifier = release.get("verifier")
    receiver_guide = release.get("receiver_guide")
    codex_record = release.get("codex_certification")
    if not all(isinstance(record, dict) for record in (archive_record, compiler, verifier, receiver_guide)):
        raise CompilerReleaseVerificationError("release_manifest_invalid")
    if stage_hint == "release-verified" and not isinstance(codex_record, dict):
        raise CompilerReleaseVerificationError("codex_certification_required")
    if stage_hint != "release-verified" and codex_record is not None:
        raise CompilerReleaseVerificationError("candidate_codex_certification_forbidden")
    archive_name = _required_string(archive_record, "filename")
    expected_checksums = {
        archive_name,
        "release-manifest.json",
        _required_string(verifier, "filename"),
        _required_string(receiver_guide, "filename"),
    }
    if expected_checksums != {
        archive_name,
        "release-manifest.json",
        VERIFIER_NAME,
        RECEIVER_GUIDE_NAME,
    }:
        raise CompilerReleaseVerificationError("release_sidecar_names_invalid")
    if isinstance(codex_record, dict):
        if _required_string(codex_record, "filename") != CODEX_CERTIFICATION_NAME:
            raise CompilerReleaseVerificationError("release_sidecar_names_invalid")
        expected_checksums.add(CODEX_CERTIFICATION_NAME)
    checksums = _checksum_lines(release_dir)
    if set(checksums) != expected_checksums:
        raise CompilerReleaseVerificationError("checksums_inventory_invalid")
    for name, digest in checksums.items():
        path = release_dir / name
        if not path.is_file() or path.is_symlink() or sha256_file(path) != digest:
            raise CompilerReleaseVerificationError(f"checksum_mismatch: {name}")
    archive_path = release_dir / archive_name
    if sha256_file(archive_path) != archive_record.get("sha256"):
        raise CompilerReleaseVerificationError("archive_hash_mismatch")
    if sha256_file(release_dir / VERIFIER_NAME) != verifier.get("sha256"):
        raise CompilerReleaseVerificationError("verifier_hash_mismatch")
    if sha256_file(release_dir / RECEIVER_GUIDE_NAME) != receiver_guide.get("sha256"):
        raise CompilerReleaseVerificationError("receiver_guide_hash_mismatch")
    certification_value = None
    if isinstance(codex_record, dict):
        certification_path = release_dir / CODEX_CERTIFICATION_NAME
        if sha256_file(certification_path) != codex_record.get("sha256"):
            raise CompilerReleaseVerificationError("codex_certification_hash_mismatch")
        certification_value = _load_json(certification_path)
        if (
            set(codex_record) != {"filename", "sha256", "schema_version", "certification_id", "archive_sha256"}
            or codex_record.get("schema_version") != CODEX_CERTIFICATION_SCHEMA
            or codex_record.get("certification_id") != certification_value.get("certification_id")
            or codex_record.get("archive_sha256") != archive_record.get("sha256")
        ):
            raise CompilerReleaseVerificationError("codex_certification_binding_invalid")
        _validate_codex_certification(
            certification_value,
            archive_name=archive_name,
            archive_sha256=str(archive_record.get("sha256")),
            version=str(compiler.get("version")),
        )
    if compiler.get("skill_name") != SKILL_NAME or compiler.get("root_directory") != SKILL_NAME:
        raise CompilerReleaseVerificationError("compiler_identity_invalid")
    artifact_stage = release.get("artifact_stage")
    legacy_manifest = artifact_stage is None
    source_candidate_identity = release.get("source_candidate_identity")
    if artifact_stage is None:
        # Manifests written before Phase 7D.6A remain readable, but their
        # result is deliberately not the ambiguous new ``verified`` state.
        artifact_stage = "legacy-verified"
    if artifact_stage not in ARTIFACT_STAGES | {"legacy-verified"}:
        raise CompilerReleaseVerificationError("artifact_stage_invalid")
    compiler_version = compiler.get("version")
    if not isinstance(compiler_version, str) or not compiler_version:
        raise CompilerReleaseVerificationError("compiler_contract_invalid")
    if artifact_stage == "legacy-verified":
        if compiler_version not in LEGACY_COMPATIBLE_VERSIONS or source_candidate_identity is not None:
            raise CompilerReleaseVerificationError("legacy_artifact_identity_invalid")
        if "mvp_acceptance" in release:
            raise CompilerReleaseVerificationError("legacy_mvp_acceptance_invalid")
    elif artifact_stage == "candidate-verified":
        if compiler_version not in CANDIDATE_VERSIONS or source_candidate_identity != compiler_version:
            raise CompilerReleaseVerificationError("candidate_artifact_identity_invalid")
        if "mvp_acceptance" in release:
            raise CompilerReleaseVerificationError("candidate_mvp_acceptance_invalid")
    else:
        if compiler_version != VERSION or source_candidate_identity is not None:
            raise CompilerReleaseVerificationError("release_artifact_identity_invalid")
        if release.get("mvp_acceptance") != _expected_mvp_acceptance_scope():
            raise CompilerReleaseVerificationError("release_mvp_acceptance_invalid")
        if certification_value is None:
            raise CompilerReleaseVerificationError("codex_certification_required")
    if require_release_verified and artifact_stage != "release-verified":
        raise CompilerReleaseVerificationError("release_stage_required")
    if compiler.get("entrypoint") != "SKILL.md":
        raise CompilerReleaseVerificationError("compiler_contract_invalid")
    distribution = release.get("distribution")
    restrictions = release.get("restrictions")
    runtime = release.get("runtime")
    expected_scan_ocr = {
        "compiler_version": "0.14.0-scan-pdf",
        "scan_ir_schema": "tkc.scanned-pdf-ir/v0.2",
        "protocol_schema": "tkc.pdf-ocr-jsonl/v0.2",
        "primary_backend": "paddleocr-ppocrv6",
        "structured_backend": "paddleocr-ppstructure-v3",
        "challenger_backend": "docling",
        "baseline_backend": "pymupdf-tesseract",
        "model_download": False,
        "network_enabled": False,
        "silent_fallback": False,
        "fake_backend_policy": "explicit-synthetic-test-mode-only",
        "pages_needing_ocr": True,
        "raster_region_policy": "contract-bound-pdf-bbox-render-crop-worker",
        "paddle_runtime_protocol": "paddle-runtime-contract-v0.2",
        "paddle_runtime_contract_schema": "tkc.paddle-runtime-contract/v0.2",
        "paddle_runtime_profile_schema": "tkc.paddle-runtime-profile/v0.2",
        "paddle_result_adapter_schema": "tkc.ppstructure-v3-result/v0.1",
        "paddle_result_adapter_protocol": "ppstructure-v3-result-adapter-v0.1",
        "paddle_ocr_result_schema": "tkc.ppocrv6-result/v0.1",
        "paddle_ocr_result_protocol": "ppocrv6-result-adapter-v0.1",
        "structure_review_schema": "tkc.scanned-pdf-structure-review/v0.1",
        "structure_review_protocol": "scanned-pdf-structure-review-v0.1",
        "reviewed_structure_is_bounded": True,
        "ocr_candidate_only_until_independent_review": True,
        "model_inventory_required": True,
        "ppocr_text_is_not_table_topology": True,
        "scan_anchor_protocol": "scan-anchor-v0.2",
        "contract_hardening_source_candidate": "0.14.4-scan-mvp-contract-hardening",
        "runtime_worker_binding_required": True,
        "object_exact_evidence_binding": True,
        "scan_integrity_verifies_commitments": True,
        "scan_full_replay_requires_exact_runtime_worker": True,
    }
    if legacy_manifest:
        expected_scan_ocr = {
            key: value
            for key, value in expected_scan_ocr.items()
            if key not in {
                "scan_anchor_protocol",
                "contract_hardening_source_candidate",
                "runtime_worker_binding_required",
                "object_exact_evidence_binding",
                "scan_integrity_verifies_commitments",
                "scan_full_replay_requires_exact_runtime_worker",
            }
        }
    expected_visual_parser_version = (
        "0.14.0-visual-parser-orchestrator"
        if legacy_manifest
        else "0.14.12-visual-parser-calibration"
    )
    expected_direct_reference = None if legacy_manifest else {
        "compiler_version": DIRECT_REFERENCE_COMPILER_VERSION,
        "protocol": DIRECT_REFERENCE_PROTOCOL,
        "schema": DIRECT_REFERENCE_SCHEMA,
        "commands": ["build", "extend", "bind-gold", "status", "validate"],
        "package_prefix": "fake-",
        "gold_modes": ["none", "reuse", "create", "update"],
        "gold_required": False,
        "gold_promotion_authority": False,
        "review_state": "deferred",
        "knowledge_verified": False,
        "output_lifecycle": "draft",
        "distribution_stage": "draft-distributable",
        "source_free_distribution": True,
        "queryable": True,
        "additive_extension": True,
        "reference_capability": True,
        "decision_capability": False,
        "executable_capability": False,
    }
    if (
        not isinstance(distribution, dict)
        or distribution.get("visibility") != "private-transfer-only"
        or distribution.get("public_publication") is not False
        or distribution.get("adapter_policy") != "thin-host-adapter-only"
        or not isinstance(restrictions, dict)
        or set(restrictions) != RESTRICTION_KEYS
        or any(restrictions[key] is not False for key in RESTRICTION_KEYS)
        or not isinstance(runtime, dict)
        or runtime.get("python") != ">=3.11"
        or runtime.get("dependencies") != ["pypdf==6.10.0"]
        or runtime.get("required_local_commands") != ["pdftoppm"]
        or runtime.get("network_required") is not False
        or runtime.get("optional_local_parser_adapters")
        != {"requirements": "requirements-parser-extras.txt", "network_required": False}
        or runtime.get("direct_reference") != expected_direct_reference
        or runtime.get("scan_ocr") != expected_scan_ocr
        or runtime.get("external_paddle_runtime")
        != {
            "protocol": "paddle-runtime-contract-v0.2",
            "contract_schema": "tkc.paddle-runtime-contract/v0.2",
            "profile_schema": "tkc.paddle-runtime-profile/v0.2",
            "result_adapter_schema": "tkc.ppstructure-v3-result/v0.1",
            "result_adapter_protocol": "ppstructure-v3-result-adapter-v0.1",
            "text_result_adapter_schema": "tkc.ppocrv6-result/v0.1",
            "text_result_adapter_protocol": "ppocrv6-result-adapter-v0.1",
            "interpreter_allowlist": "explicit-resolved-containment",
            "runtime_allowlist": "explicit-resolved-containment",
            "model_allowlist": "explicit-resolved-containment",
            "model_manifest": "per-file-sha256",
            "worker_protocol": "fixed-jsonl-crop-only",
            "source_pdf_passed": False,
            "mcp_http_dependency": False,
            "network_enabled": False,
            "model_download": False,
            "os_network_isolation": "must-report-not-verified-when-unavailable",
            "compiler_authored_attestation": False,
        }
        or runtime.get("paddle_runtime_qualification")
        != {
            "compiler_version": "0.14.0-paddle-runtime-qualification",
            "protocol": "paddle-runtime-qualification-v0.1",
            "plan_schema": "tkc.paddle-runtime-qualification-plan/v0.1",
            "receipt_schema": "tkc.paddle-runtime-qualification/v0.1",
            "qualifiable_backends": ["paddleocr-chart-parsing", "paddleocr-ppocrv6", "paddleocr-ppstructure-v3"],
            "required_repetitions": 3,
            "single_model_process": True,
            "warning_blockers": True,
            "qualification_tool_hash_bound": True,
            "operator_exclusive_heavy_model_confirmation_required": True,
            "source_pdf_passed": False,
            "candidate_only": True,
            "accuracy_claim": False,
            "receipt_in_release": False,
        }
        or runtime.get("semantic_assurance")
        != {
            "compiler_version": "0.5.0-semantic-assurance",
            "protocol": "atomic-support-review-v2",
            "assertion_schema": "tkc.semantic-assertion/v0.2",
            "support_schema": "tkc.support-matrix/v0.1",
            "attestation_schema": "tkc.review-attestation/v0.1",
            "coverage_schema": "tkc.coverage-ledger/v0.1",
            "attestation_level": "host-orchestrator-recorded-not-cryptographic",
            "review_bypass_allowed": False,
            "whole_book_completeness_claimed": False,
        }
        or runtime.get("heading_locator")
        != {
            "protocol": "heading-locator-v0.2",
            "candidate_schema": "tkc.heading-candidate/v0.2",
            "resolution_schema": "tkc.heading-resolution/v0.2",
            "override_schema": "tkc.heading-locator-override/v0.2",
            "workpack_consumes_resolution": True,
            "canonical_evidence_hash_unchanged": True,
        }
        or runtime.get("whole_book_planning")
        != {
            "compiler_version": "0.6.0-whole-book-structure-coverage",
            "protocol": "whole-book-structure-coverage-v0.2",
            "planning_states": [
                "compile",
                "context-only",
                "non-knowledge",
                "ocr-candidate",
                "visual-review",
                "gap",
                "quarantined",
            ],
            "module_plan_deterministic": True,
            "ocr_invocation": False,
            "whole_book_completeness_claimed": False,
            "review_authoring": False,
            "auto_seal": False,
            "auto_execution": False,
        }
        or runtime.get("visual_semantics")
        != {
            "compiler_version": "0.7.0-visual-semantics",
            "protocol": "visual-semantics-v0.1",
            "object_schema": "tkc.visual-object/v0.1",
            "relation_schema": "tkc.visual-relation/v0.1",
            "table_grid_schema": "tkc.table-grid/v0.1",
            "conflict_schema": "tkc.visual-conflict/v0.1",
            "review_attestation_schema": "tkc.visual-review-attestation/v0.2",
            "review_protocol": "visual-review-attestation-v0.2",
            "coordinate_space": "pdf-page-top-left-points-v1",
            "canonical_native_extractor": "pypdf==6.10.0",
            "canonical_visual_renderer": "pdftoppm",
            "candidate_only": True,
            "model_download": False,
            "network_enabled": False,
            "whole_book_completeness_claimed": False,
            "external_review_fragments_required": True,
            "compiler_authored_review_conclusions": False,
            "assurance_manifest_schema": "tkc.visual-assurance-manifest/v0.1",
        }
        or runtime.get("incremental_build_dag")
        != {
            "compiler_version": "0.14.0-incremental-build-dag",
            "protocol": "incremental-build-dag-v0.2",
            "manifest_schema": "tkc.incremental-dag-manifest/v0.2",
            "node_schema": "tkc.incremental-dag-node/v0.2",
            "edge_schema": "tkc.incremental-dag-edge/v0.2",
            "change_set_schema": "tkc.incremental-change-set/v0.2",
            "plan_schema": "tkc.incremental-build-plan/v0.2",
            "state_schema": "tkc.incremental-build-state/v0.2",
            "receipt_schema": "tkc.incremental-build-receipt/v0.2",
            "content_addressed": True,
            "typed_edges": True,
            "reuse_requires_exact_hash_protocol_dependencies": True,
            "pause_only_resume": True,
            "review_authoring": False,
            "seal": False,
            "signing": False,
            "execution": False,
            "source_material_included": False,
        }
        or runtime.get("semantic_composer")
        != {
            "compiler_version": "0.9.0-semantic-composer",
            "protocol": "semantic-composer-v0.1",
            "workpack_schema": "tkc.semantic-composer-workpack/v0.1",
            "input_schema": "tkc.semantic-composer-input/v0.1",
            "concept_ref_schema": "tkc.semantic-concept-ref/v0.1",
            "alignment_schema": "tkc.semantic-alignment-candidate/v0.1",
            "difference_schema": "tkc.semantic-difference/v0.1",
            "conflict_schema": "tkc.semantic-conflict/v0.2",
            "evidence_binding_schema": "tkc.semantic-evidence-binding/v0.1",
            "unit_map_schema": "tkc.semantic-unit-map/v0.1",
            "review_plan_schema": "tkc.semantic-review-plan/v0.1",
            "review_attestation_schema": "tkc.semantic-composer-review-attestation/v0.1",
            "receipt_schema": "tkc.semantic-composer-receipt/v0.1",
            "review_protocol": "cross-source-review-v0.1",
            "lexical_match_is_not_equivalence": True,
            "preserve_separate_no_consensus": True,
            "automatic_consensus": False,
            "automatic_merge": False,
            "promotion": False,
            "network_enabled": False,
            "review_attestation_authoring": False,
            "pause_only_resume": True,
            "source_material_included": False,
        }
        or runtime.get("visual_parser_orchestrator")
        != {
            "compiler_version": expected_visual_parser_version,
            "protocol": "visual-parser-orchestrator-v0.4",
            "job_schema": "tkc.visual-parser-job/v0.4",
            "manifest_schema": "tkc.visual-parser-manifest/v0.4",
            "evaluation_contract_schema": "tkc.visual-evaluation-contract/v0.1",
            "review_overlay_schema": "tkc.visual-review-overlay/v0.4",
            "commands": ["plan", "build", "validate", "status", "resume"],
            "native_text_first": True,
            "canonical_native_extractor": "pypdf==6.10.0",
            "candidate_only": True,
            "network_enabled": False,
            "model_download": False,
            "silent_fallback": False,
            "parser_disagreement_is_conflict": True,
            "fake_backend_policy": "job.test_mode-and-test_only_authorized-and-synthetic_source-required",
            "synthetic_only_downstream": "dag-and-composer-reject-synthetic-rows",
            "non_contiguous_ocr_scope": "fail-closed",
            "raster_region_policy": "contract-bound-page-bbox-crop-render-source-anchor;external-paddle-crop-only;missing-structure-models-not-run",
            "external_runtime_contract_schema": "tkc.paddle-runtime-contract/v0.2",
            "external_runtime_protocol": "paddle-runtime-contract-v0.2",
            "review_authoring": False,
            "promotion": False,
            "pause_only_resume": True,
            "source_material_included": False,
        }
        or runtime.get("selective_visual")
        != {
            "compiler_version": "0.14.0-selective-visual",
            "protocol": "selective-visual-pipeline-v0.1",
            "policy_schema": "tkc.visual-policy/v0.1",
            "census_schema": "tkc.visual-census/v0.1",
            "routing_schema": "tkc.visual-routing/v0.1",
            "budget_receipt_schema": "tkc.visual-budget-receipt/v0.1",
            "index_schema": "tkc.visual-index/v0.1",
            "coverage_schema": "tkc.visual-coverage/v0.2",
            "batch_receipt_schema": "tkc.visual-batch-receipt/v0.1",
            "modes": ["off", "auto", "full"],
            "visual_kinds": ["table", "figure", "chart", "diagram", "equation"],
            "contexts": ["none", "index", "on-demand"],
            "default_mode": "auto",
            "default_context": "index",
            "budget_fields": ["max_visual_pages", "max_visual_regions", "max_visual_seconds"],
            "budget_exhaustion_status": "paused-budget-exhausted",
            "source_free_visual_index": True,
            "candidate_only": True,
            "promotion": False,
            "executable": False,
            "chart_profile": "technical-chart-v2",
            "chart_protocol": "technical-chart-v2-local-pp-chart2table-paddle-dynamic",
            "chart_backend": "paddleocr-chart-parsing",
            "chart_api": "paddleocr.ChartParsing",
            "chart_result_schema": "tkc.technical-chart-result/v0.1",
            "chart_result_protocol": "technical-chart-result-adapter-v0.1",
            "chart_candidate_schema": "tkc.technical-chart-candidate/v0.2",
            "chart_model_binding": {
                "model_name": "PP-Chart2Table",
                "model_dir": "PP-Chart2Table",
                "constructor_options": {"device": "cpu", "enable_hpi": False, "engine": "paddle_dynamic"},
                "predict_options": {"batch_size": 1},
                "missing_model_policy": "not-run-missing-models",
                "unqualified_runtime_policy": "paused-runtime-unqualified",
                "download": False,
            },
            "model_download": False,
            "network_enabled": False,
            "source_pdf_passed_to_worker": False,
            "worker_protocol": "fixed-jsonl-crop-only",
            "batch_session_model_load_once": True,
            "production_batch_runner": "run_external_worker_batch",
            "batch_session_protocol": "paddle-runtime-contract-v0.2-jsonl-bounded",
            "batch_hard_total_timeout": True,
            "batch_hard_per_request_timeout": True,
            "runtime_inventory_drift_gate": True,
            "empty_batch_zero_calls": True,
            "pause_only_resume": True,
        }
    ):
        raise CompilerReleaseVerificationError("private_distribution_contract_invalid")
    records = compiler.get("files")
    if not isinstance(records, list) or not records:
        raise CompilerReleaseVerificationError("compiler_inventory_invalid")
    inventory: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise CompilerReleaseVerificationError("compiler_inventory_invalid")
        path = record.get("path")
        digest = record.get("sha256")
        size = record.get("bytes")
        if (
            not isinstance(path, str)
            or not path
            or path in inventory
            or not isinstance(digest, str)
            or not SHA256_RE.fullmatch(digest)
            or not isinstance(size, int)
            or size < 0
        ):
            raise CompilerReleaseVerificationError("compiler_inventory_invalid")
        _safe_member(f"{SKILL_NAME}/{path}")
        if Path(path).suffix.casefold() in FORBIDDEN_SUFFIXES:
            raise CompilerReleaseVerificationError("compiler_inventory_forbidden_file")
        inventory[path] = record
    if list(inventory) != sorted(inventory):
        raise CompilerReleaseVerificationError("compiler_inventory_unsorted")
    required_schema_paths = {
        "assets/schemas/visual-batch-receipt.schema.json",
        "assets/schemas/technical-chart-candidate.schema.json",
        "assets/schemas/paddle-runtime-qualification-plan.schema.json",
        "assets/schemas/paddle-runtime-qualification.schema.json",
        "assets/schemas/ppocrv6-result.schema.json",
        "assets/schemas/scanned-pdf-structure-review.schema.json",
        "assets/schemas/source-anchor.schema.json",
        "assets/schemas/scanned-pdf-ir.schema.json",
        "assets/schemas/semantic-workpack.schema.json",
    }
    required_direct_paths: set[str] = set()
    if not legacy_manifest:
        required_direct_paths = {
            "assets/schemas/direct-reference.schema.json",
            "assets/schemas/skill-release.schema.json",
            "scripts/direct_reference_runtime.py",
            "scripts/direct_reference_skill.py",
            "scripts/package_skill_release.py",
            "scripts/portable_reference_runtime.py",
            "scripts/verify_skill_release.py",
        }
    if not required_schema_paths.issubset(inventory):
        raise CompilerReleaseVerificationError("selective_visual_schema_missing")
    if not required_direct_paths.issubset(inventory):
        raise CompilerReleaseVerificationError("direct_reference_release_path_missing")
    members: dict[str, zipfile.ZipInfo] = {}
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                raise CompilerReleaseVerificationError(
                    f"archive_directory_entry_forbidden: {info.filename}"
                )
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise CompilerReleaseVerificationError(
                    f"archive_nonregular_entry: {info.filename}"
                )
            relative = _safe_member(info.filename)
            if relative in members:
                raise CompilerReleaseVerificationError(
                    f"archive_duplicate_entry: {relative}"
                )
            if Path(relative).suffix.casefold() in FORBIDDEN_SUFFIXES:
                raise CompilerReleaseVerificationError(
                    f"embedded_source_or_render_forbidden: {relative}"
                )
            members[relative] = info
        if set(members) != set(inventory):
            raise CompilerReleaseVerificationError("compiler_inventory_mismatch")
        expected_schema_versions = {
            "assets/schemas/visual-batch-receipt.schema.json": "tkc.visual-batch-receipt/v0.1",
            "assets/schemas/technical-chart-candidate.schema.json": "tkc.technical-chart-candidate/v0.2",
            "assets/schemas/paddle-runtime-qualification-plan.schema.json": "tkc.paddle-runtime-qualification-plan/v0.1",
            "assets/schemas/paddle-runtime-qualification.schema.json": "tkc.paddle-runtime-qualification/v0.1",
            "assets/schemas/ppocrv6-result.schema.json": "tkc.ppocrv6-result/v0.1",
            "assets/schemas/scanned-pdf-structure-review.schema.json": "tkc.scanned-pdf-structure-review/v0.1",
            "assets/schemas/scanned-pdf-ir.schema.json": "tkc.scanned-pdf-ir/v0.2",
            "assets/schemas/semantic-workpack.schema.json": "tkc.semantic-workpack/v0.1",
        }
        if not legacy_manifest:
            expected_schema_versions.update(
                {
                    "assets/schemas/direct-reference.schema.json": DIRECT_REFERENCE_SCHEMA,
                    "assets/schemas/skill-release.schema.json": "tkc.skill-release/v0.2",
                }
            )
        for schema_path, expected_version in expected_schema_versions.items():
            try:
                schema = json.loads(archive.read(members[schema_path]).decode("utf-8"))
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise CompilerReleaseVerificationError(
                    f"selective_visual_schema_invalid: {schema_path}"
                ) from error
            actual = (
                schema.get("properties", {})
                .get("schema_version", {})
                .get("const")
            ) if isinstance(schema, dict) else None
            if actual != expected_version:
                raise CompilerReleaseVerificationError(
                    f"selective_visual_schema_version_invalid: {schema_path}"
                )
        if len(members) != compiler.get("file_count") or len(members) != archive_record.get("file_count"):
            raise CompilerReleaseVerificationError("compiler_file_count_mismatch")
        actual_bytes = sum(info.file_size for info in members.values())
        if (
            actual_bytes != compiler.get("uncompressed_bytes")
            or actual_bytes != archive_record.get("uncompressed_bytes")
        ):
            raise CompilerReleaseVerificationError("compiler_size_mismatch")
        for relative, record in inventory.items():
            data = archive.read(members[relative])
            if len(data) != record["bytes"] or sha256_bytes(data) != record["sha256"]:
                raise CompilerReleaseVerificationError(
                    f"compiler_file_hash_mismatch: {relative}"
                )
        skill = archive.read(members["SKILL.md"])
        if sha256_bytes(skill) != compiler.get("entrypoint_sha256"):
            raise CompilerReleaseVerificationError("skill_entrypoint_hash_mismatch")
        if not skill.decode("utf-8").startswith("---\nname: fake-expert\n"):
            raise CompilerReleaseVerificationError("skill_entrypoint_identity_invalid")
        requirements = archive.read(members["requirements-compiler.txt"]).decode("utf-8")
        if "pypdf==6.10.0" not in requirements:
            raise CompilerReleaseVerificationError("runtime_dependency_contract_invalid")
        parser_extras = archive.read(members["requirements-parser-extras.txt"]).decode("utf-8")
        if (
            "PyMuPDF" not in parser_extras
            or "docling" not in parser_extras
            or "paddleocr" not in parser_extras
            or "pytesseract" not in parser_extras
        ):
            raise CompilerReleaseVerificationError("optional_parser_dependency_contract_invalid")
        archive_verifier = archive.read(members[f"scripts/{VERIFIER_NAME}"])
        if sha256_bytes(archive_verifier) != sha256_file(release_dir / VERIFIER_NAME):
            raise CompilerReleaseVerificationError("archive_verifier_drift")
        extracted_root = None
        if extract is not None:
            target = _ensure_extract_target(extract)
            for relative, info in sorted(members.items()):
                destination = target / SKILL_NAME / Path(relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("wb") as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                os.chmod(destination, 0o644)
            extracted_root = str(target / SKILL_NAME)
    return {
        "status": artifact_stage,
        "skill_name": SKILL_NAME,
        "version": compiler["version"],
        "archive_sha256": archive_record["sha256"],
        "file_count": compiler["file_count"],
        "codex_certification_id": (
            certification_value.get("certification_id")
            if certification_value is not None
            else None
        ),
        "extracted_root": extracted_root,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--extract", type=Path)
    parser.add_argument(
        "--require-release-verified",
        action="store_true",
        help="Fail unless the manifest is the explicit formal release-verified stage.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = verify_compiler_release(
            args.release_dir,
            extract=args.extract,
            require_release_verified=args.require_release_verified,
        )
    except (
        CompilerReleaseVerificationError,
        OSError,
        UnicodeDecodeError,
        ValueError,
        zipfile.BadZipFile,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
