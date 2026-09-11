#!/usr/bin/env python3
"""Run one sealed fake-expert formula module in a constrained child process."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe(root: Path, relative: object) -> Path | None:
    if not isinstance(relative, str):
        return None
    value = PurePosixPath(relative)
    if value.is_absolute() or not value.parts or ".." in value.parts:
        return None
    resolved = (root / Path(*value.parts)).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return None
    return resolved


def _limits(timeout_seconds: int):
    def apply() -> None:
        for limit, values in (
            (resource.RLIMIT_CPU, (timeout_seconds, timeout_seconds + 1)),
            (resource.RLIMIT_FSIZE, (0, 0)),
            (resource.RLIMIT_CORE, (0, 0)),
            (resource.RLIMIT_NOFILE, (32, 32)),
        ):
            try:
                resource.setrlimit(limit, values)
            except (ValueError, OSError):
                pass
        if hasattr(resource, "RLIMIT_NPROC"):
            try:
                resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
            except (ValueError, OSError):
                pass
    return apply


def _load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json_object_invalid")
    return value


def _validate_seal(root: Path) -> None:
    manifest = _load_json(root / "manifest.json")
    if (
        manifest.get("package", {}).get("status") != "ready"
        or manifest.get("capabilities") != {"reference": True, "decision_support": False, "executable": True}
        or manifest.get("build", {}).get("compiler") != "fake-expert"
        or manifest.get("build", {}).get("compiler_version") != "0.4.0-execution"
    ):
        raise ValueError("execution_package_not_ready")
    integrity = manifest.get("integrity")
    files = integrity.get("files") if isinstance(integrity, dict) else None
    if not isinstance(files, dict) or integrity.get("algorithm") != "sha256":
        raise ValueError("execution_package_unsealed")
    for relative, expected in files.items():
        path = _safe(root, relative)
        if path is None or not path.is_file() or _sha256(path) != expected:
            raise ValueError("execution_integrity_mismatch")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formula", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        request = sys.stdin.buffer.read(65537)
        if len(request) > 65536:
            raise ValueError("execution_request_too_large")
        parsed = json.loads(request.decode("utf-8"))
        if not isinstance(parsed, dict) or set(parsed) != {"inputs"}:
            raise ValueError("execution_request_invalid")
        _validate_seal(root)
        bundle = _load_json(root / "references/execution/manifest.json")
        policy = _load_json(root / "references/execution/policy.json")
        if policy.get("isolation") != "process-resource-limits-not-kernel-sandbox":
            raise ValueError("execution_policy_invalid")
        timeout = policy.get("timeout_seconds")
        if not isinstance(timeout, int) or not 1 <= timeout <= 10:
            raise ValueError("execution_policy_invalid")
        selected = next(
            (row for row in bundle.get("formulas", []) if isinstance(row, dict) and row.get("object_id") == args.formula),
            None,
        )
        if not isinstance(selected, dict):
            raise ValueError("execution_formula_unknown")
        module = _safe(root, selected.get("entrypoint"))
        if module is None or not module.is_file() or _sha256(module) != selected.get("module_sha256"):
            raise ValueError("execution_module_invalid")
        with tempfile.TemporaryDirectory(prefix="fake-expert-run-") as sandbox:
            completed = subprocess.run(
                [sys.executable, "-I", "-S", str(module)],
                input=json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                text=True,
                capture_output=True,
                cwd=sandbox,
                env={"HOME": sandbox, "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"},
                timeout=timeout,
                check=False,
                preexec_fn=_limits(timeout) if os.name == "posix" else None,
            )
        if completed.returncode != 0:
            raise ValueError("execution_process_failed")
        response = json.loads(completed.stdout)
        if not isinstance(response, dict) or response.get("status") != "ok":
            raise ValueError("execution_response_invalid")
        print(json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"status": "error", "code": str(error)}, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
