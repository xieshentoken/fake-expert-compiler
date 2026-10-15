"""Shared paths and helpers for the fake-expert test suite.

Run from the repository root:

    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v

Requirements: ``pypdf==6.10.0`` importable.  Tests that render pages need a
``pdftoppm`` binary on PATH (poppler) or ``FAKE_EXPERT_TEST_PDFTOPPM`` pointing
at one; they are skipped otherwise.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "fake-expert" / "scripts"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
HEAT_BOOK = FIXTURES / "heat-book.pdf"
LEGACY_PACKAGE = FIXTURES / "legacy-fake-heat-ref-1.1.0"
FAKE_MODEL = FIXTURES / "fake_model.py"
TOY_EMBED = FIXTURES / "toy_embed.py"
CREATED_AT = "2026-10-14T19:00:00+00:00"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
sys.dont_write_bytecode = True


def pdftoppm() -> str | None:
    explicit = os.environ.get("FAKE_EXPERT_TEST_PDFTOPPM")
    if explicit:
        return explicit if Path(explicit).is_file() else None
    return shutil.which("pdftoppm")


requires_renderer = unittest.skipUnless(pdftoppm(), "pdftoppm not available (set FAKE_EXPERT_TEST_PDFTOPPM)")


def cli_env() -> dict[str, str]:
    """Environment for CLI subprocesses: renderer on PATH, no bytecode, same pypdf."""

    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    renderer = pdftoppm()
    if renderer:
        env["PATH"] = str(Path(renderer).parent) + os.pathsep + env.get("PATH", "")
    paths = [entry for entry in sys.path if entry and Path(entry).is_dir() and (Path(entry) / "pypdf").is_dir()]
    if paths:
        env["PYTHONPATH"] = os.pathsep.join(paths + [env.get("PYTHONPATH", "")]).strip(os.pathsep)
    return env


def run_cli(script: str, *args: str, check: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True,
        text=True,
        env=cli_env(),
        cwd=str(cwd) if cwd else None,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(f"{script} {' '.join(args)} failed ({completed.returncode}):\n{completed.stderr}\n{completed.stdout}")
    return completed


def run_json(script: str, *args: str) -> dict:
    return json.loads(run_cli(script, *args).stdout)


def query_package(package: Path, query: str, *extra: str) -> dict:
    completed = subprocess.run(
        [sys.executable, "-B", str(package / "scripts/query_reference.py"), "--query", query, *extra],
        capture_output=True,
        text=True,
        env=cli_env(),
        check=True,
    )
    return json.loads(completed.stdout)


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fake-expert-test-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()
