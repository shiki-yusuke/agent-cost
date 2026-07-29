"""Codex CLI reader: ``state_*.sqlite`` threads + rollout JSONL -> facts.

The state DB records one row per thread (roughly: one Codex session); each
thread points at a rollout JSONL file holding a running ``token_count``
snapshot after every turn. A fact is the *increment* between two
consecutive snapshots, computed independently per token channel so that an
anomaly in one channel (e.g. a session reset that makes the cumulative
total go backwards) doesn't corrupt the others.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from . import ReadResult
from ..facts import Fact, normalize_model_key

_FAST_MODEL_RE = re.compile(r"-fast\b", re.IGNORECASE)
_NON_FAST_MODES = {"default", "standard", "normal"}


def _detect_mode(model: Optional[str], collaboration_mode: Optional[str]) -> str:
    """Best-effort Fast Mode classification.

    An unrecognized ``collaboration_mode`` value is left as ``"unknown"``
    rather than assumed non-fast, so a future mode name doesn't silently
    get costed at standard rates.
    """
    if model and _FAST_MODEL_RE.search(str(model)):
        return "fast"
    if collaboration_mode:
        cm = str(collaboration_mode).strip().lower()
        if "fast" in cm:
            return "fast"
        if cm in _NON_FAST_MODES:
            return "normal"
    return "unknown"


def snapshot_db(src: Path) -> Path:
    """Copy the Codex state DB to a temp file using sqlite3's backup API.

    The backup API produces a consistent snapshot even while a writer
    holds the source DB open in WAL mode, without shelling out to the
    ``sqlite3`` binary.
    """
    if not src.exists():
        raise FileNotFoundError(f"Codex state DB not found: {src}")
    fd, dst_str = tempfile.mkstemp(prefix="agent-cost-codex-snapshot-", suffix=".sqlite")
    os.close(fd)
    dst = Path(dst_str)
    dst.unlink(missing_ok=True)
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    return dst


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def fetch_threads(
    snapshot: Path,
    *,
    include_archived: bool = True,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
) -> list:
    """Read all rows of the ``threads`` table from a snapshot DB.

    Column presence is feature-detected via ``PRAGMA table_info`` so an
    older schema lacking ``archived`` or ``created_at_ms`` still loads
    (those filters are simply skipped when the column is absent).
    """
    conn = sqlite3.connect(str(snapshot))
    conn.row_factory = sqlite3.Row
    try:
        columns = _table_columns(conn, "threads")
        where = []
        params: list = []
        if not include_archived and "archived" in columns:
            where.append("archived = 0")
        if "created_at_ms" in columns:
            if since_ms is not None:
                where.append("created_at_ms >= ?")
                params.append(since_ms)
            if until_ms is not None:
                where.append("created_at_ms < ?")
                params.append(until_ms)
        sql = "SELECT * FROM threads"
        if where:
            sql += " WHERE " + " AND ".join(where)
        if "created_at_ms" in columns:
            sql += " ORDER BY created_at_ms DESC"
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def _to_utc(ts: Optional[str]) -> Optional[datetime]:
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


def parse_rollout_facts(
    rollout_path: Path,
    *,
    model_raw: Optional[str],
    session_id: Optional[str],
) -> Tuple[list, int, int]:
    """Convert a rollout JSONL's cumulative ``token_count`` events into
    per-event delta facts.

    Returns ``(facts, malformed_events, negative_deltas)``. The first
    ``token_count`` event (``total: null``) is an initialization marker
    and contributes no delta. When a channel's increment relative to the
    previous snapshot is negative, that channel's fact for this event is
    dropped and counted in ``negative_deltas`` instead of being clamped to
    zero; the running baseline still advances so the anomaly isn't
    repeated on every subsequent event.
    """
    facts: list = []
    malformed = 0
    negative_deltas = 0
    if not rollout_path.exists():
        return facts, malformed, negative_deltas

    model_key = normalize_model_key(model_raw)
    collaboration_mode: Optional[str] = None
    last_input_total = 0
    last_cached_total = 0
    last_output_total = 0
    seen_first = False

    with rollout_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if event.get("type") != "event_msg":
                continue
            payload = event.get("payload") or {}
            ptype = payload.get("type")
            if ptype == "task_started":
                cm = payload.get("collaboration_mode_kind")
                if cm is not None and collaboration_mode is None:
                    collaboration_mode = str(cm)
                continue
            if ptype != "token_count":
                continue

            info = payload.get("info") or {}
            total = info.get("total_token_usage")
            if total is None:
                continue  # initialization event, no cumulative data yet

            occurred_at = _to_utc(event.get("timestamp"))
            if occurred_at is None:
                malformed += 1
                continue

            input_total = int(total.get("input_tokens") or 0)
            cached_total = int(total.get("cached_input_tokens") or 0)
            output_total = int(total.get("output_tokens") or 0) + int(
                total.get("reasoning_output_tokens") or 0
            )

            if not seen_first:
                diff_input, diff_cached, diff_output = input_total, cached_total, output_total
                seen_first = True
            else:
                diff_input = input_total - last_input_total
                diff_cached = cached_total - last_cached_total
                diff_output = output_total - last_output_total

            mode = _detect_mode(model_raw, collaboration_mode)

            if diff_input < 0 or diff_cached < 0:
                negative_deltas += 1
            else:
                nocache = max(diff_input - diff_cached, 0)
                if nocache:
                    facts.append(
                        Fact(
                            occurred_at_utc=occurred_at,
                            agent="codex",
                            session_id=session_id,
                            model_raw=model_raw,
                            model_key=model_key,
                            token_kind="input_nocache",
                            tokens=nocache,
                            mode=mode,
                        )
                    )
                if diff_cached:
                    facts.append(
                        Fact(
                            occurred_at_utc=occurred_at,
                            agent="codex",
                            session_id=session_id,
                            model_raw=model_raw,
                            model_key=model_key,
                            token_kind="cache_read",
                            tokens=diff_cached,
                            mode=mode,
                        )
                    )

            if diff_output < 0:
                negative_deltas += 1
            elif diff_output:
                facts.append(
                    Fact(
                        occurred_at_utc=occurred_at,
                        agent="codex",
                        session_id=session_id,
                        model_raw=model_raw,
                        model_key=model_key,
                        token_kind="output",
                        tokens=diff_output,
                        mode=mode,
                    )
                )

            last_input_total = input_total
            last_cached_total = cached_total
            last_output_total = output_total

    return facts, malformed, negative_deltas


def read_codex_facts(
    codex_db_path: Path,
    codex_home: Path,
    *,
    since_utc: Optional[datetime] = None,
    until_utc: Optional[datetime] = None,
    include_archived: bool = True,
) -> ReadResult:
    if not codex_db_path.exists():
        raise FileNotFoundError(f"Codex state DB not found: {codex_db_path}")

    since_ms = int(since_utc.timestamp() * 1000) if since_utc is not None else None
    until_ms = int(until_utc.timestamp() * 1000) if until_utc is not None else None

    snapshot = snapshot_db(codex_db_path)
    try:
        threads = fetch_threads(
            snapshot,
            include_archived=include_archived,
            since_ms=since_ms,
            until_ms=until_ms,
        )
    finally:
        snapshot.unlink(missing_ok=True)

    all_facts: list = []
    malformed_total = 0
    negative_total = 0
    skipped_files = 0
    tokens_used_diffs = 0

    for thread in threads:
        rollout_path_raw = thread.get("rollout_path")
        if not rollout_path_raw:
            continue
        rollout_path = Path(rollout_path_raw)
        if not rollout_path.is_absolute():
            rollout_path = codex_home / rollout_path
        if not rollout_path.exists():
            skipped_files += 1
            continue

        facts, malformed, negative = parse_rollout_facts(
            rollout_path,
            model_raw=thread.get("model"),
            session_id=thread.get("id"),
        )
        malformed_total += malformed
        negative_total += negative

        # Diagnostic only: how far does this thread's derived-fact total
        # sit from the state DB's own `tokens_used` column. Not a source
        # of truth for the fact stream itself.
        tokens_used = thread.get("tokens_used") or 0
        derived_total = sum(f.tokens for f in facts)
        if tokens_used and derived_total:
            diff = abs(tokens_used - derived_total)
            if diff > max(100, tokens_used * 0.01):
                tokens_used_diffs += 1

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
        negative_deltas=negative_total,
        tokens_used_diffs=tokens_used_diffs,
    )
