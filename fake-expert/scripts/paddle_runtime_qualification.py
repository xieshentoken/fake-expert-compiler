#!/usr/bin/env python3
"""Qualify one installed local Paddle visual runtime without claiming accuracy.

The tool freezes an external runtime/model/profile contract, runs repeated
crop-only requests in one short-lived isolated worker, and emits a source-free
host-local qualification receipt.  It never receives a PDF, never downloads a
model, never authors Gold/review evidence, and never upgrades a visual candidate.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
from pathlib import Path
from typing import Any, Mapping, Sequence

from compiler_version import (
    PADDLE_OCR_RESULT_SCHEMA,
    PADDLE_RESULT_ADAPTER_SCHEMA,
    PADDLE_RUNTIME_QUALIFICATION_COMPILER_VERSION,
    PADDLE_RUNTIME_QUALIFICATION_PLAN_SCHEMA,
    PADDLE_RUNTIME_QUALIFICATION_PROTOCOL,
    PADDLE_RUNTIME_QUALIFICATION_SCHEMA,
    PADDLE_RUNTIME_SPLIT_QUALIFICATION_COMPILER_VERSION,
    PADDLE_RUNTIME_SPLIT_QUALIFICATION_PLAN_SCHEMA,
    PADDLE_RUNTIME_SPLIT_QUALIFICATION_PROTOCOL,
    PADDLE_RUNTIME_SPLIT_QUALIFICATION_SCHEMA,
    READONLY_COMPATIBLE_QUALIFICATION_TOOL_SHA256,
    TECHNICAL_CHART_RESULT_SCHEMA,
)
from scanned_pdf_ocr import (
    ScanOcrError,
    bind_spread_split_contract,
    validate_spread_split_contract,
)
from paddle_runtime_contract import (
    PADDLE_OCR_BACKEND,
    PADDLE_CHART_BACKEND,
    PADDLE_STRUCTURE_V3_BACKEND,
    PROFILE_MODEL_NAMES,
    TECHNICAL_CHART_PROFILE_ID,
    TECHNICAL_TEXT_PROFILE_ID,
    TECHNICAL_TABLE_PROFILE_ID,
    PaddleRuntimeContractError,
    freeze_external_runtime_contract,
    profile_definition,
    run_external_worker_batch,
    sha256_file,
    sha256_json,
    validate_runtime_contract,
    validate_private_runtime_diagnostic,
)


QUALIFIABLE_BACKENDS = {
    PADDLE_OCR_BACKEND: {
        "profile_id": TECHNICAL_TEXT_PROFILE_ID,
        "fixture_kind": "text",
        "api": "paddleocr.predict",
        "result_schema": PADDLE_OCR_RESULT_SCHEMA,
    },
    PADDLE_STRUCTURE_V3_BACKEND: {
        "profile_id": TECHNICAL_TABLE_PROFILE_ID,
        "fixture_kind": "table",
        "api": "ppstructure-v3",
        "result_schema": PADDLE_RESULT_ADAPTER_SCHEMA,
    },
    PADDLE_CHART_BACKEND: {
        "profile_id": TECHNICAL_CHART_PROFILE_ID,
        "fixture_kind": "chart",
        "api": "paddleocr.chart_parsing",
        "result_schema": TECHNICAL_CHART_RESULT_SCHEMA,
    },
}


class PaddleRuntimeQualificationError(RuntimeError):
    """Stable fail-closed qualification error."""


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PaddleRuntimeQualificationError("qualification_json_invalid") from error
    if not isinstance(value, dict):
        raise PaddleRuntimeQualificationError("qualification_json_object_required")
    return value


def _png_dimensions(path: Path) -> dict[str, int]:
    try:
        header = path.read_bytes()[:24]
    except OSError as error:
        raise PaddleRuntimeQualificationError("qualification_fixture_missing") from error
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise PaddleRuntimeQualificationError("qualification_fixture_png_required")
    width, height = struct.unpack(">II", header[16:24])
    if width < 1 or height < 1:
        raise PaddleRuntimeQualificationError("qualification_fixture_dimensions_invalid")
    return {"width": int(width), "height": int(height)}


def _positive_number(value: Any, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PaddleRuntimeQualificationError(code)
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise PaddleRuntimeQualificationError(code)
    return result


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _content_rotation(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in {0, 90, 180, 270}:
        raise PaddleRuntimeQualificationError("qualification_content_rotation_invalid")
    return value


def _configuration(backend_id: str, *, content_rotation_clockwise: int = 0) -> dict[str, Any]:
    target = QUALIFIABLE_BACKENDS[backend_id]
    profile_id = str(target["profile_id"])
    profile = profile_definition(profile_id)
    rotation = _content_rotation(content_rotation_clockwise)
    return {
        "backend_id": backend_id,
        "network_policy": "offline-enforced",
        "preprocessing": {"content_rotation_clockwise": rotation},
        "recognition": {},
        "user_config": {},
        "backend_options": {
            "api": target["api"],
            "device": "cpu",
            "allow_model_download": False,
            "profile_id": profile_id,
            "model_names": PROFILE_MODEL_NAMES[profile_id],
            "constructor_options": profile["constructor"],
            "predict_options": profile["predict"],
            "model_dirs": profile["model_dirs"],
        },
        "network_enabled": False,
        "model_download": False,
    }


def _qualification_id(receipt_without_identity: Mapping[str, Any]) -> str:
    """Return the stable ID before identity and receipt-hash fields are added."""

    return f"pqr-{sha256_json(receipt_without_identity)[:24]}"


def _qualification_identity(split_bound: bool) -> tuple[str, str, str]:
    """Return the plan/receipt identity for the legacy or split-bound route."""

    if split_bound:
        return (
            PADDLE_RUNTIME_SPLIT_QUALIFICATION_PLAN_SCHEMA,
            PADDLE_RUNTIME_SPLIT_QUALIFICATION_PROTOCOL,
            PADDLE_RUNTIME_SPLIT_QUALIFICATION_COMPILER_VERSION,
        )
    return (
        PADDLE_RUNTIME_QUALIFICATION_PLAN_SCHEMA,
        PADDLE_RUNTIME_QUALIFICATION_PROTOCOL,
        PADDLE_RUNTIME_QUALIFICATION_COMPILER_VERSION,
    )


def qualification_configuration(
    backend_id: str,
    *,
    content_rotation_clockwise: int = 0,
) -> dict[str, Any]:
    """Return the exact engine configuration bound by a qualification receipt."""

    if backend_id not in QUALIFIABLE_BACKENDS:
        raise PaddleRuntimeQualificationError("qualification_backend_unsupported")
    return _configuration(
        backend_id,
        content_rotation_clockwise=content_rotation_clockwise,
    )


def build_qualification_plan(
    *,
    backend_id: str,
    interpreter: Path,
    runtime_root: Path,
    model_root: Path,
    fixture: Path,
    planned_at: str,
    repetitions: int = 3,
    startup_timeout_seconds: float = 420.0,
    per_request_timeout_seconds: float = 120.0,
    total_timeout_seconds: float = 900.0,
    content_rotation_clockwise: int = 0,
    split_contract: Mapping[str, Any] | None = None,
    split_region_id: str | None = None,
) -> dict[str, Any]:
    if backend_id not in QUALIFIABLE_BACKENDS:
        raise PaddleRuntimeQualificationError("qualification_backend_unsupported")
    if isinstance(repetitions, bool) or not isinstance(repetitions, int) or repetitions < 2 or repetitions > 8:
        raise PaddleRuntimeQualificationError("qualification_repetitions_invalid")
    startup_timeout = _positive_number(startup_timeout_seconds, "qualification_startup_timeout_invalid")
    request_timeout = _positive_number(per_request_timeout_seconds, "qualification_request_timeout_invalid")
    total_timeout = _positive_number(total_timeout_seconds, "qualification_total_timeout_invalid")
    if total_timeout <= startup_timeout:
        raise PaddleRuntimeQualificationError("qualification_total_timeout_too_small")
    fixture = fixture.expanduser().resolve()
    if fixture.is_symlink() or not fixture.is_file():
        raise PaddleRuntimeQualificationError("qualification_fixture_missing_or_symlink")
    dimensions = _png_dimensions(fixture)
    configuration = _configuration(
        backend_id,
        content_rotation_clockwise=content_rotation_clockwise,
    )
    worker = Path(__file__).with_name("ocr_backend_worker.py").resolve()
    spec = {
        "interpreter": str(interpreter.expanduser()),
        "interpreter_allowlist": [str(interpreter.expanduser())],
        "runtime_root": str(runtime_root.expanduser().resolve()),
        "runtime_allowlist": [str(runtime_root.expanduser().resolve())],
        "model_root": str(model_root.expanduser().resolve()),
        "model_allowlist": [str(model_root.expanduser().resolve())],
        "worker_path": str(worker),
        "worker_allowlist": [str(worker)],
        "network_enabled": False,
        "model_download": False,
        "mcp_http_dependency": False,
    }
    try:
        contract = freeze_external_runtime_contract(
            spec,
            backend_id=backend_id,
            configuration=configuration,
            worker_path=worker,
            probe=True,
        )
    except PaddleRuntimeContractError as error:
        raise PaddleRuntimeQualificationError(f"qualification_runtime_contract_invalid:{error}") from error
    bound_split_contract: dict[str, Any] | None = None
    if split_contract is not None:
        if backend_id != PADDLE_OCR_BACKEND:
            raise PaddleRuntimeQualificationError("qualification_split_contract_text_backend_required")
        try:
            validate_spread_split_contract(split_contract, require_bound=False)
            split_rows = [row for row in split_contract.get("regions", []) if isinstance(row, Mapping)]
            if split_region_id is None:
                split_region_id = str(split_rows[0].get("region_id")) if split_rows else None
            selected_region = next((row for row in split_rows if row.get("region_id") == split_region_id), None)
            if not isinstance(selected_region, Mapping):
                raise ScanOcrError("spread_split_qualification_region_missing")
            if selected_region.get("crop_sha256") != sha256_file(fixture) or selected_region.get("crop_dimensions_px") != dimensions:
                raise ScanOcrError("spread_split_qualification_fixture_binding_mismatch")
            runtime_commitments = {
                "runtime_contract_sha256": contract.get("contract_sha256"),
                "runtime_inventory_sha256": (contract.get("runtime_inventory") or {}).get("inventory_sha256"),
                "model_identity_sha256": (contract.get("model_status") or {}).get("stable_identity_sha256"),
                "configuration_sha256": contract.get("configuration_sha256"),
                "worker_sha256": contract.get("worker_sha256"),
            }
            bound_split_contract = bind_spread_split_contract(
                split_contract,
                runtime_commitments=runtime_commitments,
                qualification_commitments={"plan_sha256": None, "receipt_sha256": None},
            )
        except (ScanOcrError, TypeError, ValueError) as error:
            raise PaddleRuntimeQualificationError(f"qualification_split_contract_invalid:{error}") from error
    plan = {
        "schema_version": PADDLE_RUNTIME_QUALIFICATION_PLAN_SCHEMA,
        "protocol": PADDLE_RUNTIME_QUALIFICATION_PROTOCOL,
        "compiler_version": PADDLE_RUNTIME_QUALIFICATION_COMPILER_VERSION,
        "qualification_tool": {
            "script_sha256": sha256_file(Path(__file__).resolve()),
        },
        "backend_id": backend_id,
        "profile_id": QUALIFIABLE_BACKENDS[backend_id]["profile_id"],
        "fixture": {
            "path": str(fixture),
            "kind": QUALIFIABLE_BACKENDS[backend_id]["fixture_kind"],
            "input_sha256": sha256_file(fixture),
            "dimensions_px": dimensions,
            "source_type": "local-crop-fixture",
            **({"physical_page": int(bound_split_contract["physical_page"]), "region_id": str(split_region_id)} if bound_split_contract is not None else {}),
        },
        "runtime_contract": contract,
        "execution": {
            "repetitions": repetitions,
            "startup_timeout_seconds": startup_timeout,
            "per_request_timeout_seconds": request_timeout,
            "total_timeout_seconds": total_timeout,
            "max_parallel_models": 1,
            "serial_inference": True,
            "worker_lifetime": "one-short-lived-process",
        },
        "policy": {
            "input_kind": "raster-crop",
            "source_pdf_passed": False,
            "network_enabled": False,
            "model_download": False,
            "mcp_http_dependency": False,
            "candidate_only": True,
            "promotion": False,
            "verified_gold": False,
            "accuracy_claim": False,
            "operator_exclusive_heavy_model_confirmation_required": True,
        },
        "planned_at": planned_at,
    }
    if bound_split_contract is not None:
        plan.update(
            {
                "split_contract": bound_split_contract,
                "split_contract_sha256": bound_split_contract["contract_sha256"],
                "split_core_sha256": bound_split_contract["split_core_sha256"],
                "split_region_id": str(split_region_id),
            }
        )
    plan_schema, plan_protocol, plan_compiler = _qualification_identity(bound_split_contract is not None)
    plan["schema_version"] = plan_schema
    plan["protocol"] = plan_protocol
    plan["compiler_version"] = plan_compiler
    plan["plan_sha256"] = sha256_json(plan)
    return plan


def validate_qualification_plan(plan: Mapping[str, Any], *, replay_local: bool) -> None:
    if not isinstance(plan, Mapping):
        raise PaddleRuntimeQualificationError("qualification_plan_invalid")
    split_fields_present = any(
        key in plan for key in ("split_contract", "split_contract_sha256", "split_core_sha256", "split_region_id")
    )
    expected_plan_schema, expected_protocol, expected_compiler = _qualification_identity(split_fields_present)
    if plan.get("schema_version") != expected_plan_schema or plan.get("protocol") != expected_protocol:
        raise PaddleRuntimeQualificationError("qualification_plan_schema_invalid")
    if plan.get("compiler_version") != expected_compiler:
        raise PaddleRuntimeQualificationError("qualification_plan_compiler_version_invalid")
    tool = plan.get("qualification_tool")
    if not isinstance(tool, Mapping) or not _is_sha256(tool.get("script_sha256")):
        raise PaddleRuntimeQualificationError("qualification_plan_tool_invalid")
    if plan.get("plan_sha256") != sha256_json({key: value for key, value in plan.items() if key != "plan_sha256"}):
        raise PaddleRuntimeQualificationError("qualification_plan_hash_mismatch")
    backend_id = plan.get("backend_id")
    if backend_id not in QUALIFIABLE_BACKENDS or plan.get("profile_id") != QUALIFIABLE_BACKENDS[backend_id]["profile_id"]:
        raise PaddleRuntimeQualificationError("qualification_plan_target_invalid")
    policy = plan.get("policy")
    if not isinstance(policy, Mapping) or policy.get("input_kind") != "raster-crop" or policy.get("source_pdf_passed") is not False or policy.get("network_enabled") is not False or policy.get("model_download") is not False or policy.get("mcp_http_dependency") is not False or policy.get("candidate_only") is not True or policy.get("promotion") is not False or policy.get("verified_gold") is not False or policy.get("accuracy_claim") is not False or policy.get("operator_exclusive_heavy_model_confirmation_required") is not True:
        raise PaddleRuntimeQualificationError("qualification_plan_policy_invalid")
    execution = plan.get("execution")
    repetitions = execution.get("repetitions") if isinstance(execution, Mapping) else None
    startup_timeout = execution.get("startup_timeout_seconds") if isinstance(execution, Mapping) else None
    request_timeout = execution.get("per_request_timeout_seconds") if isinstance(execution, Mapping) else None
    total_timeout = execution.get("total_timeout_seconds") if isinstance(execution, Mapping) else None
    if not isinstance(execution, Mapping) or isinstance(repetitions, bool) or not isinstance(repetitions, int) or not 2 <= repetitions <= 8 or execution.get("max_parallel_models") != 1 or execution.get("serial_inference") is not True or execution.get("worker_lifetime") != "one-short-lived-process" or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) <= 0 for value in (startup_timeout, request_timeout, total_timeout)) or float(total_timeout) <= float(startup_timeout):
        raise PaddleRuntimeQualificationError("qualification_plan_resource_policy_invalid")
    if replay_local:
        if not isinstance(tool, Mapping) or tool.get("script_sha256") != sha256_file(Path(__file__).resolve()):
            raise PaddleRuntimeQualificationError("qualification_tool_drift")
        fixture = plan.get("fixture")
        if not isinstance(fixture, Mapping):
            raise PaddleRuntimeQualificationError("qualification_plan_fixture_invalid")
        path = Path(str(fixture.get("path", ""))).expanduser().resolve()
        if path.is_symlink() or not path.is_file() or sha256_file(path) != fixture.get("input_sha256") or _png_dimensions(path) != fixture.get("dimensions_px"):
            raise PaddleRuntimeQualificationError("qualification_fixture_drift")
        contract = plan.get("runtime_contract")
        if not isinstance(contract, Mapping):
            raise PaddleRuntimeQualificationError("qualification_runtime_contract_missing")
        try:
            validate_runtime_contract(contract)
        except PaddleRuntimeContractError as error:
            raise PaddleRuntimeQualificationError(f"qualification_runtime_contract_drift:{error}") from error
        if contract.get("backend_id") != backend_id:
            raise PaddleRuntimeQualificationError("qualification_runtime_backend_mismatch")
        runtime_profile = contract.get("runtime_profile")
        if not isinstance(runtime_profile, Mapping) or runtime_profile.get("profile_id") != plan.get("profile_id"):
            raise PaddleRuntimeQualificationError("qualification_runtime_profile_mismatch")
        configuration = contract.get("configuration")
        if not isinstance(configuration, Mapping) or configuration.get("backend_id") != backend_id:
            raise PaddleRuntimeQualificationError("qualification_runtime_configuration_mismatch")
        split_contract = plan.get("split_contract")
        if split_fields_present:
            if backend_id != PADDLE_OCR_BACKEND or not isinstance(split_contract, Mapping):
                raise PaddleRuntimeQualificationError("qualification_split_contract_missing")
            try:
                validate_spread_split_contract(split_contract, require_bound=False)
            except ScanOcrError as error:
                raise PaddleRuntimeQualificationError(f"qualification_split_contract_invalid:{error}") from error
            if plan.get("split_contract_sha256") != split_contract.get("contract_sha256") or plan.get("split_core_sha256") != split_contract.get("split_core_sha256"):
                raise PaddleRuntimeQualificationError("qualification_split_contract_hash_mismatch")
            region_id = plan.get("split_region_id")
            fixture = plan.get("fixture")
            region = next((row for row in split_contract.get("regions", []) if isinstance(row, Mapping) and row.get("region_id") == region_id), None)
            if not isinstance(region, Mapping) or not isinstance(fixture, Mapping) or fixture.get("region_id") != region_id or fixture.get("physical_page") != split_contract.get("physical_page") or fixture.get("input_sha256") != region.get("crop_sha256") or fixture.get("dimensions_px") != region.get("crop_dimensions_px"):
                raise PaddleRuntimeQualificationError("qualification_split_fixture_binding_invalid")
            if configuration.get("preprocessing", {}).get("content_rotation_clockwise") != split_contract.get("rotation"):
                raise PaddleRuntimeQualificationError("qualification_split_rotation_mismatch")
            expected_runtime_commitments = {
                "runtime_contract_sha256": contract.get("contract_sha256"),
                "runtime_inventory_sha256": (contract.get("runtime_inventory") or {}).get("inventory_sha256"),
                "model_identity_sha256": (contract.get("model_status") or {}).get("stable_identity_sha256"),
                "configuration_sha256": contract.get("configuration_sha256"),
                "worker_sha256": contract.get("worker_sha256"),
            }
            if split_contract.get("runtime_commitments") != expected_runtime_commitments:
                raise PaddleRuntimeQualificationError("qualification_split_runtime_binding_invalid")


def run_qualification(
    plan: Mapping[str, Any],
    *,
    tested_at: str,
    exclusive_heavy_model_confirmed: bool,
    private_diagnostic_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_qualification_plan(plan, replay_local=True)
    if not isinstance(tested_at, str) or not tested_at.strip():
        raise PaddleRuntimeQualificationError("qualification_tested_at_required")
    if exclusive_heavy_model_confirmed is not True:
        raise PaddleRuntimeQualificationError("exclusive_heavy_model_confirmation_required")
    backend_id = str(plan["backend_id"])
    contract = dict(plan["runtime_contract"])
    fixture = dict(plan["fixture"])
    split_contract = plan.get("split_contract") if isinstance(plan.get("split_contract"), Mapping) else None
    _, qualification_protocol, qualification_compiler = _qualification_identity(split_contract is not None)
    qualification_schema = (
        PADDLE_RUNTIME_SPLIT_QUALIFICATION_SCHEMA
        if split_contract is not None
        else PADDLE_RUNTIME_QUALIFICATION_SCHEMA
    )
    execution = dict(plan["execution"])
    repetitions = int(execution["repetitions"])
    requests: list[dict[str, Any]] = []
    for index in range(repetitions):
        requests.append(
            {
                "request_id": f"qualification-{backend_id}-{index + 1}",
                "backend_id": backend_id,
                "input_kind": "raster-crop",
                "image_path": fixture["path"],
                "input_sha256": fixture["input_sha256"],
                "source_sha256": fixture["input_sha256"],
                "physical_page": int(fixture.get("physical_page", 1)),
                "render": {
                    "sha256": fixture["input_sha256"],
                    "dpi": 150,
                    "format": "png",
                    "image_dimensions_px": fixture["dimensions_px"],
                },
                "configuration": contract["configuration"],
                "model": contract["model"],
                "model_root": contract["model_root"],
                "runtime_contract_sha256": contract["contract_sha256"],
                "request_timeout_seconds": execution["startup_timeout_seconds"] if index == 0 else execution["per_request_timeout_seconds"],
            }
        )
    worker_kwargs = {
        "timeout_seconds": float(execution["total_timeout_seconds"]),
        "per_request_timeout_seconds": None,
        "allow_request_timeout_overrides": True,
        "max_requests": repetitions,
        "workspace_root": Path(str(fixture["path"])).parent,
    }
    if private_diagnostic_path is not None:
        worker_kwargs["private_diagnostic_path"] = private_diagnostic_path
    try:
        run = run_external_worker_batch(contract, requests, **worker_kwargs)
    except PaddleRuntimeContractError as error:
        raise PaddleRuntimeQualificationError(f"qualification_worker_failed:{error}") from error
    responses = [row for row in run.get("responses", []) if isinstance(row, Mapping)]
    errors = [row for row in run.get("errors", []) if isinstance(row, Mapping)]
    batch_receipt = run.get("receipt") if isinstance(run.get("receipt"), Mapping) else {}
    private_audit = run.get("private_audit") if isinstance(run.get("private_audit"), Mapping) else {}
    runtime_events = [dict(row) for row in run.get("runtime_events", []) if isinstance(row, Mapping)]
    normalized_hashes = [str(row.get("normalized_result_sha256")) for row in responses if isinstance(row.get("normalized_result_sha256"), str)]
    if backend_id == PADDLE_OCR_BACKEND:
        # Text qualification must bind the transport/output artifacts directly.
        # A normalized-result hash is intentionally not an acceptable fallback.
        output_hashes = [
            str(row["output_sha256"])
            for row in responses
            if isinstance(row.get("output_sha256"), str)
        ]
        response_hashes = [
            str((row.get("worker_receipt") or {}).get("response_sha256"))
            for row in responses
            if isinstance(row.get("worker_receipt"), Mapping)
            and isinstance((row.get("worker_receipt") or {}).get("response_sha256"), str)
        ]
    else:
        output_hashes = [
            str(row.get("output_sha256") or row.get("normalized_result_sha256"))
            for row in responses
            if isinstance(row.get("output_sha256") or row.get("normalized_result_sha256"), str)
        ]
        response_hashes = [
            str((row.get("worker_receipt") or {}).get("response_sha256") or row.get("output_sha256") or row.get("normalized_result_sha256"))
            for row in responses
            if isinstance((row.get("worker_receipt") or {}).get("response_sha256") or row.get("output_sha256") or row.get("normalized_result_sha256"), str)
        ]
    response_input_hashes = [row.get("input_sha256") for row in responses]
    result_schemas = sorted({str((row.get("result_adapter") or {}).get("schema_version")) for row in responses if isinstance(row.get("result_adapter"), Mapping)})
    issue_codes: list[str] = []
    if len(responses) != repetitions or errors:
        issue_codes.append("qualification_requests_incomplete")
    if batch_receipt.get("status") != "completed":
        issue_codes.append("qualification_batch_not_completed")
    if batch_receipt.get("model_load_calls") != 1:
        issue_codes.append("qualification_model_load_count_invalid")
    if len(normalized_hashes) != repetitions or len(set(normalized_hashes)) != 1:
        issue_codes.append("qualification_result_nondeterministic")
    expected_result_schema = str(QUALIFIABLE_BACKENDS[backend_id]["result_schema"])
    if result_schemas != [expected_result_schema]:
        issue_codes.append("qualification_result_schema_invalid")
    blocking_warning_codes = sorted(str(value) for value in private_audit.get("blocking_warning_codes", []) if isinstance(value, str))
    if blocking_warning_codes:
        issue_codes.append("qualification_blocking_runtime_warning")
    if batch_receipt.get("source_pdf_passed") is not False:
        issue_codes.append("qualification_source_boundary_invalid")
    if backend_id == PADDLE_OCR_BACKEND:
        if (
            len(output_hashes) != repetitions
            or len(set(output_hashes)) != 1
            or len(response_hashes) != repetitions
            or response_input_hashes != [fixture["input_sha256"]] * repetitions
        ):
            issue_codes.append("qualification_text_hash_binding_invalid")
    if backend_id == PADDLE_CHART_BACKEND:
        if len(runtime_events) != repetitions:
            issue_codes.append("qualification_repeat_diagnostic_missing")
        else:
            engine_ids = {str(row.get("engine_instance_sha256")) for row in runtime_events}
            reuse_shape = [row.get("engine_reused") for row in runtime_events]
            event_statuses = [row.get("status") for row in runtime_events]
            if (
                len(engine_ids) != 1
                or reuse_shape[0] is not False
                or any(value is not True for value in reuse_shape[1:])
                or event_statuses != ["ok"] * repetitions
                or any(
                    row.get("predict_method") != "predict"
                    or row.get("predict_iter_selected") is not False
                    for row in runtime_events
                )
            ):
                issue_codes.append("qualification_repeat_call_unstable")
    issue_codes = sorted(set(issue_codes))
    qualified = not issue_codes
    receipt_core = {
        "schema_version": qualification_schema,
        "protocol": qualification_protocol,
        "compiler_version": qualification_compiler,
        "plan_sha256": plan["plan_sha256"],
        "qualification_tool_sha256": plan["qualification_tool"]["script_sha256"],
        "backend_id": backend_id,
        "profile_id": plan["profile_id"],
        "runtime_contract_sha256": contract["contract_sha256"],
        "runtime_inventory_sha256": contract["runtime_inventory"]["inventory_sha256"],
        "model_identity_sha256": contract["model_status"]["stable_identity_sha256"],
        "configuration_sha256": contract["configuration_sha256"],
        "worker_sha256": contract["worker_sha256"],
        "profile_sha256": sha256_json(contract["runtime_profile"]),
        "fixture": {
            "kind": fixture["kind"],
            "input_sha256": fixture["input_sha256"],
            "dimensions_px": fixture["dimensions_px"],
            "source_type": fixture["source_type"],
        },
        "execution": {
            "repetitions": repetitions,
            "successful_requests": len(responses),
            "failed_requests": len(errors),
            "startup_timeout_seconds": execution["startup_timeout_seconds"],
            "per_request_timeout_seconds": execution["per_request_timeout_seconds"],
            "total_timeout_seconds": execution["total_timeout_seconds"],
            "model_load_calls": batch_receipt.get("model_load_calls", 0),
            "normalized_result_sha256s": normalized_hashes,
            "input_sha256s": [str(fixture["input_sha256"])] * repetitions,
            "output_sha256s": output_hashes,
            "response_sha256s": response_hashes,
            "result_schemas": result_schemas,
            "deterministic": len(normalized_hashes) == repetitions and len(set(normalized_hashes)) == 1,
            "batch_receipt_sha256": batch_receipt.get("batch_receipt_sha256"),
        },
        "runtime_warnings": {
            "warning_codes": sorted(str(value) for value in private_audit.get("warning_codes", []) if isinstance(value, str)),
            "blocking_warning_codes": blocking_warning_codes,
            "stderr_sha256": private_audit.get("stderr_sha256"),
            "stderr_bytes": int(private_audit.get("stderr_bytes", 0) or 0),
            "raw_stderr_included": False,
        },
        "resource_policy": {
            "max_parallel_models": 1,
            "serial_inference": True,
            "worker_lifetime": "one-short-lived-process",
            "model_release": "process-exit",
            "operator_exclusive_heavy_model_confirmed": True,
        },
        "qualification": {
            "status": "qualified" if qualified else "rejected",
            "route_allowed": qualified,
            "issue_codes": issue_codes,
        },
        "tested_at": tested_at,
        "source_pdf_passed": False,
        "network_enabled": False,
        "model_download": False,
        "mcp_http_dependency": False,
        "candidate_only": True,
        "promotion": False,
        "verified_gold": False,
        "accuracy_claim": False,
        "private_host_receipt": True,
        "release_included": False,
    }
    if split_contract is not None:
        receipt_core.update(
            {
                "split_contract_sha256": plan.get("split_contract_sha256"),
                "split_core_sha256": plan.get("split_core_sha256"),
                "split_region_id": plan.get("split_region_id"),
                "content_rotation_clockwise": split_contract.get("rotation"),
            }
        )
    receipt_core["qualification_id"] = _qualification_id(receipt_core)
    receipt_core["receipt_sha256"] = sha256_json(receipt_core)
    validate_runtime_qualification(
        receipt_core,
        contract=contract,
        plan=plan,
        require_qualified=False,
    )
    private_results = {
        "schema_version": "tkc.paddle-runtime-qualification-results/v0.1",
        "qualification_id": receipt_core["qualification_id"],
        "responses": responses,
        "errors": errors,
        "batch_receipt": batch_receipt,
        "runtime_events": runtime_events,
        "raw_stderr_included": False,
        "candidate_only": True,
        "promotion": False,
        "verified_gold": False,
        "release_included": False,
    }
    private_diagnostic = run.get("private_diagnostic")
    if isinstance(private_diagnostic, Mapping):
        if private_diagnostic_path is not None:
            try:
                validate_private_runtime_diagnostic(
                    private_diagnostic_path,
                    expected_sha256=str(private_diagnostic.get("sha256")),
                )
            except PaddleRuntimeContractError as error:
                raise PaddleRuntimeQualificationError(f"qualification_private_diagnostic_invalid:{error}") from error
        private_results["private_diagnostic"] = dict(private_diagnostic)
    return receipt_core, private_results


def validate_runtime_qualification(
    receipt: Mapping[str, Any],
    *,
    contract: Mapping[str, Any] | None = None,
    plan: Mapping[str, Any] | None = None,
    backend_id: str | None = None,
    profile_id: str | None = None,
    require_qualified: bool = True,
) -> None:
    if not isinstance(receipt, Mapping):
        raise PaddleRuntimeQualificationError("runtime_qualification_schema_invalid")
    split_fields_present = any(
        key in receipt for key in ("split_contract_sha256", "split_core_sha256", "split_region_id", "content_rotation_clockwise")
    )
    expected_schema = PADDLE_RUNTIME_SPLIT_QUALIFICATION_SCHEMA if split_fields_present else PADDLE_RUNTIME_QUALIFICATION_SCHEMA
    expected_protocol = PADDLE_RUNTIME_SPLIT_QUALIFICATION_PROTOCOL if split_fields_present else PADDLE_RUNTIME_QUALIFICATION_PROTOCOL
    expected_compiler = PADDLE_RUNTIME_SPLIT_QUALIFICATION_COMPILER_VERSION if split_fields_present else PADDLE_RUNTIME_QUALIFICATION_COMPILER_VERSION
    if receipt.get("schema_version") != expected_schema or receipt.get("protocol") != expected_protocol:
        raise PaddleRuntimeQualificationError("runtime_qualification_schema_invalid")
    if receipt.get("receipt_sha256") != sha256_json({key: value for key, value in receipt.items() if key != "receipt_sha256"}):
        raise PaddleRuntimeQualificationError("runtime_qualification_hash_mismatch")
    identity_material = {
        key: value
        for key, value in receipt.items()
        if key not in {"qualification_id", "receipt_sha256"}
    }
    if receipt.get("qualification_id") != _qualification_id(identity_material):
        raise PaddleRuntimeQualificationError("runtime_qualification_id_mismatch")
    if receipt.get("backend_id") not in QUALIFIABLE_BACKENDS:
        raise PaddleRuntimeQualificationError("runtime_qualification_backend_invalid")
    if receipt.get("compiler_version") != expected_compiler:
        raise PaddleRuntimeQualificationError("runtime_qualification_compiler_version_invalid")
    if not _is_sha256(receipt.get("plan_sha256")) or not _is_sha256(receipt.get("qualification_tool_sha256")):
        raise PaddleRuntimeQualificationError("runtime_qualification_plan_binding_invalid")
    if receipt.get("qualification_tool_sha256") not in {
        sha256_file(Path(__file__).resolve()),
        *READONLY_COMPATIBLE_QUALIFICATION_TOOL_SHA256,
    }:
        raise PaddleRuntimeQualificationError("runtime_qualification_tool_drift")
    if backend_id is not None and receipt.get("backend_id") != backend_id:
        raise PaddleRuntimeQualificationError("runtime_qualification_backend_mismatch")
    expected_profile = QUALIFIABLE_BACKENDS[str(receipt["backend_id"])]["profile_id"]
    if receipt.get("profile_id") != expected_profile or profile_id is not None and receipt.get("profile_id") != profile_id:
        raise PaddleRuntimeQualificationError("runtime_qualification_profile_mismatch")
    policy_false = ("source_pdf_passed", "network_enabled", "model_download", "mcp_http_dependency", "promotion", "verified_gold", "accuracy_claim", "release_included")
    if any(receipt.get(key) is not False for key in policy_false) or receipt.get("candidate_only") is not True or receipt.get("private_host_receipt") is not True:
        raise PaddleRuntimeQualificationError("runtime_qualification_policy_invalid")
    resource = receipt.get("resource_policy")
    if not isinstance(resource, Mapping) or resource.get("max_parallel_models") != 1 or resource.get("serial_inference") is not True or resource.get("model_release") != "process-exit" or resource.get("operator_exclusive_heavy_model_confirmed") is not True:
        raise PaddleRuntimeQualificationError("runtime_qualification_resource_policy_invalid")
    warnings = receipt.get("runtime_warnings")
    if not isinstance(warnings, Mapping) or warnings.get("raw_stderr_included") is not False or not isinstance(warnings.get("warning_codes"), list) or not isinstance(warnings.get("blocking_warning_codes"), list) or not _is_sha256(warnings.get("stderr_sha256")):
        raise PaddleRuntimeQualificationError("runtime_qualification_warning_receipt_invalid")
    qualification = receipt.get("qualification")
    if not isinstance(qualification, Mapping) or qualification.get("status") not in {"qualified", "rejected"} or qualification.get("route_allowed") is not (qualification.get("status") == "qualified") or not isinstance(qualification.get("issue_codes"), list):
        raise PaddleRuntimeQualificationError("runtime_qualification_status_invalid")
    fixture = receipt.get("fixture")
    expected_fixture_kind = QUALIFIABLE_BACKENDS[str(receipt["backend_id"])]["fixture_kind"]
    if not isinstance(fixture, Mapping) or fixture.get("kind") != expected_fixture_kind or fixture.get("source_type") != "local-crop-fixture":
        raise PaddleRuntimeQualificationError("runtime_qualification_fixture_invalid")
    execution = receipt.get("execution")
    if not isinstance(execution, Mapping):
        raise PaddleRuntimeQualificationError("runtime_qualification_execution_invalid")
    repetitions = execution.get("repetitions")
    successful = execution.get("successful_requests")
    failed = execution.get("failed_requests")
    normalized_hashes = execution.get("normalized_result_sha256s")
    input_hashes = execution.get("input_sha256s")
    output_hashes = execution.get("output_sha256s")
    response_hashes = execution.get("response_sha256s")
    result_schemas = execution.get("result_schemas")
    timeout_values = [
        execution.get("startup_timeout_seconds"),
        execution.get("per_request_timeout_seconds"),
        execution.get("total_timeout_seconds"),
    ]
    if (
        isinstance(repetitions, bool)
        or not isinstance(repetitions, int)
        or repetitions < 2
        or repetitions > 8
        or isinstance(successful, bool)
        or not isinstance(successful, int)
        or successful < 0
        or isinstance(failed, bool)
        or not isinstance(failed, int)
        or failed < 0
        or successful + failed != repetitions
        or not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and float(value) > 0 for value in timeout_values)
        or float(execution["total_timeout_seconds"]) <= float(execution["startup_timeout_seconds"])
        or not isinstance(normalized_hashes, list)
        or not all(_is_sha256(value) for value in normalized_hashes)
        or not isinstance(result_schemas, list)
        or not all(isinstance(value, str) for value in result_schemas)
        or (input_hashes is not None and (not isinstance(input_hashes, list) or not all(_is_sha256(value) for value in input_hashes)))
        or (output_hashes is not None and (not isinstance(output_hashes, list) or not all(_is_sha256(value) for value in output_hashes)))
        or (response_hashes is not None and (not isinstance(response_hashes, list) or not all(_is_sha256(value) for value in response_hashes)))
    ):
        raise PaddleRuntimeQualificationError("runtime_qualification_execution_invalid")
    if receipt.get("backend_id") == PADDLE_OCR_BACKEND and (
        not isinstance(input_hashes, list)
        or not isinstance(output_hashes, list)
        or not isinstance(response_hashes, list)
        or len(input_hashes) != repetitions
        or len(output_hashes) > successful
        or len(response_hashes) > successful
        or input_hashes != [fixture.get("input_sha256")] * repetitions
    ):
        raise PaddleRuntimeQualificationError("runtime_qualification_text_hash_binding_invalid")
    expected_result_schema = str(QUALIFIABLE_BACKENDS[str(receipt["backend_id"])]["result_schema"])
    qualified_evidence = (
        successful == repetitions
        and failed == 0
        and execution.get("model_load_calls") == 1
        and execution.get("deterministic") is True
        and len(normalized_hashes) == repetitions
        and len(set(normalized_hashes)) == 1
        and (output_hashes is None or len(output_hashes) == repetitions and len(set(output_hashes)) == 1)
        and result_schemas == [expected_result_schema]
        and not warnings["blocking_warning_codes"]
        and not qualification["issue_codes"]
    )
    if (qualification.get("status") == "qualified") is not qualified_evidence:
        raise PaddleRuntimeQualificationError("runtime_qualification_evidence_status_mismatch")
    binding_fields = (
        "runtime_contract_sha256",
        "runtime_inventory_sha256",
        "model_identity_sha256",
        "configuration_sha256",
        "worker_sha256",
        "profile_sha256",
    )
    if any(not _is_sha256(receipt.get(key)) for key in binding_fields):
        raise PaddleRuntimeQualificationError("runtime_qualification_binding_hash_invalid")
    if split_fields_present:
        if (
            not _is_sha256(receipt.get("split_contract_sha256"))
            or not _is_sha256(receipt.get("split_core_sha256"))
            or not isinstance(receipt.get("split_region_id"), str)
            or receipt.get("content_rotation_clockwise") not in {0, 90, 180, 270}
        ):
            raise PaddleRuntimeQualificationError("runtime_qualification_split_binding_invalid")
        if plan is not None:
            if receipt.get("split_contract_sha256") != plan.get("split_contract_sha256") or receipt.get("split_core_sha256") != plan.get("split_core_sha256") or receipt.get("split_region_id") != plan.get("split_region_id"):
                raise PaddleRuntimeQualificationError("runtime_qualification_split_mismatch")
            split_contract = plan.get("split_contract")
            if not isinstance(split_contract, Mapping) or receipt.get("content_rotation_clockwise") != split_contract.get("rotation"):
                raise PaddleRuntimeQualificationError("runtime_qualification_split_rotation_mismatch")
    if require_qualified and qualification.get("status") != "qualified":
        raise PaddleRuntimeQualificationError("runtime_unqualified")
    if plan is not None:
        if receipt.get("plan_sha256") != plan.get("plan_sha256"):
            raise PaddleRuntimeQualificationError("runtime_qualification_plan_mismatch")
        tool = plan.get("qualification_tool") if isinstance(plan, Mapping) else None
        if not isinstance(tool, Mapping) or receipt.get("qualification_tool_sha256") != tool.get("script_sha256"):
            raise PaddleRuntimeQualificationError("runtime_qualification_tool_mismatch")
    if contract is not None:
        bindings = {
            "runtime_contract_sha256": contract.get("contract_sha256"),
            "runtime_inventory_sha256": (contract.get("runtime_inventory") or {}).get("inventory_sha256"),
            "model_identity_sha256": (contract.get("model_status") or {}).get("stable_identity_sha256"),
            "configuration_sha256": contract.get("configuration_sha256"),
            "worker_sha256": contract.get("worker_sha256"),
            "profile_sha256": sha256_json(contract.get("runtime_profile")),
        }
        for key, value in bindings.items():
            if receipt.get(key) != value:
                raise PaddleRuntimeQualificationError(f"runtime_qualification_binding_mismatch:{key}")


def _ensure_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.exists():
        if not resolved.is_dir() or any(resolved.iterdir()):
            raise PaddleRuntimeQualificationError("qualification_output_not_empty")
    else:
        resolved.mkdir(parents=True)
    return resolved


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--backend", required=True, choices=sorted(QUALIFIABLE_BACKENDS))
    plan.add_argument("--interpreter", type=Path, required=True)
    plan.add_argument("--runtime-root", type=Path, required=True)
    plan.add_argument("--model-root", type=Path, required=True)
    plan.add_argument("--fixture", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--planned-at", required=True)
    plan.add_argument("--repetitions", type=int, default=3)
    plan.add_argument("--startup-timeout-seconds", type=float, default=420.0)
    plan.add_argument("--per-request-timeout-seconds", type=float, default=120.0)
    plan.add_argument("--total-timeout-seconds", type=float, default=900.0)
    plan.add_argument(
        "--content-rotation-clockwise",
        type=int,
        choices=(0, 90, 180, 270),
        default=0,
    )
    plan.add_argument("--split-contract", type=Path)
    plan.add_argument("--split-region-id")
    run = sub.add_parser("run")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--tested-at", required=True)
    run.add_argument("--exclusive-heavy-model-confirmed", action="store_true")
    verify = sub.add_parser("verify")
    verify.add_argument("--plan", type=Path, required=True)
    verify.add_argument("--receipt", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            plan = build_qualification_plan(
                backend_id=args.backend,
                interpreter=args.interpreter,
                runtime_root=args.runtime_root,
                model_root=args.model_root,
                fixture=args.fixture,
                planned_at=args.planned_at,
                repetitions=args.repetitions,
                startup_timeout_seconds=args.startup_timeout_seconds,
                per_request_timeout_seconds=args.per_request_timeout_seconds,
                total_timeout_seconds=args.total_timeout_seconds,
                content_rotation_clockwise=args.content_rotation_clockwise,
                split_contract=_read_json(args.split_contract.expanduser().resolve()) if args.split_contract else None,
                split_region_id=args.split_region_id,
            )
            output = args.output.expanduser().resolve()
            if output.exists():
                raise PaddleRuntimeQualificationError("qualification_plan_output_exists")
            _write_json(output, plan)
            print(json.dumps({"status": "planned", "plan": str(output), "plan_sha256": plan["plan_sha256"]}, ensure_ascii=False, sort_keys=True))
        elif args.command == "run":
            plan = _read_json(args.plan.expanduser().resolve())
            output = _ensure_output(args.output)
            receipt, private_results = run_qualification(
                plan,
                tested_at=args.tested_at,
                exclusive_heavy_model_confirmed=args.exclusive_heavy_model_confirmed,
                private_diagnostic_path=output / "private-runtime-diagnostic.json",
            )
            _write_json(output / "qualification-receipt.json", receipt)
            _write_json(output / "private-worker-results.json", private_results)
            print(json.dumps({"status": receipt["qualification"]["status"], "receipt": str(output / "qualification-receipt.json"), "qualification_id": receipt["qualification_id"], "issue_codes": receipt["qualification"]["issue_codes"]}, ensure_ascii=False, sort_keys=True))
            return 0 if receipt["qualification"]["status"] == "qualified" else 2
        else:
            plan = _read_json(args.plan.expanduser().resolve())
            validate_qualification_plan(plan, replay_local=True)
            receipt = _read_json(args.receipt.expanduser().resolve())
            validate_runtime_qualification(receipt, contract=plan["runtime_contract"], plan=plan, backend_id=str(plan["backend_id"]), profile_id=str(plan["profile_id"]), require_qualified=False)
            print(json.dumps({"status": "verified", "qualification_status": receipt["qualification"]["status"], "qualification_id": receipt["qualification_id"], "receipt_sha256": receipt["receipt_sha256"]}, ensure_ascii=False, sort_keys=True))
        return 0
    except (PaddleRuntimeQualificationError, PaddleRuntimeContractError, OSError, ValueError) as error:
        print(f"error: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PaddleRuntimeQualificationError",
    "QUALIFIABLE_BACKENDS",
    "build_qualification_plan",
    "qualification_configuration",
    "validate_qualification_plan",
    "run_qualification",
    "validate_runtime_qualification",
    "main",
]
