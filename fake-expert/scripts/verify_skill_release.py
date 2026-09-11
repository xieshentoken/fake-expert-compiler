#!/usr/bin/env python3
"""Verify and optionally safely extract a TKC Skill release using the standard library."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


RELEASE_SCHEMA = "tkc.skill-release/v0.2"
DIRECT_REFERENCE_SCHEMA = "tkc.direct-reference/v0.1"
DIRECT_REFERENCE_PROTOCOL = "direct-reference-draft-v0.1"
DIRECT_REFERENCE_COMPILER_VERSION = "1.1.0-direct-reference"
DIRECT_REFERENCE_RUNTIME_SHA256 = "4373790b353306851ca92debab4dc3e1b5a2c8c64178ade857b79cd99f34f19b"
PORTABLE_REFERENCE_RUNTIME_SHA256 = "50de12e92c840806121d898d1f44d36c57d80e81ad13ef82e08b4417c6ba5581"
FORBIDDEN_SUFFIXES = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".j2k",
    ".jpeg",
    ".jpg",
    ".jp2",
    ".pdf",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
}


class ReleaseVerificationError(RuntimeError):
    """Stable release-verification failure."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
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


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseVerificationError(f"invalid_json: {path.name}") from error
    if not isinstance(value, dict):
        raise ReleaseVerificationError(f"invalid_json_object: {path.name}")
    return value


def _archive_json(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    relative: str,
    error_code: str,
) -> dict[str, Any]:
    info = members.get(relative)
    if info is None:
        raise ReleaseVerificationError(error_code)
    try:
        value = json.loads(archive.read(info).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseVerificationError(error_code) from error
    if not isinstance(value, dict):
        raise ReleaseVerificationError(error_code)
    return value


def _archive_jsonl(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    relative: str,
    error_code: str,
) -> list[dict[str, Any]]:
    info = members.get(relative)
    if info is None:
        raise ReleaseVerificationError(error_code)
    try:
        lines = archive.read(info).decode("utf-8").splitlines()
        values = [json.loads(line) for line in lines if line]
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseVerificationError(error_code) from error
    if any(not isinstance(value, dict) for value in values):
        raise ReleaseVerificationError(error_code)
    return values


def _prefixed(prefix: str, relative: Any, error_code: str) -> str:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ReleaseVerificationError(error_code)
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise ReleaseVerificationError(error_code)
    return f"{prefix}{pure.as_posix()}"


def _verify_ready_reference_evidence(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    *,
    prefix: str = "",
) -> str:
    """Verify the evidence shape for one ready reference package.

    Phase-0 ``fixture-validated`` packages are retained only as an explicit
    legacy-contract compatibility profile. Modern packages must bind independent
    review/promotion records and recomputable competency receipts.
    """

    error_code = "ready_reference_evidence_invalid"
    manifest = _archive_json(archive, members, f"{prefix}manifest.json", error_code)
    if (
        manifest.get("schema_version") != "tkc.expert-skill/v0.1"
        or manifest.get("package", {}).get("status") != "ready"
        or manifest.get("capabilities")
        not in (
            {"reference": True, "decision_support": False, "executable": False},
            {"reference": True, "decision_support": False, "executable": True},
        )
        or manifest.get("distribution", {}).get("source_mode") != "source-required"
    ):
        raise ReleaseVerificationError(error_code)
    index = _archive_json(
        archive,
        members,
        f"{prefix}references/knowledge/index.json",
        error_code,
    )
    object_rows = index.get("objects")
    if not isinstance(object_rows, list) or not object_rows:
        raise ReleaseVerificationError(error_code)
    objects: dict[str, dict[str, Any]] = {}
    for row in object_rows:
        object_id = row.get("id") if isinstance(row, dict) else None
        relative = row.get("path") if isinstance(row, dict) else None
        if (
            not isinstance(object_id, str)
            or not object_id
            or object_id in objects
            or relative != f"references/knowledge/objects/{object_id}.json"
        ):
            raise ReleaseVerificationError(error_code)
        obj = _archive_json(
            archive, members, f"{prefix}{relative}", error_code
        )
        if obj.get("id") != object_id:
            raise ReleaseVerificationError(error_code)
        objects[object_id] = obj

    compiler_version = manifest.get("build", {}).get("compiler_version")
    legacy = (
        compiler_version == "0.1.0-phase0"
        and all(obj.get("status") == "fixture-validated" for obj in objects.values())
    )
    if legacy:
        tests = _archive_jsonl(
            archive,
            members,
            f"{prefix}references/competency-tests.jsonl",
            error_code,
        )
        required_categories = {
            "definition",
            "comparison",
            "equation",
            "decision",
            "failure-condition",
            "trace",
            "out-of-scope",
        }
        if (
            not tests
            or {row.get("category") for row in tests} != required_categories
            or any(row.get("result", {}).get("status") != "passed" for row in tests)
        ):
            raise ReleaseVerificationError(error_code)
        return "legacy-phase0-contract"

    for obj in objects.values():
        binding = obj.get("review_binding")
        if (
            obj.get("status") != "independently-reviewed-draft"
            or not isinstance(binding, dict)
            or not isinstance(binding.get("reviewer_instance"), str)
            or not binding["reviewer_instance"]
            or not all(
                isinstance(binding.get(field), str)
                and len(binding[field]) == 64
                and all(character in "0123456789abcdef" for character in binding[field])
                for field in (
                    "item_sha256",
                    "review_bundle_sha256",
                    "review_input_sha256",
                )
            )
        ):
            raise ReleaseVerificationError(error_code)

    required = {
        "references/evidence/promotion-report.json",
        "references/reviews/review-plan.json",
        "references/reviews/semantic-records.jsonl",
        "references/reviews/visual-receipts.jsonl",
        "references/evaluations/evaluation-plan.json",
        "references/evaluations/runtime-responses.jsonl",
        "references/evaluations/competency-receipts.jsonl",
        "references/competency-tests.jsonl",
        "references/runtime/catalog.json",
        "references/runtime/reference-map.md",
        "scripts/query_reference.py",
    }
    if any(f"{prefix}{relative}" not in members for relative in required):
        raise ReleaseVerificationError(error_code)
    report = _archive_json(
        archive,
        members,
        f"{prefix}references/evidence/promotion-report.json",
        error_code,
    )
    records = _archive_jsonl(
        archive,
        members,
        f"{prefix}references/reviews/semantic-records.jsonl",
        error_code,
    )
    record_by_ref: dict[str, dict[str, Any]] = {}
    for record in records:
        item_ref = record.get("item_ref")
        if not isinstance(item_ref, str) or not item_ref or item_ref in record_by_ref:
            raise ReleaseVerificationError(error_code)
        record_by_ref[item_ref] = record
    promoted_objects: set[str] = set()
    items = report.get("items")
    if not isinstance(items, list) or not items:
        raise ReleaseVerificationError(error_code)
    for item in items:
        if not isinstance(item, dict):
            raise ReleaseVerificationError(error_code)
        promoted_id = item.get("promoted_id")
        if promoted_id in objects:
            item_ref = item.get("item_ref")
            record = record_by_ref.get(item_ref)
            if (
                item.get("verdict") != "accepted"
                or record is None
                or item.get("review_record_sha256") != sha256_json(record)
            ):
                raise ReleaseVerificationError(error_code)
            promoted_objects.add(promoted_id)
    if promoted_objects != set(objects):
        raise ReleaseVerificationError(error_code)

    plan = _archive_json(
        archive,
        members,
        f"{prefix}references/evaluations/evaluation-plan.json",
        error_code,
    )
    catalog_path = _prefixed(prefix, plan.get("catalog"), error_code)
    runtime_path = _prefixed(prefix, plan.get("runtime"), error_code)
    suite_path = _prefixed(prefix, plan.get("test_suite"), error_code)
    catalog = _archive_json(archive, members, catalog_path, error_code)
    tests = _archive_jsonl(archive, members, suite_path, error_code)
    if (
        runtime_path not in members
        or sha256_json(catalog) != plan.get("catalog_sha256")
        or sha256_json(tests) != plan.get("test_suite_sha256")
        or sha256_bytes(archive.read(members[runtime_path]))
        != plan.get("runtime_sha256")
    ):
        raise ReleaseVerificationError(error_code)
    responses = _archive_jsonl(
        archive,
        members,
        _prefixed(prefix, plan.get("responses"), error_code),
        error_code,
    )
    receipts = _archive_jsonl(
        archive,
        members,
        _prefixed(prefix, plan.get("receipts"), error_code),
        error_code,
    )
    if (
        not tests
        or len(tests) != plan.get("test_count")
        or len(responses) != len(tests)
        or len(receipts) != len(tests)
    ):
        raise ReleaseVerificationError(error_code)
    tests_by_id = {row.get("id"): row for row in tests}
    responses_by_id = {row.get("test_id"): row for row in responses}
    if len(tests_by_id) != len(tests) or len(responses_by_id) != len(responses):
        raise ReleaseVerificationError(error_code)
    for receipt in receipts:
        test_id = receipt.get("test_id")
        test = tests_by_id.get(test_id)
        response = responses_by_id.get(test_id)
        checks = receipt.get("checks")
        if (
            test is None
            or response is None
            or receipt.get("verdict") != "passed"
            or receipt.get("run_id") != plan.get("run_id")
            or receipt.get("runtime_sha256") != plan.get("runtime_sha256")
            or receipt.get("catalog_sha256") != plan.get("catalog_sha256")
            or receipt.get("test_sha256") != sha256_json(test)
            or receipt.get("response_sha256") != sha256_json(response)
            or not isinstance(checks, list)
            or not checks
            or any(
                not isinstance(check, dict) or check.get("passed") is not True
                for check in checks
            )
        ):
            raise ReleaseVerificationError(error_code)
    return "independent-review-promotion-competency"


def _verify_ready_composite_evidence(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
) -> list[str]:
    error_code = "ready_composite_evidence_invalid"
    lock = _archive_json(archive, members, "composition-lock.json", error_code)
    modules = lock.get("modules")
    if not isinstance(modules, list) or not modules:
        raise ReleaseVerificationError(error_code)
    profiles: list[str] = []
    seen_paths: set[str] = set()
    for row in modules:
        relative_path = row.get("relative_path") if isinstance(row, dict) else None
        if (
            not isinstance(relative_path, str)
            or not relative_path.startswith("modules/")
            or relative_path in seen_paths
        ):
            raise ReleaseVerificationError(error_code)
        seen_paths.add(relative_path)
        manifest_path = f"{relative_path}/manifest.json"
        child = _archive_json(archive, members, manifest_path, error_code)
        if (
            sha256_bytes(archive.read(members[manifest_path]))
            != row.get("manifest_sha256")
            or sha256_json(child.get("integrity")) != row.get("integrity_sha256")
            or child.get("package", {}).get("id") != row.get("package_id")
            or child.get("package", {}).get("version") != row.get("package_version")
            or child.get("capabilities") != row.get("capabilities")
            or [module.get("module_id") for module in child.get("modules", [])]
            != row.get("module_ids")
        ):
            raise ReleaseVerificationError(error_code)
        profiles.append(
            _verify_ready_reference_evidence(
                archive, members, prefix=f"{relative_path}/"
            )
        )
    catalog = _archive_json(
        archive, members, "references/runtime/catalog.json", error_code
    )
    for entry in catalog.get("objects", []):
        path = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(path, str) or path not in members:
            raise ReleaseVerificationError(error_code)
    return profiles


def _verify_ready_execution_evidence(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
) -> None:
    error_code = "ready_execution_evidence_invalid"
    execution = _archive_json(
        archive, members, "references/execution/manifest.json", error_code
    )
    policy_path = _prefixed("", execution.get("policy"), error_code)
    suite_path = _prefixed("", execution.get("test_suite"), error_code)
    receipts_path = _prefixed("", execution.get("receipts"), error_code)
    runner = execution.get("runner")
    if not isinstance(runner, dict):
        raise ReleaseVerificationError(error_code)
    runner_path = _prefixed("", runner.get("entrypoint"), error_code)
    policy = _archive_json(archive, members, policy_path, error_code)
    suite = _archive_json(archive, members, suite_path, error_code)
    if (
        runner_path not in members
        or sha256_json(policy) != execution.get("policy_sha256")
        or sha256_json(suite) != execution.get("test_suite_sha256")
        or sha256_bytes(archive.read(members[runner_path])) != runner.get("sha256")
    ):
        raise ReleaseVerificationError(error_code)
    if (
        policy.get("schema_version") != "tkc.execution-policy/v0.1"
        or policy.get("execution_model") != "formula-ast-only"
        or policy.get("requires_independent_test_author") is not True
        or policy.get("requires_sealed_input") is not True
        or not isinstance(policy.get("timeout_seconds"), int)
        or policy["timeout_seconds"] < 1
        or not isinstance(policy.get("memory_limit_mib"), int)
        or policy["memory_limit_mib"] < 1
        or "denied" not in str(policy.get("network", ""))
        or "no-file-api" not in str(policy.get("filesystem", ""))
        or not isinstance(policy.get("allowed_imports"), list)
    ):
        raise ReleaseVerificationError(error_code)
    author = suite.get("author")
    cases = suite.get("cases")
    if (
        not isinstance(author, dict)
        or not isinstance(author.get("instance"), str)
        or not author["instance"]
        or author.get("instance") == execution.get("compiler_instance")
        or not isinstance(cases, list)
        or not cases
    ):
        raise ReleaseVerificationError(error_code)
    cases_by_id = {
        row.get("id"): row
        for row in cases
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    receipts = _archive_jsonl(archive, members, receipts_path, error_code)
    receipts_by_id = {
        row.get("receipt_id"): row
        for row in receipts
        if isinstance(row.get("receipt_id"), str)
    }
    if len(cases_by_id) != len(cases) or len(receipts_by_id) != len(receipts):
        raise ReleaseVerificationError(error_code)
    formulas = execution.get("formulas")
    if not isinstance(formulas, list) or not formulas:
        raise ReleaseVerificationError(error_code)
    referenced_cases: set[str] = set()
    referenced_receipts: set[str] = set()
    for formula in formulas:
        if not isinstance(formula, dict):
            raise ReleaseVerificationError(error_code)
        object_id = formula.get("object_id")
        object_path = f"references/knowledge/objects/{object_id}.json"
        entrypoint = _prefixed("", formula.get("entrypoint"), error_code)
        if object_path not in members or entrypoint not in members:
            raise ReleaseVerificationError(error_code)
        obj = _archive_json(archive, members, object_path, error_code)
        binding = obj.get("review_binding")
        ast = obj.get("formula", {}).get("ast")
        case_ids = formula.get("test_case_ids")
        receipt_ids = formula.get("receipt_ids")
        if (
            not isinstance(binding, dict)
            or formula.get("review_binding_sha256") != sha256_json(binding)
            or not isinstance(ast, dict)
            or formula.get("formula_ast_sha256") != sha256_json(ast)
            or sha256_bytes(archive.read(members[entrypoint]))
            != formula.get("module_sha256")
            or not isinstance(case_ids, list)
            or not case_ids
            or not all(isinstance(value, str) for value in case_ids)
            or not isinstance(receipt_ids, list)
            or not receipt_ids
            or not all(isinstance(value, str) for value in receipt_ids)
            or not set(case_ids).issubset(cases_by_id)
            or not set(receipt_ids).issubset(receipts_by_id)
            or author.get("instance") == binding.get("reviewer_instance")
        ):
            raise ReleaseVerificationError(error_code)
        referenced_cases.update(case_ids)
        referenced_receipts.update(receipt_ids)
        for receipt_id in receipt_ids:
            receipt = receipts_by_id[receipt_id]
            case = cases_by_id.get(receipt.get("case_id"))
            if (
                case is None
                or receipt.get("verdict") != "passed"
                or receipt.get("object_id") != object_id
                or receipt.get("module_sha256") != formula.get("module_sha256")
                or receipt.get("policy_sha256") != execution.get("policy_sha256")
                or receipt.get("test_case_sha256") != sha256_json(case)
                or receipt.get("timeout_seconds") != policy.get("timeout_seconds")
                or receipt.get("memory_limit_mib") != policy.get("memory_limit_mib")
            ):
                raise ReleaseVerificationError(error_code)
    if referenced_cases != set(cases_by_id) or referenced_receipts != set(receipts_by_id):
        raise ReleaseVerificationError(error_code)


def _verify_checksums(release_dir: Path) -> None:
    sums_path = release_dir / "SHA256SUMS"
    try:
        lines = [line for line in sums_path.read_text(encoding="utf-8").splitlines() if line]
    except OSError as error:
        raise ReleaseVerificationError("checksums_missing") from error
    expected_names = set()
    for line in lines:
        if not re_fullmatch_sha_line(line):
            raise ReleaseVerificationError("checksums_invalid")
        digest, name = line.split("  ", 1)
        if name in expected_names or "/" in name or "\\" in name:
            raise ReleaseVerificationError("checksums_invalid")
        expected_names.add(name)
        path = release_dir / name
        if not path.is_file() or path.is_symlink() or sha256_file(path) != digest:
            raise ReleaseVerificationError(f"checksum_mismatch: {name}")
    if expected_names != {
        "release-manifest.json",
        "verify_skill_release.py",
        next((name for name in expected_names if name.endswith(".zip")), ""),
    }:
        raise ReleaseVerificationError("checksums_inventory_invalid")


def re_fullmatch_sha_line(value: str) -> bool:
    if len(value) < 67 or value[64:66] != "  ":
        return False
    digest = value[:64]
    name = value[66:]
    return all(character in "0123456789abcdef" for character in digest) and bool(name)


def _safe_member(name: str, root_name: str) -> str:
    if "\\" in name:
        raise ReleaseVerificationError(f"archive_path_invalid: {name}")
    pure = PurePosixPath(name)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise ReleaseVerificationError(f"archive_path_invalid: {name}")
    if pure.parts[0] != root_name or len(pure.parts) < 2:
        raise ReleaseVerificationError(f"archive_root_invalid: {name}")
    return PurePosixPath(*pure.parts[1:]).as_posix()


def _ensure_extract_target(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise ReleaseVerificationError(f"extract_target_not_empty: {path}")
    else:
        path.mkdir(parents=True)
    return path


def verify_release(release_dir: Path, *, extract: Path | None = None) -> dict[str, Any]:
    release_dir = release_dir.expanduser().resolve()
    if not release_dir.is_dir():
        raise ReleaseVerificationError(f"release_directory_missing: {release_dir}")
    _verify_checksums(release_dir)
    release = _load_json(release_dir / "release-manifest.json")
    if release.get("schema_version") != RELEASE_SCHEMA:
        raise ReleaseVerificationError("release_schema_invalid")
    artifact_stage = release.get("artifact_stage")
    if artifact_stage not in {"ready-verified", "draft-distributable"}:
        raise ReleaseVerificationError("artifact_stage_invalid")
    package_record = release.get("package")
    archive_record = release.get("archive")
    verifier_record = release.get("verifier")
    if not all(isinstance(value, dict) for value in (package_record, archive_record, verifier_record)):
        raise ReleaseVerificationError("release_manifest_invalid")
    root_name = package_record.get("root_directory")
    archive_name = archive_record.get("filename")
    if not isinstance(root_name, str) or not root_name or not isinstance(archive_name, str):
        raise ReleaseVerificationError("release_manifest_invalid")
    archive_path = release_dir / archive_name
    if (
        not archive_path.is_file()
        or archive_path.is_symlink()
        or sha256_file(archive_path) != archive_record.get("sha256")
    ):
        raise ReleaseVerificationError("archive_hash_mismatch")
    verifier_path = release_dir / str(verifier_record.get("filename", ""))
    if (
        not verifier_path.is_file()
        or verifier_path.is_symlink()
        or sha256_file(verifier_path) != verifier_record.get("sha256")
    ):
        raise ReleaseVerificationError("verifier_hash_mismatch")
    members: dict[str, zipfile.ZipInfo] = {}
    ready_evidence_profiles: list[str] = []
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                raise ReleaseVerificationError(f"archive_directory_entry_forbidden: {info.filename}")
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise ReleaseVerificationError(f"archive_nonregular_entry: {info.filename}")
            relative = _safe_member(info.filename, root_name)
            if relative in members:
                raise ReleaseVerificationError(f"archive_duplicate_entry: {relative}")
            if Path(relative).suffix.casefold() in FORBIDDEN_SUFFIXES:
                raise ReleaseVerificationError(f"embedded_source_or_render_forbidden: {relative}")
            members[relative] = info
        if len(members) != archive_record.get("file_count"):
            raise ReleaseVerificationError("archive_file_count_mismatch")
        if sum(info.file_size for info in members.values()) != archive_record.get("uncompressed_bytes"):
            raise ReleaseVerificationError("archive_size_mismatch")
        for relative, info in members.items():
            try:
                archive.read(info).decode("utf-8")
            except UnicodeDecodeError as error:
                raise ReleaseVerificationError(
                    f"non_text_release_file: {relative}"
                ) from error
        if "manifest.json" not in members:
            raise ReleaseVerificationError("package_manifest_missing")
        manifest_bytes = archive.read(members["manifest.json"])
        if sha256_bytes(manifest_bytes) != package_record.get("manifest_sha256"):
            raise ReleaseVerificationError("package_manifest_hash_mismatch")
        try:
            package_manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReleaseVerificationError("package_manifest_invalid") from error
        expected_status = (
            "draft" if artifact_stage == "draft-distributable" else "ready"
        )
        if (
            package_manifest.get("package", {}).get("id") != package_record.get("id")
            or package_manifest.get("package", {}).get("version") != package_record.get("version")
            or package_manifest.get("package", {}).get("status") != expected_status
            or package_manifest.get("capabilities") != release.get("capabilities")
            or package_manifest.get("distribution") != release.get("distribution")
        ):
            raise ReleaseVerificationError("package_identity_mismatch")
        integrity = package_manifest.get("integrity")
        integrity_files = integrity.get("files") if isinstance(integrity, dict) else None
        if not isinstance(integrity_files, dict) or integrity.get("algorithm") != "sha256":
            raise ReleaseVerificationError("package_integrity_missing")
        if len(integrity_files) != package_record.get("integrity_file_count"):
            raise ReleaseVerificationError("package_integrity_count_mismatch")
        if set(members) != {"manifest.json", *integrity_files}:
            missing = sorted(set(integrity_files) - set(members))
            extra = sorted(set(members) - {"manifest.json", *integrity_files})
            raise ReleaseVerificationError(f"package_inventory_mismatch:missing={missing[:1]}:extra={extra[:1]}")
        for relative, expected_hash in integrity_files.items():
            if not isinstance(relative, str) or not isinstance(expected_hash, str):
                raise ReleaseVerificationError("package_integrity_invalid")
            if sha256_bytes(archive.read(members[relative])) != expected_hash:
                raise ReleaseVerificationError(f"package_integrity_mismatch: {relative}")
        lock_hash = package_record.get("composition_lock_sha256")
        if lock_hash is not None:
            if "composition-lock.json" not in members:
                raise ReleaseVerificationError("composition_lock_missing")
            if sha256_bytes(archive.read(members["composition-lock.json"])) != lock_hash:
                raise ReleaseVerificationError("composition_lock_hash_mismatch")
        adapter = release.get("adapter")
        if not isinstance(adapter, dict):
            raise ReleaseVerificationError("adapter_contract_missing")
        for field in ("skill_entrypoint", "runtime_entrypoint"):
            value = adapter.get(field)
            if not isinstance(value, str) or value not in members:
                raise ReleaseVerificationError(f"adapter_entrypoint_missing: {field}")
        package_kind = package_record.get("kind")
        expected_runtime = (
            "scripts/query_composite.py"
            if package_kind == "composite-reference"
            else "scripts/query_reference.py"
        )
        expected_response_schema = (
            "tkc.composite-runtime-response/v0.1"
            if package_kind == "composite-reference"
            else "tkc.runtime-response/v0.1"
        )
        if (
            adapter.get("skill_entrypoint") != "SKILL.md"
            or adapter.get("runtime_entrypoint") != expected_runtime
            or adapter.get("working_directory") != "package-root"
            or adapter.get("interpreter") != "python3-standard-library"
            or adapter.get("invocation")
            != [
                "python3",
                "-B",
                expected_runtime,
                "--query",
                "{query}",
                "--limit",
                "{limit}",
            ]
            or adapter.get("response_schema") != expected_response_schema
            or adapter.get("permissions")
            != ["read-package-files", "spawn-local-python"]
        ):
            raise ReleaseVerificationError("adapter_contract_invalid")
        capabilities = package_manifest.get("capabilities")
        executable = capabilities == {
            "reference": True,
            "decision_support": False,
            "executable": True,
        }
        if executable:
            if (
                package_manifest.get("build", {}).get("compiler") != "fake-expert"
                or package_manifest.get("build", {}).get("compiler_version") != "0.4.0-execution"
                or adapter.get("execution_entrypoint") != "scripts/run_formula_sandbox.py"
                or adapter.get("execution_entrypoint") not in members
                or adapter.get("execution_invocation") != ["python3", "-B", "scripts/run_formula_sandbox.py", "--formula", "{object_id}"]
                or "references/execution/manifest.json" not in members
                or "references/execution/policy.json" not in members
            ):
                raise ReleaseVerificationError("execution_release_contract_invalid")
        elif capabilities != {
            "reference": True,
            "decision_support": False,
            "executable": False,
        }:
            raise ReleaseVerificationError("release_capabilities_invalid")
        if artifact_stage == "draft-distributable":
            if executable:
                raise ReleaseVerificationError("candidate_executable_forbidden")
            required_candidate_paths = {
                "RECEIVER.md",
                "SKILL.md",
                "agents/openai.yaml",
                "references/competency-tests.jsonl",
                "references/conflicts.jsonl",
                "references/decisions/index.json",
                "references/evidence/anchors.jsonl",
                "references/evidence/source-manifest.json",
                "references/knowledge-gaps.jsonl",
                "references/knowledge/index.json",
                "references/knowledge/relations.jsonl",
                "references/procedures/index.json",
                "references/runtime/catalog.json",
                "references/runtime/direct-reference.json",
                "references/runtime/reference-map.md",
                "references/runtime/response.schema.json",
                "references/runtime/self-check.json",
                "scripts/portable_reference_runtime.py",
                "scripts/query_reference.py",
            }
            if (
                not str(package_record.get("id", "")).startswith("fake-")
                or package_manifest.get("build", {}).get("compiler") != "fake-expert"
                or package_manifest.get("build", {}).get("compiler_version")
                != DIRECT_REFERENCE_COMPILER_VERSION
                or "references/runtime/direct-reference.json" not in members
                or "references/evidence/promotion-report.json" in members
                or not required_candidate_paths.issubset(members)
                or adapter.get("response_schema_path")
                != "references/runtime/response.schema.json"
                or sha256_bytes(archive.read(members["scripts/query_reference.py"]))
                != DIRECT_REFERENCE_RUNTIME_SHA256
                or sha256_bytes(
                    archive.read(members["scripts/portable_reference_runtime.py"])
                )
                != PORTABLE_REFERENCE_RUNTIME_SHA256
            ):
                raise ReleaseVerificationError("candidate_contract_invalid")
            try:
                direct = json.loads(
                    archive.read(members["references/runtime/direct-reference.json"])
                    .decode("utf-8")
                )
                catalog = json.loads(
                    archive.read(members["references/runtime/catalog.json"])
                    .decode("utf-8")
                )
                self_check = json.loads(
                    archive.read(members["references/runtime/self-check.json"])
                    .decode("utf-8")
                )
                response_schema = json.loads(
                    archive.read(
                        members["references/runtime/response.schema.json"]
                    ).decode("utf-8")
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ReleaseVerificationError("candidate_contract_invalid") from error
            usage = direct.get("usage") if isinstance(direct, dict) else None
            gold = direct.get("gold") if isinstance(direct, dict) else None
            candidate = release.get("candidate_assurance")
            base_warnings = [
                "unreviewed-candidate",
                "not-ready",
                "not-decision-capable",
                "not-executable",
                "external-source-required-for-full-verification",
            ]
            expected_warnings = list(base_warnings)
            if isinstance(gold, dict) and gold.get("status") in {"bound", "stale"}:
                expected_warnings.append("gold-coverage-not-evaluated")
            expected_gold_coverage = {
                "not-requested": "not-applicable",
                "pending": "pending",
                "bound": "not-evaluated",
                "stale": "invalidated-by-extension",
            }
            allowed_gold_statuses = {
                "none": {"not-requested"},
                "create": {"pending", "bound", "stale"},
                "reuse": {"bound", "stale"},
                "update": {"bound", "stale"},
            }
            catalog_assurance = (
                catalog.get("policy", {}).get("assurance")
                if isinstance(catalog, dict)
                else None
            )
            if (
                direct.get("schema_version") != DIRECT_REFERENCE_SCHEMA
                or direct.get("protocol") != DIRECT_REFERENCE_PROTOCOL
                or direct.get("package")
                != {
                    "id": package_record.get("id"),
                    "version": package_record.get("version"),
                }
                or not isinstance(usage, dict)
                or usage.get("queryable") is not True
                or usage.get("distributable_stage") != "draft-distributable"
                or usage.get("knowledge_state") != "candidate"
                or usage.get("review_state") != "deferred"
                or usage.get("knowledge_verified") is not False
                or usage.get("gold_required_for_extraction") is not False
                or usage.get("gold_required_for_runtime") is not False
                or usage.get("limitations") != base_warnings
                or not isinstance(gold, dict)
                or gold.get("status") not in expected_gold_coverage
                or gold.get("status")
                not in allowed_gold_statuses.get(gold.get("mode"), set())
                or gold.get("coverage_state")
                != expected_gold_coverage.get(gold.get("status"))
                or not isinstance(gold.get("source_sha256s"), list)
                or any(
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(character not in "0123456789abcdef" for character in value)
                    for value in gold.get("source_sha256s", [])
                )
                or gold.get("promotion_authority") is not False
                or gold.get("object_coverage_verified") is not False
                or gold.get("coverage_state")
                not in {
                    "not-applicable",
                    "pending",
                    "not-evaluated",
                    "invalidated-by-extension",
                }
                or not isinstance(candidate, dict)
                or candidate.get("knowledge_state") != "candidate"
                or candidate.get("review_state") != "deferred"
                or candidate.get("knowledge_verified") is not False
                or candidate.get("gold") != gold
                or candidate.get("warnings") != expected_warnings
                or candidate.get("direct_reference_sha256")
                != sha256_bytes(
                    archive.read(members["references/runtime/direct-reference.json"])
                )
                or not isinstance(catalog, dict)
                or catalog_assurance
                != {
                    "package_version": package_record.get("version"),
                    "knowledge_state": "candidate",
                    "review_state": "deferred",
                    "gold_state": gold.get("status"),
                    "knowledge_verified": False,
                    "queryable": True,
                    "warnings": expected_warnings,
                }
                or not isinstance(self_check, dict)
                or self_check.get("runtime_sha256")
                != DIRECT_REFERENCE_RUNTIME_SHA256
                or self_check.get("base_runtime_sha256")
                != PORTABLE_REFERENCE_RUNTIME_SHA256
                or self_check.get("catalog_sha256") != sha256_json(catalog)
                or self_check.get("passed") is not True
                or not isinstance(self_check.get("checks"), list)
                or not self_check["checks"]
                or any(
                    not isinstance(row, dict) or row.get("passed") is not True
                    for row in self_check["checks"]
                )
                or not isinstance(response_schema, dict)
                or response_schema.get("$id")
                != "tkc.direct-reference-runtime-response/v0.1"
                or response_schema.get("additionalProperties") is not False
            ):
                raise ReleaseVerificationError("candidate_assurance_invalid")
            try:
                skill_text = archive.read(members["SKILL.md"]).decode("utf-8")
            except UnicodeDecodeError as error:
                raise ReleaseVerificationError("candidate_skill_invalid") from error
            if f"\nname: {package_record.get('id')}\n" not in f"\n{skill_text}":
                raise ReleaseVerificationError("candidate_skill_identity_invalid")
            try:
                index = json.loads(
                    archive.read(members["references/knowledge/index.json"])
                    .decode("utf-8")
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ReleaseVerificationError("candidate_knowledge_invalid") from error
            object_rows = index.get("objects") if isinstance(index, dict) else None
            if not isinstance(object_rows, list) or not object_rows:
                raise ReleaseVerificationError("candidate_knowledge_invalid")
            object_ids: set[str] = set()
            object_paths: set[str] = set()
            for row in object_rows:
                relative = row.get("path") if isinstance(row, dict) else None
                object_id = row.get("id") if isinstance(row, dict) else None
                if (
                    not isinstance(object_id, str)
                    or not object_id
                    or object_id in object_ids
                    or not isinstance(relative, str)
                    or relative != f"references/knowledge/objects/{object_id}.json"
                    or relative not in members
                ):
                    raise ReleaseVerificationError("candidate_knowledge_invalid")
                object_ids.add(object_id)
                object_paths.add(relative)
                try:
                    obj = json.loads(archive.read(members[relative]).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ReleaseVerificationError("candidate_knowledge_invalid") from error
                if (
                    obj.get("id") != object_id
                    or obj.get("status") != "unreviewed-candidate"
                    or "review_binding" in obj
                ):
                    raise ReleaseVerificationError("candidate_knowledge_overstated")
            exports = {
                value
                for module in package_manifest.get("modules", [])
                if isinstance(module, dict)
                for value in module.get("exports", [])
                if isinstance(value, str)
            }
            sources = package_manifest.get("sources")
            source_by_id = {
                row.get("source_id"): row
                for row in sources
                if isinstance(row, dict) and isinstance(row.get("source_id"), str)
            } if isinstance(sources, list) else {}
            try:
                source_manifest = json.loads(
                    archive.read(
                        members["references/evidence/source-manifest.json"]
                    ).decode("utf-8")
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ReleaseVerificationError("candidate_provenance_invalid") from error
            source_hashes = {
                row.get("sha256")
                for row in source_by_id.values()
                if isinstance(row.get("sha256"), str)
            }
            if (
                not source_by_id
                or source_manifest.get("sources") != sources
                or not set(gold.get("source_sha256s", [])).issubset(source_hashes)
                or (
                    gold.get("status") in {"bound", "stale"}
                    and (
                        not isinstance(gold.get("revision_sha256"), str)
                        or len(gold["revision_sha256"]) != 64
                        or any(
                            character not in "0123456789abcdef"
                            for character in gold["revision_sha256"]
                        )
                        or not isinstance(
                            gold.get("revision_schema_version"), str
                        )
                        or not gold["revision_schema_version"]
                        or len(gold["revision_schema_version"]) > 128
                        or not gold["revision_schema_version"][0].isalnum()
                        or any(
                            not (character.isalnum() or character in "._/-")
                            for character in gold["revision_schema_version"]
                        )
                        or not gold.get("source_sha256s")
                    )
                )
                or (
                    gold.get("status") not in {"bound", "stale"}
                    and (
                        gold.get("revision_sha256") is not None
                        or gold.get("revision_schema_version") is not None
                    )
                )
                or (
                    gold.get("status") == "not-requested"
                    and gold.get("source_sha256s") != []
                )
                or (
                    gold.get("status") == "pending"
                    and set(gold.get("source_sha256s", [])) != source_hashes
                )
            ):
                raise ReleaseVerificationError("candidate_provenance_invalid")
            update = direct.get("update")
            if not isinstance(update, dict):
                raise ReleaseVerificationError("candidate_update_invalid")
            update_mode = update.get("mode")
            added = update.get("added_object_ids")
            unchanged = update.get("unchanged_object_ids")
            removed = update.get("removed_object_ids")
            base_hash = update.get("base_manifest_sha256")
            update_valid = (
                set(update)
                == {
                    "mode",
                    "base_manifest_sha256",
                    "added_object_ids",
                    "unchanged_object_ids",
                    "removed_object_ids",
                }
                and update_mode in {"initial", "additive", "gold-binding-only"}
                and all(
                    isinstance(value, list)
                    for value in (added, unchanged, removed)
                )
                and all(
                    isinstance(item, str)
                    for value in (added, unchanged, removed)
                    for item in value
                )
                and added == sorted(set(added))
                and unchanged == sorted(set(unchanged))
                and removed == []
                and not set(added) & set(unchanged)
                and set(added) | set(unchanged) == object_ids
            )
            if update_mode == "initial":
                update_valid = (
                    update_valid
                    and base_hash is None
                    and set(added) == object_ids
                    and unchanged == []
                )
            elif update_mode == "additive":
                update_valid = (
                    update_valid
                    and isinstance(base_hash, str)
                    and len(base_hash) == 64
                )
            elif update_mode == "gold-binding-only":
                update_valid = (
                    update_valid
                    and isinstance(base_hash, str)
                    and len(base_hash) == 64
                    and added == []
                    and set(unchanged) == object_ids
                )
            if not update_valid:
                raise ReleaseVerificationError("candidate_update_invalid")
            bindings = direct.get("workpack_bindings")
            binding_ids: set[str] = set()
            binding_keys: set[tuple[str, str, str]] = set()
            expected_binding_fields = {
                "source_id",
                "source_sha256",
                "workpack_id",
                "workpack_sha256",
                "draft_bundle_sha256",
                "scope",
                "object_ids",
                "proposal_validation",
            }
            if object_ids != exports or not isinstance(bindings, list) or not bindings:
                raise ReleaseVerificationError("candidate_provenance_invalid")
            for binding in bindings:
                if not isinstance(binding, dict) or set(binding) != expected_binding_fields:
                    raise ReleaseVerificationError("candidate_provenance_invalid")
                source = source_by_id.get(binding.get("source_id"))
                scope = binding.get("scope")
                ids = binding.get("object_ids")
                proposal = binding.get("proposal_validation")
                key = (
                    str(binding.get("source_id")),
                    str(binding.get("workpack_sha256")),
                    str(binding.get("draft_bundle_sha256")),
                )
                if (
                    key in binding_keys
                    or not isinstance(source, dict)
                    or binding.get("source_sha256") != source.get("sha256")
                    or not isinstance(binding.get("workpack_id"), str)
                    or not binding["workpack_id"]
                    or not all(
                        isinstance(binding.get(field), str)
                        and len(binding[field]) == 64
                        and all(character in "0123456789abcdef" for character in binding[field])
                        for field in ("workpack_sha256", "draft_bundle_sha256")
                    )
                    or not isinstance(scope, dict)
                    or set(scope) != {"start_page", "end_page"}
                    or not all(isinstance(scope.get(field), int) for field in scope)
                    or scope["start_page"] < 1
                    or scope["start_page"] > scope["end_page"]
                    or not isinstance(ids, list)
                    or not ids
                    or not all(isinstance(value, str) for value in ids)
                    or ids != sorted(set(ids))
                    or not set(ids).issubset(object_ids)
                    or not isinstance(proposal, dict)
                    or proposal.get("status") != "passed"
                ):
                    raise ReleaseVerificationError("candidate_provenance_invalid")
                source_pages = source.get("scope", {}).get("physical_pages")
                if (
                    not isinstance(source_pages, list)
                    or len(source_pages) != 2
                    or scope["start_page"] < source_pages[0]
                    or scope["end_page"] > source_pages[1]
                ):
                    raise ReleaseVerificationError("candidate_provenance_invalid")
                binding_keys.add(key)
                binding_ids.update(ids)
            if binding_ids != object_ids:
                raise ReleaseVerificationError("candidate_provenance_invalid")
            expected_candidate_files = {
                "manifest.json",
                *required_candidate_paths,
                *object_paths,
            }
            if set(members) != expected_candidate_files:
                raise ReleaseVerificationError("candidate_inventory_invalid")
        else:
            if "candidate_assurance" in release:
                raise ReleaseVerificationError("ready_candidate_assurance_forbidden")
            if package_kind == "composite-reference":
                ready_evidence_profiles = _verify_ready_composite_evidence(
                    archive, members
                )
            else:
                ready_evidence_profiles = [
                    _verify_ready_reference_evidence(archive, members)
                ]
                if executable:
                    _verify_ready_execution_evidence(archive, members)
                    ready_evidence_profiles.append("reviewed-formula-execution")
        extracted_root = None
        if extract is not None:
            target = _ensure_extract_target(extract)
            for relative, info in sorted(members.items()):
                destination = target / root_name / Path(relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("wb") as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                os.chmod(destination, 0o644)
            extracted_root = str(target / root_name)
    restrictions = release.get("restrictions")
    if (
        not isinstance(restrictions, dict)
        or restrictions.get("source_pdf_included") is not False
        or restrictions.get("page_renders_included") is not False
        or restrictions.get("external_source_required_for_full_verification") is not True
        or restrictions.get("decision_certification_available") is not False
        or restrictions.get("executable_procedures_available") is not executable
        or restrictions.get("knowledge_verified")
        is not (artifact_stage == "ready-verified")
    ):
        raise ReleaseVerificationError("release_restrictions_invalid")
    return {
        "status": artifact_stage,
        "artifact_stage": artifact_stage,
        "integrity_verified": True,
        "knowledge_verified": artifact_stage == "ready-verified",
        "ready_evidence_profiles": ready_evidence_profiles,
        "package_id": package_record.get("id"),
        "package_version": package_record.get("version"),
        "archive_sha256": archive_record.get("sha256"),
        "file_count": archive_record.get("file_count"),
        "extracted_root": extracted_root,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--extract", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = verify_release(args.release_dir, extract=args.extract)
    except (ReleaseVerificationError, OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
