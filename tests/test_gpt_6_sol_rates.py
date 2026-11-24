"""Acceptance tests for gpt-6-sol and the gpt-6.1-sol release-date correction
(catalog_version 2026-11-24).

Source: developers.openai.com/api/docs/models/gpt-6-sol (retrieved 2026-11-24:
"Fast mode ... 2x"), the API changelog ("GPT-6 Sol: $2 input, $0.20 cached input,
and $10 output."; Sep 22 "Released GPT-6 Sol (gpt-6-sol) and GPT-6 Luna
(gpt-6-luna)"; Sep 29 "Released GPT-6.1 Sol (gpt-6.1-sol)") and the Enterprise
credit rate card (GPT-6 Sol 50 / 5 / 250 credits). Human-approved 2026-11-24.
"""

import copy
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import agent_cost
from agent_cost.aggregate import price_fact
from agent_cost.facts import Fact, normalize_model_key
from agent_cost.rates import load_rates

MODEL = "gpt-6-sol"
RATE_ID = "gpt-6-sol-launch-2026-09-22"
USD = {"input_nocache": "2.00", "cache_read": "0.20", "output": "10.00"}
CREDITS = {"input_nocache": "50.0", "cache_read": "5.0", "output": "250.0"}

SOL_61 = "gpt-6.1-sol"
SOL_61_RATE_ID = "gpt-6.1-sol-launch-2026-09-29"
SOL_61_OLD_RATE_ID = "gpt-6.1-sol-launch-date-unconfirmed"


def dt(s):
    return datetime.fromisoformat(s)


def _now():
    return datetime.now(timezone.utc)


def _fact(model, kind, mode, at="2026-11-24T03:00:00+00:00"):
    return Fact(dt(at), "codex", "synthetic-gpt-6-sol", model, normalize_model_key(model), kind, 1_000_000, mode)


def _raw():
    return json.loads((Path(agent_cost.__file__).parent / "rates.json").read_text(encoding="utf-8"))


def test_gpt_6_sol_resolves_from_release_date():
    catalog = load_rates()
    for occurred_at in (dt("2026-09-22T00:00:00+00:00"), _now()):
        resolved, period = catalog.rate_for(MODEL, occurred_at)
        assert resolved == MODEL
        assert period is not None, occurred_at
        assert period.rate_id == RATE_ID
        assert period.effective_until is None
        for kind, value in USD.items():
            assert period.values[kind] == Decimal(value), kind
        assert period.values["cache_write_5m"] is None
        assert period.values["cache_write_1h"] is None


def test_gpt_6_sol_is_unpriced_before_release():
    catalog = load_rates()
    at = "2026-09-21T23:59:59+00:00"
    resolved, period = catalog.rate_for(MODEL, dt(at))
    assert resolved == MODEL
    assert period is None
    assert price_fact(catalog, _fact(MODEL, "output", "normal", at)) == (None, "unpriced", None)


def test_gpt_6_sol_credits_and_entry_shape():
    catalog = load_rates()
    entry = catalog.models[MODEL]
    usd_per_credit = Decimal(catalog.usd_per_credit)
    assert entry.aliases == ()
    for other in catalog.models.values():
        assert MODEL not in other.aliases
    assert len(entry.rates) == 1
    assert entry.credits_per_mtok["cache_write_5m"] is None
    assert entry.credits_per_mtok["cache_write_1h"] is None
    for kind, value in CREDITS.items():
        assert entry.credits_per_mtok[kind] == Decimal(value), kind
        assert entry.credits_per_mtok[kind] * usd_per_credit == Decimal(USD[kind])


def test_gpt_6_sol_fast_mode_is_2x_and_no_prompt_tiers():
    catalog = load_rates()
    entry = catalog.models[MODEL]
    assert entry.fast_multiplier == Decimal("2.0")
    assert all(period.prompt_tiers == () for period in entry.rates)
    raw_entry = next(m for m in _raw()["models"] if m["model_key"] == MODEL)
    assert all("prompt_tiers" not in rate for rate in raw_entry["rates"])
    for kind, usd in USD.items():
        normal = price_fact(catalog, _fact(MODEL, kind, "normal"))
        fast = price_fact(catalog, _fact(MODEL, kind, "fast"))
        assert (normal[1], fast[1]) == ("priced", "priced")
        assert normal[0] == Decimal(usd)
        assert normal[2] == Decimal(CREDITS[kind])
        assert fast[0] == normal[0] * 2
        assert fast[2] == normal[2] * 2


def test_gpt_6_1_sol_priced_from_official_release_date():
    catalog = load_rates()
    entry = catalog.models[SOL_61]
    assert len(entry.rates) == 1
    assert all(period.rate_id != SOL_61_OLD_RATE_ID for period in entry.rates)
    resolved, period = catalog.rate_for(SOL_61, dt("2026-09-29T00:00:00+00:00"))
    assert resolved == SOL_61
    assert period is not None
    assert period.rate_id == SOL_61_RATE_ID
    assert period.effective_until is None
    assert period.values["input_nocache"] == Decimal("2.00")
    assert period.values["cache_read"] == Decimal("0.10")
    assert period.values["output"] == Decimal("10.00")
    assert entry.fast_multiplier == Decimal("2.0")
    assert SOL_61_OLD_RATE_ID not in json.dumps(_raw()["models"])


def test_gpt_6_1_sol_is_unpriced_before_release():
    catalog = load_rates()
    at = "2026-09-28T23:59:59+00:00"
    resolved, period = catalog.rate_for(SOL_61, dt(at))
    assert resolved == SOL_61
    assert period is None
    assert price_fact(catalog, _fact(SOL_61, "output", "normal", at)) == (None, "unpriced", None)


def test_gpt_6_1_terra_and_codex_auto_review_are_unchanged():
    catalog = load_rates()
    for model, rate_id in (
        ("gpt-6.1-terra", "gpt-6.1-terra-launch-date-unconfirmed"),
        ("codex-auto-review", "codex-auto-review-2026-11-terra-equivalent"),
    ):
        entry = catalog.models[model]
        assert len(entry.rates) == 1
        assert entry.rates[0].rate_id == rate_id
        assert entry.rates[0].effective_from == dt("2026-10-01T00:00:00+00:00")


def test_unpriced_boundaries_fail_if_effective_from_moves_one_day_earlier(tmp_path):
    """Negative side: the unpriced-boundary assertions above must break if
    either effective_from is moved one day earlier."""
    for model, earlier, probe in (
        (MODEL, "2026-09-21T00:00:00+00:00", "2026-09-21T23:59:59+00:00"),
        (SOL_61, "2026-09-28T00:00:00+00:00", "2026-09-28T23:59:59+00:00"),
    ):
        raw = copy.deepcopy(_raw())
        entry = next(m for m in raw["models"] if m["model_key"] == model)
        entry["rates"][0]["effective_from"] = earlier
        path = tmp_path / f"{model}.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        mutated = load_rates(path)
        _, period = mutated.rate_for(model, dt(probe))
        assert period is not None, model


def test_catalog_version_sources_and_note_record_gpt_6_sol():
    catalog = load_rates()
    assert catalog.catalog_version == "2026-11-24"
    urls = [(s.get("url"), s.get("retrieved_at")) for s in catalog.sources]
    for url in (
        "https://developers.openai.com/api/docs/models/gpt-6-sol",
        "https://developers.openai.com/api/docs/changelog",
        "https://help.openai.com/en/articles/11481834",
    ):
        assert urls.count((url, "2026-11-24")) == 1
    assert any(
        "gpt-6-sol added" in n
        and "272K" in n
        and "50/5/250" in n
        and SOL_61_RATE_ID in n
        and "human-approved 2026-11-24" in n
        for n in _raw()["notes"]
    )
