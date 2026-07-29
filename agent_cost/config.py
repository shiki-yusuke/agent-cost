"""Runtime configuration: where to find each tool's local logs.

Resolution order (highest priority first): environment variables, then
``~/.config/agent-cost/config.json`` (or the path in ``AGENT_COST_CONFIG``),
then built-in defaults.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "agent-cost" / "config.json"


@dataclass(frozen=True)
class Config:
    claude_home: Path
    codex_home: Path
    codex_db_filename: str = "state_5.sqlite"

    @property
    def claude_projects_dir(self) -> Path:
        return self.claude_home / "projects"

    @property
    def codex_db_path(self) -> Path:
        return self.codex_home / self.codex_db_filename


def _load_config_file(explicit_path: Optional[Path]) -> dict:
    config_path = explicit_path or Path(
        os.environ.get("AGENT_COST_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    if not config_path.exists():
        return {}
    try:
        with config_path.open() as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config(config_path: Optional[Path] = None) -> Config:
    file_data = _load_config_file(config_path)

    claude_home = Path(
        os.environ.get("CLAUDE_HOME")
        or file_data.get("claude_home")
        or os.path.expanduser("~/.claude")
    )
    codex_home = Path(
        os.environ.get("CODEX_HOME")
        or file_data.get("codex_home")
        or os.path.expanduser("~/.codex")
    )
    codex_db_filename = str(file_data.get("codex_db_filename") or "state_5.sqlite")

    return Config(
        claude_home=claude_home,
        codex_home=codex_home,
        codex_db_filename=codex_db_filename,
    )
