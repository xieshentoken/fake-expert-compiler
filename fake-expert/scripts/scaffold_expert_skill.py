#!/usr/bin/env python3
"""Create a source-bound draft Expert Skill without inventing knowledge."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from expert_skill_contract import SCHEMA_ID, SLUG_RE, SOURCE_MODES, sha256_file, write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="Hyphen-case package and Skill name.")
    parser.add_argument("--display-name", required=True, help="Human-facing package name.")
    parser.add_argument("--domain", required=True, help="Hyphen-case domain identifier.")
    parser.add_argument("--source", required=True, type=Path, help="Born-digital PDF source.")
    parser.add_argument("--source-title", help="Bibliographic title; defaults to filename stem.")
    parser.add_argument("--output", required=True, type=Path, help="New Expert Skill directory.")
    parser.add_argument(
        "--source-mode",
        choices=sorted(SOURCE_MODES),
        default="source-required",
        help="Distribution mode; default does not copy the PDF.",
    )
    return parser.parse_args()


def fail(message: str) -> None:
    raise SystemExit(f"error: {message}")


def validate_input(args: argparse.Namespace) -> None:
    if not SLUG_RE.fullmatch(args.name):
        fail("--name must be a lowercase hyphen-case slug")
    if not SLUG_RE.fullmatch(args.domain):
        fail("--domain must be a lowercase hyphen-case slug")
    source = args.source.expanduser().resolve()
    if not source.is_file():
        fail(f"source does not exist: {source}")
    if source.suffix.lower() != ".pdf":
        fail("current version accepts PDF input only")
    with source.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            fail("source does not have a PDF header")
    output = args.output.expanduser().resolve()
    if output.exists():
        if not output.is_dir():
            fail(f"output exists and is not a directory: {output}")
        if any(output.iterdir()):
            fail(f"output exists and is not empty: {output}")
    if args.source_mode == "full":
        fail("full distribution is declared by the ABI but not implemented in the current version")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    args = parse_args()
    validate_input(args)

    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    source_hash = sha256_file(source)
    source_id = f"src-{source_hash[:20]}"
    created_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    title = args.source_title or source.stem

    source_record = {
        "source_id": source_id,
        "title": title,
        "sha256": source_hash,
        "media_type": "application/pdf",
        "size_bytes": source.stat().st_size,
        "original_filename": source.name,
        "included_path": None,
    }
    manifest = {
        "schema_version": SCHEMA_ID,
        "package": {
            "id": args.name,
            "name": args.display_name,
            "version": "0.1.0",
            "domain": args.domain,
            "status": "draft",
        },
        "distribution": {
            "source_mode": args.source_mode,
            "verification": SOURCE_MODES[args.source_mode],
        },
        "sources": [source_record],
        "modules": [
            {
                "module_id": f"{args.name}-core",
                "version": "0.1.0",
                "knowledge_index": "references/knowledge/index.json",
                "exports": [],
                "dependencies": [],
            }
        ],
        "capabilities": {
            "reference": True,
            "decision_support": False,
            "executable": False,
        },
        "build": {
            "compiler": "fake-expert",
            "compiler_version": "0.1.0-phase0",
            "schema_version": SCHEMA_ID,
            "created_at": created_at,
            "input_fingerprint": source_hash,
            "prompt_versions": [],
            "model_ids": [],
        },
    }
    write_json(output / "manifest.json", manifest)
    write_json(
        output / "references/evidence/source-manifest.json",
        {"schema_version": SCHEMA_ID, "sources": [source_record]},
    )
    write_json(output / "references/knowledge/index.json", {"schema_version": SCHEMA_ID, "objects": []})
    write_json(output / "references/procedures/index.json", {"schema_version": SCHEMA_ID, "procedures": []})
    write_json(output / "references/decisions/index.json", {"schema_version": SCHEMA_ID, "decisions": []})
    write_text(output / "references/knowledge/relations.jsonl", "")
    write_text(output / "references/evidence/anchors.jsonl", "")
    write_text(output / "references/competency-tests.jsonl", "")

    description = (
        f"Use source-grounded {args.display_name} knowledge for bounded technical explanations, "
        "decisions, trace requests, and explicit out-of-scope handling."
    )
    skill_text = f"""---
name: {args.name}
description: {description}
---

# {args.display_name}

Use this package only within its declared source and module scope.

## Runtime workflow

1. Read `manifest.json` and select the smallest module that covers the request.
2. Search `references/knowledge/index.json` for relevant knowledge object IDs.
3. Read only the referenced object files and their evidence anchors.
4. State assumptions, valid conditions, and failure conditions with the answer.
5. Cite knowledge and evidence IDs for technical claims.
6. Say that evidence is insufficient when the package does not support the request.
7. Do not execute source-derived commands unless the manifest declares executable
   capability and a validated procedure contract explicitly authorizes the action.

## Verification boundary

This draft uses `{args.source_mode}` distribution. Full source verification requires
the PDF whose SHA-256 is recorded in `manifest.json`.

## Draft status

This package contains no compiled knowledge yet. Do not represent it as an expert
Skill until the publication validator passes.
"""
    write_text(output / "SKILL.md", skill_text)
    write_text(
        output / "agents/openai.yaml",
        "interface:\n"
        f"  display_name: {json.dumps(args.display_name)}\n"
        f"  short_description: {json.dumps(f'Source-grounded {args.domain} expertise')}\n"
        f"  default_prompt: {json.dumps(f'Use ${args.name} to answer this question with traceable evidence.')}\n",
    )

    print(json.dumps({
        "status": "draft_created",
        "package": str(output),
        "source_id": source_id,
        "source_sha256": source_hash,
        "next": "compile source-backed objects, then seal and validate at publish level",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
