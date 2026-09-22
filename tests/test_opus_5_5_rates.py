"""Acceptance tests for the claude-opus-5-5 rate entry and the claude-opus-5
effective_from correction (catalog_version 2026-09-23).

Primary sources: platform.claude.com/docs/en/about-claude/pricing (Opus 5.5
row + footnote 2), anthropic.com/claude-opus-5-5 (launch 2026-09-22),
anthropic.com/news/claude-opus-5 (launch 2026-07-24).
"""

from datetime import datetime, timezone
from decimal import Decimal

from agent_cost.rates import load_rates

UTC = timezone.utc


def dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def test_claude_opus_5_5_resolves_with_pricing_page_values_on_or_after_launch():
    catalog = load_rates()
    for occurred_at in ("2026-09-22T00:00:00+00:00", "2026-12-01T00:00:00+00:00"):
        resolved, period = catalog.rate_for("claude-opus-5-5", dt(occurred_at))
        assert resolved == "claude-opus-5-5"
        assert period is not None
        assert period.rate_id == "claude-opus-5-5-launch-2026-09-22"
        assert period.values["input_nocache"] == Decimal("4.0")
        assert period.values["cache_read"] == Decimal("0.20")
        assert period.values["cache_write_5m"] == Decimal("5.0")
        assert period.values["cache_write_1h"] == Decimal("8.0")
        assert period.values["output"] == Decimal("20.0")


def test_claude_opus_5_5_is_unpriced_before_launch_date():
    catalog = load_rates()
    resolved, before = catalog.rate_for("claude-opus-5-5", dt("2026-09-21T23:59:59+00:00"))
    assert resolved == "claude-opus-5-5"
    assert before is None


def test_claude_opus_5_5_cache_read_is_0_05x_base_input_not_standard_0_1x():
    # Footnote 2 on the pricing page: Opus 5.5 cache hits are 0.05x base input.
    catalog = load_rates()
    _, period = catalog.rate_for("claude-opus-5-5", dt("2026-10-01T00:00:00+00:00"))
    assert period.values["cache_read"] == period.values["input_nocache"] * Decimal("0.05")
    assert period.values["cache_read"] != period.values["input_nocache"] * Decimal("0.1")


def test_claude_opus_5_5_fast_multiplier_2x():
    catalog = load_rates()
    assert catalog.models["claude-opus-5-5"].fast_multiplier == Decimal("2.0")


def test_claude_opus_5_5_is_not_an_alias_of_claude_opus_5():
    catalog = load_rates()
    assert "claude-opus-5-5" not in catalog.models["claude-opus-5"].aliases
    assert catalog.models["claude-opus-5-5"].aliases == ()


def test_claude_opus_5_effective_from_is_the_official_launch_date():
    catalog = load_rates()
    resolved, before = catalog.rate_for("claude-opus-5", dt("2026-07-23T23:59:59+00:00"))
    assert resolved == "claude-opus-5"
    assert before is None
    _, on_launch = catalog.rate_for("claude-opus-5", dt("2026-07-24T00:00:00+00:00"))
    assert on_launch is not None
    assert on_launch.rate_id == "claude-opus-5-launch-2026-07-24"
    assert on_launch.values["input_nocache"] == Decimal("5.0")
    assert on_launch.values["cache_read"] == Decimal("0.50")
    assert on_launch.values["cache_write_5m"] == Decimal("6.25")
    assert on_launch.values["cache_write_1h"] == Decimal("10.0")
    assert on_launch.values["output"] == Decimal("25.0")
    assert on_launch.effective_until is None
