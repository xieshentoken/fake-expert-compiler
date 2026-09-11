#!/usr/bin/env python3
"""Compose sealed or reviewed-draft reference-only Expert Skills without rewriting child knowledge."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sys
from pathlib import Path

from composite_contract import (
    COMPOSITE_COMPILER_VERSION,
    COMPOSITE_SCHEMA,
    LOCK_SCHEMA,
    REFERENCE_CAPABILITIES,
    CompositionError,
    build_composite_catalog,
    composite_tracked_files,
    input_fingerprint,
    validate_composite_skill,
    validate_module_inputs,
)
from expert_skill_contract import SLUG_RE, sha256_file, write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", action="append", type=Path, required=True)
    parser.add_argument("--name", required=True, help="Hyphen-case composite Skill name.")
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--domain", required=True, help="Hyphen-case routing domain.")
    parser.add_argument("--version", default="0.5.0")
    parser.add_argument(
        "--status",
        choices=("draft", "ready"),
        default="ready",
        help="draft composes reviewed Phase-2 reference drafts; ready (default) composes sealed published packages.",
    )
    parser.add_argument("--created-at", required=True, help="Explicit RFC 3339 timestamp.")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _ensure_output(output: Path) -> Path:
    output = output.expanduser().resolve()
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise CompositionError(f"output_not_empty: {output}")
    else:
        output.mkdir(parents=True)
    return output


def _skill_markdown(name: str, display_name: str, *, draft: bool = False) -> str:
    if draft:
        return f"""---
name: {name}
description: Use the composed, source-grounded {display_name} to retrieve traceable technical knowledge across reviewed reference modules while preserving module attribution, conflicts, gaps, and capability boundaries. Use for bounded cross-module explanation and evidence tracing; do not use it for decision certification or executable procedures.
---

# {display_name}

Use this composite Skill as a router over reviewed child reference drafts. Never
rewrite multiple module claims into a source-free consensus.

## Runtime workflow

1. Run `python3 scripts/query_composite.py --query "<request>"` or inspect
   `references/runtime/catalog.json` directly.
2. Read only the returned object paths under `modules/` and their evidence anchors.
3. Attribute every claim to its `package_id`, object ID, and evidence IDs.
4. If multiple modules answer, present their claims separately. Do not silently
   reconcile notation, assumptions, applicability, or contradictions.
5. Preserve every returned conflict and gap. State when matching external source
   PDFs are required for full source verification.
6. Abstain from decision certification and execution. This composite draft
   exposes reviewed reference knowledge only.

## Integrity boundary

`composition-lock.json` binds the exact child manifest hashes. Each child package
is a reviewed reference draft; this composite is not yet sealed and must not be
shared as a published Skill. Seal it (`seal_expert_skill.py --mark-ready`) only
after its children are sealed and ready.
"""

    return f"""---
name: {name}
description: Use the composed, source-grounded {display_name} to retrieve traceable technical knowledge across immutable reference modules while preserving module attribution, conflicts, gaps, and capability boundaries. Use for bounded cross-module explanation and evidence tracing; do not use it for decision certification or executable procedures.
---

# {display_name}

Use this composite Skill as a router over sealed child Expert Skills. Never rewrite
multiple module claims into a source-free consensus.

## Runtime workflow

1. Run `python3 scripts/query_composite.py --query "<request>"` or inspect
   `references/runtime/catalog.json` directly.
2. Read only the returned object paths under `modules/` and their evidence anchors.
3. Attribute every claim to its `package_id`, object ID, and evidence IDs.
4. If multiple modules answer, present their claims separately. Do not silently
   reconcile notation, assumptions, applicability, or contradictions.
5. Preserve every returned conflict and gap. State when matching external source
   PDFs are required for full source verification.
6. Abstain from decision certification and execution. This Phase 5 composition
   exposes reference knowledge only.

## Integrity boundary

`composition-lock.json` binds the exact child manifest and integrity hashes. Each
child package remains independently sealed, and the top-level seal covers the full
composite tree. Host certifications for a child do not automatically certify this
composite or another host adapter.
"""


def compose(args: argparse.Namespace) -> dict[str, object]:
    if not SLUG_RE.fullmatch(args.name):
        raise CompositionError("name_invalid")
    if not SLUG_RE.fullmatch(args.domain):
        raise CompositionError("domain_invalid")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", args.version):
        raise CompositionError("version_invalid")
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
        args.created_at,
    ):
        raise CompositionError("created_at_invalid")
    try:
        parsed_created_at = dt.datetime.fromisoformat(args.created_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise CompositionError("created_at_invalid") from error
    if parsed_created_at.tzinfo is None:
        raise CompositionError("created_at_invalid")
    stage = getattr(args, "status", "ready")
    bindings = validate_module_inputs(
        args.package, level="structure" if stage == "draft" else "publish"
    )
    requested_output = args.output.expanduser().resolve()
    for package in args.package:
        package_root = package.expanduser().resolve()
        try:
            requested_output.relative_to(package_root)
        except ValueError:
            continue
        raise CompositionError(f"output_inside_input: {package_root}")
    output = _ensure_output(args.output)
    for binding, source in zip(bindings, sorted((path.expanduser().resolve() for path in args.package), key=lambda path: json.loads((path / "manifest.json").read_text(encoding="utf-8"))["package"]["id"])):
        target = output / binding["relative_path"]
        shutil.copytree(source, target)
    fingerprint = input_fingerprint(bindings)
    lock = {
        "schema_version": LOCK_SCHEMA,
        "compiler_version": COMPOSITE_COMPILER_VERSION,
        "created_at": args.created_at,
        "input_fingerprint": fingerprint,
        "policy": {
            "cross_module_claims": "preserve-separate-no-consensus",
            "source_overlap": "reject-unreviewed",
            "capability_floor": "reference-only",
        },
        "modules": bindings,
    }
    write_json(output / "composition-lock.json", lock)
    catalog = build_composite_catalog(output, bindings)
    write_json(output / "references/runtime/catalog.json", catalog)
    runtime_source = Path(__file__).with_name("portable_composite_runtime.py")
    runtime_target = output / "scripts/query_composite.py"
    runtime_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(runtime_source, runtime_target)
    (output / "SKILL.md").write_text(
        _skill_markdown(args.name, args.display_name, draft=(stage == "draft")),
        encoding="utf-8",
    )
    (output / "agents").mkdir(parents=True, exist_ok=True)
    (output / "agents/openai.yaml").write_text(
        "interface:\n"
        f"  display_name: {json.dumps(args.display_name)}\n"
        f"  short_description: {json.dumps('Traceable multi-module technical reference')}\n"
        f"  default_prompt: {json.dumps(f'Use ${args.name} to answer with module, object, and evidence attribution.')}\n",
        encoding="utf-8",
    )
    source_registry: dict[str, dict[str, object]] = {}
    for binding in bindings:
        for source in binding["sources"]:
            if not isinstance(source, dict) or not isinstance(source.get("source_id"), str):
                continue
            source_id = source["source_id"]
            record = source_registry.setdefault(
                source_id,
                {
                    "source_id": source_id,
                    "sha256": source.get("sha256"),
                    "media_type": source.get("media_type"),
                    "original_filename": source.get("original_filename"),
                    "required_by_packages": [],
                },
            )
            record["required_by_packages"].append(binding["package_id"])
    for record in source_registry.values():
        record["required_by_packages"] = sorted(set(record["required_by_packages"]))
    manifest = {
        "schema_version": COMPOSITE_SCHEMA,
        "package": {
            "id": args.name,
            "name": args.display_name,
            "version": args.version,
            "domain": args.domain,
            "status": stage,
        },
        "distribution": {
            "source_mode": "source-required",
            "verification": "external-source-required",
        },
        "sources": sorted(source_registry.values(), key=lambda row: row["source_id"]),
        "capabilities": REFERENCE_CAPABILITIES,
        "policy": {
            "cross_module_claims": "preserve-separate-no-consensus",
            "source_overlap": "reject-unreviewed",
        },
        "build": {
            "compiler": "fake-expert",
            "compiler_version": COMPOSITE_COMPILER_VERSION,
            "created_at": args.created_at,
            "input_fingerprint": fingerprint,
            "composition_lock_sha256": sha256_file(output / "composition-lock.json"),
        },
    }
    write_json(output / "manifest.json", manifest)
    manifest["integrity"] = {
        "algorithm": "sha256",
        "sealed_at": args.created_at,
        "files": composite_tracked_files(output),
    }
    write_json(output / "manifest.json", manifest)
    errors = [
        issue
        for issue in validate_composite_skill(
            output, level="structure" if stage == "draft" else "publish"
        )
        if issue.severity == "error"
    ]
    if errors:
        raise CompositionError(f"composite_validation_failed: {errors[0].code}:{errors[0].path}")
    return {
        "status": stage,
        "package": str(output),
        "package_id": args.name,
        "package_version": args.version,
        "module_package_count": len(bindings),
        "knowledge_object_count": len(catalog["objects"]),
        "conflict_count": len(catalog["conflicts"]),
        "gap_count": len(catalog["gaps"]),
        "input_fingerprint": fingerprint,
    }


def main() -> int:
    args = parse_args()
    try:
        result = compose(args)
    except (CompositionError, OSError, json.JSONDecodeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
