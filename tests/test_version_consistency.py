"""Spec: pyproject.toml's [project].version and agent_cost.__version__ must
agree, and both must read "0.5.3" for this change (the packaged catalog adds
claude-fable-5-2, claude-mythos-5-2, gpt-6.1-terra, gpt-6.1-sol and
codex-auto-review -- catalog entries only, with no code or schema change, so
it bumps the patch version). A broken
implementation would bump one file but not the other, or forget the bump
entirely.

Uses a plain regex instead of tomllib/tomli: this repo is dependency-free
by design (pyproject.toml's own dependencies = []) and tomllib is
Python-3.11+ only while the project supports 3.9+.
"""

import re
from pathlib import Path

import agent_cost

PYPROJECT_PATH = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _pyproject_version() -> str:
    text = PYPROJECT_PATH.read_text()
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', text)
    assert match, "pyproject.toml must declare a top-level version = \"...\""
    return match.group(1)


def test_pyproject_version_matches_package_version():
    assert _pyproject_version() == agent_cost.__version__


def test_version_is_0_5_2():
    assert agent_cost.__version__ == "0.5.3"
    assert _pyproject_version() == "0.5.3"
