#!/usr/bin/env python3
"""Seal shareable Expert Skill files and optionally promote a valid package."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import sys
from pathlib import Path

from composite_contract import composite_tracked_files, validate_composite_skill
from expert_skill_contract import load_json, sha256_file, tracked_files, validate_expert_skill, write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="Expert Skill directory.")
    parser.add_argument(
        "--mark-ready",
        action="store_true",
        help="Promote only if the sealed package passes publication validation.",
    )
    parser.add_argument(
        "--sealed-at",
        help="Explicit RFC 3339 timestamp for reproducible sealing; defaults to current UTC time.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.package.expanduser().resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"error: missing {manifest_path}")

    manifest = load_json(manifest_path)
    is_composite = manifest.get("schema_version") == "tkc.composite-skill/v0.1"
    validator = validate_composite_skill if is_composite else validate_expert_skill

    resealable_integrity_codes = {
        "integrity_mismatch",
        "missing_integrity_file",
        "invalid_integrity",
        "composite_integrity_mismatch",
        "composite_integrity_metadata_invalid",
    }
    structural_errors = [
        issue for issue in validator(root, level="structure")
        if issue.severity == "error" and issue.code not in resealable_integrity_codes
    ]
    if structural_errors:
        for issue in structural_errors:
            print(f"ERROR {issue.code} {issue.path}: {issue.message}", file=sys.stderr)
        return 1

    original = copy.deepcopy(manifest)
    candidate = copy.deepcopy(manifest)
    if args.mark_ready:
        candidate["package"]["status"] = "ready"
    sealed_at = args.sealed_at
    if sealed_at is None:
        sealed_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    else:
        try:
            parsed = dt.datetime.fromisoformat(sealed_at.replace("Z", "+00:00"))
        except ValueError:
            print("ERROR invalid_sealed_at: expected RFC 3339 timestamp", file=sys.stderr)
            return 2
        if parsed.tzinfo is None:
            print("ERROR invalid_sealed_at: timezone offset is required", file=sys.stderr)
            return 2

    original_lock: dict | None = None
    if is_composite:
        # The composite contract binds build.created_at, lock.created_at, and
        # integrity.sealed_at together, so sealing re-stamps the build moment.
        candidate["build"]["created_at"] = sealed_at
        lock_path = root / "composition-lock.json"
        if not lock_path.is_file():
            print("ERROR composition_lock_missing: composition-lock.json", file=sys.stderr)
            return 2
        original_lock = load_json(lock_path)
        lock = copy.deepcopy(original_lock)
        lock["created_at"] = sealed_at
        write_json(lock_path, lock)
        candidate["build"]["composition_lock_sha256"] = sha256_file(lock_path)

    if is_composite:
        tracked = composite_tracked_files(root)
    else:
        tracked = tracked_files(
            root,
            include_nested_manifests=candidate.get("build", {}).get("compiler_version") == "0.4.0-execution",
        )
    candidate["integrity"] = {
        "algorithm": "sha256",
        "sealed_at": sealed_at,
        "files": tracked,
    }
    write_json(manifest_path, candidate)

    if args.mark_ready:
        publish_errors = [
            issue for issue in validator(root, level="publish")
            if issue.severity == "error"
        ]
        if publish_errors:
            write_json(manifest_path, original)
            if original_lock is not None:
                write_json(root / "composition-lock.json", original_lock)
            for issue in publish_errors:
                print(f"ERROR {issue.code} {issue.path}: {issue.message}", file=sys.stderr)
            print("Package was not promoted; manifest restored.", file=sys.stderr)
            return 1

    print(f"sealed {len(candidate['integrity']['files'])} files")
    print(f"status {candidate['package']['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
