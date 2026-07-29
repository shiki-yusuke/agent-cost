"""Canonical fact type shared by every reader.

A "fact" is one billing event: a single token count of a single kind,
charged against a single model, at a single point in time. Readers turn
whatever a given tool's local logs record (assistant messages, rollout
snapshots, ...) into a stream of these facts; every other layer (pricing,
aggregation, export) only ever has to understand this one shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

TOKEN_KINDS = (
    "input_nocache",
    "cache_read",
    "cache_write_5m",
    "cache_write_1h",
    "cache_write_unknown",
    "output",
)

AGENTS = ("claude", "codex")
MODES = ("fast", "normal", "unknown")

# Closed set of source_quality values a reader may attach to a fact. "ok"
# covers the ordinary case; readers add a more specific value only when a
# fact's derivation has a real, nameable caveat worth surfacing downstream
# (e.g. Codex's first delta in a rollout is measured against an assumed
# zero baseline). This is never left unset -- every Fact defaults to "ok"
# so export never emits a null source_quality.
SOURCE_QUALITY_VALUES = ("ok", "first_event_delta")

_BRACKET_SUFFIX = re.compile(r"\[[^\]]*\]$")
_DATE_SUFFIX = re.compile(r"@\d{6,8}$")
_VARIANT_SUFFIX = re.compile(r"-(?:1m|200k|fast|latest)$", re.IGNORECASE)
_DOTTED_MINOR = re.compile(r"(?<=\d)\.(?=\d)")


def normalize_model_key(model_raw: Optional[str]) -> str:
    """Collapse cosmetic model-name variants into one stable key.

    Strips context-window / speed / snapshot-date suffixes (``[1m]``,
    ``@20260101``, ``-fast``, ``-200k``, ``-latest``, applied repeatedly
    since they can stack) and normalizes Claude's dotted minor-version
    notation (``claude-opus-4.7`` -> ``claude-opus-4-7``). This is a pure
    string transform with no knowledge of any rate catalog, so a fact's
    ``model_key`` stays the same even when the catalog used to price it
    is swapped out via ``--rates``.
    """
    if not model_raw:
        return "(unknown)"
    key = str(model_raw).strip()
    key = _BRACKET_SUFFIX.sub("", key)
    key = _DATE_SUFFIX.sub("", key)
    while True:
        stripped = _VARIANT_SUFFIX.sub("", key)
        if stripped == key:
            break
        key = stripped
    if key.startswith("claude-"):
        key = _DOTTED_MINOR.sub("-", key)
    return key or "(unknown)"


@dataclass(frozen=True)
class Fact:
    occurred_at_utc: datetime
    agent: str
    session_id: Optional[str]
    model_raw: Optional[str]
    model_key: str
    token_kind: str
    tokens: int
    mode: str = "unknown"
    source_quality: str = "ok"

    def __post_init__(self) -> None:
        if self.agent not in AGENTS:
            raise ValueError(f"invalid agent: {self.agent!r}")
        if self.token_kind not in TOKEN_KINDS:
            raise ValueError(f"invalid token_kind: {self.token_kind!r}")
        if self.mode not in MODES:
            raise ValueError(f"invalid mode: {self.mode!r}")
        if self.source_quality not in SOURCE_QUALITY_VALUES:
            raise ValueError(f"invalid source_quality: {self.source_quality!r}")
        if self.tokens < 0:
            raise ValueError(f"tokens must be >= 0, got {self.tokens}")
        if self.occurred_at_utc.tzinfo is None:
            raise ValueError("occurred_at_utc must be timezone-aware")
