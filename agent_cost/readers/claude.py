"""Claude Code reader: ``~/.claude/projects/**/*.jsonl`` -> facts.

Each session file is a JSONL transcript. Every ``assistant`` event that
carries a ``message.usage`` block is one billing event and becomes its own
set of facts, attributed to the model recorded on *that* event
(``message.model``) -- never to a session-wide majority model, since a
single session can span multiple models.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional, Tuple

from . import ReadResult
from ..facts import Fact, normalize_model_key


def _parse_timestamp(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        text = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _detect_mode(usage: dict) -> str:
    speed = usage.get("speed")
    if not speed:
        return "unknown"
    return "fast" if "fast" in str(speed).strip().lower() else "normal"


def parse_session_facts(jsonl_path: Path) -> Tuple[list, int]:
    """Stream one session file into facts. Returns ``(facts, malformed_events)``.

    Raises ``OSError`` if the file cannot be read at all (the caller
    counts that as a skipped file, distinct from a malformed *line*).
    """
    facts: list = []
    malformed = 0
    session_id: Optional[str] = None

    with jsonl_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue

            if not session_id and event.get("sessionId"):
                session_id = event.get("sessionId")

            if event.get("type") != "assistant":
                continue
            message = event.get("message") or {}
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue

            occurred_at = _parse_timestamp(event.get("timestamp"))
            if occurred_at is None:
                malformed += 1
                continue

            model_raw = message.get("model") or "(unknown)"
            model_key = normalize_model_key(model_raw)
            mode = _detect_mode(usage)
            sid = event.get("sessionId") or session_id

            def _emit(kind: str, tokens: int) -> None:
                if tokens > 0:
                    facts.append(
                        Fact(
                            occurred_at_utc=occurred_at,
                            agent="claude",
                            session_id=sid,
                            model_raw=model_raw,
                            model_key=model_key,
                            token_kind=kind,
                            tokens=tokens,
                            mode=mode,
                        )
                    )

            try:
                _emit("input_nocache", int(usage.get("input_tokens") or 0))
                _emit("cache_read", int(usage.get("cache_read_input_tokens") or 0))

                cache_creation = usage.get("cache_creation")
                cache_creation_total = int(usage.get("cache_creation_input_tokens") or 0)
                if isinstance(cache_creation, dict):
                    ephemeral_5m = int(cache_creation.get("ephemeral_5m_input_tokens") or 0)
                    ephemeral_1h = int(cache_creation.get("ephemeral_1h_input_tokens") or 0)
                    _emit("cache_write_5m", ephemeral_5m)
                    _emit("cache_write_1h", ephemeral_1h)
                    leftover = cache_creation_total - (ephemeral_5m + ephemeral_1h)
                    if leftover > 0:
                        _emit("cache_write_unknown", leftover)
                elif cache_creation_total:
                    _emit("cache_write_unknown", cache_creation_total)

                _emit("output", int(usage.get("output_tokens") or 0))
            except (TypeError, ValueError):
                malformed += 1
                continue

    return facts, malformed


def iter_project_files(claude_projects_dir: Path) -> Iterator[Path]:
    if not claude_projects_dir.exists():
        return
    for project_dir in sorted(claude_projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        yield from sorted(project_dir.glob("*.jsonl"))


def read_claude_facts(
    claude_projects_dir: Path,
    *,
    since_utc: Optional[datetime] = None,
    until_utc: Optional[datetime] = None,
) -> ReadResult:
    """Read every session file under ``claude_projects_dir`` into facts.

    File mtime is used only as a coarse "can we skip reading this file
    entirely" optimization (with a wide +/-1 day margin); the actual
    since/until boundary is always re-checked per fact below.
    """
    all_facts: list = []
    malformed_total = 0
    skipped_files = 0

    for jsonl_path in iter_project_files(claude_projects_dir):
        if since_utc is not None or until_utc is not None:
            try:
                mtime = datetime.fromtimestamp(jsonl_path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                skipped_files += 1
                continue
            if since_utc is not None and mtime < since_utc - timedelta(days=1):
                continue
            if until_utc is not None and mtime > until_utc + timedelta(days=1):
                continue

        try:
            facts, malformed = parse_session_facts(jsonl_path)
        except OSError:
            skipped_files += 1
            continue

        malformed_total += malformed
        for fact in facts:
            if since_utc is not None and fact.occurred_at_utc < since_utc:
                continue
            if until_utc is not None and fact.occurred_at_utc >= until_utc:
                continue
            all_facts.append(fact)

    return ReadResult(facts=all_facts, malformed_events=malformed_total, skipped_files=skipped_files)
