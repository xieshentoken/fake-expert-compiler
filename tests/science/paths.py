"""Portable source locations; private preservation evidence is explicit input."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
_candidates = [ROOT / "skills/fake-expert", ROOT / "fake-expert"]
_found = [p for p in _candidates if (p / "scripts/compiler_version.py").is_file()]
if len(_found) != 1:
    raise RuntimeError("science_test_source_layout_ambiguous_or_missing")
SKILL_ROOT = _found[0]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
