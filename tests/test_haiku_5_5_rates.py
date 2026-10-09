"""Acceptance tests for the claude-haiku-5-5 rate entry (catalog_version 2026-10-09).

Primary sources: platform.claude.com/docs/en/about-claude/pricing (Haiku 5.5
rows for prompts up to / over 100,000 tokens, no cache_read footnote; Long
context pricing section), platform.claude.com/docs/en/models/haiku-5-5/overview
(released 2026-10-07), Claude Code CHANGELOG 2.1.293 (Haiku 5.5 is the default
Haiku).
"""

from datetime import datetime, timezone
from decimal import Decimal

from agent_cost.rates import load_rates

UTC = timezone.utc

BASE = {
    "input_nocache": Decimal("0.10"),
    "cache_read": Decimal("0.01"),
    "cache_write_5m": Decimal("0.125"),
    "cache_write_1h": Decimal("0.20"),
    "output": Decimal("0.50"),
}
OVER_100K = {
    "input_nocache": Decimal("0.50"),
    "cache_read": Decimal("0.05"),
    "cache_write_5m": Decimal("0.625"),
    "cache_write_1h": Decimal("1.0"),
    "output": Decimal("2.50"),
}


def dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def test_claude_haiku_5_5_resolves_with_tiered_pricing_page_values_on_or_after_release():
    catalog = load_rates()
    for occurred_at in ("2026-10-07T00:00:00+00:00", "2026-12-01T00:00:00+00:00"):
        resolved, period = catalog.rate_for("claude-haiku-5-5", dt(occurred_at))
        assert resolved == "claude-haiku-5-5"
        assert period is not None
        assert period.rate_id == "claude-haiku-5-5-launch-2026-10-07"
        assert period.values == BASE
        assert len(period.prompt_tiers) == 1
        tier = period.prompt_tiers[0]
        assert tier.prompt_tokens_over == 100000
        assert tier.values == OVER_100K


def test_claude_haiku_5_5_is_unpriced_before_release_date():
    catalog = load_rates()
    resolved, before = catalog.rate_for("claude-haiku-5-5", dt("2026-10-06T23:59:59+00:00"))
    assert resolved == "claude-haiku-5-5"
    assert before is None


def test_claude_haiku_5_5_is_its_own_entry_not_an_alias_of_haiku_4_5():
    catalog = load_rates()
    assert "claude-haiku-5-5" in catalog.models
    assert catalog.models["claude-haiku-5-5"].aliases == ()
    assert "claude-haiku-5-5" not in catalog.models["claude-haiku-4-5"].aliases
    _, h45 = catalog.rate_for("claude-haiku-4-5", dt("2026-10-07T00:00:00+00:00"))
    _, h55 = catalog.rate_for("claude-haiku-5-5", dt("2026-10-07T00:00:00+00:00"))
    assert h45.rate_id != h55.rate_id
    assert h45.values != h55.values


def test_claude_haiku_5_5_cache_multipliers_are_standard_in_every_tier_and_no_fast_mode():
    catalog = load_rates()
    entry = catalog.models["claude-haiku-5-5"]
    assert entry.fast_multiplier == Decimal("1.0")
    period = entry.rates[0]
    for values in (period.values, period.prompt_tiers[0].values):
        assert values["cache_read"] == values["input_nocache"] * Decimal("0.1")
        assert values["cache_write_5m"] == values["input_nocache"] * Decimal("1.25")
        assert values["cache_write_1h"] == values["input_nocache"] * Decimal("2.0")


def test_catalog_version_schema_version_and_sources_record_the_haiku_5_5_primary_sources():
    catalog = load_rates()
    assert catalog.catalog_version == "2026-10-09"
    assert catalog.schema_version == "2"
    urls = {(s.get("url"), s.get("retrieved_at")) for s in catalog.sources}
    assert ("https://platform.claude.com/docs/en/about-claude/pricing", "2026-10-09") in urls
    assert ("https://platform.claude.com/docs/en/models/haiku-5-5/overview", "2026-10-09") in urls
    assert ("https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md", "2026-10-09") in urls
