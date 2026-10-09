"""Acceptance tests for the claude-sonnet-5-5 rate entry (catalog_version 2026-09-29).

Primary sources: platform.claude.com/docs/en/about-claude/pricing (Sonnet 5.5
row, no cache_read footnote), anthropic.com/claude-sonnet-5-5 (launch
2026-09-28), Claude Code CHANGELOG 2.1.284 (Sonnet 5.5 is the default Sonnet).
"""

from datetime import datetime, timezone
from decimal import Decimal

from agent_cost.rates import load_rates

UTC = timezone.utc


def dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def test_claude_sonnet_5_5_resolves_with_pricing_page_values_on_or_after_launch():
    catalog = load_rates()
    for occurred_at in ("2026-09-28T00:00:00+00:00", "2026-12-01T00:00:00+00:00"):
        resolved, period = catalog.rate_for("claude-sonnet-5-5", dt(occurred_at))
        assert resolved == "claude-sonnet-5-5"
        assert period is not None
        assert period.rate_id == "claude-sonnet-5-5-launch-2026-09-28"
        assert period.values["input_nocache"] == Decimal("2.0")
        assert period.values["cache_read"] == Decimal("0.20")
        assert period.values["cache_write_5m"] == Decimal("2.50")
        assert period.values["cache_write_1h"] == Decimal("4.0")
        assert period.values["output"] == Decimal("10.0")


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


def test_claude_sonnet_5_5_cache_read_is_standard_multiplier_and_no_fast_mode():
    catalog = load_rates()
    entry = catalog.models["claude-sonnet-5-5"]
    assert entry.fast_multiplier == Decimal("1.0")
    period = entry.rates[0]
    assert period.values["cache_read"] == period.values["input_nocache"] * Decimal("0.1")
    assert period.values["cache_write_5m"] == period.values["input_nocache"] * Decimal("1.25")
    assert period.values["cache_write_1h"] == period.values["input_nocache"] * Decimal("2")


def test_catalog_version_and_sources_record_the_sonnet_5_5_primary_sources():
    catalog = load_rates()
    assert catalog.catalog_version == "2026-10-09"
    urls = {(s.get("url"), s.get("retrieved_at")) for s in catalog.sources}
    assert ("https://platform.claude.com/docs/en/about-claude/pricing", "2026-09-29") in urls
    assert ("https://www.anthropic.com/claude-sonnet-5-5", "2026-09-29") in urls
