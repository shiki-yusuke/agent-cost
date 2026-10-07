"""Spec: pyproject.toml's [project].version and agent_cost.__version__ must
agree, and both must read "0.3.0" for this change (the output_lower_bound
source_quality value is an additive vocabulary change, so it bumps the minor
version). A broken implementation would bump one file but not the other, or
forget the bump entirely.

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


def test_version_is_0_2_0():
    assert agent_cost.__version__ == "0.3.0"
    assert _pyproject_version() == "0.3.0"
