"""Acceptance tests for gpt-6.2-sol / gpt-6.2-astra (catalog_version 2026-11-20).

Source: human-approved price table 2026-11-20 (developers.openai.com/api/docs/pricing,
Codex rate card, retrieved 2026-11-20; standard tier, short context) and the official
Model Release Notes (help.openai.com, "November 17, 2026"). Fast mode is 2x standard;
no prompt_tiers (human decision 2026-11-20). gpt-reserve has no published price.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import agent_cost
from agent_cost.aggregate import price_fact
from agent_cost.facts import Fact, normalize_model_key
from agent_cost.rates import load_rates

UTC = timezone.utc

EXPECTED = {
    "gpt-6.2-sol": (
        "gpt-6.2-sol-launch-2026-11-17",
        {"input_nocache": "2.00", "cache_read": "0.10", "output": "10.00"},
        {"input_nocache": "50.0", "cache_read": "2.5", "output": "250.0"},
    ),
    "gpt-6.2-astra": (
        "gpt-6.2-astra-launch-2026-11-17",
        {"input_nocache": "8.00", "cache_read": "0.40", "output": "40.00"},
        {"input_nocache": "200.0", "cache_read": "10.0", "output": "1000.0"},
    ),
}


def dt(s):
    return datetime.fromisoformat(s)


def _fact(model, kind, mode, at="2026-11-20T03:00:00+00:00"):
    return Fact(dt(at), "codex", "synthetic-gpt-6-2", model, normalize_model_key(model), kind, 1_000_000, mode)


def test_gpt_6_2_models_resolve_with_pricing_page_values_from_release():
    catalog = load_rates()
    for model, (rate_id, usd, _credits) in EXPECTED.items():
        for occurred_at in ("2026-11-17T00:00:00+00:00", "2026-11-20T03:00:00+00:00", "2027-06-01T00:00:00+00:00"):
            resolved, period = catalog.rate_for(model, dt(occurred_at))
            assert resolved == model
            assert period is not None, (model, occurred_at)
            assert period.rate_id == rate_id
            assert period.effective_until is None
            for kind, value in usd.items():
                assert period.values[kind] == Decimal(value), (model, kind)
            assert period.values["cache_write_5m"] is None
            assert period.values["cache_write_1h"] is None


def test_gpt_6_2_models_are_unpriced_before_release():
    catalog = load_rates()
    for model in EXPECTED:
        resolved, period = catalog.rate_for(model, dt("2026-11-16T23:59:59+00:00"))
        assert resolved == model
        assert period is None
        cost, status, credits = price_fact(catalog, _fact(model, "output", "normal", "2026-11-16T23:59:59+00:00"))
        assert (cost, status, credits) == (None, "unpriced", None)


def test_gpt_reserve_has_no_entry_and_stays_unpriced():
    catalog = load_rates()
    assert "gpt-reserve" not in catalog.models
    for entry in catalog.models.values():
        assert "gpt-reserve" not in entry.aliases
    _, period = catalog.rate_for("gpt-reserve", dt("2026-11-20T03:00:00+00:00"))
    assert period is None
    cost, status, credits = price_fact(catalog, _fact("gpt-reserve", "output", "normal"))
    assert (cost, status, credits) == (None, "unpriced", None)


def test_existing_gpt_6_1_values_are_unchanged():
    catalog = load_rates()
    for model, rate_id, usd in (
        ("gpt-6.1-sol", "gpt-6.1-sol-launch-2026-09-29", ("2.00", "0.10", "10.00")),
        ("gpt-6.1-terra", "gpt-6.1-terra-launch-date-unconfirmed", ("1.00", "0.05", "5.00")),
    ):
        entry = catalog.models[model]
        assert len(entry.rates) == 1
        assert entry.fast_multiplier == Decimal("2.0")  # corrected 2026-11-20
        _, period = catalog.rate_for(model, dt("2026-11-20T03:00:00+00:00"))
        assert period.rate_id == rate_id
        assert period.values["input_nocache"] == Decimal(usd[0])
        assert period.values["cache_read"] == Decimal(usd[1])
        assert period.values["output"] == Decimal(usd[2])


def test_separate_entries_not_aliases():
    catalog = load_rates()
    for model in EXPECTED:
        assert catalog.models[model].aliases == ()
        for other in catalog.models.values():
            assert model not in other.aliases


def test_credits_follow_usd_per_credit_and_entry_shape():
    catalog = load_rates()
    usd_per_credit = Decimal(catalog.usd_per_credit)
    for model, (_rate_id, usd, credits) in EXPECTED.items():
        entry = catalog.models[model]
        assert len(entry.rates) == 1
        assert entry.credits_per_mtok["cache_write_5m"] is None
        assert entry.credits_per_mtok["cache_write_1h"] is None
        for kind, value in credits.items():
            assert entry.credits_per_mtok[kind] == Decimal(value), (model, kind)
            assert entry.credits_per_mtok[kind] * usd_per_credit == Decimal(usd[kind])


def test_fast_mode_is_2x_standard_and_no_prompt_tiers():
    catalog = load_rates()
    raw = json.loads((Path(agent_cost.__file__).parent / "rates.json").read_text(encoding="utf-8"))
    raw_entries = {m["model_key"]: m for m in raw["models"]}
    for model, (_rate_id, usd, credits) in EXPECTED.items():
        entry = catalog.models[model]
        assert entry.fast_multiplier == Decimal("2.0")
        for period in entry.rates:
            assert period.prompt_tiers == ()
        for rate in raw_entries[model]["rates"]:
            assert "prompt_tiers" not in rate
        for kind in usd:
            normal_cost, normal_status, normal_credits = price_fact(catalog, _fact(model, kind, "normal"))
            fast_cost, fast_status, fast_credits = price_fact(catalog, _fact(model, kind, "fast"))
            assert (normal_status, fast_status) == ("priced", "priced")
            assert normal_cost == Decimal(usd[kind])
            assert normal_credits == Decimal(credits[kind])
            assert fast_cost == normal_cost * 2
            assert fast_credits == normal_credits * 2


def test_catalog_version_sources_and_note_record_gpt_6_2():
    catalog = load_rates()
    assert catalog.catalog_version == "2026-11-24"
    urls = [(s.get("url"), s.get("retrieved_at")) for s in catalog.sources]
    assert urls.count(("https://developers.openai.com/api/docs/pricing", "2026-11-20")) == 1
    assert urls.count(("https://help.openai.com/en/articles/9624314-model-release-notes", "2026-11-20")) == 1
    raw = json.loads((Path(agent_cost.__file__).parent / "rates.json").read_text(encoding="utf-8"))
    assert any("gpt-6.2-sol" in n and "gpt-reserve" in n and "272K" in n for n in raw["notes"])
