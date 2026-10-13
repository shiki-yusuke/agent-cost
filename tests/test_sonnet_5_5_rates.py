"""Acceptance tests for the claude-sonnet-5-5 rate entry (catalog_version 2026-10-13).

Primary sources: platform.claude.com/docs/en/about-claude/pricing (Sonnet 5.5
row; retrieved 2026-09-29 with no cache_read footnote, retrieved 2026-10-13 at
$0.10 cache_read with footnote 2 = 0.05x), anthropic.com/claude-sonnet-5-5
(launch 2026-09-28), anthropic.com/claude-haiku-5-5 (cache_read cut 'starting
today', 2026-10-07), Claude Code CHANGELOG 2.1.284 (Sonnet 5.5 is the default
Sonnet) and 2.1.296 (Sonnet 5.5 cache reads priced at $0.10).
"""

from datetime import datetime, timezone
from decimal import Decimal

from agent_cost.rates import load_rates

UTC = timezone.utc


def dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


LAUNCH = "claude-sonnet-5-5-launch-2026-09-28"
CACHE_READ_CUT = "claude-sonnet-5-5-cache-read-cut-2026-10-07"


def _assert_unchanged_values(period):
    assert period.values["input_nocache"] == Decimal("2.0")
    assert period.values["cache_write_5m"] == Decimal("2.50")
    assert period.values["cache_write_1h"] == Decimal("4.0")
    assert period.values["output"] == Decimal("10.0")


def test_claude_sonnet_5_5_resolves_to_launch_period_before_cache_read_cut():
    catalog = load_rates()
    for occurred_at in ("2026-09-28T00:00:00+00:00", "2026-10-06T23:59:59+00:00"):
        resolved, period = catalog.rate_for("claude-sonnet-5-5", dt(occurred_at))
        assert resolved == "claude-sonnet-5-5"
        assert period is not None
        assert period.rate_id == LAUNCH
        assert period.values["cache_read"] == Decimal("0.20")
        _assert_unchanged_values(period)


def test_claude_sonnet_5_5_resolves_to_cache_read_cut_period_from_2026_10_07():
    catalog = load_rates()
    for occurred_at in ("2026-10-07T00:00:00+00:00", "2026-12-01T00:00:00+00:00"):
        resolved, period = catalog.rate_for("claude-sonnet-5-5", dt(occurred_at))
        assert resolved == "claude-sonnet-5-5"
        assert period is not None
        assert period.rate_id == CACHE_READ_CUT
        assert period.values["cache_read"] == Decimal("0.10")
        _assert_unchanged_values(period)


def test_claude_sonnet_5_5_cache_read_cut_boundary():
    catalog = load_rates()
    _, before = catalog.rate_for("claude-sonnet-5-5", dt("2026-10-06T23:59:59+00:00"))
    _, at = catalog.rate_for("claude-sonnet-5-5", dt("2026-10-07T00:00:00+00:00"))
    assert before.rate_id == LAUNCH
    assert before.values["cache_read"] == Decimal("0.20")
    assert at.rate_id == CACHE_READ_CUT
    assert at.values["cache_read"] == Decimal("0.10")


def test_claude_sonnet_5_5_is_unpriced_before_launch_date():
    catalog = load_rates()
    resolved, before = catalog.rate_for("claude-sonnet-5-5", dt("2026-09-27T23:59:59+00:00"))
    assert resolved == "claude-sonnet-5-5"
    assert before is None


def test_claude_sonnet_5_5_is_its_own_entry_not_an_alias_of_sonnet_5():
    # Same values as claude-sonnet-5 today, but the pricing page lists the rows
    # independently; a future change to one must not silently apply to the other.
    catalog = load_rates()
    assert "claude-sonnet-5-5" in catalog.models
    assert catalog.models["claude-sonnet-5-5"].aliases == ()
    assert "claude-sonnet-5-5" not in catalog.models["claude-sonnet-5"].aliases
    _, s5 = catalog.rate_for("claude-sonnet-5", dt("2026-09-28T00:00:00+00:00"))
    _, s55 = catalog.rate_for("claude-sonnet-5-5", dt("2026-09-28T00:00:00+00:00"))
    assert s5.rate_id != s55.rate_id
    assert s5.values == s55.values


def test_claude_sonnet_5_cache_read_is_not_cut_with_sonnet_5_5():
    # The 2026-10-07 cut applies to Sonnet 5.5 only (pricing-page footnote 2
    # names Opus 5.5 and Sonnet 5.5); claude-sonnet-5 keeps cache_read 0.20.
    catalog = load_rates()
    for occurred_at in ("2026-10-07T00:00:00+00:00", "2026-12-01T00:00:00+00:00"):
        _, s5 = catalog.rate_for("claude-sonnet-5", dt(occurred_at))
        _, s55 = catalog.rate_for("claude-sonnet-5-5", dt(occurred_at))
        assert s5.values["cache_read"] == Decimal("0.20")
        assert s55.values["cache_read"] == Decimal("0.10")
        assert s5.values != s55.values


def test_claude_sonnet_5_5_cache_read_multiplier_per_period_and_no_fast_mode():
    catalog = load_rates()
    entry = catalog.models["claude-sonnet-5-5"]
    assert entry.fast_multiplier == Decimal("1.0")
    periods = {p.rate_id: p for p in entry.rates}
    assert set(periods) == {LAUNCH, CACHE_READ_CUT}
    launch = periods[LAUNCH]
    cut = periods[CACHE_READ_CUT]
    assert launch.values["cache_read"] == launch.values["input_nocache"] * Decimal("0.1")
    assert cut.values["cache_read"] == cut.values["input_nocache"] * Decimal("0.05")
    for period in (launch, cut):
        assert period.values["cache_write_5m"] == period.values["input_nocache"] * Decimal("1.25")
        assert period.values["cache_write_1h"] == period.values["input_nocache"] * Decimal("2")


def test_catalog_version_and_sources_record_the_sonnet_5_5_primary_sources():
    catalog = load_rates()
    assert catalog.catalog_version == "2026-10-13"
    urls = {(s.get("url"), s.get("retrieved_at")) for s in catalog.sources}
    assert ("https://platform.claude.com/docs/en/about-claude/pricing", "2026-09-29") in urls
    assert ("https://www.anthropic.com/claude-sonnet-5-5", "2026-09-29") in urls
    assert ("https://platform.claude.com/docs/en/about-claude/pricing", "2026-10-13") in urls
    assert ("https://www.anthropic.com/claude-haiku-5-5", "2026-10-13") in urls
