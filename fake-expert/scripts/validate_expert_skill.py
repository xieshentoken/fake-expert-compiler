#!/usr/bin/env python3
"""Validate a generated Expert Skill at structure or publication level."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from expert_skill_contract import issue_summary, validate_expert_skill


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="Expert Skill directory.")
    parser.add_argument("--level", choices=("structure", "publish"), default="structure")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    issues = validate_expert_skill(args.package, level=args.level)
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
        state = "PASS" if payload["passed"] else "FAIL"
        print(f"{state} [{args.level}] {payload['package']}")
        for issue in issues:
            print(f"{issue.severity.upper()} {issue.code} {issue.path}: {issue.message}")
        print(f"errors={summary['errors']} warnings={summary['warnings']}")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

