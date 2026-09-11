#!/usr/bin/env python3
"""Compile and evaluate a portable reference-tier runtime from a reviewed draft."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from compiler_version import PHASE3_COMPILER_VERSION
from expert_skill_contract import (
    SCHEMA_ID,
    load_json,
    load_jsonl,
    sha256_file,
    sha256_json,
    validate_expert_skill,
    write_json,
)
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


SUITE_SCHEMA = "tkc.competency-suite/v0.1"
TEST_SCHEMA = "tkc.competency-test/v0.2"
PLAN_SCHEMA = "tkc.competency-evaluation-plan/v0.1"
RECEIPT_SCHEMA = "tkc.competency-receipt/v0.1"
COMPILER_VERSION = PHASE3_COMPILER_VERSION
REQUIRED_CATEGORIES = {
    "definition",
    "comparison",
    "equation",
    "decision",
    "failure-condition",
    "trace",
    "out-of-scope",
}
RFC3339_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)


class ReferenceTierError(RuntimeError):
    """Stable reference-tier compiler failure."""


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
    temporary.replace(path)


def _ensure_output(source: Path, output: Path) -> Path:
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    if not source.is_dir():
        raise ReferenceTierError(f"input_package_missing: {source}")
    if output.exists() and any(output.iterdir()):
        raise ReferenceTierError(f"output_not_empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, output, dirs_exist_ok=True)
    return output


def build_runtime_catalog(package: Path, policy: dict[str, Any]) -> dict[str, Any]:
    manifest = load_json(package / "manifest.json")
    index = load_json(package / "references/knowledge/index.json")
    entries: list[dict[str, Any]] = []
    for record in index.get("objects", []):
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ReferenceTierError("knowledge_index_invalid")
        obj = load_json(package / record["path"])
        entries.append(
            {
                "id": obj["id"],
                "type": obj["type"],
                "title": obj["title"],
                "path": record["path"],
                "evidence_ids": sorted(obj.get("evidence_ids", [])),
                "source_ids": sorted(obj.get("source_ids", [])),
                "weighted_terms": object_weighted_terms(obj),
            }
        )

    conflicts: list[dict[str, Any]] = []
    for conflict in load_jsonl(package / "references/conflicts.jsonl"):
        claim_ids = sorted(
            claim.get("claim_id")
            for claim in conflict.get("claims", [])
            if isinstance(claim, dict) and isinstance(claim.get("claim_id"), str)
        )
        conflicts.append(
            {
                "id": conflict["id"],
                "title": conflict["topic"],
                "status": conflict["status"],
                "claim_ids": claim_ids,
                "weighted_terms": conflict_weighted_terms(conflict),
            }
        )

    gaps: list[dict[str, Any]] = []
    for gap in load_jsonl(package / "references/knowledge-gaps.jsonl"):
        gaps.append(
            {
                "id": gap["id"],
                "title": gap["statement"],
                "status": gap["status"],
                "weighted_terms": gap_weighted_terms(gap),
            }
        )

    modules = manifest.get("modules", [])
    module_id = modules[0].get("module_id") if modules and isinstance(modules[0], dict) else None
    return {
        "schema_version": CATALOG_SCHEMA,
        "runtime_version": RUNTIME_VERSION,
        "package_id": manifest["package"]["id"],
        "module_id": module_id,
        "capabilities": manifest["capabilities"],
        "policy": policy,
        "objects": sorted(entries, key=lambda row: row["id"]),
        "conflicts": sorted(conflicts, key=lambda row: row["id"]),
        "gaps": sorted(gaps, key=lambda row: row["id"]),
    }


def _validate_suite(
    suite: dict[str, Any],
    object_ids: set[str],
    evidence_ids: set[str],
    conflict_ids: set[str],
    has_equation_objects: bool = True,
) -> list[dict[str, Any]]:
    if suite.get("schema_version") != SUITE_SCHEMA:
        raise ReferenceTierError("competency_suite_schema_invalid")
    tests = suite.get("tests")
    if not isinstance(tests, list) or not tests:
        raise ReferenceTierError("competency_tests_missing")
    seen: set[str] = set()
    categories: set[str] = set()
    output: list[dict[str, Any]] = []
    for index, test in enumerate(tests):
        if not isinstance(test, dict):
            raise ReferenceTierError(f"competency_test_invalid: {index}")
        test_id = test.get("id")
        category = test.get("category")
        prompt = test.get("prompt")
        expected = test.get("expected")
        if (
            not isinstance(test_id, str)
            or not test_id
            or test_id in seen
            or category not in REQUIRED_CATEGORIES
            or not isinstance(prompt, str)
            or not prompt
            or not isinstance(expected, dict)
        ):
            raise ReferenceTierError(f"competency_test_invalid: {test_id}")
        seen.add(test_id)
        categories.add(category)
        behavior = expected.get("behavior")
        if behavior not in {"answer", "abstain", "escalate"}:
            raise ReferenceTierError(f"competency_behavior_invalid: {test_id}")
        if category == "decision" and behavior == "answer":
            raise ReferenceTierError(f"reference_tier_decision_must_abstain: {test_id}")
        if category == "out-of-scope" and behavior != "abstain":
            raise ReferenceTierError(f"out_of_scope_must_abstain: {test_id}")
        for field, known in (
            ("required_object_ids", object_ids),
            ("forbidden_object_ids", object_ids),
            ("required_evidence_ids", evidence_ids),
            ("required_conflict_ids", conflict_ids),
        ):
            values = expected.get(field, [])
            if (
                not isinstance(values, list)
                or any(not isinstance(value, str) for value in values)
                or values != sorted(set(values))
                or set(values) - known
            ):
                raise ReferenceTierError(f"competency_reference_invalid: {test_id}.{field}")
        reason_codes = expected.get("required_reason_codes", [])
        if (
            not isinstance(reason_codes, list)
            or any(not isinstance(value, str) for value in reason_codes)
            or reason_codes != sorted(set(reason_codes))
        ):
            raise ReferenceTierError(f"competency_reason_invalid: {test_id}")
        limit = test.get("runtime", {}).get("limit", 8) if isinstance(test.get("runtime"), dict) else 8
        if not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ReferenceTierError(f"competency_runtime_invalid: {test_id}")
        output.append(
            {
                "schema_version": TEST_SCHEMA,
                "id": test_id,
                "category": category,
                "prompt": prompt,
                "runtime": {"limit": limit},
                "expected": {
                    "behavior": behavior,
                    "required_object_ids": expected.get("required_object_ids", []),
                    "forbidden_object_ids": expected.get("forbidden_object_ids", []),
                    "required_evidence_ids": expected.get("required_evidence_ids", []),
                    "required_conflict_ids": expected.get("required_conflict_ids", []),
                    "required_reason_codes": reason_codes,
                },
            }
        )
    required_categories = set(REQUIRED_CATEGORIES)
    if not has_equation_objects:
        required_categories.discard("equation")
    missing = required_categories - categories
    if missing:
        raise ReferenceTierError(f"competency_categories_missing: {sorted(missing)}")
    return sorted(output, key=lambda row: row["id"])


def _runtime_guide(catalog: dict[str, Any]) -> str:
    lines = [
        "# Runtime reference map",
        "",
        "This file is generated only from accepted canonical object metadata. It does not",
        "add technical claims. Use the query runtime to select objects, then read those",
        "object files and cite their knowledge and evidence IDs.",
        "",
        "## Objects",
        "",
        "| Type | Title | Object ID | Evidence IDs |",
        "| --- | --- | --- | --- |",
    ]
    for entry in catalog["objects"]:
        title = str(entry["title"]).replace("|", "\\|")
        evidence = ", ".join(entry["evidence_ids"])
        lines.append(f"| {entry['type']} | {title} | `{entry['id']}` | `{evidence}` |")
    lines.extend(["", "## Unresolved conflicts", ""])
    for conflict in catalog["conflicts"]:
        lines.append(
            f"- `{conflict['id']}` — {conflict['title']} — claims: "
            + ", ".join(f"`{value}`" for value in conflict["claim_ids"])
        )
    lines.extend(["", "## Known package gaps", ""])
    for gap in catalog["gaps"]:
        lines.append(f"- `{gap['id']}` — {gap['title']}")
    return "\n".join(lines) + "\n"


def _manifest_scope(manifest: dict[str, Any]) -> tuple[str, str, str]:
    """Return bounded display labels derived only from the input manifest."""

    package = manifest.get("package", {})
    package = package if isinstance(package, dict) else {}
    domain = package.get("domain")
    domain_label = str(domain).strip() if isinstance(domain, str) and domain.strip() else "declared domain"
    source_labels: list[str] = []
    for source in manifest.get("sources", []):
        if not isinstance(source, dict):
            continue
        title = source.get("title")
        source_id = source.get("source_id")
        label = title if isinstance(title, str) and title.strip() else source_id
        if isinstance(label, str) and label.strip():
            source_labels.append(" ".join(label.split()))
    source_scope = ", ".join(dict.fromkeys(source_labels)) or "the manifest-declared sources"
    capabilities = manifest.get("capabilities", {})
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    capability_scope = ", ".join(
        f"{key}={str(capabilities[key]).lower()}"
        for key in sorted(capabilities)
        if isinstance(key, str)
    ) or "manifest-declared capabilities"
    return domain_label, source_scope, capability_scope


def _skill_markdown(
    name: str,
    display_name: str,
    manifest: dict[str, Any],
) -> str:
    domain, source_scope, capability_scope = _manifest_scope(manifest)
    return f"""---
name: {name}
description: Use the ready, source-grounded {display_name} for bounded reference retrieval in the declared {domain} domain, bound to the manifest source scope ({source_scope}); preserve recorded conflicts and abstain from capabilities outside the manifest declaration ({capability_scope}).
---

# {display_name}

Use this ready reference-tier Skill only within the source scope declared in
`manifest.json`. It provides reference knowledge, not decision support or executable
domain procedures.

## Runtime workflow

1. Run `python3 scripts/query_reference.py --query "<request>"` from this Skill
   directory, or inspect `references/runtime/catalog.json` directly.
2. If the runtime returns `abstain`, report its reason code and do not improvise an
   answer from general model memory.
3. Read only the returned object paths in the runtime catalog, plus the returned
   evidence anchors, conflicts, and knowledge gaps.
4. State assumptions, applicability, validity conditions, and failure conditions.
5. Cite both knowledge object IDs and evidence IDs for every technical answer.
6. Keep source-printed and compiler-derived alternatives distinct whenever an
   unresolved conflict is returned.
7. Refuse stability, safety, reactor-readiness, design-certification, or other
   decision requests not supported by this reference tier.

## Verification boundary

The source PDF is not distributed. Source verification requires the external PDF
whose SHA-256 is declared in `manifest.json`. Competency receipts validate the
deterministic package-only retrieval layer; they do not prove human expert approval
or identical behavior on every host Agent.
"""


def compile_reference_tier(
    input_package: Path,
    suite_path: Path,
    output: Path,
    *,
    created_at: str,
) -> dict[str, Any]:
    if not RFC3339_RE.fullmatch(created_at):
        raise ReferenceTierError("created_at_invalid")
    output = _ensure_output(input_package, output)
    manifest_path = output / "manifest.json"
    manifest = load_json(manifest_path)
    if manifest.get("package", {}).get("status") != "draft":
        raise ReferenceTierError("input_package_not_draft")
    if manifest.get("capabilities") != {
        "reference": True,
        "decision_support": False,
        "executable": False,
    }:
        raise ReferenceTierError("input_package_not_reference_only")
    manifest.pop("integrity", None)
    semantic_assured = (
        output / "references/semantic/assurance-manifest.json"
    ).is_file()
    target_version = "0.5.0" if semantic_assured else "0.3.0"
    manifest["package"]["version"] = target_version
    manifest["package"]["status"] = "draft"
    for module in manifest.get("modules", []):
        if isinstance(module, dict):
            module["version"] = target_version
    manifest["build"]["compiler_version"] = COMPILER_VERSION
    manifest["build"]["created_at"] = created_at
    manifest["build"]["prompt_versions"] = sorted(
        set(manifest["build"].get("prompt_versions", []))
        | {"runtime-reference-v0.1", "competency-runtime-v0.1"}
    )
    write_json(manifest_path, manifest)

    suite = load_json(suite_path.expanduser().resolve())
    policy = suite.get("runtime_policy")
    if not isinstance(policy, dict):
        raise ReferenceTierError("runtime_policy_missing")
    for field in ("decision_guard_terms", "out_of_scope_terms"):
        values = policy.get(field)
        if (
            not isinstance(values, list)
            or any(not isinstance(value, str) or not value for value in values)
            or values != sorted(set(values))
        ):
            raise ReferenceTierError(f"runtime_policy_invalid: {field}")

    portable_source = Path(__file__).with_name("portable_reference_runtime.py")
    runtime_target = output / "scripts/query_reference.py"
    runtime_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(portable_source, runtime_target)

    catalog = build_runtime_catalog(output, policy)
    catalog_path = output / "references/runtime/catalog.json"
    write_json(catalog_path, catalog)
    guide_path = output / "references/runtime/reference-map.md"
    guide_path.write_text(_runtime_guide(catalog), encoding="utf-8")

    object_ids = {entry["id"] for entry in catalog["objects"]}
    evidence_ids = {
        value for entry in catalog["objects"] for value in entry["evidence_ids"]
    }
    conflict_ids = {entry["id"] for entry in catalog["conflicts"]}
    has_equation_objects = any(
        str(entry.get("type", "")).casefold() == "equation"
        for entry in catalog["objects"]
    )
    tests = _validate_suite(
        suite, object_ids, evidence_ids, conflict_ids, has_equation_objects
    )
    tests_path = output / "references/competency-tests.jsonl"
    _write_jsonl(tests_path, tests)

    responses: list[dict[str, Any]] = []
    for test in tests:
        response = query_catalog(
            catalog,
            test["prompt"],
            limit=test["runtime"]["limit"],
        )
        responses.append({"test_id": test["id"], "response": response})

    catalog_hash = sha256_json(catalog)
    suite_hash = sha256_json(tests)
    runtime_hash = sha256_file(runtime_target)
    run_id = "cer-" + hashlib.sha256(
        (catalog_hash + suite_hash + runtime_hash + EVALUATOR_VERSION).encode("utf-8")
    ).hexdigest()[:20]
    receipts: list[dict[str, Any]] = []
    for test, response_row in zip(tests, responses):
        response = response_row["response"]
        checks = evaluate_response(test, response)
        receipt_id = "cr-" + hashlib.sha256(
            (run_id + test["id"]).encode("utf-8")
        ).hexdigest()[:20]
        receipts.append(
            {
                "schema_version": RECEIPT_SCHEMA,
                "receipt_id": receipt_id,
                "test_id": test["id"],
                "run_id": run_id,
                "evaluator": "deterministic-runtime",
                "evaluator_version": EVALUATOR_VERSION,
                "test_sha256": sha256_json(test),
                "catalog_sha256": catalog_hash,
                "runtime_sha256": runtime_hash,
                "response_sha256": sha256_json(response_row),
                "checks": checks,
                "verdict": "passed" if all(check["passed"] for check in checks) else "failed",
            }
        )

    responses_path = output / "references/evaluations/runtime-responses.jsonl"
    receipts_path = output / "references/evaluations/competency-receipts.jsonl"
    plan_path = output / "references/evaluations/evaluation-plan.json"
    _write_jsonl(responses_path, responses)
    _write_jsonl(receipts_path, receipts)
    plan = {
        "schema_version": PLAN_SCHEMA,
        "run_id": run_id,
        "created_at": created_at,
        "evaluator": "deterministic-runtime",
        "evaluator_version": EVALUATOR_VERSION,
        "catalog": "references/runtime/catalog.json",
        "catalog_sha256": catalog_hash,
        "runtime": "scripts/query_reference.py",
        "runtime_sha256": runtime_hash,
        "test_suite": "references/competency-tests.jsonl",
        "test_suite_sha256": suite_hash,
        "responses": "references/evaluations/runtime-responses.jsonl",
        "receipts": "references/evaluations/competency-receipts.jsonl",
        "test_count": len(tests),
        "attestation_level": "deterministic-recomputed-not-human",
    }
    write_json(plan_path, plan)

    phase2_bundle = load_json(
        output / "references/evidence/promotion-report.json"
    ).get("review_bundle_sha256")
    manifest["build"]["input_fingerprint"] = sha256_json(
        {
            "phase2_review_bundle_sha256": phase2_bundle,
            "runtime_sha256": runtime_hash,
            "catalog_sha256": catalog_hash,
            "competency_suite_sha256": suite_hash,
            **(
                {
                    "semantic_assurance_manifest_sha256": sha256_file(
                        output / "references/semantic/assurance-manifest.json"
                    )
                }
                if semantic_assured
                else {}
            ),
        }
    )
    write_json(manifest_path, manifest)

    name = manifest["package"]["id"]
    display_name = manifest["package"]["name"]
    domain, _source_scope, _capability_scope = _manifest_scope(manifest)
    (output / "SKILL.md").write_text(
        _skill_markdown(name, display_name, manifest), encoding="utf-8"
    )
    (output / "agents/openai.yaml").write_text(
        "interface:\n"
        f"  display_name: {json.dumps(display_name)}\n"
        f"  short_description: {json.dumps(f'Source-grounded {domain} reference with traceable citations')}\n"
        f"  default_prompt: \"Use ${name} to answer from its runtime-selected objects, cite knowledge and evidence IDs, and abstain outside scope.\"\n",
        encoding="utf-8",
    )

    if any(receipt["verdict"] != "passed" for receipt in receipts):
        failed = [receipt["test_id"] for receipt in receipts if receipt["verdict"] != "passed"]
        raise ReferenceTierError(f"competency_evaluation_failed: {failed}")
    structural_errors = [
        issue for issue in validate_expert_skill(output, level="structure")
        if issue.severity == "error"
    ]
    if structural_errors:
        first = structural_errors[0]
        raise ReferenceTierError(f"structure_gate_failed: {first.code} {first.path}")
    return {
        "package": str(output),
        "run_id": run_id,
        "object_count": len(catalog["objects"]),
        "conflict_count": len(catalog["conflicts"]),
        "gap_count": len(catalog["gaps"]),
        "test_count": len(tests),
        "passed_test_count": sum(receipt["verdict"] == "passed" for receipt in receipts),
        "status": manifest["package"]["status"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--suite", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = compile_reference_tier(
            args.input,
            args.suite,
            args.output,
            created_at=args.created_at,
        )
    except (ReferenceTierError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            f"PASS package={result['package']} tests={result['passed_test_count']}/"
            f"{result['test_count']} status={result['status']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
