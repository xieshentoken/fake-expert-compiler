#!/usr/bin/env python3
"""Query a direct Reference Draft while preserving its candidate assurance."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from portable_reference_runtime import RESPONSE_SCHEMA, RUNTIME_VERSION, query_catalog


DECISION_GUARD_SUBSTRINGS = (
    "批准",
    "执行",
    "决策",
    "认证",
    "安全设计",
    "推荐",
)


def query_direct_catalog(
    catalog: dict[str, Any], query: str, *, limit: int = 8
) -> dict[str, Any]:
    """Return the legacy runtime result plus the bound candidate trust state."""

    policy = catalog.get("policy") if isinstance(catalog.get("policy"), dict) else {}
    assurance = policy.get("assurance")
    if not isinstance(assurance, dict):
        raise ValueError("direct_reference_assurance_missing")
    normalized_query = query.casefold()
    if any(term in normalized_query for term in DECISION_GUARD_SUBSTRINGS):
        response = {
            "schema_version": RESPONSE_SCHEMA,
            "runtime_version": RUNTIME_VERSION,
            "behavior": "abstain",
            "reason_codes": ["decision_capability_unavailable"],
            "matched_object_ids": [],
            "evidence_ids": [],
            "conflict_ids": [],
            "gap_ids": [],
        }
    else:
        response = query_catalog(catalog, query, limit=limit)
    result = dict(response)
    result["package_id"] = catalog.get("package_id")
    result["module_id"] = catalog.get("module_id")
    result["assurance"] = assurance
    warnings = assurance.get("warnings")
    result["warnings"] = list(warnings) if isinstance(warnings, list) else []
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument(
        "--catalog",
        type=Path,
        help="Defaults to references/runtime/catalog.json relative to the Skill root.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    script_root = Path(__file__).resolve().parent
    catalog_path = args.catalog or script_root.parent / "references/runtime/catalog.json"
    try:
        with catalog_path.open("r", encoding="utf-8") as handle:
            catalog = json.load(handle)
        response = query_direct_catalog(catalog, args.query, limit=args.limit)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
