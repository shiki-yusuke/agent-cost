"""Claude Code reader: ``~/.claude/projects/**/*.jsonl`` -> facts.

Each session file is a JSONL transcript. Every *logical* ``assistant``
message that carries a ``message.usage`` block is one billing event and
becomes its own set of facts, attributed to the model recorded on *that*
event (``message.model``) -- never to a session-wide majority model, since
a single session can span multiple models. "Logical" matters: Claude Code
writes one JSONL line per content block of a message, repeating the same
``usage`` on every line, so lines sharing the same message are deduplicated
first -- see ``parse_session_detailed``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional, Tuple

from . import ReadResult
from ..facts import Fact, normalize_model_key


@dataclass
class ClaudeParseResult:
    """Detailed result of parsing one Claude Code session file.

    ``duplicate_rows_skipped``, ``conflicting_duplicate_groups`` and
    ``missing_dedup_identity_rows`` are per-file dedup diagnostics -- see
    ``parse_session_detailed``'s docstring for what each counts.
    """

    facts: list = field(default_factory=list)
    malformed_events: int = 0
    duplicate_rows_skipped: int = 0
    conflicting_duplicate_groups: int = 0
    missing_dedup_identity_rows: int = 0


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


def _extract_tokens(usage: dict) -> Optional[dict]:
    """Convert a raw ``usage`` block into ``{token_kind: amount}`` (amount > 0
    only). Returns ``None`` if any field fails int conversion -- the caller
    counts that as one malformed event, matching the pre-dedup behavior of
    dropping the whole line's facts on a single bad field.
    """
    tokens: dict = {}
    try:
        input_nocache = int(usage.get("input_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)

        cache_creation = usage.get("cache_creation")
        cache_creation_total = int(usage.get("cache_creation_input_tokens") or 0)
        cache_write_5m = 0
        cache_write_1h = 0
        cache_write_unknown = 0
        if isinstance(cache_creation, dict):
            cache_write_5m = int(cache_creation.get("ephemeral_5m_input_tokens") or 0)
            cache_write_1h = int(cache_creation.get("ephemeral_1h_input_tokens") or 0)
            leftover = cache_creation_total - (cache_write_5m + cache_write_1h)
            if leftover > 0:
                cache_write_unknown = leftover
        elif cache_creation_total:
            cache_write_unknown = cache_creation_total

        output = int(usage.get("output_tokens") or 0)
    except (TypeError, ValueError):
        return None

    for kind, amount in (
        ("input_nocache", input_nocache),
        ("cache_read", cache_read),
        ("cache_write_5m", cache_write_5m),
        ("cache_write_1h", cache_write_1h),
        ("cache_write_unknown", cache_write_unknown),
        ("output", output),
    ):
        if amount > 0:
            tokens[kind] = amount
    return tokens


def _billing_signature(model_raw, mode: str, tokens: dict) -> tuple:
    """A duplicate-comparable fingerprint of what a row would bill.

    Deliberately narrower than the raw ``usage`` dict: two rows that differ
    only in a non-billing field (e.g. ``iterations``, ``server_tool_use``)
    must not be flagged as conflicting duplicates.
    """
    return (
        model_raw,
        mode,
        tokens.get("input_nocache", 0),
        tokens.get("cache_read", 0),
        tokens.get("cache_write_5m", 0),
        tokens.get("cache_write_1h", 0),
        tokens.get("cache_write_unknown", 0),
        tokens.get("output", 0),
    )


def parse_session_detailed(jsonl_path: Path) -> ClaudeParseResult:
    """Stream one session file into facts, deduplicating rows that share the
    same logical message.

    Claude Code's transcript writes one JSONL line per *content block* of an
    assistant message, not one line per message: every line belonging to the
    same message carries the same ``message.id`` / ``requestId`` and the same
    ``message.usage`` (observed behavior; not a documented, stable schema --
    see the design review this implements). Treating every line as its own
    billing event over-counts a single message 3-7x. Dedup is per-file only
    (a global, cross-file dedup would risk merging facts across sessions).

    Dedup key: ``(message.id, requestId)`` when both are present, else
    whichever of the two is present alone. A row with *neither* identifier
    is emitted individually as before (can't be deduplicated) and counted in
    ``missing_dedup_identity_rows`` -- this is a compatibility fallback, not
    a claim that such a row is billing-accurate.

    Within a dedup group, the adopted row is the last one with a non-null
    ``message.stop_reason`` (Anthropic's own guidance: earlier lines can be
    streaming placeholders, the last one carries the final usage), falling
    back to the last row if none has a ``stop_reason``. The adopted row's
    usage is what gets emitted -- ``occurred_at_utc`` and the group's
    position in the output order both come from the *first-seen* row instead,
    so timestamps and Claude Code's original message ordering aren't
    disturbed by which row happened to be final. If the group's rows don't
    all share the same billing signature (model/mode/token counts),
    ``conflicting_duplicate_groups`` counts it -- the adopted row is still
    emitted, never dropped, since silently discarding a differing usage
    would just trade over-counting for under-counting.

    Raises ``OSError`` if the file cannot be read at all (the caller counts
    that as a skipped file, distinct from a malformed *line*).
    """
    malformed = 0
    duplicate_rows_skipped = 0
    conflicting_duplicate_groups = 0
    missing_dedup_identity_rows = 0
    session_id: Optional[str] = None

    groups: dict = {}
    # Order in which each dedup key (or standalone row) was first seen, so
    # the final facts list preserves Claude Code's original line ordering
    # regardless of which line within a group ends up "adopted".
    emit_order: list = []
    standalone_rows: dict = {}

    with jsonl_path.open() as fh:
        for line_no, line in enumerate(fh):
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

            tokens = _extract_tokens(usage)
            if tokens is None:
                malformed += 1
                continue

            model_raw = message.get("model") or "(unknown)"
            mode = _detect_mode(usage)
            sid = event.get("sessionId") or session_id
            stop_reason = message.get("stop_reason")
            msg_id = message.get("id")
            req_id = event.get("requestId")

            row = {
                "occurred_at": occurred_at,
                "model_raw": model_raw,
                "mode": mode,
                "sid": sid,
                "stop_reason": stop_reason,
                "tokens": tokens,
            }

            if msg_id and req_id:
                key = ("both", msg_id, req_id)
            elif msg_id:
                key = ("id", msg_id)
            elif req_id:
                key = ("req", req_id)
            else:
                key = None

            if key is None:
                missing_dedup_identity_rows += 1
                marker = ("__single__", line_no)
                standalone_rows[marker] = row
                emit_order.append(marker)
                continue

            if key not in groups:
                groups[key] = []
                emit_order.append(key)
            groups[key].append(row)

    facts: list = []
    for marker in emit_order:
        if marker in standalone_rows:
            row = standalone_rows[marker]
        else:
            rows = groups[marker]
            duplicate_rows_skipped += len(rows) - 1

            adopted = None
            for candidate in reversed(rows):
                if candidate["stop_reason"] is not None:
                    adopted = candidate
                    break
            if adopted is None:
                adopted = rows[-1]

            signatures = {
                _billing_signature(r["model_raw"], r["mode"], r["tokens"]) for r in rows
            }
            if len(signatures) > 1:
                conflicting_duplicate_groups += 1

            row = {
                "occurred_at": rows[0]["occurred_at"],
                "model_raw": adopted["model_raw"],
                "mode": adopted["mode"],
                "sid": rows[0]["sid"],
                "tokens": adopted["tokens"],
            }

        model_key = normalize_model_key(row["model_raw"])
        for kind, amount in row["tokens"].items():
            facts.append(
                Fact(
                    occurred_at_utc=row["occurred_at"],
                    agent="claude",
                    session_id=row["sid"],
                    model_raw=row["model_raw"],
                    model_key=model_key,
                    token_kind=kind,
                    tokens=amount,
                    mode=row["mode"],
                )
            )

    return ClaudeParseResult(
        facts=facts,
        malformed_events=malformed,
        duplicate_rows_skipped=duplicate_rows_skipped,
        conflicting_duplicate_groups=conflicting_duplicate_groups,
        missing_dedup_identity_rows=missing_dedup_identity_rows,
    )


def parse_session_facts(jsonl_path: Path) -> Tuple[list, int]:
    """Stream one session file into facts. Returns ``(facts, malformed_events)``.

    Compatibility wrapper around ``parse_session_detailed`` -- prefer that
    for callers that also want the dedup diagnostics.
    """
    result = parse_session_detailed(jsonl_path)
    return result.facts, result.malformed_events


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

    A file's mtime is used only as a coarse "can we skip reading this
    file entirely" optimization on the *since* side: a file untouched
    since well before the window cannot contain any event inside it
    (every event's timestamp is <= the file's mtime), so skipping it is
    safe. There is no equivalent skip on the *until* side -- a file
    modified after the window can easily still contain earlier events
    that fall inside it, so skipping on a late mtime would silently drop
    real in-window data. The exact since/until boundary is always
    re-checked per fact below regardless.
    """
    all_facts: list = []
    malformed_total = 0
    skipped_files = 0
    duplicate_rows_skipped_total = 0
    conflicting_duplicate_groups_total = 0
    missing_dedup_identity_rows_total = 0

    for jsonl_path in iter_project_files(claude_projects_dir):
        if since_utc is not None:
            try:
                mtime = datetime.fromtimestamp(jsonl_path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                skipped_files += 1
                continue
            if mtime < since_utc - timedelta(days=1):
                continue

        try:
            result = parse_session_detailed(jsonl_path)
        except OSError:
            skipped_files += 1
            continue
        facts, malformed = result.facts, result.malformed_events
        duplicate_rows_skipped_total += result.duplicate_rows_skipped
        conflicting_duplicate_groups_total += result.conflicting_duplicate_groups
        missing_dedup_identity_rows_total += result.missing_dedup_identity_rows

        malformed_total += malformed
        for fact in facts:
            if since_utc is not None and fact.occurred_at_utc < since_utc:
                continue
            if until_utc is not None and fact.occurred_at_utc >= until_utc:
                continue
            all_facts.append(fact)

    return ReadResult(
        facts=all_facts,
        malformed_events=malformed_total,
        skipped_files=skipped_files,
        duplicate_rows_skipped=duplicate_rows_skipped_total,
        conflicting_duplicate_groups=conflicting_duplicate_groups_total,
        missing_dedup_identity_rows=missing_dedup_identity_rows_total,
    )
