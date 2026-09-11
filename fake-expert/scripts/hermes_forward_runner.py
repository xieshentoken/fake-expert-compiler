#!/usr/bin/env python3
"""Run sealed-Skill challenges through fresh, isolated Hermes one-shot sessions."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import Any

from expert_skill_contract import (
    load_json,
    load_jsonl,
    sha256_file,
    tracked_files,
    write_json,
)
from host_forward_test import (
    HostForwardTestError,
    build_run_record,
    format_agent_prompt,
    sha256_text,
    validate_challenges,
    write_jsonl,
)


HERMES_HOST_ID = "hermes-agent"
DEEPSEEK_PROVIDER = "deepseek"
ALLOWED_FILE_TOOLS = {"read_file", "search_files"}
FORBIDDEN_TRANSCRIPT_MARKERS = (
    "MEMORY (your personal notes)",
    "USER PROFILE (who the user is)",
)
SECRET_NAME_RE = re.compile(r"(?:api[_-]?key|token|secret|password)", re.IGNORECASE)
SAFE_ENV_NAMES = {
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TZ",
}


def hermes_command(
    executable: str,
    prompt: str,
    usage_file: Path,
    *,
    model_id: str,
    provider: str,
) -> list[str]:
    """Return the explicit, cold-session Hermes invocation."""

    return [
        executable,
        "-z",
        prompt,
        "--model",
        model_id,
        "--provider",
        provider,
        "--toolsets",
        "file",
        "--safe-mode",
        "--ignore-user-config",
        "--ignore-rules",
        "--usage-file",
        str(usage_file),
    ]


def format_hermes_agent_prompt(package: Path, challenge: dict[str, Any]) -> str:
    """Adapt the host-neutral challenge to Hermes' portable-package boundary."""

    generic = format_agent_prompt(package, challenge, package_locator="package")
    _, remainder = generic.split("\n", 1)
    return f"""A portable Skill package is already mounted at `package`; it is not installed in Hermes HOME or any profile. Do not search for the Skill. Your first file-tool action must read `package/SKILL.md`.

Every file-tool path must be exactly `package` or begin with `package/`. Never request `.`, `..`, an absolute path, HOME, profiles, `/usr`, `/opt`, or any parent directory. This adapter intentionally exposes only file tools: do not search for Python, shells, or executables. Follow the package workflow by inspecting its script and the selected package records directly.

{remainder}"""


def _parse_env_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_provider_credential(path: Path, variable: str) -> str:
    """Load exactly one provider credential without copying the source env file."""

    if not variable or SECRET_NAME_RE.search(variable) is None:
        raise HostForwardTestError("credential_variable_invalid")
    try:
        lines = path.expanduser().read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HostForwardTestError(f"credential_file_unavailable: {path}") from exc
    values: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, raw = stripped.split("=", 1)
        if name.strip() == variable:
            value = _parse_env_value(raw)
            if value:
                values.append(value)
    if len(values) != 1:
        raise HostForwardTestError(f"credential_variable_unresolved: {variable}")
    return values[0]


def isolated_hermes_env(home: Path, credential_variable: str, credential: str) -> dict[str, str]:
    """Build an environment that carries one credential into an empty Hermes home."""

    env = {name: os.environ[name] for name in SAFE_ENV_NAMES if name in os.environ}
    env.update(
        {
            "HOME": str(home),
            "HERMES_HOME": str(home),
            "HERMES_SAFE_MODE": "1",
            "HERMES_IGNORE_USER_CONFIG": "1",
            "HERMES_IGNORE_RULES": "1",
            "TMPDIR": str(home),
            credential_variable: credential,
        }
    )
    return env


def _tool_calls(transcript: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    for message in transcript.get("messages", []):
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            arguments = function.get("arguments")
            if not isinstance(name, str):
                continue
            try:
                parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
            except json.JSONDecodeError:
                parsed = None
            calls.append((name, parsed if isinstance(parsed, dict) else {}))
    return calls


def _package_relative_path(value: str, package_root: Path) -> str | None:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute():
        try:
            relative = Path(value).expanduser().resolve().relative_to(package_root.resolve())
        except (OSError, ValueError):
            return None
        return str(PurePosixPath("package", *relative.parts))
    if ".." in path.parts or not path.parts or path.parts[0] != "package":
        return None
    return str(path)


def seatbelt_profile(
    *,
    package_root: Path,
    hermes_home: Path,
    control_root: Path,
    launcher: Path,
    install_root: Path,
    interpreter_root: Path,
) -> str:
    """Build a macOS profile that cannot read user data outside the staged package."""

    def quote(path: Path | str) -> str:
        return str(path).replace("\\", "\\\\").replace('"', '\\"')

    # sandbox-exec compares canonical filesystem paths. macOS commonly exposes
    # temporary directories through /var even though their canonical path uses a
    # private system prefix; allowing only the unresolved spelling keeps the
    # process cwd available without broad user-data access.
    package_root = package_root.resolve()
    hermes_home = hermes_home.resolve()
    control_root = control_root.resolve()
    launcher = launcher.resolve()
    install_root = install_root.resolve()
    interpreter_root = interpreter_root.resolve()

    read_roots = [
        "/System",
        "/usr",
        "/bin",
        "/sbin",
        "/Library",
        "/private/etc",
        Path("/private") / "var" / "db" / "timezone",
        Path("/opt") / "homebrew",
        launcher,
        install_root,
        interpreter_root,
        package_root,
        hermes_home,
        control_root,
    ]
    write_roots = ["/dev", hermes_home, control_root]
    metadata_paths: set[str] = set()
    for value in (
        package_root,
        hermes_home,
        control_root,
        launcher,
        install_root,
        interpreter_root,
    ):
        path = Path(value)
        metadata_paths.add(str(path))
        metadata_paths.update(str(parent) for parent in path.parents)
    read_rules = "\n".join(f'  (subpath "{quote(path)}")' for path in read_roots)
    write_rules = "\n".join(f'  (subpath "{quote(path)}")' for path in write_roots)
    metadata_rules = "\n".join(
        f'  (literal "{quote(path)}")' for path in sorted(metadata_paths)
    )
    cwd_traversal = "\n".join(
        f'  (literal "{quote(path)}")'
        for path in sorted({str(parent) for parent in package_root.parents})
    )
    return f'''(version 1)
(deny default)
(import "system.sb")
(allow process*)
(allow network*)
(allow sysctl-read)
(allow mach-lookup)
(allow file-read-metadata
{metadata_rules})
(allow file-read*
{read_rules}
{cwd_traversal})
(allow file-write*
{write_rules})
'''


def sandboxed_command(profile: Path, command: list[str]) -> list[str]:
    return ["/usr/bin/sandbox-exec", "-f", str(profile), *command]


def audit_hermes_transcript(
    transcript_text: str,
    *,
    session_id: str,
    model_id: str,
    provider: str,
    credential: str,
    package_root: Path,
    expected_prompt: str,
) -> dict[str, Any]:
    """Fail closed when a supposedly cold Hermes run used ambient context or unsafe tools."""

    issues: list[str] = []
    try:
        rows = [json.loads(line) for line in transcript_text.splitlines() if line.strip()]
    except json.JSONDecodeError:
        rows = []
        issues.append("transcript_json_invalid")
    if len(rows) != 1 or not isinstance(rows[0], dict):
        transcript: dict[str, Any] = {}
        issues.append("transcript_session_count_invalid")
    else:
        transcript = rows[0]
    system_prompt = transcript.get("system_prompt")
    if not isinstance(system_prompt, str):
        issues.append("system_prompt_missing")
        system_prompt = ""
    for marker in FORBIDDEN_TRANSCRIPT_MARKERS:
        if marker in system_prompt:
            issues.append("ambient_context_injected")
            break
    if transcript.get("id") != session_id:
        issues.append("session_binding_invalid")
    if transcript.get("model") != model_id:
        issues.append("model_binding_invalid")
    if transcript.get("billing_provider") != provider:
        issues.append("provider_binding_invalid")
    if credential and credential in transcript_text:
        issues.append("credential_leaked")
    user_messages = [
        message.get("content")
        for message in transcript.get("messages", [])
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    if user_messages != [expected_prompt]:
        issues.append("prompt_binding_invalid")

    calls = _tool_calls(transcript)
    messages = transcript.get("messages", []) if isinstance(transcript.get("messages"), list) else []
    result_by_call_id = {
        message.get("tool_call_id"): str(message.get("content") or "")
        for message in messages
        if isinstance(message, dict) and isinstance(message.get("tool_call_id"), str)
    }
    accessed: set[str] = set()
    unavailable_attempts: set[str] = set()
    for name, arguments in calls:
        matching_results = [
            content
            for message in messages
            if isinstance(message, dict)
            for call in (message.get("tool_calls") or [])
            if isinstance(call, dict)
            and isinstance(call.get("function"), dict)
            and call["function"].get("name") == name
            for content in [
                result_by_call_id.get(call.get("id"))
                or result_by_call_id.get(call.get("call_id"))
                or ""
            ]
        ]
        unavailable = name not in ALLOWED_FILE_TOOLS and matching_results and all(
            "does not exist" in content.casefold() for content in matching_results
        )
        if unavailable:
            unavailable_attempts.add(name)
        elif name not in ALLOWED_FILE_TOOLS:
            issues.append("unsafe_tool_used")
        for field in ("path", "directory"):
            value = arguments.get(field)
            if not isinstance(value, str):
                continue
            relative = _package_relative_path(value, package_root)
            if relative is None:
                issues.append("package_scope_escape")
            else:
                accessed.add(relative)
    checks = {
        "single_session": "transcript_session_count_invalid" not in issues,
        "session_bound": "session_binding_invalid" not in issues,
        "model_bound": "model_binding_invalid" not in issues,
        "provider_bound": "provider_binding_invalid" not in issues,
        "ambient_memory_absent": "ambient_context_injected" not in issues,
        "credential_absent": "credential_leaked" not in issues,
        "prompt_bound": "prompt_binding_invalid" not in issues,
        "read_only_file_tools": "unsafe_tool_used" not in issues,
        "package_scope_only": "package_scope_escape" not in issues,
    }
    return {
        "schema_version": "tkc.hermes-isolation-audit/v0.1",
        "session_id": session_id,
        "model_id": model_id,
        "provider": provider,
        "tool_call_count": len(calls),
        "tool_names": sorted({name for name, _ in calls}),
        "unavailable_tool_attempts": sorted(unavailable_attempts),
        "accessed_package_paths": sorted(accessed),
        "checks": checks,
        "issue_codes": sorted(set(issues)),
        "passed": not issues,
    }


def _export_transcript(
    executable: str,
    session_id: str,
    output: Path,
    *,
    cwd: Path,
    env: dict[str, str],
    profile: Path,
    timeout_seconds: int,
) -> str:
    completed = subprocess.run(
        sandboxed_command(profile, [
            executable,
            "sessions",
            "export",
            str(output),
            "--format",
            "jsonl",
            "--session-id",
            session_id,
            "--redact",
            "--yes",
        ]),
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0 or not output.is_file():
        raise HostForwardTestError(
            "hermes_transcript_export_failed: "
            + (completed.stderr.strip() or completed.stdout.strip() or str(completed.returncode))
        )
    return output.read_text(encoding="utf-8")


def _run_one(
    *,
    executable: str,
    canonical_package: Path,
    challenge: dict[str, Any],
    attempt: int,
    work_root: Path,
    artifacts_root: Path,
    credential_variable: str,
    credential: str,
    model_id: str,
    provider: str,
    host_version: str,
    install_root: Path,
    interpreter_root: Path,
    timeout_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    stem = f"{challenge['id']}-attempt-{attempt}"
    stage = Path(tempfile.mkdtemp(prefix=f"{stem}-", dir=work_root))
    cwd = stage / "cwd"
    package = cwd / "package"
    home = stage / "hermes-home"
    control = stage / "control"
    cwd.mkdir()
    home.mkdir()
    control.mkdir()
    shutil.copytree(canonical_package, package)
    before = tracked_files(package)
    if before != tracked_files(canonical_package):
        raise HostForwardTestError("temporary_package_copy_mismatch")
    if any(path.suffix.casefold() == ".pdf" for path in package.rglob("*")):
        raise HostForwardTestError("temporary_package_contains_pdf")

    usage_path = control / "usage.json"
    profile = control / "seatbelt.sb"
    profile.write_text(
        seatbelt_profile(
            package_root=package,
            hermes_home=home,
            control_root=control,
            launcher=Path(executable),
            install_root=install_root,
            interpreter_root=interpreter_root,
        ),
        encoding="utf-8",
    )
    prompt = format_hermes_agent_prompt(canonical_package, challenge)
    env = isolated_hermes_env(home, credential_variable, credential)
    completed = subprocess.run(
        sandboxed_command(profile, hermes_command(
            executable,
            prompt,
            usage_path,
            model_id=model_id,
            provider=provider,
        )),
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    raw_response = completed.stdout
    artifact_dir = artifacts_root / stem
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "response.json").write_text(raw_response, encoding="utf-8")
    (artifact_dir / "host-stderr.txt").write_text(completed.stderr, encoding="utf-8")
    write_json(
        artifact_dir / "host-process.json",
        {
            "schema_version": "tkc.host-process/v0.1",
            "challenge_id": challenge["id"],
            "attempt": attempt,
            "returncode": completed.returncode,
        },
    )
    if completed.returncode != 0 and not raw_response.strip():
        raw_response = (
            f"HOST_RUNNER_ERROR: hermes exit {completed.returncode}; "
            "final response was not written"
        )
        (artifact_dir / "response.json").write_text(raw_response, encoding="utf-8")
    if not usage_path.is_file():
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise HostForwardTestError(
            "hermes_usage_missing" + (f": {detail[:500]}" if detail else "")
        )
    usage = load_json(usage_path)
    session_id = usage.get("session_id")
    if (
        not isinstance(session_id, str)
        or not session_id
        or usage.get("model") != model_id
        or usage.get("provider") != provider
    ):
        raise HostForwardTestError("hermes_usage_binding_invalid")
    transcript_path = control / "transcript.jsonl"
    transcript = _export_transcript(
        executable,
        session_id,
        transcript_path,
        cwd=cwd,
        env=env,
        profile=profile,
        timeout_seconds=timeout_seconds,
    )
    after = tracked_files(package)
    package_unchanged = before == after
    audit = audit_hermes_transcript(
        transcript,
        session_id=session_id,
        model_id=model_id,
        provider=provider,
        credential=credential,
        package_root=package,
        expected_prompt=prompt,
    )
    audit["challenge_id"] = challenge["id"]
    audit["attempt"] = attempt
    audit["package_file_count"] = len(before)
    audit["adapter_prompt_sha256"] = sha256_text(prompt)
    audit["package_unchanged"] = package_unchanged
    audit["checks"]["package_unchanged"] = package_unchanged
    if not package_unchanged:
        audit["issue_codes"] = sorted(set(audit["issue_codes"] + ["temporary_package_modified"]))
        audit["passed"] = False
    (artifact_dir / "response.json").write_text(raw_response, encoding="utf-8")
    (artifact_dir / "transcript.jsonl").write_text(transcript, encoding="utf-8")
    write_json(artifact_dir / "usage.json", usage)
    write_json(artifact_dir / "isolation-audit.json", audit)
    if not audit["passed"]:
        raise HostForwardTestError(
            f"hermes_isolation_failed: {challenge['id']} attempt {attempt}: "
            + ",".join(audit["issue_codes"])
        )
    run = build_run_record(
        canonical_package,
        challenge,
        attempt=attempt,
        agent_instance=session_id,
        host_id=HERMES_HOST_ID,
        host_version=host_version,
        model_id=model_id,
        raw_response=raw_response,
        raw_transcript=transcript,
    )
    egress = {
        "schema_version": "tkc.hermes-egress-receipt/v0.1",
        "challenge_id": challenge["id"],
        "attempt": attempt,
        "session_id": session_id,
        "provider": provider,
        "model_id": model_id,
        "staged_package_file_count": len(before),
        "staged_package_manifest_sha256": sha256_file(package / "manifest.json"),
        "original_pdf_staged": False,
        "hidden_expectations_staged": False,
        "adapter_prompt_sha256": sha256_text(prompt),
        "actual_package_paths_transmitted": audit["accessed_package_paths"],
        "response_sha256": sha256_file(artifact_dir / "response.json"),
        "transcript_sha256": sha256_file(artifact_dir / "transcript.jsonl"),
        "usage_sha256": sha256_file(artifact_dir / "usage.json"),
        "isolation_audit_sha256": sha256_file(artifact_dir / "isolation-audit.json"),
    }
    shutil.rmtree(stage)
    return run, audit, egress


def run_hermes_challenges(
    package: Path,
    challenges_path: Path,
    output: Path,
    *,
    attempts: int,
    model_id: str,
    provider: str,
    credential_env_file: Path,
    credential_variable: str,
    executable: str = "hermes",
    timeout_seconds: int = 300,
    concurrency: int = 1,
) -> list[dict[str, Any]]:
    package = package.expanduser().resolve()
    challenges = validate_challenges(load_jsonl(challenges_path))
    if attempts < 1 or concurrency < 1:
        raise HostForwardTestError("run_count_invalid")
    if not model_id or not provider:
        raise HostForwardTestError("hermes_environment_missing")
    resolved = shutil.which(executable)
    if resolved is None:
        raise HostForwardTestError(f"hermes_executable_missing: {executable}")
    version = subprocess.run(
        [resolved, "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if version.returncode != 0:
        raise HostForwardTestError("hermes_version_unavailable")
    host_version = version.stdout.strip() or version.stderr.strip()
    credential = load_provider_credential(credential_env_file, credential_variable)
    output = output.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise HostForwardTestError(f"output_not_empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    artifacts_root = output / "artifacts"
    artifacts_root.mkdir()

    jobs = [
        (challenge, attempt)
        for challenge in challenges
        for attempt in range(1, attempts + 1)
    ]
    results: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    install_match = re.search(r"^Install directory:\s*(.+)$", host_version, re.MULTILINE)
    if install_match is None:
        raise HostForwardTestError("hermes_install_root_unavailable")
    install_root = Path(install_match.group(1).strip()).resolve()
    interpreter = (install_root / "venv/bin/python").resolve()
    interpreter_root = interpreter.parents[2]
    if not Path("/usr/bin/sandbox-exec").is_file():
        raise HostForwardTestError("macos_seatbelt_unavailable")

    with tempfile.TemporaryDirectory(prefix="tkc-phase4b-hermes-") as temporary_work:
        work_root = Path(temporary_work)
        with ThreadPoolExecutor(max_workers=min(concurrency, len(jobs))) as pool:
            futures = [
                pool.submit(
                    _run_one,
                    executable=resolved,
                    canonical_package=package,
                    challenge=challenge,
                    attempt=attempt,
                    work_root=work_root,
                    artifacts_root=artifacts_root,
                    credential_variable=credential_variable,
                    credential=credential,
                    model_id=model_id,
                    provider=provider,
                    host_version=host_version,
                    install_root=install_root,
                    interpreter_root=interpreter_root,
                    timeout_seconds=timeout_seconds,
                )
                for challenge, attempt in jobs
            ]
            try:
                for future in as_completed(futures):
                    results.append(future.result())
            except Exception:
                for future in futures:
                    future.cancel()
                raise
    runs = sorted((row[0] for row in results), key=lambda row: (row["challenge_id"], row["attempt"]))
    audits = sorted((row[1] for row in results), key=lambda row: (row["challenge_id"], row["attempt"]))
    egress = sorted((row[2] for row in results), key=lambda row: (row["challenge_id"], row["attempt"]))
    write_jsonl(output / "runs.jsonl", runs)
    write_jsonl(output / "challenges.jsonl", challenges)
    write_jsonl(output / "isolation-audits.jsonl", audits)
    write_jsonl(output / "egress-receipts.jsonl", egress)
    return runs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--challenges", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--provider", default=DEEPSEEK_PROVIDER)
    parser.add_argument("--credential-env-file", required=True, type=Path)
    parser.add_argument("--credential-variable", default="DEEPSEEK_API_KEY")
    parser.add_argument("--hermes", default="hermes")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=1)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        rows = run_hermes_challenges(
            args.package,
            args.challenges,
            args.output,
            attempts=args.attempts,
            model_id=args.model_id,
            provider=args.provider,
            credential_env_file=args.credential_env_file,
            credential_variable=args.credential_variable,
            executable=args.hermes,
            timeout_seconds=args.timeout_seconds,
            concurrency=args.concurrency,
        )
        parsed = sum(row.get("parsed_response") is not None for row in rows)
        print(f"completed {len(rows)} isolated Hermes runs; structured responses={parsed}")
        return 0 if parsed == len(rows) else 1
    except (HostForwardTestError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
