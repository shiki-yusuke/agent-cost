"""Tests for Claude reader dedup (E0-a3).

Expected values are derived from the spec contract given to the tester role
(not from the implementation), summarized inline per test. Fixtures live in
``tests/fixtures/dedup/*.jsonl``.

Dedup contract (spec):
  - key = (message.id, requestId) when both present; message.id alone when
    requestId is absent; requestId alone when message.id is absent; rows
    with neither are emitted individually (not deduped) and counted in
    ``missing_dedup_identity_rows``.
  - Adopted row per key = the last row whose ``message.stop_reason`` is not
    null/missing; if none qualifies, the last row of the group.
  - ``occurred_at`` and a key's position in ``facts`` order come from the
    *first* row of the group (first-seen ordering), even though the usage
    values come from the adopted row.
  - ``duplicate_rows_skipped`` = sum over keys of (row_count - 1).
  - billing signature = (model, mode, input_nocache, cache_read,
    cache_write_5m, cache_write_1h, cache_write_unknown, output). Two or
    more distinct signatures within one key's group increments
    ``conflicting_duplicate_groups`` by 1 (the adopted row is still
    emitted, never dropped).
"""

import json
from pathlib import Path

import agent_cost
from agent_cost import cli
from agent_cost.readers.claude import parse_session_detailed, parse_session_facts, read_claude_facts

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "dedup"


def _fixture(name: str) -> Path:
    return FIXTURES_DIR / name


def test_exact_duplicate_rows_collapse_to_one_and_are_counted():
    """Spec: 3 rows sharing (id, requestId) with identical usage must emit
    exactly one row's worth of tokens, skip 2, and report zero conflicts.
    A broken implementation would either sum all 3 rows' tokens (no dedup)
    or fail to increment duplicate_rows_skipped."""
    result = parse_session_detailed(_fixture("exact_duplicate.jsonl"))
    assert result.malformed_events == 0
    assert sum(f.tokens for f in result.facts) == 100 + 50  # one row's usage only
    assert result.duplicate_rows_skipped == 2
    assert result.conflicting_duplicate_groups == 0
    assert result.missing_dedup_identity_rows == 0


def test_placeholder_row_is_superseded_by_final_stop_reason_row():
    """Spec: within one key, the row whose stop_reason is not null wins over
    an earlier null-stop_reason placeholder, but occurred_at still comes
    from the first row. A broken implementation would either keep the
    placeholder's output_tokens=1 or take occurred_at from the final row."""
    result = parse_session_detailed(_fixture("placeholder_then_final.jsonl"))
    output_tokens = [f.tokens for f in result.facts if f.token_kind == "output"]
    assert output_tokens == [500]
    assert result.conflicting_duplicate_groups == 1
    assert all(f.occurred_at_utc.isoformat().startswith("2026-06-01T00:00:00") for f in result.facts)
    assert result.duplicate_rows_skipped == 1


def test_all_null_stop_reason_falls_back_to_last_row_and_flags_conflict():
    """Spec: when no row in the group has a non-null stop_reason, the last
    row's usage is adopted. A broken implementation would keep the first
    row's usage (output=10) instead of the last (output=20), or fail to
    flag the conflict since usage genuinely differs."""
    result = parse_session_detailed(_fixture("all_null_stop_reason_conflict.jsonl"))
    output_tokens = [f.tokens for f in result.facts if f.token_kind == "output"]
    assert output_tokens == [20]
    assert result.conflicting_duplicate_groups == 1


def test_nonbilling_field_difference_is_not_a_conflict():
    """Spec: billing signature excludes non-billing usage fields (e.g.
    ``iterations``); two rows differing only there must not be flagged as
    conflicting. A broken implementation would compare the full usage dict
    verbatim and false-positive a conflict here."""
    result = parse_session_detailed(_fixture("nonbilling_field_diff.jsonl"))
    assert result.conflicting_duplicate_groups == 0
    assert sum(f.tokens for f in result.facts) == 100 + 50
    assert result.duplicate_rows_skipped == 1


def test_rows_missing_both_identities_are_emitted_individually():
    """Spec: rows with neither message.id nor requestId are never deduped
    against each other and are each counted in
    missing_dedup_identity_rows. A broken implementation would either drop
    one of the two rows or fail to increment the counter."""
    result = parse_session_detailed(_fixture("missing_both_ids.jsonl"))
    assert sum(f.tokens for f in result.facts) == (10 + 5) * 2
    assert result.missing_dedup_identity_rows == 2
    assert result.duplicate_rows_skipped == 0


def test_fallback_key_message_id_only_when_request_id_absent():
    """Spec: with requestId absent on both rows, message.id alone is the
    dedup key -- the two rows must still collapse to one. A broken
    implementation would treat rows without requestId as having no
    identity at all (double-counting them as missing_dedup_identity_rows)."""
    result = parse_session_detailed(_fixture("fallback_message_id_only.jsonl"))
    assert sum(f.tokens for f in result.facts) == 30 + 15
    assert result.duplicate_rows_skipped == 1
    assert result.missing_dedup_identity_rows == 0


def test_fallback_key_request_id_only_when_message_id_absent():
    """Spec: with message.id absent on both rows, requestId alone is the
    dedup key -- symmetric case to the message.id-only fallback above."""
    result = parse_session_detailed(_fixture("fallback_request_id_only.jsonl"))
    assert sum(f.tokens for f in result.facts) == 40 + 20
    assert result.duplicate_rows_skipped == 1
    assert result.missing_dedup_identity_rows == 0


def test_interleaved_keys_keep_first_seen_order_and_first_occurred_at():
    """Spec: facts order and occurred_at both follow *first-seen* position,
    not last. For an A, B, A row sequence, all of key A's facts must come
    before key B's in the output, and A's occurred_at must be the first
    A row's timestamp (00:00:00), not the repeated third row's
    (00:02:00). A broken implementation might sort by last-seen or use the
    last row's timestamp for the surviving fact."""
    result = parse_session_detailed(_fixture("interleaved_aba.jsonl"))
    timestamps_in_order = [f.occurred_at_utc.isoformat() for f in result.facts]
    assert timestamps_in_order, "expected at least one fact"
    # No fact should carry the second A row's timestamp (00:02:00) -- that
    # would mean occurred_at was taken from the last row, not the first.
    assert all(not ts.startswith("2026-06-01T00:02:00") for ts in timestamps_in_order)
    # Key A (00:00:00) must appear before key B (00:01:00) throughout.
    first_b_index = next(i for i, ts in enumerate(timestamps_in_order) if ts.startswith("2026-06-01T00:01:00"))
    assert all(ts.startswith("2026-06-01T00:00:00") for ts in timestamps_in_order[:first_b_index])
    assert result.duplicate_rows_skipped == 1


def test_dedup_is_scoped_per_file_not_across_files(tmp_path):
    """Spec: dedup is per-file. Two separate files that happen to share the
    same (id, requestId) key must each contribute their own row -- reading
    them both via read_claude_facts must yield 2 rows' worth of tokens
    and zero duplicate_rows_skipped, since no single *file* contains a
    duplicate. A broken implementation that deduped across the whole
    read_claude_facts() call would collapse this to 1 row's tokens and
    report 1 duplicate skipped."""
    projects = tmp_path / "projects"
    proj = projects / "-fixture-proj"
    proj.mkdir(parents=True)
    (proj / "a.jsonl").write_text(_fixture("perfile_a.jsonl").read_text())
    (proj / "b.jsonl").write_text(_fixture("perfile_b.jsonl").read_text())

    result = read_claude_facts(projects)
    assert sum(f.tokens for f in result.facts) == (5 + 5) * 2
    assert result.duplicate_rows_skipped == 0


def test_parse_session_facts_stays_a_two_tuple_matching_detailed():
    """Spec: parse_session_facts keeps its existing 2-tuple return shape
    (facts, malformed_events) for backward compatibility, and its values
    must match the corresponding fields on parse_session_detailed's
    result for the same file. A broken implementation might change the
    tuple's arity or have the two entry points disagree."""
    facts_tuple = parse_session_facts(_fixture("exact_duplicate.jsonl"))
    assert len(facts_tuple) == 2
    facts, malformed = facts_tuple

    detailed = parse_session_detailed(_fixture("exact_duplicate.jsonl"))
    assert facts == detailed.facts
    assert malformed == detailed.malformed_events


def test_measure_json_exposes_dedup_counters_and_producer_metadata(tmp_path, monkeypatch, capsys):
    """Spec: measure's data_quality must carry the 3 new counters, and the
    top level must carry producer_version (matching agent_cost.__version__)
    and accounting_basis == "agent-cost-raw-total/v2". A broken
    implementation would omit these keys or let producer_version drift
    from the installed package version."""
    claude_home = tmp_path / "claude_home"
    codex_home = tmp_path / "codex_home"
    claude_home.mkdir()
    codex_home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("AGENT_COST_CONFIG", raising=False)

    project_dir = claude_home / "projects" / "-fixture-proj"
    project_dir.mkdir(parents=True)
    (project_dir / "session.jsonl").write_text(_fixture("exact_duplicate.jsonl").read_text())

    rc = cli.main(["measure", "--session-id", "s-exact", "--format", "json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    dq = payload["data_quality"]
    assert dq["duplicate_rows_skipped"] == 2
    assert dq["conflicting_duplicate_groups"] == 0
    assert dq["missing_dedup_identity_rows"] == 0
    assert payload["producer_version"] == agent_cost.__version__
    assert payload["accounting_basis"] == "agent-cost-raw-total/v2"
