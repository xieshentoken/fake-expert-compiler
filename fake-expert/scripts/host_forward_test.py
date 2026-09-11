#!/usr/bin/env python3
"""Record, score, and validate host-Agent forward tests for a sealed Expert Skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from expert_skill_contract import (
    load_json,
    load_jsonl,
    sha256_file,
    sha256_json,
    tracked_files,
    validate_expert_skill,
    write_json,
)


CHALLENGE_SCHEMA = "tkc.host-challenge/v0.1"
EXPECTATION_SCHEMA = "tkc.host-expectation/v0.1"
RESPONSE_SCHEMA = "tkc.host-response/v0.1"
RUN_SCHEMA = "tkc.host-run-record/v0.1"
RECEIPT_SCHEMA = "tkc.host-evaluation-receipt/v0.1"
CERTIFICATION_SCHEMA = "tkc.host-certification/v0.1"
SCORER_VERSION = "deterministic-host-forward-scorer-v0.1"
ATTESTATION_LEVEL = "host-orchestrator-recorded-not-cryptographic"
CERTIFICATION_SCOPE = "host-reference-behavior-only"
BEHAVIORS = {"answer", "abstain", "escalate"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
RUN_RECORD_FIELDS = {
    "record_id",
    "schema_version",
    "challenge_id",
    "challenge_sha256",
    "prompt_contract_sha256",
    "attempt",
    "agent_instance",
    "host_id",
    "host_version",
    "model_id",
    "attestation_level",
    "package_binding",
    "raw_response",
    "raw_response_sha256",
    "raw_transcript",
    "raw_transcript_sha256",
    "parsed_response",
    "parse_error",
}


class HostForwardTestError(RuntimeError):
    """Stable host-forward-test contract failure."""


@dataclass(frozen=True)
class ForwardIssue:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(canonical_json_bytes(row).decode("utf-8") + "\n")
    temporary.replace(path)


def _unique_strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise HostForwardTestError(f"invalid_string_array: {field}")
    if len(value) != len(set(value)):
        raise HostForwardTestError(f"duplicate_array_value: {field}")
    return sorted(value)


def _load_inventory(package: Path) -> dict[str, Any]:
    package = package.expanduser().resolve()
    manifest_path = package / "manifest.json"
    if not manifest_path.is_file():
        raise HostForwardTestError("package_manifest_missing")
    issues = validate_expert_skill(package, level="publish")
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        raise HostForwardTestError(
            "package_publish_invalid: " + ",".join(sorted({issue.code for issue in errors}))
        )
    manifest = load_json(manifest_path)
    package_record = manifest.get("package", {})
    integrity = manifest.get("integrity", {})
    if package_record.get("status") != "ready" or integrity.get("algorithm") != "sha256":
        raise HostForwardTestError("package_not_sealed")
    declared_files = integrity.get("files")
    if not isinstance(declared_files, dict) or any(
        not isinstance(path, str) or not SHA256_RE.fullmatch(digest)
        for path, digest in declared_files.items()
    ):
        raise HostForwardTestError("package_integrity_invalid")
    declared = dict(declared_files)
    actual = tracked_files(package)
    if declared != actual:
        raise HostForwardTestError("package_integrity_stale")

    catalog_path = package / "references/runtime/catalog.json"
    anchors_path = package / "references/evidence/anchors.jsonl"
    conflicts_path = package / "references/conflicts.jsonl"
    gaps_path = package / "references/knowledge-gaps.jsonl"
    adapter_path = package / "agents/openai.yaml"
    runtime_path = package / "scripts/query_reference.py"
    for required in (catalog_path, anchors_path, conflicts_path, gaps_path, adapter_path, runtime_path):
        if not required.is_file():
            raise HostForwardTestError(f"package_forward_input_missing: {required.relative_to(package)}")

    catalog = load_json(catalog_path)
    object_ids = {
        row["id"]
        for row in catalog.get("objects", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    evidence_ids: set[str] = set()
    evidence_to_objects: dict[str, set[str]] = {}
    for anchor in load_jsonl(anchors_path):
        if not isinstance(anchor, dict) or not isinstance(anchor.get("id"), str):
            raise HostForwardTestError("package_anchor_invalid")
        evidence_id = anchor["id"]
        evidence_ids.add(evidence_id)
        evidence_to_objects[evidence_id] = {
            value
            for value in anchor.get("supports", [])
            if isinstance(value, str)
        }
    conflict_ids = {
        row["id"]
        for row in load_jsonl(conflicts_path)
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    gap_ids = {
        row["id"]
        for row in load_jsonl(gaps_path)
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    return {
        "package": package,
        "manifest": manifest,
        "package_id": package_record.get("id"),
        "package_version": package_record.get("version"),
        "manifest_sha256": sha256_file(manifest_path),
        "integrity_sha256": sha256_json(integrity),
        "adapter_sha256": sha256_file(adapter_path),
        "runtime_sha256": sha256_file(runtime_path),
        "capabilities": manifest.get("capabilities", {}),
        "object_ids": object_ids,
        "evidence_ids": evidence_ids,
        "conflict_ids": conflict_ids,
        "gap_ids": gap_ids,
        "evidence_to_objects": evidence_to_objects,
    }


def package_binding(package: Path) -> dict[str, Any]:
    inventory = _load_inventory(package)
    return {
        "package_id": inventory["package_id"],
        "package_version": inventory["package_version"],
        "manifest_sha256": inventory["manifest_sha256"],
        "integrity_sha256": inventory["integrity_sha256"],
        "adapter_sha256": inventory["adapter_sha256"],
        "runtime_sha256": inventory["runtime_sha256"],
    }


def validate_challenge(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "id",
        "category",
        "prompt",
    }:
        raise HostForwardTestError("challenge_schema_invalid")
    challenge_id = value.get("id")
    category = value.get("category")
    prompt = value.get("prompt")
    if value.get("schema_version") != CHALLENGE_SCHEMA:
        raise HostForwardTestError("challenge_schema_invalid")
    if not isinstance(challenge_id, str) or not ID_RE.fullmatch(challenge_id):
        raise HostForwardTestError("challenge_id_invalid")
    if not isinstance(category, str) or not ID_RE.fullmatch(category):
        raise HostForwardTestError("challenge_category_invalid")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
        raise HostForwardTestError("challenge_prompt_invalid")
    lowered_keys = {str(key).casefold() for key in value}
    if "expected" in lowered_keys or "answer" in lowered_keys or "verdict" in lowered_keys:
        raise HostForwardTestError("challenge_leaks_expectation")
    return {
        "schema_version": CHALLENGE_SCHEMA,
        "id": challenge_id,
        "category": category,
        "prompt": prompt.strip(),
    }


def validate_challenges(rows: Iterable[Any]) -> list[dict[str, Any]]:
    output = [validate_challenge(row) for row in rows]
    ids = [row["id"] for row in output]
    if not output or len(ids) != len(set(ids)):
        raise HostForwardTestError("challenge_coverage_invalid")
    return sorted(output, key=lambda row: row["id"])


EXPECTATION_ARRAY_FIELDS = (
    "required_object_ids",
    "allowed_object_ids",
    "required_evidence_ids",
    "allowed_evidence_ids",
    "required_conflict_ids",
    "allowed_conflict_ids",
    "required_gap_ids",
    "allowed_gap_ids",
    "required_reason_codes",
    "allowed_reason_codes",
    "required_answer_terms",
    "forbidden_answer_terms",
)


def validate_expectation(value: Any, inventory: dict[str, Any]) -> dict[str, Any]:
    required_keys = {"schema_version", "challenge_id", "allowed_behaviors", *EXPECTATION_ARRAY_FIELDS}
    if not isinstance(value, dict) or set(value) != required_keys:
        raise HostForwardTestError("expectation_schema_invalid")
    if value.get("schema_version") != EXPECTATION_SCHEMA:
        raise HostForwardTestError("expectation_schema_invalid")
    challenge_id = value.get("challenge_id")
    allowed_behaviors = _unique_strings(value.get("allowed_behaviors"), "allowed_behaviors")
    if not isinstance(challenge_id, str) or not ID_RE.fullmatch(challenge_id):
        raise HostForwardTestError("expectation_challenge_invalid")
    if not allowed_behaviors or set(allowed_behaviors) - BEHAVIORS:
        raise HostForwardTestError("expectation_behavior_invalid")
    arrays = {field: _unique_strings(value.get(field), field) for field in EXPECTATION_ARRAY_FIELDS}
    for field, known in (
        ("required_object_ids", inventory["object_ids"]),
        ("allowed_object_ids", inventory["object_ids"]),
        ("required_evidence_ids", inventory["evidence_ids"]),
        ("allowed_evidence_ids", inventory["evidence_ids"]),
        ("required_conflict_ids", inventory["conflict_ids"]),
        ("allowed_conflict_ids", inventory["conflict_ids"]),
        ("required_gap_ids", inventory["gap_ids"]),
        ("allowed_gap_ids", inventory["gap_ids"]),
    ):
        if set(arrays[field]) - known:
            raise HostForwardTestError(f"expectation_reference_unresolved: {field}")
    for required, allowed in (
        ("required_object_ids", "allowed_object_ids"),
        ("required_evidence_ids", "allowed_evidence_ids"),
        ("required_conflict_ids", "allowed_conflict_ids"),
        ("required_gap_ids", "allowed_gap_ids"),
        ("required_reason_codes", "allowed_reason_codes"),
    ):
        if not set(arrays[required]).issubset(arrays[allowed]):
            raise HostForwardTestError(f"expectation_required_not_allowed: {required}")
    if set(allowed_behaviors) == {"abstain"} and any(
        arrays[field]
        for field in ("allowed_object_ids", "allowed_evidence_ids", "allowed_conflict_ids", "allowed_gap_ids")
    ):
        raise HostForwardTestError("expectation_abstention_must_not_cite")
    return {
        "schema_version": EXPECTATION_SCHEMA,
        "challenge_id": challenge_id,
        "allowed_behaviors": allowed_behaviors,
        **arrays,
    }


def parse_agent_response(raw_response: str) -> dict[str, Any]:
    if not isinstance(raw_response, str) or not raw_response.strip():
        raise HostForwardTestError("host_response_empty")
    text = raw_response.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HostForwardTestError(f"host_response_not_json: {exc.msg}") from exc
    expected_keys = {
        "schema_version",
        "challenge_id",
        "behavior",
        "answer",
        "object_ids",
        "evidence_ids",
        "conflict_ids",
        "gap_ids",
        "reason_codes",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise HostForwardTestError("host_response_schema_invalid")
    if value.get("schema_version") != RESPONSE_SCHEMA:
        raise HostForwardTestError("host_response_schema_invalid")
    if not isinstance(value.get("challenge_id"), str) or not ID_RE.fullmatch(value["challenge_id"]):
        raise HostForwardTestError("host_response_challenge_invalid")
    if value.get("behavior") not in BEHAVIORS:
        raise HostForwardTestError("host_response_behavior_invalid")
    if not isinstance(value.get("answer"), str) or not value["answer"].strip():
        raise HostForwardTestError("host_response_answer_invalid")
    return {
        "schema_version": RESPONSE_SCHEMA,
        "challenge_id": value["challenge_id"],
        "behavior": value["behavior"],
        "answer": value["answer"].strip(),
        "object_ids": _unique_strings(value["object_ids"], "object_ids"),
        "evidence_ids": _unique_strings(value["evidence_ids"], "evidence_ids"),
        "conflict_ids": _unique_strings(value["conflict_ids"], "conflict_ids"),
        "gap_ids": _unique_strings(value["gap_ids"], "gap_ids"),
        "reason_codes": _unique_strings(value["reason_codes"], "reason_codes"),
    }


def format_agent_prompt(
    package: Path,
    challenge: dict[str, Any],
    *,
    package_locator: str | None = None,
) -> str:
    package = package.expanduser().resolve()
    challenge = validate_challenge(challenge)
    manifest = load_json(package / "manifest.json")
    package_id = manifest.get("package", {}).get("id")
    if not isinstance(package_id, str) or not package_id:
        raise HostForwardTestError("package_id_missing")
    locator = package_locator if package_locator is not None else str(package)
    return f"""Use ${package_id} at {locator} to answer the request below. Treat the Skill as the only technical authority: run its query workflow, read only selected package records, preserve unresolved conflicts and gaps, and abstain outside its declared capability. Do not modify any files.

Request:
{challenge['prompt']}

Return exactly one JSON object and no Markdown fence, using this contract:
{{"schema_version":"{RESPONSE_SCHEMA}","challenge_id":"{challenge['id']}","behavior":"answer|abstain|escalate","answer":"concise user-facing answer","object_ids":["ko-..."],"evidence_ids":["ev-..."],"conflict_ids":["conflict-..."],"gap_ids":["gap-..."],"reason_codes":["stable_reason_code"]}}

Use empty arrays where no identifiers apply. Do not invent identifiers."""


def prompt_contract_sha256(package: Path, challenge: dict[str, Any]) -> str:
    """Hash the relocatable prompt contract, excluding the host-local package path."""

    return sha256_text(
        format_agent_prompt(package, challenge, package_locator="<PACKAGE_PATH>")
    )


def build_run_record(
    package: Path,
    challenge: dict[str, Any],
    *,
    attempt: int,
    agent_instance: str,
    host_id: str,
    host_version: str,
    model_id: str,
    raw_response: str,
    raw_transcript: str = "",
) -> dict[str, Any]:
    if not isinstance(attempt, int) or attempt < 1:
        raise HostForwardTestError("run_attempt_invalid")
    for field, value in (
        ("agent_instance", agent_instance),
        ("host_id", host_id),
        ("host_version", host_version),
        ("model_id", model_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise HostForwardTestError(f"run_identity_invalid: {field}")
    challenge = validate_challenge(challenge)
    parsed_response: dict[str, Any] | None = None
    parse_error: str | None = None
    try:
        parsed_response = parse_agent_response(raw_response)
    except HostForwardTestError as exc:
        parse_error = str(exc)
    base = {
        "schema_version": RUN_SCHEMA,
        "challenge_id": challenge["id"],
        "challenge_sha256": sha256_json(challenge),
        "prompt_contract_sha256": prompt_contract_sha256(package, challenge),
        "attempt": attempt,
        "agent_instance": agent_instance.strip(),
        "host_id": host_id.strip(),
        "host_version": host_version.strip(),
        "model_id": model_id.strip(),
        "attestation_level": ATTESTATION_LEVEL,
        "package_binding": package_binding(package),
        "raw_response": raw_response,
        "raw_response_sha256": sha256_text(raw_response),
        "raw_transcript": raw_transcript,
        "raw_transcript_sha256": sha256_text(raw_transcript),
        "parsed_response": parsed_response,
        "parse_error": parse_error,
    }
    return {"record_id": "hrr-" + sha256_json(base)[:24], **base}


def _check(code: str, passed: bool, message: str) -> dict[str, Any]:
    return {"code": code, "passed": bool(passed), "message": message}


def score_run(
    inventory: dict[str, Any],
    challenge: dict[str, Any],
    expectation: dict[str, Any],
    run: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    challenge = validate_challenge(challenge)
    expectation = validate_expectation(expectation, inventory)
    checks: list[dict[str, Any]] = []
    expected_binding = {
        "package_id": inventory["package_id"],
        "package_version": inventory["package_version"],
        "manifest_sha256": inventory["manifest_sha256"],
        "integrity_sha256": inventory["integrity_sha256"],
        "adapter_sha256": inventory["adapter_sha256"],
        "runtime_sha256": inventory["runtime_sha256"],
    }
    checks.append(_check("run_schema_valid", isinstance(run, dict) and set(run) == RUN_RECORD_FIELDS and run.get("schema_version") == RUN_SCHEMA, "run record schema"))
    checks.append(_check("challenge_binding_valid", run.get("challenge_id") == challenge["id"] and run.get("challenge_sha256") == sha256_json(challenge), "challenge ID and hash"))
    checks.append(_check("prompt_contract_binding_valid", run.get("prompt_contract_sha256") == prompt_contract_sha256(inventory["package"], challenge), "relocatable host prompt contract hash"))
    checks.append(_check("package_binding_fresh", run.get("package_binding") == expected_binding, "current sealed package and adapter hashes"))
    checks.append(_check("attestation_label_valid", run.get("attestation_level") == ATTESTATION_LEVEL, "non-cryptographic host attestation label"))
    raw = run.get("raw_response")
    checks.append(_check("raw_response_hash_valid", isinstance(raw, str) and run.get("raw_response_sha256") == sha256_text(raw or ""), "raw final response hash"))
    transcript = run.get("raw_transcript")
    checks.append(_check("raw_transcript_hash_valid", isinstance(transcript, str) and run.get("raw_transcript_sha256") == sha256_text(transcript or ""), "raw host transcript hash"))
    response = run.get("parsed_response")
    checks.append(_check("response_parse_valid", isinstance(response, dict) and run.get("parse_error") is None, "structured response parsed"))
    expected_response: dict[str, Any] | None = None
    expected_parse_error: str | None = None
    try:
        expected_response = parse_agent_response(raw or "")
    except HostForwardTestError as exc:
        expected_parse_error = str(exc)
    checks.append(
        _check(
            "response_recomputes_from_raw",
            response == expected_response and run.get("parse_error") == expected_parse_error,
            "parsed response and parse error recompute from raw final response",
        )
    )
    if not isinstance(response, dict):
        return checks, "failed"
    try:
        normalized = parse_agent_response(canonical_json_bytes(response).decode("utf-8"))
        response_contract_valid = normalized == response
    except HostForwardTestError:
        response_contract_valid = False
    checks.append(_check("response_contract_valid", response_contract_valid, "response fields and identifier arrays"))
    checks.append(_check("response_challenge_valid", response.get("challenge_id") == challenge["id"], "response challenge ID"))
    checks.append(_check("behavior_matches", response.get("behavior") in expectation["allowed_behaviors"], "allowed answer/abstain/escalate behavior"))

    mappings = (
        ("object_ids", "required_object_ids", "allowed_object_ids", inventory["object_ids"]),
        ("evidence_ids", "required_evidence_ids", "allowed_evidence_ids", inventory["evidence_ids"]),
        ("conflict_ids", "required_conflict_ids", "allowed_conflict_ids", inventory["conflict_ids"]),
        ("gap_ids", "required_gap_ids", "allowed_gap_ids", inventory["gap_ids"]),
        ("reason_codes", "required_reason_codes", "allowed_reason_codes", None),
    )
    for response_field, required_field, allowed_field, known in mappings:
        actual = set(response.get(response_field, [])) if isinstance(response.get(response_field), list) else set()
        required = set(expectation[required_field])
        allowed = set(expectation[allowed_field])
        checks.append(_check(f"{response_field}_required", required.issubset(actual), f"required {response_field}"))
        checks.append(_check(f"{response_field}_allowed", actual.issubset(allowed), f"no unexpected {response_field}"))
        if known is not None:
            checks.append(_check(f"{response_field}_resolved", actual.issubset(known), f"all {response_field} resolve in package"))

    returned_objects = set(response.get("object_ids", []))
    evidence_coherent = all(
        bool(inventory["evidence_to_objects"].get(evidence_id, set()) & returned_objects)
        for evidence_id in response.get("evidence_ids", [])
    )
    checks.append(_check("evidence_object_coherent", evidence_coherent, "every evidence ID supports a returned object"))
    answer_folded = response.get("answer", "").casefold()
    checks.append(_check("required_answer_terms", all(term.casefold() in answer_folded for term in expectation["required_answer_terms"]), "required answer boundary terms"))
    checks.append(_check("forbidden_answer_terms", all(term.casefold() not in answer_folded for term in expectation["forbidden_answer_terms"]), "forbidden answer claims absent"))
    verdict = "passed" if all(check["passed"] for check in checks) else "failed"
    return checks, verdict


def _receipt_for(
    inventory: dict[str, Any],
    challenge: dict[str, Any],
    expectation: dict[str, Any],
    run: dict[str, Any],
) -> dict[str, Any]:
    checks, verdict = score_run(inventory, challenge, expectation, run)
    base = {
        "schema_version": RECEIPT_SCHEMA,
        "scorer": "deterministic-host-forward",
        "scorer_version": SCORER_VERSION,
        "scorer_sha256": sha256_file(Path(__file__)),
        "attestation_level": ATTESTATION_LEVEL,
        "record_id": run.get("record_id"),
        "challenge_id": challenge["id"],
        "attempt": run.get("attempt"),
        "agent_instance": run.get("agent_instance"),
        "host_id": run.get("host_id"),
        "host_version": run.get("host_version"),
        "model_id": run.get("model_id"),
        "package_binding": run.get("package_binding"),
        "challenge_sha256": sha256_json(challenge),
        "expectation_sha256": sha256_json(expectation),
        "run_record_sha256": sha256_json(run),
        "checks": checks,
        "verdict": verdict,
    }
    return {"receipt_id": "her-" + sha256_json(base)[:24], **base}


def _validate_run_identity(run: Any) -> None:
    if (
        not isinstance(run, dict)
        or set(run) != RUN_RECORD_FIELDS
        or run.get("schema_version") != RUN_SCHEMA
    ):
        raise HostForwardTestError("run_schema_invalid")
    required = (
        "record_id",
        "challenge_id",
        "challenge_sha256",
        "prompt_contract_sha256",
        "agent_instance",
        "host_id",
        "host_version",
        "model_id",
        "raw_response",
        "raw_response_sha256",
        "raw_transcript_sha256",
    )
    if any(not isinstance(run.get(field), str) or not run[field] for field in required):
        raise HostForwardTestError("run_identity_invalid")
    if not isinstance(run.get("raw_transcript"), str):
        raise HostForwardTestError("run_identity_invalid")
    if not isinstance(run.get("attempt"), int) or run["attempt"] < 1:
        raise HostForwardTestError("run_attempt_invalid")
    base = {key: value for key, value in run.items() if key != "record_id"}
    if run["record_id"] != "hrr-" + sha256_json(base)[:24]:
        raise HostForwardTestError("run_record_id_stale")


def evaluate_certification(
    package: Path,
    challenges: Iterable[Any],
    expectations: Iterable[Any],
    runs: Iterable[Any],
    *,
    attempts_per_challenge: int,
    issued_at: str,
    commitment: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(attempts_per_challenge, int) or attempts_per_challenge < 1:
        raise HostForwardTestError("attempt_count_invalid")
    if not isinstance(issued_at, str) or not issued_at:
        raise HostForwardTestError("certification_timestamp_missing")
    inventory = _load_inventory(package)
    challenge_rows = validate_challenges(challenges)
    expectation_rows = [validate_expectation(row, inventory) for row in expectations]
    expectation_by_id = {row["challenge_id"]: row for row in expectation_rows}
    challenge_by_id = {row["id"]: row for row in challenge_rows}
    if len(expectation_by_id) != len(expectation_rows) or set(expectation_by_id) != set(challenge_by_id):
        raise HostForwardTestError("expectation_coverage_invalid")
    expectation_bundle_sha256 = sha256_json(
        sorted(expectation_rows, key=lambda row: row["challenge_id"])
    )
    if (
        not isinstance(commitment, dict)
        or commitment.get("schema_version") != "tkc.hidden-expectation-commitment/v0.1"
        or commitment.get("challenge_count") != len(challenge_rows)
        or commitment.get("expectation_bundle_sha256") != expectation_bundle_sha256
        or not isinstance(commitment.get("committed_at"), str)
        or not commitment["committed_at"]
    ):
        raise HostForwardTestError("expectation_commitment_invalid")
    run_rows = list(runs)
    for run in run_rows:
        _validate_run_identity(run)
    run_keys = [(run["challenge_id"], run["attempt"]) for run in run_rows]
    if len(run_keys) != len(set(run_keys)):
        raise HostForwardTestError("run_coverage_duplicate")
    expected_keys = {
        (challenge_id, attempt)
        for challenge_id in challenge_by_id
        for attempt in range(1, attempts_per_challenge + 1)
    }
    if set(run_keys) != expected_keys:
        raise HostForwardTestError("run_coverage_invalid")
    agent_instances = [run["agent_instance"] for run in run_rows]
    if len(agent_instances) != len(set(agent_instances)):
        raise HostForwardTestError("agent_instance_reused")

    receipts = [
        _receipt_for(
            inventory,
            challenge_by_id[run["challenge_id"]],
            expectation_by_id[run["challenge_id"]],
            run,
        )
        for run in sorted(run_rows, key=lambda row: (row["challenge_id"], row["attempt"]))
    ]
    passed = sum(receipt["verdict"] == "passed" for receipt in receipts)
    failed = len(receipts) - passed
    host_ids = sorted({run["host_id"] for run in run_rows})
    host_versions = sorted({run["host_version"] for run in run_rows})
    model_ids = sorted({run["model_id"] for run in run_rows})
    if len(host_ids) != 1 or len(host_versions) != 1 or len(model_ids) != 1:
        raise HostForwardTestError("run_environment_mixed")
    transcript_values = [run.get("raw_transcript", "") for run in run_rows]
    transcript_capture = (
        "full"
        if all(transcript_values)
        else "final-response-only"
        if not any(transcript_values)
        else "partial"
    )
    package_record = {
        "package_id": inventory["package_id"],
        "package_version": inventory["package_version"],
        "manifest_sha256": inventory["manifest_sha256"],
        "integrity_sha256": inventory["integrity_sha256"],
        "adapter_sha256": inventory["adapter_sha256"],
        "runtime_sha256": inventory["runtime_sha256"],
    }
    base = {
        "schema_version": CERTIFICATION_SCHEMA,
        "issued_at": issued_at,
        "attestation_level": ATTESTATION_LEVEL,
        "scorer_version": SCORER_VERSION,
        "scorer_sha256": sha256_file(Path(__file__)),
        "package_binding": package_record,
        "capabilities": inventory["capabilities"],
        "host_ids": host_ids,
        "host_versions": host_versions,
        "model_ids": model_ids,
        "transcript_capture": transcript_capture,
        "challenge_count": len(challenge_rows),
        "attempts_per_challenge": attempts_per_challenge,
        "run_count": len(run_rows),
        "passed_run_count": passed,
        "failed_run_count": failed,
        "challenge_bundle_sha256": sha256_json(challenge_rows),
        "expectation_bundle_sha256": sha256_json(sorted(expectation_rows, key=lambda row: row["challenge_id"])),
        "run_bundle_sha256": sha256_json(sorted(run_rows, key=lambda row: (row["challenge_id"], row["attempt"]))),
        "receipt_bundle_sha256": sha256_json(receipts),
        "expectation_commitment_sha256": sha256_json(commitment),
        "expectations_committed_at": commitment["committed_at"],
        "receipt_ids": [receipt["receipt_id"] for receipt in receipts],
        "status": "certified" if failed == 0 else "failed",
        "scope": CERTIFICATION_SCOPE,
        "limitations": [
            "host_identity_is_not_cryptographic",
            "model_identity_is_host_reported",
            "deterministic_id_and_boundary_scoring_not_full_prose_semantic_review",
            "no_decision_support",
            "no_executable_domain_procedures",
        ],
    }
    report = {"certification_id": "hcert-" + sha256_json(base)[:24], **base}
    return (
        report,
        challenge_rows,
        sorted(expectation_rows, key=lambda row: row["challenge_id"]),
        sorted(run_rows, key=lambda row: (row["challenge_id"], row["attempt"])),
        receipts,
    )


def write_certification_bundle(
    package: Path,
    challenges: Iterable[Any],
    expectations: Iterable[Any],
    runs: Iterable[Any],
    output: Path,
    *,
    attempts_per_challenge: int,
    issued_at: str,
    commitment: dict[str, Any],
) -> dict[str, Any]:
    output = output.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise HostForwardTestError(f"output_not_empty: {output}")
    report, challenge_rows, expectation_rows, run_rows, receipts = evaluate_certification(
        package,
        challenges,
        expectations,
        runs,
        attempts_per_challenge=attempts_per_challenge,
        issued_at=issued_at,
        commitment=commitment,
    )
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "certification.json", report)
    write_jsonl(output / "challenges.jsonl", challenge_rows)
    write_jsonl(output / "expectations.jsonl", expectation_rows)
    write_jsonl(output / "runs.jsonl", run_rows)
    write_jsonl(output / "receipts.jsonl", receipts)
    write_json(output / "expectation-commitment.json", commitment)
    return report


def validate_certification_bundle(package: Path, bundle: Path) -> list[ForwardIssue]:
    issues: list[ForwardIssue] = []
    bundle = bundle.expanduser().resolve()
    required = {
        "certification.json",
        "challenges.jsonl",
        "expectations.jsonl",
        "runs.jsonl",
        "receipts.jsonl",
        "expectation-commitment.json",
    }
    actual = {path.name for path in bundle.iterdir() if path.is_file()} if bundle.is_dir() else set()
    if actual != required:
        return [ForwardIssue("certification_files_invalid", "Expected exactly the six certification files.")]
    try:
        stored = load_json(bundle / "certification.json")
        report, challenges, expectations, runs, receipts = evaluate_certification(
            package,
            load_jsonl(bundle / "challenges.jsonl"),
            load_jsonl(bundle / "expectations.jsonl"),
            load_jsonl(bundle / "runs.jsonl"),
            attempts_per_challenge=stored.get("attempts_per_challenge"),
            issued_at=stored.get("issued_at"),
            commitment=load_json(bundle / "expectation-commitment.json"),
        )
        if report != stored:
            issues.append(ForwardIssue("certification_report_stale", "Certification report does not recompute."))
        if receipts != load_jsonl(bundle / "receipts.jsonl"):
            issues.append(ForwardIssue("host_receipt_stale", "Host receipts do not recompute."))
        if stored.get("status") != "certified":
            issues.append(ForwardIssue("host_certification_failed", "One or more forward-test runs failed."))
        if stored.get("capabilities") != {"reference": True, "decision_support": False, "executable": False}:
            issues.append(ForwardIssue("capability_scope_invalid", "Certification changed the reference-only capability boundary."))
        if len(challenges) != stored.get("challenge_count") or len(runs) != stored.get("run_count"):
            issues.append(ForwardIssue("certification_count_stale", "Certification counts do not match bundle content."))
        if {row["challenge_id"] for row in expectations} != {row["id"] for row in challenges}:
            issues.append(ForwardIssue("expectation_coverage_invalid", "Expectations do not cover challenges exactly."))
    except (HostForwardTestError, OSError, ValueError, json.JSONDecodeError) as exc:
        issues.append(ForwardIssue("certification_invalid", str(exc)))
    return issues


def _load_json_or_jsonl(path: Path) -> list[Any]:
    return load_jsonl(path) if path.suffix == ".jsonl" else load_json(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prompt = subparsers.add_parser("prompt", help="Render a leak-free Agent prompt for one challenge.")
    prompt.add_argument("--package", required=True, type=Path)
    prompt.add_argument("--challenge", required=True, type=Path)

    record = subparsers.add_parser("record", help="Record one raw host-Agent final response.")
    record.add_argument("--package", required=True, type=Path)
    record.add_argument("--challenge", required=True, type=Path)
    record.add_argument("--attempt", required=True, type=int)
    record.add_argument("--agent-instance", required=True)
    record.add_argument("--host-id", required=True)
    record.add_argument("--host-version", required=True)
    record.add_argument("--model-id", required=True)
    record.add_argument("--response", required=True, type=Path)
    record.add_argument("--output", required=True, type=Path)

    certify = subparsers.add_parser("certify", help="Score runs and write a certification sidecar.")
    certify.add_argument("--package", required=True, type=Path)
    certify.add_argument("--challenges", required=True, type=Path)
    certify.add_argument("--expectations", required=True, type=Path)
    certify.add_argument("--runs", required=True, type=Path)
    certify.add_argument("--commitment", required=True, type=Path)
    certify.add_argument("--output", required=True, type=Path)
    certify.add_argument("--attempts", required=True, type=int)
    certify.add_argument("--issued-at", required=True)

    validate = subparsers.add_parser("validate", help="Recompute a certification sidecar.")
    validate.add_argument("--package", required=True, type=Path)
    validate.add_argument("--bundle", required=True, type=Path)
    validate.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "prompt":
            value = load_json(args.challenge)
            print(format_agent_prompt(args.package, value))
            return 0
        if args.command == "record":
            record = build_run_record(
                args.package,
                load_json(args.challenge),
                attempt=args.attempt,
                agent_instance=args.agent_instance,
                host_id=args.host_id,
                host_version=args.host_version,
                model_id=args.model_id,
                raw_response=args.response.read_text(encoding="utf-8"),
            )
            write_json(args.output, record)
            print(f"recorded {record['record_id']}")
            return 0
        if args.command == "certify":
            report = write_certification_bundle(
                args.package,
                _load_json_or_jsonl(args.challenges),
                _load_json_or_jsonl(args.expectations),
                _load_json_or_jsonl(args.runs),
                args.output,
                attempts_per_challenge=args.attempts,
                issued_at=args.issued_at,
                commitment=load_json(args.commitment),
            )
            print(f"{report['status']} {report['passed_run_count']}/{report['run_count']} {report['certification_id']}")
            return 0 if report["status"] == "certified" else 1
        issues = validate_certification_bundle(args.package, args.bundle)
        if args.json_output:
            print(json.dumps({"passed": not issues, "issues": [issue.to_dict() for issue in issues]}, ensure_ascii=False, indent=2))
        elif issues:
            for issue in issues:
                print(f"ERROR {issue.code}: {issue.message}")
        else:
            print(f"PASS host certification {args.bundle.resolve()}")
        return 0 if not issues else 1
    except (HostForwardTestError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
