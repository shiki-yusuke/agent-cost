"""Log readers: turn a tool's local files into a stream of canonical facts.

Each reader module exposes a ``ReadResult`` (the facts plus counters for
anything that looked broken along the way) and a top-level ``read_*``
function. Readers never make network calls and never write to the source
logs they read.
"""

from dataclasses import dataclass, field
from typing import List

from ..facts import Fact


@dataclass
class ReadResult:
    facts: List[Fact] = field(default_factory=list)
    malformed_events: int = 0
    skipped_files: int = 0
    negative_deltas: int = 0
    # Codex-only diagnostic: number of threads where the state DB's
    # `tokens_used` column diverges from the sum of that thread's derived
    # facts by more than 1% or 100 tokens. Not part of the report JSON's
    # `data_quality` (which is reader-agnostic); surfaced for `doctor` /
    # tests.
    tokens_used_diffs: int = 0
    # Claude-only dedup diagnostics (see readers/claude.py's
    # parse_session_detailed docstring). Always 0 / empty for the Codex
    # reader. The three ints are file-level totals, NOT scoped to any
    # window or session; `claude_dedup_units` carries the same
    # information per group/row (as `ClaudeDedupUnit`, duck-typed here to
    # avoid a circular import with readers.claude) so a caller can
    # re-scope it -- see `agent_cost.aggregate.scope_dedup_units`.
    duplicate_rows_skipped: int = 0
    conflicting_duplicate_groups: int = 0
    missing_dedup_identity_rows: int = 0
    claude_dedup_units: List = field(default_factory=list)
