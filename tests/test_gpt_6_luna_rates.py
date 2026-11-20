"""Acceptance tests for gpt-6-luna (catalog_version 2026-11-20).

Source: developers.openai.com/api/docs/models/gpt-6-luna (retrieved 2026-11-20:
Input $0.10 / Cached input $0.01 / Output $0.50, fast mode 2x), the API changelog
(Sep 22 "Released GPT-6 Sol (gpt-6-sol) and GPT-6 Luna (gpt-6-luna)") and the
Enterprise credit rate card (2.5 / 0.25 / 12.5 credits). Human-approved 2026-11-20.
"""

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import agent_cost
from agent_cost.aggregate import price_fact
from agent_cost.facts import Fact, normalize_model_key
from agent_cost.rates import load_rates

MODEL = "gpt-6-luna"
RATE_ID = "gpt-6-luna-launch-2026-09-22"
USD = {"input_nocache": "0.10", "cache_read": "0.01", "output": "0.50"}
CREDITS = {"input_nocache": "2.5", "cache_read": "0.25", "output": "12.5"}


def dt(s):
    return datetime.fromisoformat(s)


def _fact(model, kind, mode, at="2026-11-20T03:00:00+00:00"):
    return Fact(dt(at), "codex", "synthetic-gpt-6-luna", model, normalize_model_key(model), kind, 1_000_000, mode)


def test_gpt_6_luna_resolves_from_release_date():
    catalog = load_rates()
    for occurred_at in ("2026-09-22T00:00:00+00:00", "2026-11-20T03:00:00+00:00"):
        resolved, period = catalog.rate_for(MODEL, dt(occurred_at))
        assert resolved == MODEL
        assert period is not None, occurred_at
        assert period.rate_id == RATE_ID
        assert period.effective_until is None
        for kind, value in USD.items():
            assert period.values[kind] == Decimal(value), kind
        assert period.values["cache_write_5m"] is None
        assert period.values["cache_write_1h"] is None


def test_gpt_6_luna_is_unpriced_before_release():
    catalog = load_rates()
    at = "2026-09-21T23:59:59+00:00"
    resolved, period = catalog.rate_for(MODEL, dt(at))
    assert resolved == MODEL
    assert period is None
    assert price_fact(catalog, _fact(MODEL, "output", "normal", at)) == (None, "unpriced", None)


def test_gpt_6_luna_is_separate_and_gpt_5_6_luna_is_unchanged():
    catalog = load_rates()
    assert catalog.models[MODEL].aliases == ()
    for entry in catalog.models.values():
        assert MODEL not in entry.aliases
    old = catalog.models["gpt-5.6-luna"]
    assert old.aliases == ()
    assert old.fast_multiplier == Decimal("1.0")
    assert old.credits_per_mtok["input_nocache"] == Decimal("25.0")
    assert old.credits_per_mtok["output"] == Decimal("150.0")
    _, period = catalog.rate_for("gpt-5.6-luna", dt("2026-11-20T03:00:00+00:00"))
    assert period.rate_id == "gpt-5.6-luna-2026-07-secondary-sourced"
    assert period.values["input_nocache"] == Decimal("1.00")
    assert period.values["cache_read"] == Decimal("0.10")
    assert period.values["output"] == Decimal("6.0")


def test_gpt_6_luna_credits_and_entry_shape():
    catalog = load_rates()
    entry = catalog.models[MODEL]
    usd_per_credit = Decimal(catalog.usd_per_credit)
    assert len(entry.rates) == 1
    assert entry.credits_per_mtok["cache_write_5m"] is None
    assert entry.credits_per_mtok["cache_write_1h"] is None
    for kind, value in CREDITS.items():
        assert entry.credits_per_mtok[kind] == Decimal(value), kind
        assert entry.credits_per_mtok[kind] * usd_per_credit == Decimal(USD[kind])


def test_gpt_6_luna_fast_mode_is_2x_and_no_prompt_tiers():
    catalog = load_rates()
    entry = catalog.models[MODEL]
    assert entry.fast_multiplier == Decimal("2.0")
    assert all(period.prompt_tiers == () for period in entry.rates)
    raw = json.loads((Path(agent_cost.__file__).parent / "rates.json").read_text(encoding="utf-8"))
    raw_entry = next(m for m in raw["models"] if m["model_key"] == MODEL)
    assert all("prompt_tiers" not in rate for rate in raw_entry["rates"])
    for kind, usd in USD.items():
        normal = price_fact(catalog, _fact(MODEL, kind, "normal"))
        fast = price_fact(catalog, _fact(MODEL, kind, "fast"))
        assert (normal[1], fast[1]) == ("priced", "priced")
        assert normal[0] == Decimal(usd)
        assert normal[2] == Decimal(CREDITS[kind])
        assert fast[0] == normal[0] * 2
        assert fast[2] == normal[2] * 2


def test_gpt_6_sol_has_no_entry():
    catalog = load_rates()
    assert "gpt-6-sol" not in catalog.models
    assert price_fact(catalog, _fact("gpt-6-sol", "output", "normal")) == (None, "unpriced", None)


def test_sources_and_note_record_gpt_6_luna_and_fast_fix():
    catalog = load_rates()
    assert catalog.catalog_version == "2026-11-20"
    urls = [(s.get("url"), s.get("retrieved_at")) for s in catalog.sources]
    for url in (
        "https://developers.openai.com/api/docs/changelog",
        "https://help.openai.com/en/articles/20001415-chatgpt-rate-card-enterprise-token-based-pricing",
    ):
        assert urls.count((url, "2026-11-20")) == 1
    raw = json.loads((Path(agent_cost.__file__).parent / "rates.json").read_text(encoding="utf-8"))
    assert any("gpt-6-luna" in n and "272K" in n and "1.0 to 2.0" in n and "gpt-6-sol" in n for n in raw["notes"])
