"""agent-cost: estimate AI coding agent token usage and cost from local logs.

Reads Claude Code and Codex CLI logs that already exist on disk and turns
them into per-event "facts" (billing events), then prices those facts
against a versioned rate catalog. Everything happens locally; there are no
network calls.
"""

__version__ = "0.1.1"
