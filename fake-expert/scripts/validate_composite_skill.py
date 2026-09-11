#!/usr/bin/env python3
"""Validate an immutable-module composite Expert Skill."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from composite_contract import validate_composite_skill
from expert_skill_contract import issue_summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--level", choices=("structure", "publish"), default="structure")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()
    issues = validate_composite_skill(args.package, level=args.level)
    summary = issue_summary(issues)
    payload = {
        "package": str(args.package.expanduser().resolve()),
        "level": args.level,
        "passed": summary["errors"] == 0,
        "summary": summary,
        "issues": [issue.to_dict() for issue in issues],
    }
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"{'PASS' if payload['passed'] else 'FAIL'} [{args.level}] {payload['package']}")
        for issue in issues:
            print(f"{issue.severity.upper()} {issue.code} {issue.path}: {issue.message}")
        print(f"errors={summary['errors']} warnings={summary['warnings']}")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

