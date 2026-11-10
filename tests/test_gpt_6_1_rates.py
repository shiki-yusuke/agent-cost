"""Acceptance tests for gpt-6.1-terra / gpt-6.1-sol / codex-auto-review (catalog_version 2026-11-09).

Source: human-approved price table pricing-2026-11-v4 (developers.openai.com/api/docs/pricing,
retrieved 2026-11-05; standard tier, short context). codex-auto-review (Codex automatic
review / guardian subagent) has no published price; priced as gpt-6.1-terra by a human
decision on 2026-11-06.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import agent_cost
from agent_cost.rates import load_rates

UTC = timezone.utc

EXPECTED = {
    "gpt-6.1-terra": (
        "gpt-6.1-terra-launch-date-unconfirmed",
        {"input_nocache": "1.00", "cache_read": "0.05", "output": "5.00"},
        {"input_nocache": "25.0", "cache_read": "1.25", "output": "125.0"},
    ),
    "gpt-6.1-sol": (
        "gpt-6.1-sol-launch-date-unconfirmed",
        {"input_nocache": "2.00", "cache_read": "0.10", "output": "10.00"},
        {"input_nocache": "50.0", "cache_read": "2.5", "output": "250.0"},
    ),
    "codex-auto-review": (
        "codex-auto-review-2026-11-terra-equivalent",
        {"input_nocache": "1.00", "cache_read": "0.05", "output": "5.00"},
        {"input_nocache": "25.0", "cache_read": "1.25", "output": "125.0"},
    ),
}


def dt(s):
    return datetime.fromisoformat(s)


def test_gpt_6_1_models_resolve_with_pricing_page_values_from_effective_from():
    catalog = load_rates()
    for model, (rate_id, usd, _credits) in EXPECTED.items():
        for occurred_at in ("2026-10-01T00:00:00+00:00", "2026-11-04T07:49:42+00:00", "2027-01-01T00:00:00+00:00"):
            resolved, period = catalog.rate_for(model, dt(occurred_at))
            assert resolved == model
            assert period is not None, (model, occurred_at)
            assert period.rate_id == rate_id
            assert period.effective_until is None
            for kind, value in usd.items():
                assert period.values[kind] == Decimal(value), (model, kind)
            assert period.values["cache_write_5m"] is None
            assert period.values["cache_write_1h"] is None
            assert period.prompt_tiers == ()


def test_gpt_6_1_models_are_unpriced_before_effective_from():
    catalog = load_rates()
    for model in EXPECTED:
        resolved, period = catalog.rate_for(model, dt("2026-09-30T23:59:59+00:00"))
        assert resolved == model
        assert period is None


def test_gpt_6_1_models_credits_follow_usd_per_credit_and_entry_shape():
    catalog = load_rates()
    usd_per_credit = Decimal(catalog.usd_per_credit)
    for model, (_rate_id, usd, credits) in EXPECTED.items():
        entry = catalog.models[model]
        assert entry.aliases == ()
        assert entry.fast_multiplier == Decimal("1.0")
        assert entry.credits_per_mtok["cache_write_5m"] is None
        assert entry.credits_per_mtok["cache_write_1h"] is None
        for kind, value in credits.items():
            assert entry.credits_per_mtok[kind] == Decimal(value), (model, kind)
            assert entry.credits_per_mtok[kind] * usd_per_credit == Decimal(usd[kind])


def test_codex_auto_review_is_priced_like_gpt_6_1_terra_but_is_its_own_entry():
    catalog = load_rates()
    at = dt("2026-11-06T00:00:00+00:00")
    _, terra = catalog.rate_for("gpt-6.1-terra", at)
    _, review = catalog.rate_for("codex-auto-review", at)
    assert review.values == terra.values
    assert review.rate_id != terra.rate_id
    assert "codex-auto-review" not in catalog.models["gpt-6.1-terra"].aliases


def test_unknown_gpt_6_1_luna_stays_unpriced():
    catalog = load_rates()
    assert "gpt-6.1-luna" not in catalog.models
    resolved, period = catalog.rate_for("gpt-6.1-luna", dt("2026-11-06T00:00:00+00:00"))
    assert period is None


def test_existing_gpt_5_6_terra_values_are_unchanged():
    catalog = load_rates()
    entry = catalog.models["gpt-5.6-terra"]
    assert len(entry.rates) == 1
    _, period = catalog.rate_for("gpt-5.6-terra", dt("2026-11-06T00:00:00+00:00"))
    assert period.rate_id == "gpt-5.6-terra-2026-07-secondary-sourced"
    assert period.values["input_nocache"] == Decimal("2.50")
    assert period.values["cache_read"] == Decimal("0.25")
    assert period.values["output"] == Decimal("15.0")
    assert entry.credits_per_mtok["input_nocache"] == Decimal("62.5")
    assert entry.credits_per_mtok["cache_read"] == Decimal("6.25")
    assert entry.credits_per_mtok["output"] == Decimal("375.0")


def test_catalog_version_and_source_record_the_gpt_6_1_pricing_page():
    catalog = load_rates()
    assert catalog.catalog_version >= "2026-11-09"
    urls = [(s.get("url"), s.get("retrieved_at")) for s in catalog.sources]
    assert urls.count(("https://developers.openai.com/api/docs/pricing", "2026-11-05")) == 1
    raw = json.loads((Path(agent_cost.__file__).parent / "rates.json").read_text(encoding="utf-8"))
    assert any("codex-auto-review" in n and "2026-11-06" in n for n in raw["notes"])
