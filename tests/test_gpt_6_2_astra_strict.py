"""gpt-6.2-astra takes the conservative Astra path; synthetic data only.

Precedent: tests/test_astra_pricing.py (gpt-6-astra). Rates 8.00/0.40/40.00
USD and 200/10/1000 credits per MTok, fast 2x, effective 2026-11-17.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from agent_cost.aggregate import build_rows, price_fact, rows_totals
from agent_cost.facts import Fact, normalize_model_key
from agent_cost.rates import load_rates
from agent_cost.readers.codex import parse_rollout_facts

AT = datetime(2026, 11, 20, 3, 0, 0, tzinfo=timezone.utc)
SESSION = "synthetic-astra-6-2"


def _fact(model="gpt-6.2-astra", mode="normal", agent="codex", kind="input_nocache"):
    return Fact(AT, agent, SESSION, model, normalize_model_key(model), kind, 1_000_000, mode)


def _event(kind, **payload):
    return {"type": "event_msg", "timestamp": "2026-11-20T03:00:00Z",
            "payload": {"type": kind, **payload}}


def _settings(model, tier):
    settings = {"model": model, "model_provider_id": "openai",
                "collaboration_mode": {"mode": "default", "settings": {"model": model}}}
    if tier is not None:
        settings["service_tier"] = tier
    return _event("thread_settings_applied", thread_id=SESSION, thread_settings=settings)


def _turn(turn, model="gpt-6.2-astra", tier="default", n=1, complete=True):
    events = [_settings(model, tier), _event("task_started", turn_id=turn),
              {"type": "turn_context", "payload": {"turn_id": turn, "model": model}},
              _event("token_count", info={"total_token_usage": {
                  "input_tokens": n * 1_000_000, "cached_input_tokens": 0, "output_tokens": 0}})]
    if complete:
        events.append(_event("task_complete", turn_id=turn))
    return events


def _read(tmp_path, events, db_model="gpt-6.2-astra"):
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    facts, _, _ = parse_rollout_facts(path, model_raw=db_model, session_id=SESSION)
    return facts


def _total(facts):
    rows, _ = build_rows(facts, load_rates())
    return rows_totals(rows)


@pytest.mark.parametrize("models,costs", [
    (("gpt-6.2-astra", "gpt-5.5", "gpt-6.2-astra"), [8, 5, 8]),
    (("gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.2-sol"), [2, 8, 2]),
])
def test_turn_level_attribution_across_model_switches(tmp_path, models, costs):
    # The DB's final model is the last turn's; the pre-pass still finds Astra.
    events = []
    for i, model in enumerate(models, 1):
        events += _turn(str(i), model=model, n=i)
    facts = _read(tmp_path, events, db_model=models[-1])
    assert [f.model_raw for f in facts] == list(models)
    assert [price_fact(load_rates(), f)[0] for f in facts] == costs


def test_switch_from_astra_is_detected_when_db_model_is_other(tmp_path):
    events = _turn("1", model="gpt-6.2-astra") + _turn("2", model="gpt-5.5", n=2)
    facts = _read(tmp_path, events, db_model="gpt-5.5")
    assert [f.model_raw for f in facts] == ["gpt-6.2-astra", "gpt-5.5"]
    assert [f.mode for f in facts] == ["normal", "normal"]


def test_priority_tier_is_fast_and_priced_at_2x(tmp_path):
    events = _turn("1", tier="default") + _turn("2", tier="priority", n=2)
    facts = _read(tmp_path, events)
    assert [f.mode for f in facts] == ["normal", "fast"]
    priced = [price_fact(load_rates(), f) for f in facts]
    assert priced == [(Decimal(8), "priced", Decimal(200)), (Decimal(16), "priced", Decimal(400))]
    assert _total(facts)["estimated_cost_usd"] == 24


@pytest.mark.parametrize("tier", [None, "flex", "PRIORITY"])
def test_unknown_mode_stays_unpriced(tmp_path, tier):
    facts = _read(tmp_path, _turn("1", tier=tier, complete=False))
    assert [(f.model_raw, f.mode) for f in facts] == [("gpt-6.2-astra", "unknown")]
    assert _total(facts)["unpriced_tokens"] == 1_000_000
    assert _total(facts)["estimated_cost_usd"] == 0


def test_guard_rejects_unknown_mode_and_other_agent():
    # Negative side: without the guard these resolve to gpt-6.2-astra and price.
    catalog = load_rates()
    assert price_fact(catalog, _fact(mode="unknown")) == (None, "unpriced", None)
    assert price_fact(catalog, _fact(agent="claude")) == (None, "unpriced", None)
    assert price_fact(catalog, _fact())[1] == "priced"


@pytest.mark.parametrize("variant", ["gpt-6.2-astra-2026-11-17", "gpt-6.2-astra-fast", "gpt-6.2-astra@20261117"])
def test_non_exact_raw_ids_stay_unpriced(tmp_path, variant):
    catalog = load_rates()
    assert price_fact(catalog, _fact(model=variant)) == (None, "unpriced", None)
    facts = _read(tmp_path, _turn("1", model=variant, tier="priority", complete=False), db_model=variant)
    assert _total(facts)["unpriced_tokens"] == 1_000_000


@pytest.mark.parametrize("variant", ["gpt-6.2-astra-fast", "gpt-6.2-astra@20261117"])
def test_normalized_variants_reach_the_guard(variant):
    # These normalize onto the gpt-6.2-astra entry, so only the guard keeps them unpriced.
    resolved, period = load_rates().rate_for(normalize_model_key(variant), AT)
    assert resolved == "gpt-6.2-astra" and period is not None


def test_gpt_6_2_sol_stays_on_the_ordinary_path():
    assert price_fact(load_rates(), _fact(model="gpt-6.2-sol", mode="unknown"))[1] == "priced"
