#!/usr/bin/env python3
"""Emit a deterministic stable-ID and dependency-impact report for a Skill update."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from expert_skill_contract import (
    load_json,
    load_jsonl,
    sha256_file,
    sha256_json,
    validate_expert_skill,
    write_json,
)


REPORT_SCHEMA = "tkc.incremental-update-report/v0.1"
DIFFER_VERSION = "0.1.0-phase5"


class UpdateDiffError(RuntimeError):
    """Stable incremental-update diff failure."""


def _rows(package: Path, relative: str) -> list[dict[str, Any]]:
    path = package / relative
    if not path.is_file():
        return []
    return [row for row in load_jsonl(path) if isinstance(row, dict)]


def _inventory(package: Path) -> dict[str, dict[str, dict[str, Any]]]:
    inventory: dict[str, dict[str, dict[str, Any]]] = {
        "source": {},
        "module": {},
        "object": {},
        "evidence": {},
        "relation": {},
        "conflict": {},
        "gap": {},
        "procedure": {},
        "decision": {},
        "competency_test": {},
    }
    manifest = load_json(package / "manifest.json")
    for source in manifest.get("sources", []):
        if isinstance(source, dict) and isinstance(source.get("source_id"), str):
            inventory["source"][source["source_id"]] = source
    for module in manifest.get("modules", []):
        if isinstance(module, dict) and isinstance(module.get("module_id"), str):
            inventory["module"][module["module_id"]] = module
    index = load_json(package / "references/knowledge/index.json")
    for entry in index.get("objects", []):
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            record = load_json(package / entry["path"])
            if isinstance(record, dict) and isinstance(record.get("id"), str):
                inventory["object"][record["id"]] = record
    for kind, relative in (
        ("evidence", "references/evidence/anchors.jsonl"),
        ("relation", "references/knowledge/relations.jsonl"),
        ("conflict", "references/conflicts.jsonl"),
        ("gap", "references/knowledge-gaps.jsonl"),
        ("competency_test", "references/competency-tests.jsonl"),
    ):
        for record in _rows(package, relative):
            if isinstance(record.get("id"), str):
                inventory[kind][record["id"]] = record
    for kind, relative, field in (
        ("procedure", "references/procedures/index.json", "procedures"),
        ("decision", "references/decisions/index.json", "decisions"),
    ):
        index_path = package / relative
        if not index_path.is_file():
            continue
        index_record = load_json(index_path)
        for entry in index_record.get(field, []):
            if not isinstance(entry, dict):
                continue
            identifier = entry.get("id")
            path = entry.get("path")
            if isinstance(identifier, str) and isinstance(path, str):
                inventory[kind][identifier] = load_json(package / path)
    return inventory


def _identity_projection(kind: str, record: dict[str, Any]) -> dict[str, Any]:
    if kind == "source":
        return {
            "source_id": record.get("source_id"),
            "sha256": record.get("sha256"),
        }
    projection = copy.deepcopy(record)
    for field in ("review_binding", "confidence", "status", "version"):
        projection.pop(field, None)
    if kind == "competency_test":
        projection.pop("result", None)
    return projection


def _classify_kind(
    kind: str,
    old: dict[str, dict[str, Any]],
    new: dict[str, dict[str, Any]],
) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
    old_ids = set(old)
    new_ids = set(new)
    common = old_ids & new_ids
    changed = sorted(identifier for identifier in common if sha256_json(old[identifier]) != sha256_json(new[identifier]))
    unchanged = sorted(common - set(changed))
    identity_violations = []
    for identifier in changed:
        if sha256_json(_identity_projection(kind, old[identifier])) != sha256_json(
            _identity_projection(kind, new[identifier])
        ):
            identity_violations.append(
                {
                    "kind": kind,
                    "id": identifier,
                    "code": "stable_id_semantic_drift",
                }
            )
    return {
        "added": sorted(new_ids - old_ids),
        "changed": changed,
        "removed": sorted(old_ids - new_ids),
        "unchanged": unchanged,
    }, identity_violations


def _claim_ids(conflict: dict[str, Any]) -> set[str]:
    return {
        claim["claim_id"]
        for claim in conflict.get("claims", [])
        if isinstance(claim, dict) and isinstance(claim.get("claim_id"), str)
    }


def build_report(old_package: Path, new_package: Path, *, generated_at: str) -> dict[str, Any]:
    try:
        parsed_generated_at = dt.datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise UpdateDiffError("generated_at_invalid") from error
    if parsed_generated_at.tzinfo is None:
        raise UpdateDiffError("generated_at_invalid")
    old_package = old_package.expanduser().resolve()
    new_package = new_package.expanduser().resolve()
    for label, package in (("old", old_package), ("new", new_package)):
        errors = [
            issue
            for issue in validate_expert_skill(package, level="structure")
            if issue.severity == "error"
        ]
        if errors:
            raise UpdateDiffError(f"{label}_package_invalid: {errors[0].code}:{errors[0].path}")
    old_manifest = load_json(old_package / "manifest.json")
    new_manifest = load_json(new_package / "manifest.json")
    if old_manifest.get("package", {}).get("id") != new_manifest.get("package", {}).get("id"):
        raise UpdateDiffError("package_identity_changed")
    old_inventory = _inventory(old_package)
    new_inventory = _inventory(new_package)
    changes: dict[str, dict[str, list[str]]] = {}
    violations: list[dict[str, str]] = []
    for kind in old_inventory:
        changes[kind], kind_violations = _classify_kind(
            kind, old_inventory[kind], new_inventory[kind]
        )
        if kind == "module":
            kind_violations = []
        violations.extend(kind_violations)
    changed_evidence = set(changes["evidence"]["added"] + changes["evidence"]["changed"] + changes["evidence"]["removed"])
    impacted_objects = set(changes["object"]["added"] + changes["object"]["changed"] + changes["object"]["removed"])
    for inventory in (old_inventory["object"], new_inventory["object"]):
        for object_id, record in inventory.items():
            if changed_evidence & set(record.get("evidence_ids", [])):
                impacted_objects.add(object_id)
    for kind in ("relation", "conflict"):
        changed_ids = set(changes[kind]["added"] + changes[kind]["changed"] + changes[kind]["removed"])
        for inventory in (old_inventory[kind], new_inventory[kind]):
            for identifier in changed_ids:
                record = inventory.get(identifier)
                if not isinstance(record, dict):
                    continue
                if kind == "relation":
                    for field in ("source_id", "target_id"):
                        if isinstance(record.get(field), str):
                            impacted_objects.add(record[field])
                else:
                    impacted_objects.update(_claim_ids(record))
    affected_modules: set[str] = set()
    for manifest in (old_manifest, new_manifest):
        for module in manifest.get("modules", []):
            if isinstance(module, dict) and impacted_objects & set(module.get("exports", [])):
                module_id = module.get("module_id")
                if isinstance(module_id, str):
                    affected_modules.add(module_id)
    all_changed = any(
        values[status]
        for values in changes.values()
        for status in ("added", "changed", "removed")
    ) or sha256_json(old_manifest) != sha256_json(new_manifest)
    invalidated_artifacts = []
    if all_changed:
        invalidated_artifacts = [
            "manifest.integrity",
            "references/runtime/catalog.json",
            "references/runtime/reference-map.md",
            "references/evaluations/evaluation-plan.json",
            "references/evaluations/runtime-responses.jsonl",
            "references/evaluations/competency-receipts.jsonl",
            "host-certification-sidecars",
        ]
    conflicted = sorted(
        {
            claim_id
            for conflict in new_inventory["conflict"].values()
            if conflict.get("status") == "unresolved"
            for claim_id in _claim_ids(conflict)
        }
    )
    old_capabilities = old_manifest.get("capabilities")
    new_capabilities = new_manifest.get("capabilities")
    capability_escalations = sorted(
        capability
        for capability in ("reference", "decision_support", "executable")
        if isinstance(old_capabilities, dict)
        and isinstance(new_capabilities, dict)
        and old_capabilities.get(capability) is False
        and new_capabilities.get(capability) is True
    )
    if capability_escalations:
        violations.extend(
            {
                "kind": "capability",
                "id": capability,
                "code": "capability_escalation_requires_recertification",
            }
            for capability in capability_escalations
        )
    return {
        "schema_version": REPORT_SCHEMA,
        "differ_version": DIFFER_VERSION,
        "generated_at": generated_at,
        "package_id": old_manifest["package"]["id"],
        "old": {
            "version": old_manifest["package"].get("version"),
            "manifest_sha256": sha256_file(old_package / "manifest.json"),
            "integrity_sha256": sha256_json(old_manifest.get("integrity")),
            "input_fingerprint": old_manifest.get("build", {}).get("input_fingerprint"),
        },
        "new": {
            "version": new_manifest["package"].get("version"),
            "manifest_sha256": sha256_file(new_package / "manifest.json"),
            "integrity_sha256": sha256_json(new_manifest.get("integrity")),
            "input_fingerprint": new_manifest.get("build", {}).get("input_fingerprint"),
        },
        "changes": changes,
        "conflicted_object_ids": conflicted,
        "identity_violations": sorted(violations, key=lambda row: (row["kind"], row["id"])),
        "capability_escalations": capability_escalations,
        "dependency_impact": {
            "affected_object_ids": sorted(impacted_objects),
            "affected_module_ids": sorted(affected_modules),
            "invalidated_artifacts": invalidated_artifacts,
        },
        "safe_for_incremental_rebuild": not violations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", type=Path, required=True)
    parser.add_argument("--new", type=Path, required=True)
    parser.add_argument("--generated-at", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = build_report(args.old, args.new, generated_at=args.generated_at)
    except (UpdateDiffError, OSError, json.JSONDecodeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.output:
        write_json(args.output.expanduser().resolve(), report)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["safe_for_incremental_rebuild"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
