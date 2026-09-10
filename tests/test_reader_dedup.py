"""Tests for Claude reader dedup (E0-a3).

Expected values are derived from the spec contract given to the tester role
(not from the implementation), summarized inline per test. Fixtures live in
``tests/fixtures/dedup/*.jsonl``. This file reflects the architect's 2nd
review round of the contract (see module docstring bullets below); a few
cases from the 1st round were reversed accordingly.

Dedup contract (spec, round 2):
  - key = (message.id, requestId), and dedup only happens when *both* are
    present as non-empty strings ("full pair"). A row where either side is
    missing, not a string (list/dict/number), or an empty string is never
    deduped against anything -- it is emitted individually, counted in
    ``missing_dedup_identity_rows``, and its Fact's ``source_quality`` is
    ``"identity_missing"``. Single-ID fallback (message.id-only or
    requestId-only) from round 1 is abolished.
  - Adopted row per key = the last row whose ``message.stop_reason`` is a
    non-empty string ("" does not count as complete); if none qualifies,
    the last row of the group. However, if any row *after* that candidate
    has a different billing signature, the adopted row becomes the group's
    last row regardless of stop_reason.
  - ``Fact.occurred_at_utc`` is the *adopted* row's timestamp (not the
    first row's). Only the *position* of a key's facts in the output
    ``facts`` list follows first-seen order.
  - ``duplicate_rows_skipped`` = sum over dedup-eligible keys of
    (row_count - 1). Rows counted in missing_dedup_identity_rows are not
    part of this sum.
  - Conflict detection (final contract, confirmed against real data)
    treats the *input-side* signature -- (model, mode, input_nocache,
    cache_read, cache_write_5m, cache_write_1h, cache_write_unknown) --
    separately from ``output_tokens``. A group where the input-side
    signature is identical across every row AND ``output_tokens`` is
    non-decreasing row-by-row is normal streaming progression, NOT a
    conflict. A group IS a conflict iff the input-side signature differs
    across any two rows, or ``output_tokens`` decreases (is non-monotonic)
    at any step in row order. The adopted-row rule above is unaffected by
    this distinction -- it is still evaluated purely from stop_reason and
    "does a later row disagree" (using this same conflict definition for
    "disagree").
  - Within the input-side signature, mode == "unknown" (no usage.speed
    key -- real streaming's non-final rows) is a wildcard: mixing
    "unknown" with exactly one concrete mode value is not, by itself, a
    conflict. Two *concrete* mode values differing (e.g. "fast" vs
    "normal") is still a conflict. The emitted Fact's mode is always the
    adopted row's own value.
  - measure's 3 counters in data_quality are summed only over the
    requested session ids within the report window, not globally across
    every fact read from disk.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import agent_cost
from agent_cost import cli
from agent_cost.aggregate import filter_facts, scope_dedup_units
from agent_cost.readers.claude import ClaudeDedupUnit, parse_session_detailed, parse_session_facts, read_claude_facts

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "dedup"


def _fixture(name: str) -> Path:
    return FIXTURES_DIR / name


def _write_jsonl(path: Path, events: list) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _sig_usage(**overrides) -> dict:
    """Baseline usage exercising every billing-signature dimension at once
    (input_nocache, cache_read, cache_write_5m, cache_write_1h, a
    cache_write_unknown leftover, and output), so a single-field override
    changes exactly one dimension."""
    base = {
        "input_tokens": 100,
        "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": 60,
        "cache_creation": {"ephemeral_5m_input_tokens": 30, "ephemeral_1h_input_tokens": 20},
        "output_tokens": 40,
    }
    base.update(overrides)
    return base


def _sig_event(ts: str, usage: dict, *, model: str = "claude-opus-4-8", stop_reason="end_turn") -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "sessionId": "s-sig",
        "requestId": "req-sig",
        "message": {"id": "msg-sig", "model": model, "stop_reason": stop_reason, "usage": usage},
    }


# ---------------------------------------------------------------------------
# Basic dedup / conflict / adopted-row behavior
# ---------------------------------------------------------------------------


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


def test_placeholder_row_is_superseded_and_occurred_at_is_the_adopted_rows():
    """Spec (final): the adopted row is the last non-empty-stop_reason row,
    and Fact.occurred_at_utc comes from *that adopted row*, not the first
    row. Here the null-stop_reason placeholder (00:00:00) is superseded
    by the end_turn row (00:00:05), so occurred_at must be 00:00:05. This
    fixture's input side is identical across both rows and output is
    non-decreasing (1 -> 500), which the final conflict contract treats
    as normal streaming progression -- NOT a conflict. A broken
    implementation stuck on round-1's "first row" occurred_at rule would
    report 00:00:00 instead; one still flagging any output_tokens
    difference as a conflict would report conflicting_duplicate_groups=1
    here."""
    result = parse_session_detailed(_fixture("placeholder_then_final.jsonl"))
    output_tokens = [f.tokens for f in result.facts if f.token_kind == "output"]
    assert output_tokens == [500]
    assert result.conflicting_duplicate_groups == 0
    assert all(f.occurred_at_utc.isoformat().startswith("2026-06-01T00:00:05") for f in result.facts)
    assert result.duplicate_rows_skipped == 1


def test_all_null_stop_reason_falls_back_to_last_row_and_flags_conflict():
    """Spec: when no row in the group has a non-empty stop_reason, the last
    row's usage is adopted. The two rows here differ on the input side
    (input_tokens 100 vs 200) with output also increasing (10 -> 20), so
    under the final conflict contract this is a conflict regardless of
    output direction (input-side difference alone is sufficient). A
    broken implementation would keep the first row's usage (output=10)
    instead of the last (output=20), or fail to flag the conflict since
    the input side genuinely differs."""
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


# ---------------------------------------------------------------------------
# Full-pair-only identity (round 2: single-ID fallback abolished)
# ---------------------------------------------------------------------------


def test_rows_missing_both_identities_are_emitted_individually():
    """Spec: rows with neither message.id nor requestId are never deduped
    against each other, are each counted in missing_dedup_identity_rows,
    and each carry source_quality == "identity_missing". A broken
    implementation would either drop one of the two rows or fail to
    increment the counter / set source_quality."""
    result = parse_session_detailed(_fixture("missing_both_ids.jsonl"))
    assert sum(f.tokens for f in result.facts) == (10 + 5) * 2
    assert result.missing_dedup_identity_rows == 2
    assert result.duplicate_rows_skipped == 0
    assert all(f.source_quality == "identity_missing" for f in result.facts)


def test_message_id_only_match_no_longer_merges_round2():
    """Spec (round 2): single-ID fallback is abolished -- two rows sharing
    only message.id (no requestId on either) must NOT collapse into one.
    Each is emitted individually and counted as missing_dedup_identity_rows.
    A broken implementation still running round-1's fallback rule would
    merge them into 1 row and report duplicate_rows_skipped=1, missing=0."""
    result = parse_session_detailed(_fixture("fallback_message_id_only.jsonl"))
    assert sum(f.tokens for f in result.facts) == (30 + 15) * 2
    assert result.duplicate_rows_skipped == 0
    assert result.missing_dedup_identity_rows == 2
    assert all(f.source_quality == "identity_missing" for f in result.facts)


def test_request_id_only_match_no_longer_merges_round2():
    """Spec (round 2): symmetric case -- two rows sharing only requestId
    (no message.id on either) must NOT collapse into one either."""
    result = parse_session_detailed(_fixture("fallback_request_id_only.jsonl"))
    assert sum(f.tokens for f in result.facts) == (40 + 20) * 2
    assert result.duplicate_rows_skipped == 0
    assert result.missing_dedup_identity_rows == 2
    assert all(f.source_quality == "identity_missing" for f in result.facts)


def test_f_single_id_match_does_not_merge_distinct_usage(tmp_path):
    """Spec item F: requestId absent on both rows, same message.id, but
    with genuinely different usage (10 vs 20 output tokens) -- token total
    must be 30 (both rows counted) and missing_dedup_identity_rows must be
    2. A broken implementation still merging on message.id alone would
    report a token total of only 20 (last-row-wins) or 10 (first-row-wins)
    and missing=0."""
    result = parse_session_detailed(_fixture("single_id_match_no_merge.jsonl"))
    output_tokens = [f.tokens for f in result.facts if f.token_kind == "output"]
    assert sorted(output_tokens) == [10, 20]
    assert sum(output_tokens) == 30
    assert result.missing_dedup_identity_rows == 2
    assert result.duplicate_rows_skipped == 0


def test_a_non_string_or_empty_identities_are_identity_missing_not_a_crash():
    """Spec item A: message.id as a list, requestId as a number, and
    message.id as an empty string must each be treated as "not a valid
    identity" (non-string or empty), never merged, and must not raise --
    the reader must not try to hash/compare a list or treat "" as present.
    A broken implementation would crash on the unhashable list id, or
    treat the empty string / number as a usable identity."""
    result = parse_session_detailed(_fixture("non_string_ids.jsonl"))
    assert result.malformed_events == 0
    assert result.missing_dedup_identity_rows == 3
    assert result.duplicate_rows_skipped == 0
    assert result.conflicting_duplicate_groups == 0
    assert all(f.source_quality == "identity_missing" for f in result.facts)
    assert sum(f.tokens for f in result.facts) == (10 + 1) + (20 + 2) + (30 + 3)


# ---------------------------------------------------------------------------
# stop_reason adoption edge cases (round 2 item B)
# ---------------------------------------------------------------------------


def test_b1_conflicting_row_after_completion_row_wins_adoption():
    """Spec item B(i): a non-empty-stop_reason row (input=10, output=10) is
    followed, within the same key, by a null-stop_reason row with a
    *different input-side signature* (input=20, output=20). Since a
    same-input/non-decreasing-output pair would NOT be a conflict under
    the final contract, this fixture deliberately differs on the input
    side (input_tokens) so the "later row disagrees" rule still applies:
    the adopted row becomes the group's last row regardless of
    stop_reason -- output must be 20, and conflict must be flagged. A
    broken implementation that always trusts the first non-null
    stop_reason row would report output=10 and might miss the conflict."""
    result = parse_session_detailed(_fixture("stop_reason_conflict_after_completion.jsonl"))
    output_tokens = [f.tokens for f in result.facts if f.token_kind == "output"]
    assert output_tokens == [20]
    assert result.conflicting_duplicate_groups == 1


def test_b2_empty_string_stop_reason_is_not_treated_as_complete():
    """Spec item B(ii): stop_reason == "" (empty string) must not count as
    a completed row. With every row in the group at stop_reason="", there
    is no adoption candidate, so the group falls back to the last row --
    same rule as the all-null case. The two rows also differ on the input
    side (input=10 vs 20, deliberately, so this stays a conflict under
    the final contract regardless of output direction) -- a broken
    implementation treating "" as a valid non-empty stop_reason would
    still land on the last row here (it's also the group's last
    completion) by coincidence, so the binding check is conflict
    detection: a false "no conflict" would mean the two rows' differing
    usage was never compared at all."""
    result = parse_session_detailed(_fixture("stop_reason_empty_string_not_complete.jsonl"))
    output_tokens = [f.tokens for f in result.facts if f.token_kind == "output"]
    assert output_tokens == [20]
    assert result.conflicting_duplicate_groups == 1


def test_b3_multiple_completion_rows_with_differing_signature_last_completion_wins():
    """Spec item B(iii): two rows both have a non-empty stop_reason but a
    different *input-side signature* (input=10 vs 20; output also
    increases 10 -> 20, which alone would not be a conflict, so the input
    difference is what makes this a conflict), and the candidate (last
    non-empty-stop_reason row) is also the group's last row -- so no
    "later disagreeing row" exists to override it. The adopted row must
    be that last completion row (output=20) per the plain candidate rule.
    A broken implementation might instead prefer the first completion row
    (output=10), or fail to flag the conflict by only comparing output."""
    result = parse_session_detailed(_fixture("multiple_completions_conflict.jsonl"))
    output_tokens = [f.tokens for f in result.facts if f.token_kind == "output"]
    assert output_tokens == [20]
    assert result.conflicting_duplicate_groups == 1


# ---------------------------------------------------------------------------
# Billing signature dimensions (round 2 item C; conflict rule finalized
# against real data -- see the module docstring's "Conflict detection"
# bullet). Only the 7 *input-side* dimensions unconditionally trigger a
# conflict when changed alone; output_tokens is judged by direction
# (non-decreasing = no conflict, decreasing = conflict), not by mere
# difference, so it is tested separately below.
# ---------------------------------------------------------------------------

INPUT_SIDE_SIGNATURE_DIMENSION_OVERRIDES = {
    "model": {},  # handled via the model= kwarg instead of a usage override
    "mode": {"speed": "fast"},
    "input_nocache": {"input_tokens": 200},
    "cache_read": {"cache_read_input_tokens": 99},
    "cache_write_5m": {
        "cache_creation_input_tokens": 61,
        "cache_creation": {"ephemeral_5m_input_tokens": 31, "ephemeral_1h_input_tokens": 20},
    },
    "cache_write_1h": {
        "cache_creation_input_tokens": 61,
        "cache_creation": {"ephemeral_5m_input_tokens": 30, "ephemeral_1h_input_tokens": 21},
    },
    # leftover = cache_creation_input_tokens - (5m + 1h): base leftover is
    # 60 - 50 = 10; bumping the total to 70 with the same 5m/1h breakdown
    # changes only the leftover (cache_write_unknown), nothing else.
    "cache_write_unknown": {"cache_creation_input_tokens": 70},
}


@pytest.mark.parametrize("dimension", sorted(INPUT_SIDE_SIGNATURE_DIMENSION_OVERRIDES))
def test_c_each_input_side_signature_dimension_alone_triggers_conflict(tmp_path, dimension):
    """Spec item C (final): the input-side signature is (model, mode,
    input_nocache, cache_read, cache_write_5m, cache_write_1h,
    cache_write_unknown). output_tokens is held constant (non-decreasing,
    trivially, since it's unchanged) across both rows here, so any
    conflict reported must come from the one input-side dimension changed
    -- proving each of these 7 dimensions is actually compared. The mode
    dimension is special-cased to start both rows from a *concrete* mode
    ("normal") rather than the default "unknown": mode's "unknown" (no
    usage.speed key) is a wildcard for conflict purposes (see the
    dedicated wildcard tests below), so an unknown-vs-"fast" pair would
    NOT conflict and would falsely pass this dimension. Using two
    concrete values ("normal" vs "fast") proves the dimension is actually
    compared. A broken implementation comparing only a subset of these
    fields (e.g. omitting mode, or computing cache_write_unknown's
    leftover wrong) would report conflicting_duplicate_groups == 0 for
    that one dimension."""
    base_usage = _sig_usage()
    changed_usage = _sig_usage()
    model = "claude-opus-4-8"
    if dimension == "model":
        model = "claude-sonnet-5"
    elif dimension == "mode":
        base_usage["speed"] = "normal"
        changed_usage.update(INPUT_SIDE_SIGNATURE_DIMENSION_OVERRIDES[dimension])
    else:
        changed_usage.update(INPUT_SIDE_SIGNATURE_DIMENSION_OVERRIDES[dimension])

    events = [
        _sig_event("2026-06-01T00:00:00Z", base_usage),
        _sig_event("2026-06-01T00:00:05Z", changed_usage, model=model),
    ]
    path = tmp_path / f"sig_{dimension}.jsonl"
    _write_jsonl(path, events)

    result = parse_session_detailed(path)
    assert result.conflicting_duplicate_groups == 1, dimension
    assert result.duplicate_rows_skipped == 1, dimension


# ---------------------------------------------------------------------------
# mode "unknown" wildcard (final review, confirmed against real data:
# streaming's non-final rows carry no usage.speed at all, so mode ==
# "unknown" there must not spuriously conflict with the final row's
# concrete mode)
# ---------------------------------------------------------------------------


def test_mode_unknown_is_a_wildcard_not_a_conflict():
    """Spec (mode wildcard, final): mode == "unknown" (no usage.speed key,
    matching real streaming's non-final rows) is a wildcard for conflict
    purposes -- a group mixing "unknown" with exactly one concrete mode
    value ("normal") is NOT a conflict, even though the two rows'
    signatures differ in that field. The emitted Fact's mode is the
    adopted row's concrete value ("normal"), never "unknown". A broken
    implementation treating "unknown" as just another distinct mode value
    would flag conflicting_duplicate_groups == 1 here."""
    result = parse_session_detailed(_fixture("mode_unknown_wildcard_no_conflict.jsonl"))
    assert result.conflicting_duplicate_groups == 0
    output_facts = [f for f in result.facts if f.token_kind == "output"]
    assert len(output_facts) == 1
    assert output_facts[0].tokens == 50
    assert all(f.mode == "normal" for f in result.facts)


def test_mode_concrete_values_differing_is_a_conflict():
    """Spec (mode wildcard, final): the "unknown" wildcard exemption only
    covers "unknown" itself -- two rows with two *different concrete*
    mode values ("fast" vs "normal"), with token counts and stop_reason
    otherwise identical/completed, is a genuine conflict. The adopted row
    is still the group's last row (mode "normal"). A broken
    implementation that widened the wildcard exemption to any mode
    mismatch (not just "unknown") would report
    conflicting_duplicate_groups == 0 here."""
    result = parse_session_detailed(_fixture("mode_concrete_values_differ_conflict.jsonl"))
    assert result.conflicting_duplicate_groups == 1
    assert all(f.mode == "normal" for f in result.facts)


def test_c_output_increase_with_identical_input_side_is_not_a_conflict(tmp_path):
    """Spec item C (final): with the input-side signature identical across
    both rows, a non-decreasing output_tokens change (40 -> 80) is normal
    streaming progression, not a conflict. A broken implementation still
    treating any output_tokens difference as a distinct signature would
    report conflicting_duplicate_groups == 1 here."""
    base_usage = _sig_usage()
    increased_usage = _sig_usage(output_tokens=80)

    events = [
        _sig_event("2026-06-01T00:00:00Z", base_usage),
        _sig_event("2026-06-01T00:00:05Z", increased_usage),
    ]
    path = tmp_path / "sig_output_increase.jsonl"
    _write_jsonl(path, events)

    result = parse_session_detailed(path)
    assert result.conflicting_duplicate_groups == 0
    output_tokens = [f.tokens for f in result.facts if f.token_kind == "output"]
    assert output_tokens == [80]


def test_c_output_decrease_with_identical_input_side_is_a_conflict(tmp_path):
    """Spec item C (final): with the input-side signature identical, a
    *decreasing* output_tokens change (40 -> 10) is not normal streaming
    progression and must be flagged as a conflict, even though nothing on
    the input side differs. A broken implementation that only compares
    input-side fields (never checking output monotonicity) would report
    conflicting_duplicate_groups == 0 here."""
    base_usage = _sig_usage()
    decreased_usage = _sig_usage(output_tokens=10)

    events = [
        _sig_event("2026-06-01T00:00:00Z", base_usage),
        _sig_event("2026-06-01T00:00:05Z", decreased_usage),
    ]
    path = tmp_path / "sig_output_decrease.jsonl"
    _write_jsonl(path, events)

    result = parse_session_detailed(path)
    assert result.conflicting_duplicate_groups == 1
    output_tokens = [f.tokens for f in result.facts if f.token_kind == "output"]
    assert output_tokens == [10]


def test_output_non_monotonic_across_three_rows_is_a_conflict_last_row_adopted():
    """Spec (final): output monotonicity is checked across the full row
    sequence in order, not just first-vs-last -- 5 -> 20 -> 15 has a
    decrease at the last step even though input-side signature never
    changes and the first-to-last direction (5 -> 15) is an increase, so
    this must be flagged as a conflict. The adopted row remains the
    group's last row (output=15) per the ordinary candidate rule (all
    three rows are end_turn, so the last is the candidate, and there is
    no later row to override it). A broken implementation checking only
    the endpoints (5 vs 15) or only consecutive-pair sums would miss the
    dip and report no conflict."""
    result = parse_session_detailed(_fixture("non_monotonic_output_three_rows.jsonl"))
    output_tokens = [f.tokens for f in result.facts if f.token_kind == "output"]
    assert output_tokens == [15]
    assert result.conflicting_duplicate_groups == 1
    assert result.duplicate_rows_skipped == 2


# ---------------------------------------------------------------------------
# Ordering / occurred_at / per-file scoping / compatibility
# ---------------------------------------------------------------------------


def test_interleaved_keys_keep_first_seen_facts_order_with_adopted_occurred_at():
    """Spec: facts *order* still follows first-seen key position (A's
    facts precede B's, since A appears first in the file), but each key's
    occurred_at is now its *adopted* row's timestamp. Key A's group is
    rows at 00:00:00 and 00:02:00 (both end_turn, same signature) -> the
    candidate is the last one (00:02:00), and there's no later
    disagreeing row, so A's facts carry occurred_at=00:02:00 even though
    they are positioned before B's facts (which carry 00:01:00). A broken
    implementation reusing round-1's "occurred_at = first row" rule would
    report 00:00:00 for A instead."""
    result = parse_session_detailed(_fixture("interleaved_aba.jsonl"))
    # Key A's usage is (input=10, output=1); key B's is (input=20, output=2)
    # -- these values are unique enough to identify which fact belongs to
    # which key without relying on occurred_at.
    a_indices = [i for i, f in enumerate(result.facts) if f.tokens in (10, 1)]
    b_indices = [i for i, f in enumerate(result.facts) if f.tokens in (20, 2)]
    assert len(a_indices) == 2
    assert len(b_indices) == 2
    assert max(a_indices) < min(b_indices), "key A's facts must precede key B's (first-seen order)"

    a_facts = [result.facts[i] for i in a_indices]
    b_facts = [result.facts[i] for i in b_indices]
    assert all(f.occurred_at_utc.isoformat().startswith("2026-06-01T00:02:00") for f in a_facts)
    assert all(f.occurred_at_utc.isoformat().startswith("2026-06-01T00:01:00") for f in b_facts)
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


# ---------------------------------------------------------------------------
# Month boundary (round 2 item D)
# ---------------------------------------------------------------------------


def test_d_adopted_timestamp_crossing_month_boundary_drives_window_filtering():
    """Spec item D: a null-stop_reason placeholder at 2026-05-31T23:59:59Z
    (output=1) is superseded by an end_turn row at 2026-06-01T00:00:01Z
    (output=20). Because occurred_at is the *adopted* row's timestamp, the
    surviving output fact (20 tokens) is attributed to June: it must
    appear in a --since 2026-06-01 window and must NOT appear in a
    --until 2026-05-31 window. A broken implementation using the first
    row's (May) timestamp would flip both of these window checks."""
    result = parse_session_detailed(_fixture("month_boundary_placeholder_then_final.jsonl"))
    output_facts = [f for f in result.facts if f.token_kind == "output"]
    assert len(output_facts) == 1
    assert output_facts[0].tokens == 20

    since_june = datetime(2026, 6, 1, tzinfo=timezone.utc)
    until_may = datetime(2026, 5, 31, tzinfo=timezone.utc)
    assert list(filter_facts(result.facts, since_utc=since_june)) != []
    assert list(filter_facts(result.facts, until_utc=until_may)) == []


# ---------------------------------------------------------------------------
# scope_dedup_units window semantics (final review, non-blocker follow-up)
# ---------------------------------------------------------------------------


def test_scope_dedup_units_half_open_window_includes_since_excludes_until():
    """Spec: scope_dedup_units re-scopes the 3 dedup counters to the
    half-open window [since_utc, until_utc), the same semantics
    filter_facts uses for Fact.occurred_at_utc. A unit whose
    occurred_at_utc equals `since` exactly must be included; a unit whose
    occurred_at_utc equals `until` exactly must be excluded. A broken
    implementation using an inclusive `until` bound (occurred_at <=
    until) would count the until-boundary unit too, inflating all 3
    returned counters."""
    since = datetime(2026, 6, 1, tzinfo=timezone.utc)
    until = datetime(2026, 6, 2, tzinfo=timezone.utc)

    unit_at_since = ClaudeDedupUnit(
        session_id="s1",
        occurred_at_utc=since,
        duplicate_rows_skipped=2,
        conflicting=True,
        missing_identity=False,
    )
    unit_at_until = ClaudeDedupUnit(
        session_id="s1",
        occurred_at_utc=until,
        duplicate_rows_skipped=5,
        conflicting=False,
        missing_identity=True,
    )

    duplicate_rows_skipped, conflicting_duplicate_groups, missing_dedup_identity_rows = scope_dedup_units(
        [unit_at_since, unit_at_until],
        since_utc=since,
        until_utc=until,
    )
    assert duplicate_rows_skipped == 2
    assert conflicting_duplicate_groups == 1
    assert missing_dedup_identity_rows == 0


# ---------------------------------------------------------------------------
# measure/v1 contract additions
# ---------------------------------------------------------------------------


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


def test_e_measure_dedup_counters_do_not_leak_across_unrequested_sessions(tmp_path, monkeypatch, capsys):
    """Spec item E: the 3 dedup counters in measure's data_quality must be
    scoped to the *requested* session id(s), not summed globally over
    every fact read from disk. A fixture has a clean "requested" session
    and a dirty "unrequested" session (1 conflicting duplicate group,
    made a conflict via an input-side difference so it stays a conflict
    under the final contract regardless of output direction, + 1
    identity-missing row). Querying --session-id requested must report
    all 3 counters as 0; querying --session-id unrequested must report
    them non-zero. A broken implementation reusing the
    malformed_events/skipped_files pattern (summed once over the whole
    read, before session filtering) would leak the dirty session's counts
    into the clean session's report."""
    claude_home = tmp_path / "claude_home"
    codex_home = tmp_path / "codex_home"
    claude_home.mkdir()
    codex_home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("AGENT_COST_CONFIG", raising=False)

    project_dir = claude_home / "projects" / "-fixture-proj"
    project_dir.mkdir(parents=True)
    (project_dir / "session.jsonl").write_text(_fixture("measure_global_leakage.jsonl").read_text())

    rc = cli.main(["measure", "--session-id", "requested", "--format", "json"])
    assert rc == 0
    clean_dq = json.loads(capsys.readouterr().out)["data_quality"]
    assert clean_dq["duplicate_rows_skipped"] == 0
    assert clean_dq["conflicting_duplicate_groups"] == 0
    assert clean_dq["missing_dedup_identity_rows"] == 0

    rc = cli.main(["measure", "--session-id", "unrequested", "--format", "json"])
    assert rc == 0
    dirty_dq = json.loads(capsys.readouterr().out)["data_quality"]
    assert dirty_dq["conflicting_duplicate_groups"] == 1
    assert dirty_dq["missing_dedup_identity_rows"] == 1
    assert dirty_dq["duplicate_rows_skipped"] == 1
