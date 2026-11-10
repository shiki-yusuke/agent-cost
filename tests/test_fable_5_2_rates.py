"""Acceptance tests for claude-fable-5-2 / claude-mythos-5-2 (catalog_version 2026-11-10).

Source: platform.claude.com/docs/en/about-claude/pricing (retrieved 2026-11-10) and the
announcement anthropic.com/claude-fable-and-mythos-5-2 (published 2026-11-09).
"""

from datetime import datetime
from decimal import Decimal

from agent_cost.rates import load_rates

USD = {
    "input_nocache": "8.0",
    "cache_read": "0.20",
    "cache_write_5m": "10.0",
    "cache_write_1h": "16.0",
    "output": "40.0",
}
MODELS = ("claude-fable-5-2", "claude-mythos-5-2")


def dt(s):
    return datetime.fromisoformat(s)


def test_fable_and_mythos_5_2_resolve_with_pricing_page_values_from_launch():
    catalog = load_rates()
    for model in MODELS:
        for occurred_at in ("2026-11-09T00:00:00+00:00", "2026-11-10T03:00:00+00:00", "2027-06-01T00:00:00+00:00"):
            resolved, period = catalog.rate_for(model, dt(occurred_at))
            assert resolved == model
            assert period is not None, (model, occurred_at)
            assert period.rate_id == f"{model}-launch-2026-11-09"
            assert period.effective_until is None
            assert period.prompt_tiers == ()
            for kind, value in USD.items():
                assert period.values[kind] == Decimal(value), (model, kind)


def test_fable_and_mythos_5_2_are_unpriced_before_launch():
    catalog = load_rates()
    for model in MODELS:
        resolved, period = catalog.rate_for(model, dt("2026-11-08T23:59:59+00:00"))
        assert resolved == model
        assert period is None


def test_cache_read_is_0_025x_and_fast_mode_is_2x():
    catalog = load_rates()
    for model in MODELS:
        entry = catalog.models[model]
        assert entry.aliases == ()
        assert entry.fast_multiplier == Decimal("2.0")  # fast mode $16 / $80
        assert len(entry.rates) == 1
        _, period = catalog.rate_for(model, dt("2026-11-10T00:00:00+00:00"))
        assert period.values["cache_read"] == period.values["input_nocache"] * Decimal("0.025")


def test_separate_entries_not_aliases():
    catalog = load_rates()
    for model in MODELS:
        for other in catalog.models.values():
            assert model not in other.aliases


def test_fable_5_1_is_unchanged_and_mythos_5_1_stays_unpriced():
    catalog = load_rates()
    _, period = catalog.rate_for("claude-fable-5-1", dt("2026-11-10T00:00:00+00:00"))
    assert period.rate_id == "claude-fable-5-1-local-first-observed-2026-09-02"
    assert period.values["input_nocache"] == Decimal("10.0")
    assert period.values["cache_read"] == Decimal("0.25")
    assert period.values["output"] == Decimal("50.0")
    assert "claude-mythos-5-1" not in catalog.models
    _, period = catalog.rate_for("claude-mythos-5-1", dt("2026-11-10T00:00:00+00:00"))
    assert period is None


def test_catalog_version_and_sources_record_fable_5_2():
    catalog = load_rates()
    assert catalog.catalog_version == "2026-11-10"
    urls = [(s.get("url"), s.get("retrieved_at")) for s in catalog.sources]
    assert ("https://www.anthropic.com/claude-fable-and-mythos-5-2", "2026-11-10") in urls
    assert ("https://platform.claude.com/docs/en/about-claude/pricing", "2026-11-10") in urls
