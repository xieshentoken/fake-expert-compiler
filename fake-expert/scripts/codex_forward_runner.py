#!/usr/bin/env python3
"""Run leak-free host challenges in fresh non-interactive Codex CLI sessions."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from expert_skill_contract import load_jsonl
from host_forward_test import (
    HostForwardTestError,
    build_run_record,
    format_agent_prompt,
    validate_challenges,
    write_jsonl,
)


def codex_command(
    executable: str,
    package: Path,
    output_schema: Path,
    last_message: Path,
    prompt: str,
    *,
    model: str | None = None,
) -> list[str]:
    command = [
        executable,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--json",
        "--color",
        "never",
        "--sandbox",
        "read-only",
        "--output-schema",
        str(output_schema),
        "--output-last-message",
        str(last_message),
        "--cd",
        str(package),
    ]
    if model:
        command.extend(["--model", model])
    command.append(prompt)
    return command


def _thread_id(transcript: str) -> str | None:
    for line in transcript.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        if value.get("type") in {"thread.started", "thread_started"}:
            candidate = value.get("thread_id") or value.get("thread", {}).get("id")
            if isinstance(candidate, str) and candidate:
                return candidate
    return None


def run_codex_challenges(
    package: Path,
    challenges_path: Path,
    output: Path,
    *,
    attempts: int,
    model_id: str,
    executable: str = "codex",
    timeout_seconds: int = 300,
) -> list[dict[str, Any]]:
    package = package.expanduser().resolve()
    challenges = validate_challenges(load_jsonl(challenges_path))
    if attempts < 1:
        raise HostForwardTestError("attempt_count_invalid")
    if not model_id:
        raise HostForwardTestError("model_id_missing")
    resolved_executable = shutil.which(executable)
    if resolved_executable is None:
        raise HostForwardTestError(f"codex_executable_missing: {executable}")
    completed_version = subprocess.run(
        [resolved_executable, "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed_version.returncode != 0:
        raise HostForwardTestError("codex_version_unavailable")
    host_version = completed_version.stdout.strip() or completed_version.stderr.strip()
    output = output.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise HostForwardTestError(f"output_not_empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    transcripts = output / "transcripts"
    transcripts.mkdir()
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "assets/schemas/host-response.schema.json"
    )
    if not schema_path.is_file():
        raise HostForwardTestError("host_response_schema_missing")

    rows: list[dict[str, Any]] = []
    for challenge in challenges:
        for attempt in range(1, attempts + 1):
            stem = f"{challenge['id']}-attempt-{attempt}"
            last_message = output / f"{stem}.response.json"
            prompt = format_agent_prompt(package, challenge)
            completed = subprocess.run(
                codex_command(
                    resolved_executable,
                    package,
                    schema_path,
                    last_message,
                    prompt,
                    model=model_id,
                ),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            transcript = completed.stdout
            if completed.stderr:
                transcript += ("\n" if transcript else "") + completed.stderr
            transcript_path = transcripts / f"{stem}.jsonl"
            transcript_path.write_text(transcript, encoding="utf-8")
            if last_message.is_file():
                raw_response = last_message.read_text(encoding="utf-8")
            else:
                raw_response = (
                    "HOST_RUNNER_ERROR: codex exit "
                    f"{completed.returncode}; final response was not written"
                )
            agent_instance = _thread_id(completed.stdout) or f"codex-cli-unreported-{stem}"
            rows.append(
                build_run_record(
                    package,
                    challenge,
                    attempt=attempt,
                    agent_instance=agent_instance,
                    host_id="codex-cli",
                    host_version=host_version,
                    model_id=model_id,
                    raw_response=raw_response,
                    raw_transcript=transcript,
                )
            )
    write_jsonl(output / "runs.jsonl", rows)
    write_jsonl(output / "challenges.jsonl", challenges)
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--challenges", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        rows = run_codex_challenges(
            args.package,
            args.challenges,
            args.output,
            attempts=args.attempts,
            model_id=args.model_id,
            executable=args.codex,
            timeout_seconds=args.timeout_seconds,
        )
        parsed = sum(row.get("parsed_response") is not None for row in rows)
        print(f"completed {len(rows)} runs; structured responses={parsed}")
        return 0 if parsed == len(rows) else 1
    except (HostForwardTestError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
