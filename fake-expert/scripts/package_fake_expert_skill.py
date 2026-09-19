#!/usr/bin/env python3
"""Build a deterministic private release of the host-neutral fake-expert Skill."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import stat
import sys
import zipfile
from pathlib import Path
from typing import Any

from compiler_version import (
    DIRECT_REFERENCE_COMPILER_VERSION,
    DIRECT_REFERENCE_PROTOCOL,
    DIRECT_REFERENCE_SCHEMA,
    HEADING_LOCATOR_PROTOCOL,
    RELEASE_VERSION as VERSION,
    UX_SOURCE_CANDIDATE_VERSION,
    SCIENCE_LIFECYCLE_COMPILER_VERSION,
    SCAN_PDF_COMPILER_VERSION,
    SEMANTIC_ASSURANCE_COMPILER_VERSION,
    SEMANTIC_ASSURANCE_PROTOCOL,
    WHOLE_BOOK_PLANNER_COMPILER_VERSION,
    WHOLE_BOOK_PLANNER_PROTOCOL,
    VISUAL_ASSURANCE_MANIFEST_SCHEMA,
    VISUAL_CONFLICT_SCHEMA,
    VISUAL_OBJECT_SCHEMA,
    VISUAL_RELATION_SCHEMA,
    TABLE_GRID_SCHEMA,
    VISUAL_REVIEW_ATTESTATION_SCHEMA,
    VISUAL_REVIEW_PROTOCOL,
    VISUAL_SEMANTICS_COMPILER_VERSION,
    VISUAL_SEMANTICS_PROTOCOL,
    INCREMENTAL_BUILD_DAG_COMPILER_VERSION,
    INCREMENTAL_BUILD_DAG_PROTOCOL,
    INCREMENTAL_CHANGE_SET_SCHEMA,
    INCREMENTAL_BUILD_PLAN_SCHEMA,
    INCREMENTAL_BUILD_RECEIPT_SCHEMA,
    INCREMENTAL_BUILD_STATE_SCHEMA,
    INCREMENTAL_DAG_EDGE_SCHEMA,
    INCREMENTAL_DAG_MANIFEST_SCHEMA,
    INCREMENTAL_DAG_NODE_SCHEMA,
    ALIGNMENT_CANDIDATE_SCHEMA,
    COMPOSER_CONFLICT_SCHEMA,
    COMPOSER_INPUT_SCHEMA,
    COMPOSER_RECEIPT_SCHEMA,
    COMPOSER_REVIEW_ATTESTATION_SCHEMA,
    COMPOSER_REVIEW_PLAN_SCHEMA,
    COMPOSER_WORKPACK_SCHEMA,
    CONCEPT_REF_SCHEMA,
    DIFFERENCE_SCHEMA,
    EVIDENCE_BINDING_SCHEMA,
    SEMANTIC_COMPOSER_COMPILER_VERSION,
    SEMANTIC_COMPOSER_PROTOCOL,
    UNIT_MAP_SCHEMA,
    VISUAL_PARSER_ORCHESTRATOR_COMPILER_VERSION,
    VISUAL_PARSER_ORCHESTRATOR_PROTOCOL,
    VISUAL_PARSER_JOB_SCHEMA,
    VISUAL_PARSER_MANIFEST_SCHEMA,
    VISUAL_EVALUATION_CONTRACT_SCHEMA,
    VISUAL_REVIEW_OVERLAY_SCHEMA,
    OCR_PROTOCOL_SCHEMA,
    PADDLE_RUNTIME_PROFILE_SCHEMA,
    PADDLE_RUNTIME_QUALIFICATION_PLAN_SCHEMA,
    PADDLE_RUNTIME_QUALIFICATION_SCHEMA,
    PADDLE_RUNTIME_QUALIFICATION_PROTOCOL,
    PADDLE_RUNTIME_QUALIFICATION_COMPILER_VERSION,
    PADDLE_RESULT_ADAPTER_SCHEMA,
    PADDLE_RESULT_ADAPTER_PROTOCOL,
    PADDLE_OCR_RESULT_SCHEMA,
    PADDLE_OCR_RESULT_PROTOCOL,
    SCANNED_PDF_STRUCTURE_REVIEW_SCHEMA,
    SCANNED_PDF_STRUCTURE_REVIEW_PROTOCOL,
    SCAN_ANCHOR_PROTOCOL,
    SCAN_MVP_CONTRACT_HARDENING_COMPILER_VERSION,
    PADDLE_RUNTIME_CONTRACT_SCHEMA,
    PADDLE_RUNTIME_PROTOCOL,
    SELECTIVE_VISUAL_COMPILER_VERSION,
    SELECTIVE_VISUAL_PROTOCOL,
    VISUAL_POLICY_SCHEMA,
    VISUAL_CENSUS_SCHEMA,
    VISUAL_ROUTING_SCHEMA,
    VISUAL_BUDGET_RECEIPT_SCHEMA,
    VISUAL_INDEX_SCHEMA,
    VISUAL_COVERAGE_SCHEMA,
    VISUAL_BATCH_RECEIPT_SCHEMA,
    TECHNICAL_CHART_PROFILE_ID,
    TECHNICAL_CHART_PROFILE_PROTOCOL,
    TECHNICAL_CHART_BACKEND,
    TECHNICAL_CHART_API,
    TECHNICAL_CHART_MODEL_NAME,
    TECHNICAL_CHART_MODEL_DIR,
    TECHNICAL_CHART_RESULT_SCHEMA,
    TECHNICAL_CHART_RESULT_PROTOCOL,
    TECHNICAL_CHART_CANDIDATE_SCHEMA,
)


RELEASE_SCHEMA = "tkc.fake-expert-release/v0.4"
SKILL_NAME = "fake-expert"
ARCHIVE_NAME = f"{SKILL_NAME}-v{VERSION}.zip"
SOURCE_CANDIDATE_VERSIONS = {
    VERSION,
    SCAN_MVP_CONTRACT_HARDENING_COMPILER_VERSION,
    UX_SOURCE_CANDIDATE_VERSION,
    SCIENCE_LIFECYCLE_COMPILER_VERSION,
}
ARTIFACT_STAGES = {"candidate-verified", "release-verified"}
MVP_ACCEPTANCE_SCHEMA = "fake-expert-mvp-1.2-product-scope-v1"
VERIFIER_NAME = "verify_fake_expert_release.py"
RECEIVER_GUIDE_NAME = "RECEIVER.md"
CODEX_CERTIFICATION_NAME = "codex-certification.json"
CODEX_CERTIFICATION_SCHEMA = "tkc.fake-expert-compiler-codex-certification/v0.1"
CODEX_CERTIFICATION_PROTOCOL = "fake-expert-compiler-codex-negative-v0.1"
ALLOWED_TOP_LEVEL_FILES = {
    "SKILL.md",
    "requirements-compiler.txt",
    "requirements-parser-extras.txt",
}
ALLOWED_TOP_LEVEL_DIRECTORIES = {"agents", "assets", "references", "scripts"}
ALLOWED_SUFFIXES = {".json", ".md", ".py", ".txt", ".yaml", ".yml"}
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
RFC3339_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)


class CompilerReleasePackagingError(RuntimeError):
    """Stable private compiler-release packaging failure."""


MVP_ACCEPTANCE_SCOPE = {
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
    path: Path,
    *,
    archive_name: str,
    archive_sha256: str,
    version: str,
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise CompilerReleasePackagingError("codex_certification_missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CompilerReleasePackagingError("codex_certification_invalid") from error
    required = {
        "schema_version", "protocol", "certification_id", "issued_at",
        "compiler_version", "archive", "host", "challenge_set", "results",
        "claims", "limitations", "attestation", "certification_sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise CompilerReleasePackagingError("codex_certification_invalid")
    _validate_created_at(str(value.get("issued_at", "")))
    archive = value.get("archive")
    results = value.get("results")
    claims = value.get("claims")
    attestation = value.get("attestation")
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
        value.get("schema_version") != CODEX_CERTIFICATION_SCHEMA
        or value.get("protocol") != CODEX_CERTIFICATION_PROTOCOL
        or value.get("compiler_version") != version
        or not isinstance(value.get("certification_id"), str)
        or not re.fullmatch(r"fxcert-[0-9a-f]{20}", value["certification_id"])
        or archive != {"filename": archive_name, "sha256": archive_sha256}
        or not isinstance(value.get("host"), dict)
        or set(value["host"]) != {"kind", "agent_id", "model"}
        or value["host"].get("kind") != "codex-isolated-subagent"
        or not all(isinstance(item, str) and item for item in value["host"].values())
        or not isinstance(value.get("challenge_set"), dict)
        or set(value["challenge_set"]) != {"sha256", "count"}
        or not isinstance(value["challenge_set"].get("count"), int)
        or value["challenge_set"]["count"] < 1
        or not re.fullmatch(r"[0-9a-f]{64}", str(value["challenge_set"].get("sha256")))
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
            or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("evidence_sha256")))
            for row in results["observations"]
        )
        or not isinstance(claims, dict)
        or set(claims) != expected_claims
        or any(claims[key] is not True for key in expected_claims)
        or not isinstance(value.get("limitations"), list)
        or not value["limitations"]
        or attestation != expected_attestation
        or value.get("certification_sha256") != _certification_hash(value)
    ):
        raise CompilerReleasePackagingError("codex_certification_invalid")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _validate_created_at(value: str) -> None:
    if not RFC3339_RE.fullmatch(value):
        raise CompilerReleasePackagingError("created_at_invalid")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CompilerReleasePackagingError("created_at_invalid") from error
    if parsed.tzinfo is None:
        raise CompilerReleasePackagingError("created_at_invalid")


def _ensure_empty_output(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise CompilerReleasePackagingError(f"output_not_empty: {path}")
    else:
        path.mkdir(parents=True)
    return path


def _source_files(skill_root: Path, *, science: bool = False) -> list[Path]:
    required = {
        "SKILL.md",
        "requirements-compiler.txt",
        "requirements-parser-extras.txt",
        "agents/openai.yaml",
        "assets/schemas/fake-expert-release.schema.json",
        "assets/schemas/compiler-codex-certification.schema.json",
        "assets/schemas/direct-reference.schema.json",
        "assets/schemas/skill-release.schema.json",
        "references/private-host-distribution.md",
        "scripts/direct_reference_runtime.py",
        "scripts/direct_reference_skill.py",
        "scripts/package_fake_expert_skill.py",
        "scripts/package_skill_release.py",
        "scripts/portable_reference_runtime.py",
        "scripts/verify_fake_expert_release.py",
        "scripts/verify_skill_release.py",
    }
    paths: list[Path] = []
    if science:
        from verify_fake_expert_release import SCIENCE_REQUIRED_PATHS, science_source_file_error
        required.update(SCIENCE_REQUIRED_PATHS)
    for path in sorted(skill_root.rglob("*")):
        relative = path.relative_to(skill_root)
        if path.is_symlink():
            raise CompilerReleasePackagingError(f"nonregular_file: {relative.as_posix()}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise CompilerReleasePackagingError(f"nonregular_file: {relative.as_posix()}")
        if any(part in {"__pycache__", ".pytest_cache"} for part in relative.parts):
            continue
        if relative.name == ".DS_Store":
            continue
        if any(part.startswith(".") for part in relative.parts):
            raise CompilerReleasePackagingError(f"hidden_file_forbidden: {relative.as_posix()}")
        if relative.parts[0] not in ALLOWED_TOP_LEVEL_DIRECTORIES | ALLOWED_TOP_LEVEL_FILES:
            raise CompilerReleasePackagingError(f"unexpected_path: {relative.as_posix()}")
        if len(relative.parts) == 1 and relative.name not in ALLOWED_TOP_LEVEL_FILES:
            raise CompilerReleasePackagingError(f"unexpected_path: {relative.as_posix()}")
        if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            raise CompilerReleasePackagingError(
                f"embedded_source_or_render_forbidden: {relative.as_posix()}"
            )
        if path.suffix.casefold() not in ALLOWED_SUFFIXES:
            raise CompilerReleasePackagingError(f"unsupported_release_file: {relative.as_posix()}")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise CompilerReleasePackagingError(
                f"non_text_release_file: {relative.as_posix()}"
            ) from error
        if science:
            error = science_source_file_error(relative.as_posix(), content)
            if error:
                raise CompilerReleasePackagingError(error + ":" + relative.as_posix())
            if relative.parent.as_posix() == "assets/domain-profiles":
                from science_workpack import domain_inputs
                domain_inputs(json.loads(content))
        paths.append(path)
    found = {path.relative_to(skill_root).as_posix() for path in paths}
    missing = sorted(required - found)
    if missing:
        raise CompilerReleasePackagingError(f"required_file_missing: {missing[0]}")
    return paths


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.flag_bits |= 0x800
    return info


def _legacy_receiver_guide() -> str:
    return f"""# fake-expert private release\n\nThis directory is a local/private transfer artifact. Do not publish it to a public\nsite or send a source PDF to another service without the source owner's explicit\nauthorization.\n\n1. Run `python3 verify_fake_expert_release.py .` before extracting the ZIP.\n2. Extract `{ARCHIVE_NAME}` into the target host's Skill staging location.\n3. Install `pypdf==6.10.0` from the extracted `fake-expert/requirements-compiler.txt`,\n   and make local `pdftoppm` available. PyMuPDF/Docling are optional local layout\n   adapters in `requirements-parser-extras.txt`; they never replace pypdf evidence\n   hashes, and Docling model-network access requires separate authorization.\n4. Run `PYTHONDONTWRITEBYTECODE=1 python3 fake-expert/scripts/environment_preflight.py --json`.\n5. Have the receiving host read `fake-expert/SKILL.md` first, then follow\n   `references/private-host-distribution.md` for its thin adapter boundary.\n\nThe archive contains the reusable compiler only. It does not contain any book PDF,\ncompiled knowledge module, hidden evaluation, review material, provider credential,\nor host transcript.\n"""


def _receiver_guide(
    *,
    archive_name: str = ARCHIVE_NAME,
    artifact_stage: str = "candidate-verified",
    source_candidate_identity: str | None = VERSION,
) -> str:
    if artifact_stage == "release-verified":
        stage_text = f"This is the formal private fake-expert compiler Skill v{VERSION} release."
        identity_text = "Formal product release identity: `release-verified`."
        boundary_text = (
            "This product release does not certify any book's knowledge. The manifest's "
            "`mvp_acceptance` block records the accepted product scope and the "
            "separate deferred-review knowledge boundary."
        )
        verify_command = "python3 verify_fake_expert_release.py . --require-release-verified"
    else:
        stage_text = "This is not a formal MVP 1.2 release; it is a source-candidate compiler artifact."
        identity_text = (
            f"Source-candidate identity: `{source_candidate_identity or VERSION}`."
        )
        boundary_text = (
            "Candidate verification does not grant release, accuracy, Gold, execution, "
            "or host certification."
        )
        verify_command = "python3 verify_fake_expert_release.py ."
    return f"""# fake-expert private release

This directory is a local/private transfer artifact. Do not publish it to a public
site or send a source PDF to another service without the source owner's explicit
authorization.

Artifact stage: `{artifact_stage}`. {identity_text}
{stage_text} {boundary_text}

1. Run `{verify_command}` before extracting the ZIP.
   Formal v1.2.0 verification also checks `{CODEX_CERTIFICATION_NAME}` against the
   exact archive SHA; the certificate does not claim per-book knowledge accuracy.
2. Extract `{archive_name}` into the target host's Skill staging location.
3. Install `pypdf==6.10.0` and make local `pdftoppm` available. PaddleOCR/PP-Structure
   is the explicit primary scan adapter, Docling is an explicit challenger, and
   PyMuPDF/Tesseract is an optional baseline. The release never downloads models,
   enables network OCR, or silently changes the selected backend.
4. Run `PYTHONDONTWRITEBYTECODE=1 python3 fake-expert/scripts/environment_preflight.py --json`.
5. Have the receiving host read `fake-expert/SKILL.md` first, then follow
   `references/private-host-distribution.md`, `references/semantic-promotion.md`,
   and `references/scanned-pdf-ocr.md`. The v0.7 visual-semantics protocol
   requires externally authored risk-tiered review attestations and cannot be
   bypassed by the local pipeline orchestrator. Whole-book planning emits
   page/segment/module coverage records and leaves structure, OCR, semantic,
   visual, competency, seal, and execution gates explicit. The incremental
   build DAG is content-addressed and the semantic Composer is source-attributed
   and pause-only: they compute invalidation/alignment candidates and
   prepared-not-executed intent receipts but cannot author review, seal, sign, certify,
   or execute an artifact.

For the shortest candidate-knowledge path, run
`scripts/direct_reference_skill.py build` against a validated semantic workpack.
It creates a source-free, queryable `fake-*` Reference Skill at lifecycle `draft`.
Gold defaults to `none`; `bind-gold` can later create, reuse, or update a hash-only
binding in a new version, and `extend` adds another workpack without mutating the
base package. Package such candidates only with
`scripts/package_skill_release.py --artifact-stage draft-distributable`.
Every query carries `unreviewed-candidate`; Gold is never promotion authority.

The unified Phase 7D.1 visual parser orchestrator is local and candidate-only.
`paddleocr-ppocrv6` is the explicit text route and `paddleocr-ppstructure-v3` is
the separate structured table/layout route; Docling is the challenger and
PyMuPDF/Tesseract the optional baseline. A fake backend is accepted only with the
job's explicit synthetic test-only authorization and is marked `synthetic_only`;
it cannot enter the DAG or Composer as real evidence. Raster jobs freeze an
allowlisted external runtime/model/worker contract and send only a temporary crop
over fixed JSONL, never the source PDF. Missing structure models are not-run and
cannot be replaced by PP-OCR text topology.

Phase 7D.3 adds a deterministic selective visual policy. New jobs default to
`visual-mode=auto`; `off` records `not-inspected-by-policy` without disabling
正文 OCR, `auto` runs a no-model census before bounded visual work, and `full`
routes the selected scope. `visual-kinds` is closed to table/figure/chart/diagram/
equation, budgets pause with explicit receipts, and `visual-context=index` keeps
raw crops/OCR text outside default Agent context. The production crop path is
`run_external_worker_batch`: one bounded JSONL session per backend/profile loads
the model at most once, applies hard total/per-request timeouts, and retains
partial item errors. The optional `technical-chart-v2` route uses the local
`paddleocr.ChartParsing` API with `PP-Chart2Table`, explicit `paddle_dynamic`,
and batch size 1. PP-StructureV3 and Chart2Table must first pass a matching
host-local repeated-crop qualification receipt; otherwise the route pauses as
`paused-runtime-unqualified`. Qualification proves only runtime/profile binding
and repeatability, not accuracy. Missing model/runtime inventory is fail-closed
and chart rows remain candidate-only, never promotion or executable evidence.
Census region bboxes are coarse page-envelope route
hints, not confirmed chart localization.

The archive contains the reusable compiler only. It does not contain any book PDF,
page render, OCR transcript, model, compiled knowledge module, hidden evaluation,
review material, provider credential, or host transcript.
"""


def package_fake_expert_skill(
    skill_root: Path,
    output: Path,
    *,
    created_at: str,
    version: str = VERSION,
    artifact_stage: str = "candidate-verified",
    codex_certification: Path | None = None,
) -> dict[str, Any]:
    """Package the checked-in compiler Skill without including source material."""

    _validate_created_at(created_at)
    if version not in SOURCE_CANDIDATE_VERSIONS:
        raise CompilerReleasePackagingError("unsupported_release_version")
    if artifact_stage not in ARTIFACT_STAGES:
        raise CompilerReleasePackagingError("artifact_stage_invalid")
    if artifact_stage == "release-verified" and version != VERSION:
        raise CompilerReleasePackagingError("release_stage_requires_current_version")
    if artifact_stage == "release-verified" and codex_certification is None:
        raise CompilerReleasePackagingError("codex_certification_required")
    if artifact_stage == "candidate-verified" and codex_certification is not None:
        raise CompilerReleasePackagingError("candidate_codex_certification_forbidden")
    skill_root = skill_root.expanduser().resolve()
    if skill_root.name != SKILL_NAME or not skill_root.is_dir():
        raise CompilerReleasePackagingError(f"skill_root_invalid: {skill_root}")
    entrypoint = skill_root / "SKILL.md"
    try:
        skill_text = entrypoint.read_text(encoding="utf-8")
    except OSError as error:
        raise CompilerReleasePackagingError("skill_entrypoint_missing") from error
    if not skill_text.startswith("---\nname: fake-expert\n"):
        raise CompilerReleasePackagingError("skill_entrypoint_identity_invalid")
    files = _source_files(skill_root, science=version == SCIENCE_LIFECYCLE_COMPILER_VERSION)
    output = _ensure_empty_output(output)
    archive_name = f"{SKILL_NAME}-v{version}.zip"
    archive_path = output / archive_name
    inventory = []
    uncompressed_bytes = 0
    with zipfile.ZipFile(
        archive_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for path in files:
            relative = path.relative_to(skill_root).as_posix()
            data = path.read_bytes()
            uncompressed_bytes += len(data)
            inventory.append(
                {"path": relative, "sha256": sha256_bytes(data), "bytes": len(data)}
            )
            archive.writestr(
                _zip_info(f"{SKILL_NAME}/{relative}"),
                data,
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
    certification_value = None
    certification_target = None
    if artifact_stage == "release-verified":
        assert codex_certification is not None
        certification_source = codex_certification.expanduser().resolve()
        certification_value = _validate_codex_certification(
            certification_source,
            archive_name=archive_name,
            archive_sha256=sha256_file(archive_path),
            version=version,
        )
        certification_target = output / CODEX_CERTIFICATION_NAME
        shutil.copyfile(certification_source, certification_target)
    verifier_source = skill_root / "scripts" / VERIFIER_NAME
    verifier_text = verifier_source.read_text(encoding="utf-8")
    standalone_bindings = {
        "VERSION": VERSION,
        "UX_SOURCE_CANDIDATE_VERSION": UX_SOURCE_CANDIDATE_VERSION,
        "SCIENCE_LIFECYCLE_COMPILER_VERSION": SCIENCE_LIFECYCLE_COMPILER_VERSION,
        "DIRECT_REFERENCE_COMPILER_VERSION": DIRECT_REFERENCE_COMPILER_VERSION,
        "DIRECT_REFERENCE_PROTOCOL": DIRECT_REFERENCE_PROTOCOL,
        "DIRECT_REFERENCE_SCHEMA": DIRECT_REFERENCE_SCHEMA,
    }
    for binding, expected in standalone_bindings.items():
        match = re.search(
            rf"{binding} = \"([^\"]+)\"  # standalone-release fallback",
            verifier_text,
        )
        if match is None or match.group(1) != expected:
            raise CompilerReleasePackagingError("verifier_version_mismatch")
    verifier_target = output / VERIFIER_NAME
    shutil.copyfile(verifier_source, verifier_target)
    receiver_target = output / RECEIVER_GUIDE_NAME
    receiver_target.write_text(
        _receiver_guide(
            archive_name=archive_name,
            artifact_stage=artifact_stage,
            source_candidate_identity=version if artifact_stage == "candidate-verified" else None,
        ),
        encoding="utf-8",
    )
    release = {
        "schema_version": RELEASE_SCHEMA,
        "artifact_stage": artifact_stage,
        "release_created_at": created_at,
        "compiler": {
            "skill_name": SKILL_NAME,
            "version": version,
            "root_directory": SKILL_NAME,
            "entrypoint": "SKILL.md",
            "entrypoint_sha256": sha256_file(entrypoint),
            "file_count": len(inventory),
            "uncompressed_bytes": uncompressed_bytes,
            "files": inventory,
        },
        "archive": {
            "filename": archive_name,
            "sha256": sha256_file(archive_path),
            "file_count": len(inventory),
            "uncompressed_bytes": uncompressed_bytes,
            "format": "zip",
            "timestamp_profile": "dos-epoch-fixed-v1",
        },
        "runtime": {
            "python": ">=3.11",
            "dependencies": ["pypdf==6.10.0"],
            "required_local_commands": ["pdftoppm"],
            "network_required": False,
            "optional_local_parser_adapters": {
                "requirements": "requirements-parser-extras.txt",
                "network_required": False,
            },
            "direct_reference": {
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
            },
            "scan_ocr": {
                "compiler_version": SCAN_PDF_COMPILER_VERSION,
                "scan_ir_schema": "tkc.scanned-pdf-ir/v0.2",
                "protocol_schema": OCR_PROTOCOL_SCHEMA,
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
                "paddle_runtime_protocol": PADDLE_RUNTIME_PROTOCOL,
                "paddle_runtime_contract_schema": PADDLE_RUNTIME_CONTRACT_SCHEMA,
                "paddle_runtime_profile_schema": PADDLE_RUNTIME_PROFILE_SCHEMA,
                "paddle_result_adapter_schema": PADDLE_RESULT_ADAPTER_SCHEMA,
                "paddle_result_adapter_protocol": PADDLE_RESULT_ADAPTER_PROTOCOL,
                "paddle_ocr_result_schema": PADDLE_OCR_RESULT_SCHEMA,
                "paddle_ocr_result_protocol": PADDLE_OCR_RESULT_PROTOCOL,
                "structure_review_schema": SCANNED_PDF_STRUCTURE_REVIEW_SCHEMA,
                "structure_review_protocol": SCANNED_PDF_STRUCTURE_REVIEW_PROTOCOL,
                "reviewed_structure_is_bounded": True,
                "ocr_candidate_only_until_independent_review": True,
                "model_inventory_required": True,
                "ppocr_text_is_not_table_topology": True,
                "scan_anchor_protocol": SCAN_ANCHOR_PROTOCOL,
                "contract_hardening_source_candidate": SCAN_MVP_CONTRACT_HARDENING_COMPILER_VERSION,
                "runtime_worker_binding_required": True,
                "object_exact_evidence_binding": True,
                "scan_integrity_verifies_commitments": True,
                "scan_full_replay_requires_exact_runtime_worker": True,
            },
            "external_paddle_runtime": {
                "protocol": PADDLE_RUNTIME_PROTOCOL,
                "contract_schema": PADDLE_RUNTIME_CONTRACT_SCHEMA,
                "profile_schema": PADDLE_RUNTIME_PROFILE_SCHEMA,
                "result_adapter_schema": PADDLE_RESULT_ADAPTER_SCHEMA,
                "result_adapter_protocol": PADDLE_RESULT_ADAPTER_PROTOCOL,
                "text_result_adapter_schema": PADDLE_OCR_RESULT_SCHEMA,
                "text_result_adapter_protocol": PADDLE_OCR_RESULT_PROTOCOL,
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
            },
            "paddle_runtime_qualification": {
                "compiler_version": PADDLE_RUNTIME_QUALIFICATION_COMPILER_VERSION,
                "protocol": PADDLE_RUNTIME_QUALIFICATION_PROTOCOL,
                "plan_schema": PADDLE_RUNTIME_QUALIFICATION_PLAN_SCHEMA,
                "receipt_schema": PADDLE_RUNTIME_QUALIFICATION_SCHEMA,
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
            },
            "semantic_assurance": {
                "compiler_version": SEMANTIC_ASSURANCE_COMPILER_VERSION,
                "protocol": SEMANTIC_ASSURANCE_PROTOCOL,
                "assertion_schema": "tkc.semantic-assertion/v0.2",
                "support_schema": "tkc.support-matrix/v0.1",
                "attestation_schema": "tkc.review-attestation/v0.1",
                "coverage_schema": "tkc.coverage-ledger/v0.1",
                "attestation_level": "host-orchestrator-recorded-not-cryptographic",
                "review_bypass_allowed": False,
                "whole_book_completeness_claimed": False,
            },
            "heading_locator": {
                "protocol": HEADING_LOCATOR_PROTOCOL,
                "candidate_schema": "tkc.heading-candidate/v0.2",
                "resolution_schema": "tkc.heading-resolution/v0.2",
                "override_schema": "tkc.heading-locator-override/v0.2",
                "workpack_consumes_resolution": True,
                "canonical_evidence_hash_unchanged": True,
            },
            "whole_book_planning": {
                "compiler_version": WHOLE_BOOK_PLANNER_COMPILER_VERSION,
                "protocol": WHOLE_BOOK_PLANNER_PROTOCOL,
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
            },
            "visual_semantics": {
                "compiler_version": VISUAL_SEMANTICS_COMPILER_VERSION,
                "protocol": VISUAL_SEMANTICS_PROTOCOL,
                "object_schema": VISUAL_OBJECT_SCHEMA,
                "relation_schema": VISUAL_RELATION_SCHEMA,
                "table_grid_schema": TABLE_GRID_SCHEMA,
                "conflict_schema": VISUAL_CONFLICT_SCHEMA,
                "review_attestation_schema": VISUAL_REVIEW_ATTESTATION_SCHEMA,
                "review_protocol": VISUAL_REVIEW_PROTOCOL,
                "coordinate_space": "pdf-page-top-left-points-v1",
                "canonical_native_extractor": "pypdf==6.10.0",
                "canonical_visual_renderer": "pdftoppm",
                "candidate_only": True,
                "model_download": False,
                "network_enabled": False,
                "whole_book_completeness_claimed": False,
                "external_review_fragments_required": True,
                "compiler_authored_review_conclusions": False,
                "assurance_manifest_schema": VISUAL_ASSURANCE_MANIFEST_SCHEMA,
            },
            "incremental_build_dag": {
                "compiler_version": INCREMENTAL_BUILD_DAG_COMPILER_VERSION,
                "protocol": INCREMENTAL_BUILD_DAG_PROTOCOL,
                "manifest_schema": INCREMENTAL_DAG_MANIFEST_SCHEMA,
                "node_schema": INCREMENTAL_DAG_NODE_SCHEMA,
                "edge_schema": INCREMENTAL_DAG_EDGE_SCHEMA,
                "change_set_schema": INCREMENTAL_CHANGE_SET_SCHEMA,
                "plan_schema": INCREMENTAL_BUILD_PLAN_SCHEMA,
                "state_schema": INCREMENTAL_BUILD_STATE_SCHEMA,
                "receipt_schema": INCREMENTAL_BUILD_RECEIPT_SCHEMA,
                "content_addressed": True,
                "typed_edges": True,
                "reuse_requires_exact_hash_protocol_dependencies": True,
                "pause_only_resume": True,
                "review_authoring": False,
                "seal": False,
                "signing": False,
                "execution": False,
                "source_material_included": False,
            },
            "semantic_composer": {
                "compiler_version": SEMANTIC_COMPOSER_COMPILER_VERSION,
                "protocol": SEMANTIC_COMPOSER_PROTOCOL,
                "workpack_schema": COMPOSER_WORKPACK_SCHEMA,
                "input_schema": COMPOSER_INPUT_SCHEMA,
                "concept_ref_schema": CONCEPT_REF_SCHEMA,
                "alignment_schema": ALIGNMENT_CANDIDATE_SCHEMA,
                "difference_schema": DIFFERENCE_SCHEMA,
                "conflict_schema": COMPOSER_CONFLICT_SCHEMA,
                "evidence_binding_schema": EVIDENCE_BINDING_SCHEMA,
                "unit_map_schema": UNIT_MAP_SCHEMA,
                "review_plan_schema": COMPOSER_REVIEW_PLAN_SCHEMA,
                "review_attestation_schema": COMPOSER_REVIEW_ATTESTATION_SCHEMA,
                "receipt_schema": COMPOSER_RECEIPT_SCHEMA,
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
            },
            "visual_parser_orchestrator": {
                "compiler_version": VISUAL_PARSER_ORCHESTRATOR_COMPILER_VERSION,
                "protocol": VISUAL_PARSER_ORCHESTRATOR_PROTOCOL,
                "job_schema": VISUAL_PARSER_JOB_SCHEMA,
                "manifest_schema": VISUAL_PARSER_MANIFEST_SCHEMA,
                "evaluation_contract_schema": VISUAL_EVALUATION_CONTRACT_SCHEMA,
                "review_overlay_schema": VISUAL_REVIEW_OVERLAY_SCHEMA,
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
                "external_runtime_contract_schema": PADDLE_RUNTIME_CONTRACT_SCHEMA,
                "external_runtime_protocol": PADDLE_RUNTIME_PROTOCOL,
                "review_authoring": False,
                "promotion": False,
                "pause_only_resume": True,
                "source_material_included": False,
            },
            "selective_visual": {
                "compiler_version": SELECTIVE_VISUAL_COMPILER_VERSION,
                "protocol": SELECTIVE_VISUAL_PROTOCOL,
                "policy_schema": VISUAL_POLICY_SCHEMA,
                "census_schema": VISUAL_CENSUS_SCHEMA,
                "routing_schema": VISUAL_ROUTING_SCHEMA,
                "budget_receipt_schema": VISUAL_BUDGET_RECEIPT_SCHEMA,
                "index_schema": VISUAL_INDEX_SCHEMA,
                "coverage_schema": VISUAL_COVERAGE_SCHEMA,
                "batch_receipt_schema": VISUAL_BATCH_RECEIPT_SCHEMA,
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
                "chart_profile": TECHNICAL_CHART_PROFILE_ID,
                "chart_protocol": TECHNICAL_CHART_PROFILE_PROTOCOL,
                "chart_backend": TECHNICAL_CHART_BACKEND,
                "chart_api": TECHNICAL_CHART_API,
                "chart_result_schema": TECHNICAL_CHART_RESULT_SCHEMA,
                "chart_result_protocol": TECHNICAL_CHART_RESULT_PROTOCOL,
                "chart_candidate_schema": TECHNICAL_CHART_CANDIDATE_SCHEMA,
                "chart_model_binding": {
                    "model_name": TECHNICAL_CHART_MODEL_NAME,
                    "model_dir": TECHNICAL_CHART_MODEL_DIR,
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
            },
        },
        "distribution": {
            "visibility": "private-transfer-only",
            "public_publication": False,
            "adapter_policy": "thin-host-adapter-only",
        },
        "restrictions": {
            "source_pdf_included": False,
            "page_renders_included": False,
            "compiled_knowledge_included": False,
            "hidden_evaluations_included": False,
            "review_material_included": False,
            "credentials_included": False,
            "host_transcripts_included": False,
        },
        "verifier": {
            "filename": VERIFIER_NAME,
            "sha256": sha256_file(verifier_target),
            "interpreter": "python3-standard-library",
        },
        "receiver_guide": {
            "filename": RECEIVER_GUIDE_NAME,
            "sha256": sha256_file(receiver_target),
        },
    }
    if artifact_stage == "candidate-verified":
        release["source_candidate_identity"] = version
    else:
        release["mvp_acceptance"] = MVP_ACCEPTANCE_SCOPE
        assert certification_value is not None and certification_target is not None
        release["codex_certification"] = {
            "filename": CODEX_CERTIFICATION_NAME,
            "sha256": sha256_file(certification_target),
            "schema_version": CODEX_CERTIFICATION_SCHEMA,
            "certification_id": certification_value["certification_id"],
            "archive_sha256": certification_value["archive"]["sha256"],
        }
    manifest_path = output / "release-manifest.json"
    _write_json(manifest_path, release)
    checksum_paths = [archive_path, manifest_path, verifier_target, receiver_target]
    if certification_target is not None:
        checksum_paths.append(certification_target)
    (output / "SHA256SUMS").write_text(
        "".join(f"{sha256_file(path)}  {path.name}\n" for path in sorted(checksum_paths)),
        encoding="utf-8",
    )
    return release


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skill_root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--version", default=VERSION)
    parser.add_argument(
        "--artifact-stage",
        choices=sorted(ARTIFACT_STAGES),
        default="candidate-verified",
        help="Keep the default candidate stage; select release-verified explicitly for the formal compiler release.",
    )
    parser.add_argument(
        "--codex-certification",
        type=Path,
        help="Required external exact-archive Codex certification for release-verified packaging.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = package_fake_expert_skill(
            args.skill_root,
            args.output,
            created_at=args.created_at,
            version=args.version,
            artifact_stage=args.artifact_stage,
            codex_certification=args.codex_certification,
        )
    except (CompilerReleasePackagingError, OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
