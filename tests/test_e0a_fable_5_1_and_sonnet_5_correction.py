"""Acceptance tests for E0-a (claude-fable-5-1 rate entry) and E0-a2
(claude-sonnet-5 pricing correction), written against the current state of
agent_cost/rates.json / rates.py / aggregate.py in this worktree.

Each test cites the acceptance-criterion number (A1-A7, B8-B10) it covers,
as specified in the task handoff -- not derived from reading a diff.
"""

from datetime import datetime, timezone
from decimal import Decimal

from agent_cost.aggregate import price_fact
from agent_cost.facts import Fact
from agent_cost.rates import load_rates

UTC = timezone.utc


def dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def _fact(model_key, token_kind, tokens, occurred_at="2026-09-05T00:00:00+00:00"):
    return Fact(
        occurred_at_utc=dt(occurred_at),
        agent="claude",
        session_id="s1",
        model_raw=model_key,
        model_key=model_key,
        token_kind=token_kind,
        tokens=tokens,
    )


# --- A. claude-fable-5-1 (E0-a) ---------------------------------------------


def test_a1_claude_fable_5_1_resolves_with_expected_values_on_or_after_effective_from():
    # AC1: resolves at/after 2026-08-30T00:00:00+00:00 with the specified values.
    catalog = load_rates()
    for occurred_at in ("2026-08-30T00:00:00+00:00", "2026-12-01T00:00:00+00:00"):
        resolved, period = catalog.rate_for("claude-fable-5-1", dt(occurred_at))
        assert resolved == "claude-fable-5-1"
        assert period is not None
        assert period.values["input_nocache"] == Decimal("10.0")
        assert period.values["cache_read"] == Decimal("0.25")
        assert period.values["cache_write_5m"] == Decimal("12.5")
        assert period.values["cache_write_1h"] == Decimal("20.0")
        assert period.values["output"] == Decimal("50.0")


def test_a2_claude_fable_5_1_is_unpriced_before_effective_from_boundary():
    # AC2: strictly before 2026-08-30T00:00:00+00:00 -> period is None; exact
    # boundary (00:00:00) is priced (checked in test_a1).
    catalog = load_rates()
    resolved, before = catalog.rate_for("claude-fable-5-1", dt("2026-08-29T23:59:59+00:00"))
    assert resolved == "claude-fable-5-1"
    assert before is None


def test_a3_claude_fable_5_1_cache_write_kinds_price_as_expected():
    # AC3: cache_write_5m and cache_write_1h both price with status "priced";
    # cache_write_unknown prices with status "lower_bound" (not "unpriced",
    # not "priced") using the 5m rate for the cost math.
    catalog = load_rates()

    cost_5m, status_5m, _ = price_fact(
        catalog, _fact("claude-fable-5-1", "cache_write_5m", 1_000_000)
    )
    assert status_5m == "priced"
    assert cost_5m == Decimal("12.5")

    cost_1h, status_1h, _ = price_fact(
        catalog, _fact("claude-fable-5-1", "cache_write_1h", 1_000_000)
    )
    assert status_1h == "priced"
    assert cost_1h == Decimal("20.0")

    cost_unknown, status_unknown, _ = price_fact(
        catalog, _fact("claude-fable-5-1", "cache_write_unknown", 1_000_000)
    )
    assert status_unknown == "lower_bound"
    assert status_unknown not in ("unpriced", "priced")
    assert cost_unknown == Decimal("12.5")  # tokens/1e6 * cache_write_5m rate (12.5)


def test_a4_claude_fable_5_without_suffix_is_unaffected():
    # AC4: claude-fable-5 (no "-1" suffix) keeps cache_read == 1.00, both
    # before and after 2026-08-30 -- confirms E0-a did not touch this entry.
    catalog = load_rates()
    for occurred_at in ("2026-07-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"):
        resolved, period = catalog.rate_for("claude-fable-5", dt(occurred_at))
        assert resolved == "claude-fable-5"
        assert period is not None
        assert period.values["cache_read"] == Decimal("1.00")


def test_a5_unknown_model_still_returns_none_none():
    # AC5: E0-a must not have broken the unknown-model path.
    catalog = load_rates()
    resolved, period = catalog.rate_for("totally-unknown-model-xyz", dt("2026-09-05T00:00:00+00:00"))
    assert resolved is None
    assert period is None


def test_a6_sources_contains_recent_platform_claude_com_pricing_entry():
    # AC6: sources array has an entry whose url mentions platform.claude.com
    # and pricing, retrieved on/after 2026-09-09.
    catalog = load_rates()
    matches = [
        s
        for s in catalog.sources
        if "platform.claude.com" in s.get("url", "") and "pricing" in s.get("url", "")
    ]
    assert matches, catalog.sources
    assert any(s.get("retrieved_at", "") >= "2026-09-09" for s in matches)


def test_a7_catalog_loads_and_has_non_empty_sha256():
    # AC7: load_rates() succeeds without raising, sha256 is a non-empty hex string.
    catalog = load_rates()
    assert catalog.sha256
    assert len(catalog.sha256) == 64
    int(catalog.sha256, 16)  # raises ValueError if not valid hex


# --- B. claude-sonnet-5 pricing correction (E0-a2) --------------------------


def test_b8_claude_sonnet_5_price_is_continuous_across_2026_09_01_boundary():
    # AC8: no jump to 3.0/0.30/3.75/6.0/15.0 anywhere around 2026-09-01;
    # the $2/$0.20/$2.50/$4/$10 table holds just before, exactly at, and well
    # after the boundary.
    catalog = load_rates()
    expected = {
        "input_nocache": Decimal("2.0"),
        "cache_read": Decimal("0.20"),
        "cache_write_5m": Decimal("2.50"),
        "cache_write_1h": Decimal("4.0"),
        "output": Decimal("10.0"),
    }
    for occurred_at in (
        "2026-08-31T23:00:00+00:00",
        "2026-09-01T00:00:00+00:00",
        "2026-12-01T00:00:00+00:00",
    ):
        resolved, period = catalog.rate_for("claude-sonnet-5", dt(occurred_at))
        assert resolved == "claude-sonnet-5"
        assert period is not None
        for field, value in expected.items():
            assert period.values[field] == value, (occurred_at, field)


def test_b9_no_claude_sonnet_5_period_with_the_erroneous_3_0_rate_on_or_after_sep_1():
    # AC9: confirm the erroneous higher-priced period is gone entirely (not
    # just shadowed by period ordering) by inspecting every stored period.
    catalog = load_rates()
    entry = catalog.models["claude-sonnet-5"]
    sep_1 = dt("2026-09-01T00:00:00+00:00")
    offending = [
        p
        for p in entry.rates
        if p.effective_from >= sep_1 and p.values["input_nocache"] == Decimal("3.0")
    ]
    assert offending == []


def test_b10_price_fact_matches_hand_computed_totals_from_the_2_dollar_table():
    # AC10: recompute-from-raw-tokens check. Sum price_fact's output for a
    # synthetic set of facts and compare against a hand-computed total using
    # the $2/$0.20/$2.50/$4/$10 per-MTok table (not any external ledger figure).
    catalog = load_rates()
    token_counts = {
        "input_nocache": 1_000_000,
        "cache_read": 2_000_000,
        "cache_write_5m": 500_000,
        "cache_write_1h": 250_000,
        "output": 100_000,
    }
    per_mtok_rate = {
        "input_nocache": Decimal("2.0"),
        "cache_read": Decimal("0.20"),
        "cache_write_5m": Decimal("2.50"),
        "cache_write_1h": Decimal("4.0"),
        "output": Decimal("10.0"),
    }

    expected_total = Decimal("0")
    actual_total = Decimal("0")
    for token_kind, tokens in token_counts.items():
        expected_total += (Decimal(tokens) / Decimal(1_000_000)) * per_mtok_rate[token_kind]
        cost, status, _ = price_fact(
            catalog, _fact("claude-sonnet-5", token_kind, tokens, occurred_at="2026-09-05T00:00:00+00:00")
        )
        assert status == "priced"
        actual_total += cost

    assert actual_total == expected_total
