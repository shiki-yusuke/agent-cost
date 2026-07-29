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
