#!/usr/bin/env python3
"""Quick, offline validator for the Phase 7D.5B candidate benchmark layer."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from incremental_build_dag import validate_dag_directory
from visual_benchmark import (
    VisualBenchmarkError,
    load_phase7d5a_workpack,
    plan_benchmark,
    resume_benchmark,
    validate_qualification_gate,
)


def validate(repo_root: Path, workpack: Path) -> dict[str, object]:
    """Replay the workpack and a disposable plan/status/resume job."""

    root = Path(repo_root).expanduser().resolve()
    workpack = Path(workpack).expanduser().resolve()
    facts = load_phase7d5a_workpack(workpack)
    if len(facts["candidate_ids"]) != 31:
        raise VisualBenchmarkError("quick_validator_candidate_count_invalid")
    qualification = validate_qualification_gate(facts)
    with tempfile.TemporaryDirectory(prefix="phase7d5b-quick-validator-") as temporary:
        job = Path(temporary) / "job"
        planned = plan_benchmark(workpack, job, created_at="2026-08-24T00:00:00+00:00")
        first = planned["status"]
        before = {str(path.relative_to(job)): path.read_bytes() for path in job.rglob("*") if path.is_file()}
        resumed = resume_benchmark(job)
        after = {str(path.relative_to(job)): path.read_bytes() for path in job.rglob("*") if path.is_file()}
        if first["state"] != "paused-independent-gold" or resumed["state"] != first["state"]:
            raise VisualBenchmarkError("quick_validator_pause_state_invalid")
        if before != after:
            raise VisualBenchmarkError("quick_validator_resume_not_idempotent")
        dag_path = job / first["dag"]["revision_locator"]
        if validate_dag_directory(dag_path):
            raise VisualBenchmarkError("quick_validator_dag_invalid")
        plan_text = (job / "benchmark-plan.json").read_text(encoding="utf-8")
        if any(token in plan_text for token in ("/Users/", ".pdf", ".png", "http://", "https://")):
            raise VisualBenchmarkError("quick_validator_plan_privacy_leak")
    return {
        "status": "verified",
        "candidate_item_count": len(facts["candidate_ids"]),
        "calibration_count": len(facts["calibration_ids"]),
        "test_count": len(facts["test_ids"]),
        "benchmark_state": "paused-independent-gold",
        "qualification_state": qualification["status"],
        "mcp_used": False,
        "network_used": False,
        "model_invocations": 0,
        "release_included": False,
        "repo_root": str(root),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--workpack", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = validate(args.repo_root, args.workpack)
    except (VisualBenchmarkError, OSError, ValueError) as error:
        print(json.dumps({"status": "rejected", "error": str(error)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
