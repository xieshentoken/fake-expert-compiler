#!/usr/bin/env python3
"""Promote independently reviewed formula ASTs through a constrained execution tier.

This is deliberately a separate promotion authority.  Semantic proposals and the
Phase 2 promoter never set ``capabilities.executable``.  This tool starts only
from a sealed, ready, reference-only package and writes an executable package only
after deterministic AST checks and separately authored test cases pass in bounded
child processes.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from expert_skill_contract import (
    EXECUTION_COMPILER_VERSION,
    load_json,
    load_jsonl,
    sha256_file,
    sha256_json,
    validate_expert_skill,
    write_json,
)
from formula_execution import (
    FORMULA_AST_SCHEMA,
    FormulaContract,
    FormulaContractError,
    evaluate_formula_ast,
    render_formula_module,
    validate_formula_ast,
)


EXECUTION_BUNDLE_SCHEMA = "tkc.execution-bundle/v0.1"
EXECUTION_TEST_SUITE_SCHEMA = "tkc.execution-test-suite/v0.1"
EXECUTION_RECEIPT_SCHEMA = "tkc.execution-receipt/v0.1"
POLICY_SCHEMA = "tkc.execution-policy/v0.1"
RFC3339_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)
IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9-]{0,79}$")


class ExecutableFormulaTierError(RuntimeError):
    """Stable execution-tier failure."""


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    temporary.replace(path)


def _parse_timestamp(value: str, code: str) -> None:
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        raise ExecutableFormulaTierError(code)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ExecutableFormulaTierError(code) from error
    if parsed.tzinfo is None:
        raise ExecutableFormulaTierError(code)


def _decimal(value: Any, code: str) -> Decimal:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ExecutableFormulaTierError(code)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ExecutableFormulaTierError(code) from error
    if not result.is_finite():
        raise ExecutableFormulaTierError(code)
    return result


def _read_ready_reference(input_package: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    input_package = input_package.expanduser().resolve()
    errors = [issue for issue in validate_expert_skill(input_package, level="publish") if issue.severity == "error"]
    if errors:
        raise ExecutableFormulaTierError(f"input_publish_invalid:{errors[0].code}:{errors[0].path}")
    manifest = load_json(input_package / "manifest.json")
    if manifest.get("package", {}).get("status") != "ready":
        raise ExecutableFormulaTierError("input_package_not_ready")
    if manifest.get("capabilities") != {"reference": True, "decision_support": False, "executable": False}:
        raise ExecutableFormulaTierError("input_package_not_reference_only")
    index = load_json(input_package / "references/knowledge/index.json")
    objects: dict[str, dict[str, Any]] = {}
    for entry in index.get("objects", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not isinstance(entry.get("path"), str):
            raise ExecutableFormulaTierError("knowledge_index_invalid")
        objects[entry["id"]] = load_json(input_package / entry["path"])
    promotion = load_json(input_package / "references/evidence/promotion-report.json")
    promotion_rows = promotion.get("items")
    if not isinstance(promotion_rows, list):
        raise ExecutableFormulaTierError("promotion_report_invalid")
    records = load_jsonl(input_package / "references/reviews/semantic-records.jsonl")
    record_by_hash = {
        sha256_json(record): record for record in records if isinstance(record, dict)
    }
    attestation_path = input_package / "references/reviews/review-attestations.jsonl"
    attestations = load_jsonl(attestation_path) if attestation_path.is_file() else []
    attestations_by_item: dict[str, list[dict[str, Any]]] = {}
    for attestation in attestations:
        if isinstance(attestation, dict) and isinstance(attestation.get("item_ref"), str):
            attestations_by_item.setdefault(attestation["item_ref"], []).append(attestation)
    binding_by_object: dict[str, dict[str, Any]] = {}
    for row in promotion_rows:
        if not isinstance(row, dict) or row.get("item_kind") != "object" or row.get("verdict") != "accepted":
            continue
        promoted_id = row.get("promoted_id")
        record_hash = row.get("review_record_sha256")
        if isinstance(promoted_id, str) and isinstance(record_hash, str) and record_hash in record_by_hash:
            binding_by_object[promoted_id] = {
                "row": row,
                "record": record_by_hash[record_hash],
                "attestations": attestations_by_item.get(str(row.get("item_ref")), []),
            }
    return manifest, objects, binding_by_object


def _unresolved_conflict_objects(package: Path) -> set[str]:
    result: set[str] = set()
    for conflict in load_jsonl(package / "references/conflicts.jsonl"):
        if not isinstance(conflict, dict) or conflict.get("status") != "unresolved":
            continue
        for claim in conflict.get("claims", []):
            if isinstance(claim, dict) and isinstance(claim.get("claim_id"), str):
                result.add(claim["claim_id"])
    return result


def _formula_candidates(
    input_package: Path,
    objects: dict[str, dict[str, Any]],
    review_rows: dict[str, dict[str, Any]],
    object_ids: list[str],
) -> dict[str, tuple[dict[str, Any], FormulaContract, dict[str, Any]]]:
    selected = sorted(set(object_ids))
    if not selected:
        raise ExecutableFormulaTierError("formula_object_required")
    unresolved = _unresolved_conflict_objects(input_package)
    output: dict[str, tuple[dict[str, Any], FormulaContract, dict[str, Any]]] = {}
    for object_id in selected:
        obj = objects.get(object_id)
        if not isinstance(obj, dict) or obj.get("type") != "Equation":
            raise ExecutableFormulaTierError(f"formula_object_invalid:{object_id}")
        if object_id in unresolved:
            raise ExecutableFormulaTierError(f"formula_unresolved_conflict:{object_id}")
        if obj.get("origin") not in {"source-explicit", "source-paraphrase"}:
            raise ExecutableFormulaTierError(f"formula_origin_not_source_bound:{object_id}")
        if obj.get("status") != "independently-reviewed-draft":
            raise ExecutableFormulaTierError(f"formula_not_independently_reviewed:{object_id}")
        formula = obj.get("formula")
        if not isinstance(formula, dict):
            raise ExecutableFormulaTierError(f"formula_metadata_missing:{object_id}")
        if formula.get("representation_relation") not in {"identical", "notation-normalized"}:
            raise ExecutableFormulaTierError(f"formula_representation_not_executable:{object_id}")
        if formula.get("dimension_check") not in {"consistent", "consistent_with_stated_units", "dimensionless", "length-versus-length"}:
            raise ExecutableFormulaTierError(f"formula_dimension_not_reviewed:{object_id}")
        if not isinstance(formula.get("source_notation"), str) or not isinstance(formula.get("normalized_notation"), str):
            raise ExecutableFormulaTierError(f"formula_source_notation_missing:{object_id}")
        ast_value = formula.get("ast")
        if formula.get("ast_schema_version") != FORMULA_AST_SCHEMA or not isinstance(ast_value, dict):
            raise ExecutableFormulaTierError(f"formula_ast_missing:{object_id}")
        if formula.get("ast_sha256") != sha256_json(ast_value):
            raise ExecutableFormulaTierError(f"formula_ast_hash_mismatch:{object_id}")
        try:
            contract = validate_formula_ast(ast_value)
        except FormulaContractError as error:
            raise ExecutableFormulaTierError(f"formula_ast_invalid:{object_id}:{error}") from error
        review = review_rows.get(object_id)
        binding = obj.get("review_binding")
        if not isinstance(review, dict) or not isinstance(binding, dict):
            raise ExecutableFormulaTierError(f"formula_review_binding_missing:{object_id}")
        row, record = review["row"], review["record"]
        for field in ("item_sha256", "review_input_sha256", "reviewer_instance"):
            if binding.get(field) != record.get(field) or row.get(field) != record.get(field):
                raise ExecutableFormulaTierError(f"formula_review_binding_mismatch:{object_id}")
        if (
            record.get("verdict") != "accepted"
            or record.get("evidence_support") != "full"
            or record.get("formula_ast_sha256") != formula.get("ast_sha256")
        ):
            raise ExecutableFormulaTierError(f"formula_review_not_accepted:{object_id}")
        if not isinstance(obj.get("evidence_ids"), list) or not obj["evidence_ids"]:
            raise ExecutableFormulaTierError(f"formula_evidence_missing:{object_id}")
        output[object_id] = (obj, contract, review)
    return output


def _validate_test_suite(
    suite_path: Path,
    candidates: dict[str, tuple[dict[str, Any], FormulaContract, dict[str, Any]]],
    compiler_instance: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        suite = load_json(suite_path.expanduser().resolve())
    except (OSError, json.JSONDecodeError) as error:
        raise ExecutableFormulaTierError("execution_test_suite_invalid") from error
    if not isinstance(suite, dict) or set(suite) != {"schema_version", "issued_at", "author", "cases"}:
        raise ExecutableFormulaTierError("execution_test_suite_shape_invalid")
    if suite.get("schema_version") != EXECUTION_TEST_SUITE_SCHEMA:
        raise ExecutableFormulaTierError("execution_test_suite_schema_invalid")
    _parse_timestamp(suite.get("issued_at"), "execution_test_suite_timestamp_invalid")
    author = suite.get("author")
    if not isinstance(author, dict) or set(author) != {"instance", "attestation_level"}:
        raise ExecutableFormulaTierError("execution_test_author_invalid")
    author_instance = author.get("instance")
    if not isinstance(author_instance, str) or not author_instance or author_instance == compiler_instance:
        raise ExecutableFormulaTierError("execution_test_author_not_independent")
    if author.get("attestation_level") != "host-orchestrator-recorded-not-cryptographic":
        raise ExecutableFormulaTierError("execution_test_attestation_invalid")
    review_instances = {
        instance
        for _, _, review in candidates.values()
        for instance in (
            [review["record"].get("reviewer_instance")]
            + [
                attestation.get("reviewer_instance")
                for attestation in review.get("attestations", [])
                if isinstance(attestation, dict)
            ]
        )
        if isinstance(instance, str)
    }
    proposer_instances = {
        instance
        for _, _, review in candidates.values()
        for instance in (
            [review["record"].get("proposer_instance")]
            + [
                attestation.get("proposer_instance")
                for attestation in review.get("attestations", [])
                if isinstance(attestation, dict)
            ]
        )
        if isinstance(instance, str)
    }
    if author_instance in review_instances or author_instance in proposer_instances:
        raise ExecutableFormulaTierError("execution_test_author_not_independent")
    raw_cases = suite.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ExecutableFormulaTierError("execution_test_cases_missing")
    case_ids: set[str] = set()
    per_formula: dict[str, int] = {object_id: 0 for object_id in candidates}
    normalized: list[dict[str, Any]] = []
    for raw in raw_cases:
        if not isinstance(raw, dict) or set(raw) != {"id", "object_id", "inputs", "expected", "tolerance"}:
            raise ExecutableFormulaTierError("execution_test_case_shape_invalid")
        case_id = raw.get("id")
        object_id = raw.get("object_id")
        if not isinstance(case_id, str) or not IDENTIFIER_RE.fullmatch(case_id) or case_id in case_ids:
            raise ExecutableFormulaTierError("execution_test_case_id_invalid")
        if object_id not in candidates:
            raise ExecutableFormulaTierError(f"execution_test_object_unbound:{object_id}")
        case_ids.add(case_id)
        _, contract, _ = candidates[object_id]
        try:
            # Validate the data both independently and against the generated result.
            result = evaluate_formula_ast(contract, raw.get("inputs"))
        except FormulaContractError as error:
            raise ExecutableFormulaTierError(f"execution_test_input_invalid:{case_id}:{error}") from error
        expected = raw.get("expected")
        if not isinstance(expected, dict) or set(expected) != {"value", "unit"}:
            raise ExecutableFormulaTierError(f"execution_test_expected_invalid:{case_id}")
        if expected.get("unit") != result["unit"]:
            raise ExecutableFormulaTierError(f"execution_test_expected_unit_invalid:{case_id}")
        _decimal(expected.get("value"), f"execution_test_expected_value_invalid:{case_id}")
        tolerance = raw.get("tolerance")
        if not isinstance(tolerance, dict) or set(tolerance) != {"absolute", "relative"}:
            raise ExecutableFormulaTierError(f"execution_test_tolerance_invalid:{case_id}")
        absolute = _decimal(tolerance.get("absolute"), f"execution_test_tolerance_invalid:{case_id}")
        relative = _decimal(tolerance.get("relative"), f"execution_test_tolerance_invalid:{case_id}")
        if absolute < 0 or relative < 0 or (absolute == 0 and relative == 0):
            raise ExecutableFormulaTierError(f"execution_test_tolerance_invalid:{case_id}")
        normalized.append(copy.deepcopy(raw))
        per_formula[object_id] += 1
    missing = sorted(object_id for object_id, count in per_formula.items() if count < 2)
    if missing:
        raise ExecutableFormulaTierError(f"execution_test_coverage_insufficient:{','.join(missing)}")
    return suite, sorted(normalized, key=lambda row: row["id"])


def _resource_limits(timeout_seconds: int, memory_limit_mib: int):
    def apply() -> None:
        # macOS may expose RLIMIT_AS while rejecting an address-space limit for a
        # framework Python process.  We therefore do not claim an OS RSS cap. The
        # generated evaluator has a bounded AST/stdin surface; these portable POSIX
        # limits still constrain CPU, output files, descriptors, dumps, and children.
        for limit, values in (
            (resource.RLIMIT_CPU, (timeout_seconds, timeout_seconds + 1)),
            (resource.RLIMIT_FSIZE, (0, 0)),
            (resource.RLIMIT_CORE, (0, 0)),
            (resource.RLIMIT_NOFILE, (32, 32)),
        ):
            try:
                resource.setrlimit(limit, values)
            except (ValueError, OSError):
                pass
        if hasattr(resource, "RLIMIT_NPROC"):
            try:
                resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
            except (ValueError, OSError):
                pass
    return apply


def _run_case(
    module_path: Path,
    case: dict[str, Any],
    *,
    timeout_seconds: int,
    memory_limit_mib: int,
    sandbox_root: Path,
) -> dict[str, Any]:
    request = json.dumps({"inputs": case["inputs"]}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-S", str(module_path)],
            input=request,
            capture_output=True,
            text=True,
            cwd=sandbox_root,
            env={"HOME": str(sandbox_root), "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"},
            timeout=timeout_seconds,
            check=False,
            preexec_fn=_resource_limits(timeout_seconds, memory_limit_mib) if os.name == "posix" else None,
        )
    except subprocess.TimeoutExpired:
        return {"verdict": "failed", "issue_code": "execution_timeout", "output": None}
    if completed.returncode != 0:
        return {"verdict": "failed", "issue_code": "execution_process_failed", "output": completed.stdout[-512:]}
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"verdict": "failed", "issue_code": "execution_response_invalid", "output": completed.stdout[-512:]}
    if not isinstance(response, dict) or response.get("status") != "ok" or not isinstance(response.get("result"), dict):
        return {"verdict": "failed", "issue_code": "execution_response_invalid", "output": response}
    result = response["result"]
    expected = case["expected"]
    if result.get("unit") != expected["unit"]:
        return {"verdict": "failed", "issue_code": "execution_output_unit_mismatch", "output": result}
    try:
        actual = _decimal(result.get("value"), "execution_actual_invalid")
        target = _decimal(expected.get("value"), "execution_expected_invalid")
        absolute = _decimal(case["tolerance"]["absolute"], "execution_tolerance_invalid")
        relative = _decimal(case["tolerance"]["relative"], "execution_tolerance_invalid")
    except ExecutableFormulaTierError:
        return {"verdict": "failed", "issue_code": "execution_numeric_invalid", "output": result}
    passed = abs(actual - target) <= max(absolute, abs(target) * relative)
    return {"verdict": "passed" if passed else "failed", "issue_code": None if passed else "execution_expected_value_mismatch", "output": result}


def _ensure_empty_output(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ExecutableFormulaTierError(f"output_not_empty:{path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def compile_executable_formula_tier(
    input_package: Path,
    test_suite_path: Path,
    output: Path,
    *,
    formula_object_ids: list[str],
    package_version: str,
    created_at: str,
    compiler_instance: str,
    timeout_seconds: int,
    memory_limit_mib: int,
) -> dict[str, Any]:
    _parse_timestamp(created_at, "created_at_invalid")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", package_version):
        raise ExecutableFormulaTierError("package_version_invalid")
    if not isinstance(compiler_instance, str) or not compiler_instance:
        raise ExecutableFormulaTierError("compiler_instance_invalid")
    if not 1 <= timeout_seconds <= 10 or not 64 <= memory_limit_mib <= 512:
        raise ExecutableFormulaTierError("execution_limits_invalid")

    source = input_package.expanduser().resolve()
    manifest, objects, review_rows = _read_ready_reference(source)
    candidates = _formula_candidates(source, objects, review_rows, formula_object_ids)
    suite, cases = _validate_test_suite(test_suite_path, candidates, compiler_instance)
    modules = {object_id: render_formula_module(contract) for object_id, (_, contract, _) in candidates.items()}

    receipts: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="fake-expert-exec-") as temporary:
        temporary_root = Path(temporary)
        for object_id, source_code in modules.items():
            module_path = temporary_root / f"{object_id}.py"
            module_path.write_text(source_code, encoding="utf-8")
            os.chmod(module_path, 0o500)
            for case in (row for row in cases if row["object_id"] == object_id):
                outcome = _run_case(module_path, case, timeout_seconds=timeout_seconds, memory_limit_mib=memory_limit_mib, sandbox_root=temporary_root)
                receipt = {
                    "schema_version": EXECUTION_RECEIPT_SCHEMA,
                    "receipt_id": "xer-" + hashlib.sha256((object_id + case["id"] + sha256_file(module_path)).encode("utf-8")).hexdigest()[:20],
                    "case_id": case["id"],
                    "object_id": object_id,
                    "module_sha256": sha256_file(module_path),
                    "test_case_sha256": sha256_json(case),
                    "policy_sha256": None,
                    "isolation": "process-resource-limits-not-kernel-sandbox",
                    "timeout_seconds": timeout_seconds,
                    "memory_limit_mib": memory_limit_mib,
                    "verdict": outcome["verdict"],
                    "issue_code": outcome["issue_code"],
                    "output_sha256": sha256_json(outcome["output"]) if outcome["output"] is not None else None,
                }
                receipts.append(receipt)
    failed = [row["case_id"] for row in receipts if row["verdict"] != "passed"]
    if failed:
        raise ExecutableFormulaTierError(f"execution_test_failed:{','.join(failed)}")

    policy = {
        "schema_version": POLICY_SCHEMA,
        "execution_model": "formula-ast-only",
        "isolation": "process-resource-limits-not-kernel-sandbox",
        "network": "denied-by-generated-runtime-surface",
        "filesystem": "generated-runtime-has-no-file-api; child-file-size-limit-zero",
        "subprocess": "denied-by-generated-runtime-surface; child-process-limit-zero-when-supported",
        "allowed_imports": ["decimal", "json", "re", "sys"],
        "timeout_seconds": timeout_seconds,
        "memory_limit_mib": memory_limit_mib,
        "memory_guard": "bounded-ast-and-stdin-not-os-rss",
        "requires_sealed_input": True,
        "requires_independent_test_author": True,
    }
    policy_hash = sha256_json(policy)
    for receipt in receipts:
        receipt["policy_sha256"] = policy_hash

    destination = _ensure_empty_output(output)
    shutil.copytree(source, destination, dirs_exist_ok=True)
    runner_relative = "scripts/run_formula_sandbox.py"
    runner_source = Path(__file__).with_name("formula_sandbox_runner.py")
    if not runner_source.is_file():
        raise ExecutableFormulaTierError("execution_runner_missing")
    runner_path = destination / runner_relative
    runner_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(runner_source, runner_path)
    os.chmod(runner_path, 0o500)
    destination_manifest = copy.deepcopy(manifest)
    destination_manifest.pop("integrity", None)
    destination_manifest["package"]["version"] = package_version
    destination_manifest["package"]["status"] = "draft"
    destination_manifest["capabilities"] = {"reference": True, "decision_support": False, "executable": True}
    for module in destination_manifest["modules"]:
        if isinstance(module, dict):
            module["version"] = package_version
    destination_manifest["build"]["compiler"] = "fake-expert"
    destination_manifest["build"]["compiler_version"] = EXECUTION_COMPILER_VERSION
    destination_manifest["build"]["created_at"] = created_at
    destination_manifest["build"]["prompt_versions"] = sorted(set(destination_manifest["build"].get("prompt_versions", [])) | {"execution-formula-ast-v0.1"})
    destination_manifest["build"]["execution"] = {
        "bundle": "references/execution/manifest.json",
        "policy": "references/execution/policy.json",
        "input_manifest_sha256": sha256_file(source / "manifest.json"),
        "compiler_instance": compiler_instance,
    }
    destination_manifest["build"]["input_fingerprint"] = sha256_json(
        {
            "input_manifest_sha256": sha256_file(source / "manifest.json"),
            "formula_ast_sha256": {object_id: sha256_json(contract.ast) for object_id, (_, contract, _) in candidates.items()},
            "test_suite_sha256": sha256_json(suite),
            "policy_sha256": policy_hash,
        }
    )
    write_json(destination / "manifest.json", destination_manifest)

    execution_root = destination / "references/execution"
    execution_root.mkdir(parents=True, exist_ok=True)
    write_json(execution_root / "policy.json", policy)
    write_json(execution_root / "test-suite.json", suite)
    _write_jsonl(execution_root / "receipts.jsonl", sorted(receipts, key=lambda row: row["receipt_id"]))

    procedures: list[dict[str, Any]] = []
    formula_records: list[dict[str, Any]] = []
    for object_id, (obj, contract, review) in sorted(candidates.items()):
        relative_module = f"scripts/executable/{object_id}.py"
        module_path = destination / relative_module
        module_path.parent.mkdir(parents=True, exist_ok=True)
        module_path.write_text(modules[object_id], encoding="utf-8")
        os.chmod(module_path, 0o500)
        procedure_id = f"execute-{object_id}"
        procedure_relative = f"references/procedures/{procedure_id}.json"
        procedure = {
            "schema_version": destination_manifest["schema_version"],
            "id": procedure_id,
            "purpose": f"Evaluate the independently reviewed formula AST for {obj['title']} with read-only numeric inputs.",
            "inputs": [{"symbol": name, **contract.ast["symbols"][name]} for name in contract.inputs],
            "outputs": [{"symbol": contract.output, **contract.ast["symbols"][contract.output]}],
            "preconditions": ["Input units must match the reviewed AST dimensions.", "Use only the sealed generated entrypoint."],
            "stop_conditions": ["Stop on unit mismatch, non-finite numeric value, timeout, process failure, or source/review binding mismatch."],
            "evidence_ids": sorted(obj["evidence_ids"]),
            "source_object_id": object_id,
            "formula_module": relative_module,
            "formula_ast_sha256": sha256_json(contract.ast),
            "review_binding": obj["review_binding"],
            "execution": {
                "entrypoint": runner_relative,
                "arguments": ["--formula", object_id],
                "risk_tier": "read-only",
                "permissions": [],
                "requires_human_review": False,
                "failure_behavior": "return structured error; do not infer or retry with altered formula",
            },
        }
        write_json(destination / procedure_relative, procedure)
        procedures.append({"id": procedure_id, "path": procedure_relative})
        related_receipts = sorted(row["receipt_id"] for row in receipts if row["object_id"] == object_id)
        formula_records.append(
            {
                "object_id": object_id,
                "procedure_id": procedure_id,
                "entrypoint": relative_module,
                "module_sha256": sha256_file(module_path),
                "formula_ast_sha256": sha256_json(contract.ast),
                "source_evidence_ids": sorted(obj["evidence_ids"]),
                "review_binding_sha256": sha256_json(obj["review_binding"]),
                "review_item_ref": review["row"]["item_ref"],
                "test_case_ids": sorted(row["id"] for row in cases if row["object_id"] == object_id),
                "receipt_ids": related_receipts,
            }
        )
    write_json(destination / "references/procedures/index.json", {"schema_version": destination_manifest["schema_version"], "procedures": procedures})
    bundle = {
        "schema_version": EXECUTION_BUNDLE_SCHEMA,
        "input": {
            "package_id": manifest["package"]["id"],
            "package_version": manifest["package"]["version"],
            "manifest_sha256": sha256_file(source / "manifest.json"),
            "integrity_sha256": sha256_json(manifest.get("integrity")),
        },
        "policy": "references/execution/policy.json",
        "policy_sha256": policy_hash,
        "test_suite": "references/execution/test-suite.json",
        "test_suite_sha256": sha256_json(suite),
        "receipts": "references/execution/receipts.jsonl",
        "compiler_instance": compiler_instance,
        "runner": {"entrypoint": runner_relative, "sha256": sha256_file(runner_path)},
        "formulas": formula_records,
    }
    write_json(execution_root / "manifest.json", bundle)

    errors = [issue for issue in validate_expert_skill(destination, level="structure") if issue.severity == "error"]
    if errors:
        raise ExecutableFormulaTierError(f"execution_structure_gate_failed:{errors[0].code}:{errors[0].path}")
    return {
        "package": str(destination),
        "formula_count": len(formula_records),
        "test_count": len(receipts),
        "passed_test_count": len(receipts),
        "capabilities": destination_manifest["capabilities"],
        "status": "draft-unsealed",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--tests", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--formula-object", action="append", dest="formula_object_ids", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--compiler-instance", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=3)
    parser.add_argument("--memory-limit-mib", type=int, default=128)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = compile_executable_formula_tier(
            args.input,
            args.tests,
            args.output,
            formula_object_ids=args.formula_object_ids,
            package_version=args.version,
            created_at=args.created_at,
            compiler_instance=args.compiler_instance,
            timeout_seconds=args.timeout_seconds,
            memory_limit_mib=args.memory_limit_mib,
        )
    except (ExecutableFormulaTierError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
