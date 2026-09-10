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
class ClaudeDedupUnit:
    """One dedup group, or one identity-missing row, carrying just enough
    (``session_id``, the timestamp actually used for the fact(s) it
    produced) for a caller to re-scope the three file-level dedup totals
    to a specific window and/or set of session ids after the fact -- see
    ``agent_cost.aggregate.scope_dedup_units`` and its callers in
    ``cli.py``'s ``cmd_report``/``cmd_measure``. Only emitted when it
    contributes something (a group with no duplicate and no conflict adds
    nothing, so it's skipped -- this list stays proportional to actual
    duplication, not to every message in the file).
    """

    session_id: Optional[str]
    occurred_at_utc: datetime
    duplicate_rows_skipped: int = 0
    conflicting: bool = False
    missing_identity: bool = False


@dataclass
class ClaudeParseResult:
    """Detailed result of parsing one Claude Code session file.

    ``duplicate_rows_skipped``, ``conflicting_duplicate_groups`` and
    ``missing_dedup_identity_rows`` are file-level dedup diagnostic totals
    -- see ``parse_session_detailed``'s docstring for what each counts.
    ``dedup_units`` carries the same information broken out per group/row
    so a caller can re-scope it (see ``ClaudeDedupUnit``).
    """

    facts: list = field(default_factory=list)
    malformed_events: int = 0
    duplicate_rows_skipped: int = 0
    conflicting_duplicate_groups: int = 0
    missing_dedup_identity_rows: int = 0
    dedup_units: list = field(default_factory=list)


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


def _valid_id(value) -> Optional[str]:
    """Accept only a non-empty ``str`` as a dedup identifier.

    ``message.id``/``requestId`` are expected to be strings, but nothing
    guarantees it -- a malformed or unexpected transcript could carry a
    list/dict/int/bool there. Those are not hashable-safe (or meaningful)
    dedup keys, so they're treated the same as "absent" rather than risking
    a ``TypeError`` from an unhashable dict key.
    """
    return value if isinstance(value, str) and value else None


def _usage_signature(model_raw, mode: str, tokens: dict) -> tuple:
    """A duplicate-comparable fingerprint of what a row would bill.

    Deliberately narrower than the raw ``usage`` dict: two rows that differ
    only in a non-billing field (e.g. ``iterations``, ``server_tool_use``)
    must not be flagged as conflicting duplicates. Deliberately excludes
    timestamp and session id too: the actual rate period a fact prices
    against is resolved later, from ``occurred_at_utc``
    (``aggregate.price_fact``), not compared here, and a session-id
    mismatch within one dedup group would be a different kind of anomaly
    than a usage conflict, worth a separate diagnostic rather than folding
    into this one.
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


def _select_adopted_row(rows: list) -> dict:
    """Pick the row within one dedup group whose usage gets emitted.

    Primary rule: the last row whose ``message.stop_reason`` is a
    non-empty string (null, missing, ``False``, ``""`` and non-string
    values all fail to qualify -- only a real stop-reason string counts as
    "complete"). Falls back to the group's last row if none qualifies.

    Override: if any row *after* that primary pick has a different usage
    signature, the group's actual last row is adopted instead. A
    "complete" row followed by a further, differently-valued row is not
    something Claude Code is documented to do, but if it happens the most
    recently observed state should win over an earlier "complete" marker
    that turned out not to be final -- this keeps the rule deterministic
    (always resolvable to one specific row) rather than guessing which of
    two candidates is more trustworthy.
    """
    adopted_idx = None
    for idx in range(len(rows) - 1, -1, -1):
        stop_reason = rows[idx]["stop_reason"]
        if isinstance(stop_reason, str) and stop_reason:
            adopted_idx = idx
            break

    if adopted_idx is None:
        return rows[-1]

    adopted_signature = _usage_signature(
        rows[adopted_idx]["model_raw"], rows[adopted_idx]["mode"], rows[adopted_idx]["tokens"]
    )
    for later_row in rows[adopted_idx + 1 :]:
        later_signature = _usage_signature(later_row["model_raw"], later_row["mode"], later_row["tokens"])
        if later_signature != adopted_signature:
            return rows[-1]

    return rows[adopted_idx]


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

    Dedup key: ``(message.id, requestId)`` -- **only** when *both* are
    present and are non-empty strings (see ``_valid_id``). A single
    identifier alone is not enough: without the other half, two genuinely
    different messages could coincidentally share it (e.g. ``message.id``
    without ``requestId`` in a context where ``requestId`` isn't always
    populated), silently merging unrelated billing events. A row missing
    either half of the pair is therefore never deduplicated -- it's
    emitted as its own fact, counted in ``missing_dedup_identity_rows``,
    and gets ``source_quality="identity_missing"`` rather than ``"ok"``,
    so downstream consumers can see it wasn't dedup-verified.

    Within a dedup group, the adopted row (whose usage becomes the emitted
    fact) is chosen by ``_select_adopted_row`` -- see its docstring.
    ``occurred_at_utc`` on the emitted fact is the *adopted* row's own
    timestamp (not the group's first-seen timestamp): the adopted row is
    what determines the actual token counts, and pricing/month-bucketing
    both key off ``occurred_at_utc``, so using an earlier placeholder row's
    timestamp here could shift real tokens across a rate-period or month
    boundary that they don't actually belong to. Only the group's
    *position* in the output ``facts`` list follows first-seen order, so a
    later-arriving duplicate of an earlier message doesn't reorder it past
    messages that came after it in the original transcript. If the group's
    rows don't all share the same usage signature (model/mode/token
    counts), ``conflicting_duplicate_groups`` counts it -- the adopted row
    is still emitted, never dropped, since silently discarding a differing
    usage would just trade over-counting for under-counting.

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
            msg_id = _valid_id(message.get("id"))
            req_id = _valid_id(event.get("requestId"))

            row = {
                "occurred_at": occurred_at,
                "model_raw": model_raw,
                "mode": mode,
                "sid": sid,
                "stop_reason": stop_reason,
                "tokens": tokens,
            }

            key = (msg_id, req_id) if (msg_id is not None and req_id is not None) else None

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
    dedup_units: list = []
    for marker in emit_order:
        if marker in standalone_rows:
            row = standalone_rows[marker]
            source_quality = "identity_missing"
            dedup_units.append(
                ClaudeDedupUnit(
                    session_id=row["sid"],
                    occurred_at_utc=row["occurred_at"],
                    missing_identity=True,
                )
            )
        else:
            rows = groups[marker]
            group_skipped = len(rows) - 1
            duplicate_rows_skipped += group_skipped

            adopted = _select_adopted_row(rows)

            signatures = {_usage_signature(r["model_raw"], r["mode"], r["tokens"]) for r in rows}
            group_conflicting = len(signatures) > 1
            if group_conflicting:
                conflicting_duplicate_groups += 1

            if group_skipped > 0 or group_conflicting:
                dedup_units.append(
                    ClaudeDedupUnit(
                        session_id=adopted["sid"],
                        occurred_at_utc=adopted["occurred_at"],
                        duplicate_rows_skipped=group_skipped,
                        conflicting=group_conflicting,
                    )
                )

            row = {
                "occurred_at": adopted["occurred_at"],
                "model_raw": adopted["model_raw"],
                "mode": adopted["mode"],
                "sid": adopted["sid"],
                "tokens": adopted["tokens"],
            }
            source_quality = "ok"

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
                    source_quality=source_quality,
                )
            )

    return ClaudeParseResult(
        facts=facts,
        malformed_events=malformed,
        duplicate_rows_skipped=duplicate_rows_skipped,
        conflicting_duplicate_groups=conflicting_duplicate_groups,
        missing_dedup_identity_rows=missing_dedup_identity_rows,
        dedup_units=dedup_units,
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
    all_dedup_units: list = []

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

        # Mirror the same [since, until) window on dedup_units as on facts
        # above, so a caller that passed a window here sees a consistent
        # picture between the two -- the file-level totals accumulated
        # just above are intentionally NOT windowed (same as
        # malformed_events elsewhere), only this per-unit list is.
        for unit in result.dedup_units:
            if since_utc is not None and unit.occurred_at_utc < since_utc:
                continue
            if until_utc is not None and unit.occurred_at_utc >= until_utc:
                continue
            all_dedup_units.append(unit)

    return ReadResult(
        facts=all_facts,
        malformed_events=malformed_total,
        skipped_files=skipped_files,
        duplicate_rows_skipped=duplicate_rows_skipped_total,
        conflicting_duplicate_groups=conflicting_duplicate_groups_total,
        missing_dedup_identity_rows=missing_dedup_identity_rows_total,
        claude_dedup_units=all_dedup_units,
    )
