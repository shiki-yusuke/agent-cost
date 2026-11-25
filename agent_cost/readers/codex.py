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
from datetime import datetime, timedelta, timezone
from fractions import Fraction
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


_SERVICE_TIER_MODES = {"priority": "fast", "default": "normal"}


def _service_tier_mode(tier) -> Optional[str]:
    """Map an explicit ``thread_settings.service_tier`` to a mode.

    ``None`` (absent or null) means "not stated" and leaves the caller to
    fall back to :func:`_detect_mode`. Any other value that is not exactly
    ``"priority"`` or ``"default"`` (unknown name, case variant, non-string)
    is ``"unknown"`` so it is never silently costed at standard rates.
    """
    if tier is None:
        return None
    if isinstance(tier, str) and tier in _SERVICE_TIER_MODES:
        return _SERVICE_TIER_MODES[tier]
    return "unknown"


_STARTUP_SKEW = Fraction(1)  # seconds, compared exactly (no rounding or truncation)
# The extended ISO-8601 form ``_to_utc`` reads, with a ``.`` or ``,`` fraction.
# ``fullmatch`` with ``re.ASCII``: no trailing newline, no non-ASCII digits.
_EXACT_TS_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:[.,](\d+))?(Z|[+-]\d{2}:?\d{2})?",
    re.ASCII,
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _exact_seconds(ts) -> Optional[Fraction]:
    """Seconds since the epoch, keeping every fractional digit of ``ts``.

    ``datetime`` keeps microseconds only, so ``...:01.000000001`` would look
    exactly one second after ``...:00``. ``None`` (never an exception) if
    ``ts`` is not a timestamp ``_to_utc`` reads in the form above, or its
    whole seconds cannot be converted (e.g. out of the ``datetime`` range).
    """
    if not isinstance(ts, str):
        return None
    match = _EXACT_TS_RE.fullmatch(ts)
    if not match:
        return None
    base, fraction, zone = match.groups()
    try:
        if _to_utc(ts) is None:  # only timestamps the fact timestamps accept
            return None
        whole = _to_utc(base + (zone or ""))
        if whole is None:
            return None
        seconds = (whole - _EPOCH) // timedelta(seconds=1)
    except (OverflowError, ValueError, TypeError):
        return None
    digits = fraction or "0"
    return Fraction(seconds) + Fraction(int(digits), 10 ** len(digits))


def _is_usage_record(event: dict) -> bool:
    """A record showing that the turn already sent a request or used tokens."""
    payload = event.get("payload")
    ptype = payload.get("type") if isinstance(payload, dict) else None
    if "token_usage_record" in (event.get("type"), ptype):
        return True
    if event.get("type") == "event_msg" and ptype == "token_count":
        info = payload.get("info")
        return isinstance(info, dict) and info.get("total_token_usage") is not None
    return False


_INIT_MESSAGE_ROLES = {"user", "developer", "system"}


def _is_turn_setup_record(event: dict) -> bool:
    """Records Codex writes while initializing a turn, before its first context.

    Observed order (2026-11-25): ``task_started`` > ``additional_tools`` >
    non-assistant ``message`` records > ``world_state`` > ``turn_context``.
    Anything else (assistant message, reasoning, tool calls, other events)
    means the turn is already running. A non-object payload is malformed and
    reported separately through :meth:`_StartupSnapshot.spoil`.
    """
    kind = event.get("type")
    if kind == "world_state":
        return True
    payload = event.get("payload")
    if kind != "response_item" or not isinstance(payload, dict):
        return False
    ptype = payload.get("type")
    if ptype == "additional_tools":
        return True
    role = payload.get("role")
    # Only a string can name an allowed role; any other type (list, dict, ...)
    # is outside the allowlist rather than an unhashable lookup.
    return ptype == "message" and isinstance(role, str) and role in _INIT_MESSAGE_ROLES


def _valid_snapshot_model(settings) -> Optional[str]:
    """The snapshot's model if ``thread_settings`` passes the Astra path's checks.

    ``model`` must be a non-empty string, ``model_provider_id`` must be
    ``"openai"``, ``collaboration_mode`` must be an object (absent/null count
    as empty; a wrong type is invalid) whose ``settings`` (if present) is an
    object naming no other model.
    """
    if not isinstance(settings, dict):
        return None
    model = settings.get("model")
    collab = settings.get("collaboration_mode")
    if collab is None:  # absent/null only; a wrong type ([], "", 0) is invalid
        collab = {}
    if not isinstance(collab, dict):
        return None
    nested = collab.get("settings", {})
    if not isinstance(model, str) or not model or settings.get("model_provider_id") != "openai":
        return None
    if not isinstance(nested, dict) or nested.get("model", model) != model:
        return None
    return model


class _StartupSnapshot:
    """Recognize the settings snapshot written while a turn is initialized.

    Some Codex builds write the owned ``thread_settings_applied`` snapshot
    right after the turn's first ``turn_context`` instead of before
    ``task_started``. Both paths treat such a snapshot as if it had arrived
    before the start (the turn's own settings) only when all of these hold:

    1. the turn started with nothing captured and has seen no owned snapshot
       yet (the caller decides the former and passes it to :meth:`begin`);
    2. the snapshot is the record directly after the turn's first
       ``turn_context`` (same turn ID; no context with that ID was seen before
       the start either), with the same non-empty model, and passes the Astra
       path's snapshot checks (:func:`_valid_snapshot_model`);
    3. both records carry timestamps of the form :func:`_exact_seconds`
       reads, at most one second apart, compared exactly as fractions;
    4. between the start and the first context only turn-setup records were
       seen (:func:`_is_turn_setup_record`), and nothing at all came between
       the context and the snapshot;
    5. no overlap: no other turn was still open when this one started, and
       none has been since the last time every open turn was closed; no
       malformed record (including a non-object or missing payload),
       invalidation or invalid owned snapshot in the turn (callers report
       these through :meth:`spoil`).

    Anything else keeps each path's existing handling of a mid-turn snapshot.
    Per-turn history is keyed by turn ID and kept for the whole rollout: one
    turn's completion never erases another turn's contexts. No input raises.
    """

    def __init__(self):
        self.open = []  # IDs (None if anonymous) of started, not yet completed turns
        self.overlapped = False  # a turn started while another was open
        self.contexts = {}  # turn ID -> models of its turn_context records (whole rollout)
        self._reset(None, False)

    def _reset(self, turn_id, eligible):
        self.turn_id = turn_id
        self.eligible = bool(eligible) and turn_id is not None
        self.seen_context = False
        self.candidate = None  # (model, timestamp) while the first context is the latest record

    def begin(self, turn_id, eligible):
        turn_id = turn_id if isinstance(turn_id, str) and turn_id else None
        if self.open:
            self.overlapped = True
        self.open.append(turn_id)
        prior_context = turn_id is not None and turn_id in self.contexts
        self._reset(turn_id, eligible and not self.overlapped)
        self.seen_context = prior_context  # the turn's first context came before its start

    def finish(self, turn_id, closes_active=False):
        """Record any completion event.

        ``closes_active``: the caller's existing rule closed its active turn
        with this completion (e.g. an ID-less completion on the ordinary path).
        That turn is then no longer open here either, so it cannot make a later
        turn look overlapped. Contexts recorded per turn are kept.
        """
        turn_id = turn_id if isinstance(turn_id, str) and turn_id else None
        if turn_id in self.open:
            self.open.remove(turn_id)
        elif None in self.open:
            self.open.remove(None)
        elif closes_active and self.turn_id in self.open:
            self.open.remove(self.turn_id)
        if not self.open:
            self.overlapped = False
        if closes_active:
            self._reset(None, False)

    def spoil(self):
        self.eligible = False
        self.candidate = None

    def step(self, event):
        """Record any event other than an owned snapshot."""
        is_context = event.get("type") == "turn_context"
        payload = event.get("payload")
        context_id = payload.get("turn_id") if is_context and isinstance(payload, dict) else None
        if is_context and isinstance(context_id, str):
            self.contexts.setdefault(context_id, []).append(payload.get("model"))
        if self.turn_id is None:
            return
        self.candidate = None
        if is_context:
            if context_id != self.turn_id:
                self.spoil()  # another turn's context inside this turn
            elif not self.seen_context:
                self.seen_context = True
                self.candidate = (payload.get("model"), event.get("timestamp"))
        elif self.seen_context or not _is_turn_setup_record(event):
            # The turn is already running (response, tool call, usage, other
            # event) or past its first context: no later snapshot is startup.
            self.eligible = False

    def claim(self, event, model) -> bool:
        """Consume an owned, valid snapshot; True if it is the turn's startup snapshot."""
        candidate, eligible = self.candidate, self.eligible
        self.spoil()  # at most one owned snapshot per turn can qualify
        if not eligible or candidate is None:
            return False
        context_model, context_ts = candidate
        if not isinstance(model, str) or not model or context_model != model:
            return False
        at, context_at = _exact_seconds(event.get("timestamp")), _exact_seconds(context_ts)
        if at is None or context_at is None:
            return False
        return abs(at - context_at) <= _STARTUP_SKEW


class _OwnedServiceTier:
    """Turn-scoped ``service_tier`` for the ordinary (non-Astra) path.

    Only ``thread_settings_applied`` snapshots owned by this rollout's thread
    (``thread_id == session_id``, the Astra path's ownership key) count; with
    no owner, explicit tiers are ignored. A snapshot takes effect only at the
    next ``task_started``/``turn_started``: the producer keeps an in-flight
    turn's captured settings (docs/astra-pricing.md, producer notes). Outside
    a turn (no markers, before the first start, between turns) the caller's
    existing detection applies. If a turn started without an explicit tier and
    one first appears mid-turn, the rest of the turn keeps the detected mode
    when the tier agrees with it and is ``"unknown"`` otherwise. A completion
    closes the turn unless both it and the turn carry differing turn IDs.

    Exception: a startup snapshot (see :class:`_StartupSnapshot`) whose model
    also equals the DB model written into the facts is the turn's own setting,
    exactly as if it had arrived before the start. A turn that captured an
    owned snapshot at its start (even one without a tier) never adopts one.
    """

    def __init__(self, session_id, model_raw: Optional[str] = None):
        self.session_id = session_id
        self.model_raw = model_raw  # the model written into this rollout's facts
        self.pending: Optional[str] = None  # latest owned snapshot's mode
        self.has_pending = False  # an owned snapshot exists (its tier may be absent/null)
        self.current: Optional[str] = None  # mode captured at turn start
        self.in_turn = False
        self.turn_id: Optional[str] = None
        self.mid_turn: set = set()  # explicit modes seen mid-turn (current is None)
        self.startup = _StartupSnapshot()

    @staticmethod
    def _id(payload) -> Optional[str]:
        turn_id = payload.get("turn_id")
        return turn_id if isinstance(turn_id, str) and turn_id else None

    def note_malformed(self):
        self.startup.spoil()

    def observe(self, event):
        if not isinstance(event, dict):
            return
        payload = event.get("payload")
        if event.get("type") != "event_msg" or not isinstance(payload, dict):
            self.startup.step(event)
            return
        kind = payload.get("type")
        if kind == "thread_settings_applied":
            settings = payload.get("thread_settings")
            if not self.session_id or payload.get("thread_id") != self.session_id:
                self.startup.step(event)
                return
            if not isinstance(settings, dict):
                self.startup.spoil()
                return
            # Each snapshot replaces the previous one: absent/null resets to
            # "not stated" instead of inheriting an earlier tier.
            self.pending = _service_tier_mode(settings.get("service_tier"))
            self.has_pending = True
            model = _valid_snapshot_model(settings)
            if self.startup.claim(event, model if model == self.model_raw else None):
                self.current = self.pending  # the turn's own startup setting
                return
            if self.in_turn and self.current is None and self.pending is not None:
                self.mid_turn.add(self.pending)
            return
        self.startup.step(event)
        if kind in ("task_started", "turn_started"):
            overlap = self.in_turn
            self.in_turn = True
            self.turn_id = self._id(payload)
            self.current = self.pending
            self.mid_turn = set()
            self.startup.begin(self.turn_id, not overlap and not self.has_pending)
        elif kind in ("task_complete", "turn_complete", "turn_aborted"):
            done = self._id(payload)
            if done and self.turn_id and done != self.turn_id:
                self.startup.finish(done)
                return  # another turn's completion; the active turn continues
            self.startup.finish(done, closes_active=self.in_turn)
            self.in_turn = False
            self.turn_id = None
            self.current = None
            self.mid_turn = set()

    def mode(self, detected: str) -> Optional[str]:
        """Tier-derived mode for the next delta, or ``None`` to use ``detected``."""
        if not self.in_turn:
            return None
        if self.current is not None:
            return self.current
        if self.mid_turn - {detected}:
            return "unknown"
        return None


# Exact raw model IDs that take the conservative Astra path. Detection below
# matches these as prefixes, so snapshot/alias variants also take the strict
# path; pricing still requires the exact raw ID (see aggregate.price_fact).
_ASTRA_MODEL_IDS = ("gpt-6-astra", "gpt-6.2-astra")


def _contains_astra(rollout_path: Path, model: Optional[str]) -> bool:
    """Select the conservative path without trusting the DB's final model.

    A streaming pre-pass also catches Astra -> another-model sessions. Other
    rollouts retain their existing reader behavior. No token data is collected here.
    """
    def astra(value):
        return isinstance(value, str) and value.startswith(_ASTRA_MODEL_IDS)

    if astra(model):
        return True
    with rollout_path.open() as fh:
        for line in fh:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # counted by the main pass
            if not isinstance(event, dict):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            settings = payload.get("thread_settings")
            if astra(payload.get("model")) or (
                isinstance(settings, dict) and astra(settings.get("model"))
            ):
                return True
    return False


class _AstraTurnPricing:
    """Conservative request-setting attribution for Astra-containing rollouts.

    Codex 0.153.4 emits thread-owned settings before starting a new turn, but
    standalone updates can also occur while an older request is in flight.
    Only a matching turn context and a stable, owned snapshot permit pricing.
    When that fails, an exact Astra model may still be recovered from the
    active turn's context, always with mode ``"unknown"`` (never priced).
    See docs/astra-pricing.md for pinned producer sources and deliberate limits.
    """

    def __init__(self, session_id):
        self.session_id = session_id
        self.pending = None
        self.active = None
        self.context = None
        self.selected = None
        self.ambiguous = False
        # Model recovery evidence (see _recovered_model).
        # Owned-but-invalid snapshot since the last valid owned one (wherever that
        # valid one arrived, including mid-turn).
        self.tainted = False
        self.contexts = {}  # turn_id -> models of its turn_context events
        self.startup = _StartupSnapshot()
        self._reset_turn()

    def _reset_turn(self, overlap=False):
        self.turn_tainted = False  # invalid owned snapshot during this turn; sticky
        self.turn_overlap = overlap
        self.turn_invalidated = False
        self.turn_snapshots = []  # models of valid owned snapshots seen mid-turn

    def invalidate(self):
        self.pending = None
        self.selected = None
        self.ambiguous = True
        self.turn_invalidated = True
        self.startup.spoil()

    def _snapshot(self, payload):
        """Return ``(owned, setting)``; ``setting`` is ``(model, mode)`` or None if invalid."""
        settings = payload.get("thread_settings")
        if not self.session_id or payload.get("thread_id") != self.session_id:
            return False, None
        model = _valid_snapshot_model(settings)
        if model is None:
            return True, None
        tier = settings.get("service_tier")
        # Only explicit, evidenced request choices; absent/null are not default.
        mode = "fast" if tier == "priority" else "normal" if tier == "default" else "unknown"
        return True, (model, mode)

    def observe(self, event):
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            self.invalidate()
            return
        is_snapshot = event.get("type") == "event_msg" and payload.get("type") == "thread_settings_applied"
        if not is_snapshot:
            self.startup.step(event)
        if event.get("type") == "turn_context":
            self.context = (payload.get("turn_id"), payload.get("model"))
            if isinstance(self.context[0], str):
                self.contexts.setdefault(self.context[0], []).append(self.context[1])
            if self.active is not None and self.context[0] != self.active:
                self.ambiguous = True
            if self.active is not None and self.selected and self.context[1] != self.selected[0]:
                self.ambiguous = True
            return
        if event.get("type") != "event_msg":
            return
        kind = payload.get("type")
        if kind == "thread_settings_applied":
            owned, setting = self._snapshot(payload)
            if not owned:
                self.startup.step(event)
            elif setting is None:
                self.startup.spoil()
            elif self._claim_startup(event, setting):
                # Written while the turn was initialized: the turn's own setting,
                # as if it had arrived before task_started (_StartupSnapshot).
                self.tainted = False
                self.turn_snapshots.append(setting[0])
                self.pending = self.selected = setting
                return
            if owned and setting is None:
                self.tainted = True
                if self.active is not None:
                    self.turn_tainted = True
            elif setting is not None:
                self.tainted = False
                if self.active is not None:
                    self.turn_snapshots.append(setting[0])
            if self.active is not None and not (
                    setting is not None and setting == self.selected and setting[0] in _ASTRA_MODEL_IDS):
                # Settings updates do not change already captured request settings.
                # The next cumulative delta may straddle that update: never guess.
                # Only an identical owned Astra re-send (e.g. after compaction) is
                # known not to change anything.
                self.ambiguous = True
            self.pending = setting
        elif kind in ("task_started", "turn_started"):
            overlap = self.active is not None
            turn_id = payload.get("turn_id")
            self.active = turn_id if isinstance(turn_id, str) and turn_id else None
            self.selected = self.pending
            self.ambiguous = overlap
            if self.context and self.context[0] != self.active:
                self.context = None
            self.contexts = {k: v for k, v in self.contexts.items() if k == self.active}
            self._reset_turn(overlap=overlap)
            # An earlier invalid snapshot (self.tainted) does not block it: a
            # startup snapshot clears it exactly as the same snapshot placed
            # right before task_started would.
            self.startup.begin(self.active, not overlap and self.selected is None)
        elif kind in ("task_complete", "turn_complete", "turn_aborted"):
            if not self.active or payload.get("turn_id") != self.active:
                self.startup.finish(payload.get("turn_id"))
                self.invalidate()
                return
            self.active = None
            self.selected = None
            self.context = None
            self.ambiguous = False
            self.contexts = {}
            self._reset_turn()
            self.startup.finish(self.active, closes_active=True)

    def _claim_startup(self, event, setting) -> bool:
        """Whether a valid owned snapshot is the active turn's startup snapshot."""
        model = setting[0]
        context_ok = (
            self.active is not None and not self.ambiguous
            and self.context == (self.active, model)
            and self.contexts.get(self.active) == [model]
        )
        return self.startup.claim(event, model) and context_ok

    def _recovered_model(self):
        """Exact Astra model of the active turn's context, if nothing contradicts it.

        Used only when pricing attribution fails; the caller pairs it with mode
        ``"unknown"`` so it stays unpriced. Non-Astra models are never recovered
        because ordinary catalogs price mode=unknown as standard.
        """
        if not self.active or not self.context or self.context[0] != self.active:
            return None
        model = self.context[1]
        if not isinstance(model, str) or model not in _ASTRA_MODEL_IDS:
            return None
        if (self.turn_overlap or self.turn_invalidated or self.turn_tainted
                or self.tainted):
            return None
        if any(m != model for m in self.contexts.get(self.active, [])):
            return None
        if self.selected and self.selected[0] != model:
            return None
        if any(m != model for m in self.turn_snapshots):
            return None
        return model

    def attribution(self):
        if (not self.active or self.ambiguous or not self.selected
                or self.context != (self.active, self.selected[0])):
            return self._recovered_model(), "unknown"
        model, mode = self.selected
        # Use the existing unknown-model representation for uncertain non-Astra
        # usage too; those catalogs historically price mode=unknown as standard.
        if mode == "unknown":
            return (model if model in _ASTRA_MODEL_IDS else None), mode
        return model, mode


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
    """Read rows of the ``threads`` table from a snapshot DB.

    Column presence is feature-detected via ``PRAGMA table_info`` so an
    older schema lacking ``archived`` or ``created_at_ms`` still loads
    (those filters are simply skipped when the column is absent).

    ``since_ms``/``until_ms`` are an optional, opt-in convenience for
    callers that only care about thread *creation* time (e.g. ad-hoc
    inspection). ``read_codex_facts`` deliberately does not use them for
    its window filtering: a thread created before a report window can
    still emit usage inside it, so the only correct place to apply
    since/until is per-fact, against each fact's own timestamp.
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

    ``output`` is ``output_tokens`` alone. Cross-checking real rollout
    files confirms ``total_tokens == input_tokens + output_tokens`` for
    every sample observed; ``reasoning_output_tokens`` is a breakdown of
    (already-counted) output tokens, not an additional charge, so adding
    it in would double-count reasoning tokens.

    The very first successfully-computed delta in a rollout is measured
    against an assumed-zero baseline (there is no earlier snapshot to
    diff against), which is correct for a thread that really did start
    from nothing but would overstate usage for one resumed from a prior
    context the rollout doesn't capture. Facts from that first delta
    carry ``source_quality="first_event_delta"`` so that caveat is
    visible downstream instead of looking identical to an ordinary turn.
    """
    facts: list = []
    malformed = 0
    negative_deltas = 0
    if not rollout_path.exists():
        return facts, malformed, negative_deltas

    strict = _AstraTurnPricing(session_id) if _contains_astra(rollout_path, model_raw) else None
    model_key = normalize_model_key(model_raw)
    collaboration_mode: Optional[str] = None
    tier = _OwnedServiceTier(session_id, model_raw)
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
                tier.note_malformed()
                if strict is not None:
                    strict.invalidate()
                continue
            # A non-object payload (or event) is malformed. A missing payload
            # is malformed for startup purposes only: it keeps its existing
            # handling otherwise, but never counts as a turn-setup record.
            bad_shape = not isinstance(event, dict) or not isinstance(event.get("payload", {}), dict)
            if bad_shape or "payload" not in event:
                tier.note_malformed()  # never a startup turn on either path
                if strict is not None:
                    strict.startup.spoil()
            if strict is not None and bad_shape:
                malformed += 1
                strict.invalidate()
                continue
            if bad_shape:
                continue  # nothing more to read from this line on the ordinary path
            if strict is not None:
                strict.observe(event)
            tier.observe(event)
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
                tier.note_malformed()
                continue

            input_total = int(total.get("input_tokens") or 0)
            cached_total = int(total.get("cached_input_tokens") or 0)
            output_total = int(total.get("output_tokens") or 0)

            is_first_delta = not seen_first
            if is_first_delta:
                diff_input, diff_cached, diff_output = input_total, cached_total, output_total
                seen_first = True
            else:
                diff_input = input_total - last_input_total
                diff_cached = cached_total - last_cached_total
                diff_output = output_total - last_output_total

            fact_model, fact_key = model_raw, model_key
            detected = _detect_mode(model_raw, collaboration_mode)
            tier_mode = tier.mode(detected)
            mode = tier_mode or detected
            if tier_mode == "unknown":
                # Ordinary catalogs price mode=unknown as standard; use the
                # unknown-model representation (as the Astra path does) so an
                # unrecognized tier stays unpriced.
                fact_model, fact_key = None, normalize_model_key(None)
            if strict is not None:
                fact_model, mode = strict.attribution()
                fact_key = normalize_model_key(fact_model)
            quality = "first_event_delta" if is_first_delta else "ok"

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
                            model_raw=fact_model,
                            model_key=fact_key,
                            token_kind="input_nocache",
                            tokens=nocache,
                            mode=mode,
                            source_quality=quality,
                        )
                    )
                if diff_cached:
                    facts.append(
                        Fact(
                            occurred_at_utc=occurred_at,
                            agent="codex",
                            session_id=session_id,
                            model_raw=fact_model,
                            model_key=fact_key,
                            token_kind="cache_read",
                            tokens=diff_cached,
                            mode=mode,
                            source_quality=quality,
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
                        model_raw=fact_model,
                        model_key=fact_key,
                        token_kind="output",
                        tokens=diff_output,
                        mode=mode,
                        source_quality=quality,
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
    """Read every thread's rollout into facts.

    Threads are never filtered by ``created_at_ms``: a thread created
    before the window can still emit usage events inside it. A rollout
    file's mtime is used only as a coarse "we can skip reading this
    file" optimization on the *since* side (a file untouched since well
    before the window cannot contain events inside it); there is no
    corresponding *until*-side skip, since a file modified after the
    window can still contain earlier in-window events. The exact
    boundary is always re-checked per fact below.
    """
    if not codex_db_path.exists():
        raise FileNotFoundError(f"Codex state DB not found: {codex_db_path}")

    snapshot = snapshot_db(codex_db_path)
    try:
        threads = fetch_threads(snapshot, include_archived=include_archived)
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

        if since_utc is not None:
            try:
                mtime = datetime.fromtimestamp(rollout_path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                skipped_files += 1
                continue
            if mtime < since_utc - timedelta(days=1):
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
