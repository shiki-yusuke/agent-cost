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


# ── Astra model recovery (a) and identical mid-turn re-send (b1) ──

def _ctx(turn, model="gpt-6.2-astra"):
    return {"type": "turn_context", "payload": {"turn_id": turn, "model": model}}


def _count(n):
    return _event("token_count", info={"total_token_usage": {
        "input_tokens": n * 1_000_000, "cached_input_tokens": 0, "output_tokens": 0}})


def _bad(kind):
    bad = _settings("gpt-6.2-astra", "default")
    settings = bad["payload"]["thread_settings"]
    if kind == "provider":
        settings["model_provider_id"] = "other-provider"
    else:
        settings["collaboration_mode"]["settings"]["model"] = "gpt-6.2-sol"
    return bad


def _attributed(facts):
    return [(f.model_key, f.mode, price_fact(load_rates(), f)[1]) for f in facts]


UNKNOWN = ("(unknown)", "unknown", "unpriced")
RECOVERED = ("gpt-6.2-astra", "unknown", "unpriced")


def test_identical_astra_resend_keeps_pricing(tmp_path):
    # P1: the same owned snapshot re-sent mid-turn (e.g. after compaction).
    events = _turn("t1", complete=False) + [_settings("gpt-6.2-astra", "default"), _count(2)]
    facts = _read(tmp_path, events)
    assert _attributed(facts) == [("gpt-6.2-astra", "normal", "priced")] * 2
    assert _total(facts)["estimated_cost_usd"] == 16


def test_tier_only_resend_recovers_model_but_stays_unpriced(tmp_path):
    events = _turn("t1", complete=False) + [_settings("gpt-6.2-astra", "priority"), _count(2)]
    facts = _read(tmp_path, events)
    assert _attributed(facts)[1] == RECOVERED
    assert _total(facts)["unpriced_tokens"] == 1_000_000


@pytest.mark.parametrize("shape", ["resend", "first_after_start"])
def test_non_astra_turn_in_astra_rollout_is_not_recovered(tmp_path, shape):
    events = _turn("t1", complete=True)
    if shape == "resend":
        events += _turn("t2", model="gpt-6.2-sol", n=2, complete=False)
        events += [_settings("gpt-6.2-sol", "default"), _count(3)]
        expected = [("gpt-6.2-sol", "normal", "priced"), UNKNOWN]
    else:
        events += [_event("task_started", turn_id="t2"), _ctx("t2", "gpt-6.2-sol"),
                   _settings("gpt-6.2-sol", "default"), _count(3)]
        expected = [UNKNOWN]
    facts = _read(tmp_path, events)
    assert _attributed(facts)[1:] == expected
    assert facts[-1].tokens == (1_000_000 if shape == "resend" else 2_000_000)


@pytest.mark.parametrize("tier", ["default", None])
def test_first_snapshot_after_turn_start_recovers_model_only(tmp_path, tier):
    # P2 (with and without a tier): the snapshot cannot be told apart from an update.
    events = [_event("task_started", turn_id="t1"), _ctx("t1"),
              _settings("gpt-6.2-astra", tier), _count(1)]
    assert _attributed(_read(tmp_path, events)) == [RECOVERED]


def test_turn_without_any_snapshot_recovers_model_only(tmp_path):
    # P3: no owned snapshot in the whole rollout.
    events = [_event("task_started", turn_id="t1"), _ctx("t1"), _count(1)]
    assert _attributed(_read(tmp_path, events)) == [RECOVERED]


@pytest.mark.parametrize("kind", ["provider", "nested_model"])
@pytest.mark.parametrize("where", ["before_start", "mid_turn"])
def test_owned_invalid_snapshot_blocks_recovery(tmp_path, kind, where):
    if where == "before_start":
        events = [_bad(kind), _event("task_started", turn_id="t1"), _ctx("t1"), _count(1)]
    else:
        events = [_event("task_started", turn_id="t1"), _ctx("t1"), _bad(kind), _count(1)]
    assert _attributed(_read(tmp_path, events)) == [UNKNOWN]


def test_context_snapshot_model_mismatch_is_not_recovered(tmp_path):
    events = [_settings("gpt-6.2-sol", "default"), _event("task_started", turn_id="t1"),
              _ctx("t1"), _count(1)]
    assert _attributed(_read(tmp_path, events)) == [UNKNOWN]
    mid = [_event("task_started", turn_id="t1"), _ctx("t1"),
           _settings("gpt-6.2-sol", "default"), _count(1)]
    assert _attributed(_read(tmp_path, mid)) == [UNKNOWN]


def test_overlapping_turn_is_not_recovered(tmp_path):
    # No snapshot at all, so only the overlap (t1 still active at t2's start) blocks it.
    events = [_event("task_started", turn_id="t1"), _ctx("t1", "gpt-6.2-sol"), _count(1),
              _event("task_started", turn_id="t2"), _ctx("t2"), _count(2)]
    assert _attributed(_read(tmp_path, events))[1] == UNKNOWN


def test_context_model_change_within_turn_is_not_recovered(tmp_path):
    events = [_event("task_started", turn_id="t1"), _ctx("t1", "gpt-6.2-sol"),
              _ctx("t1", "gpt-6.2-astra"), _count(1)]
    assert _attributed(_read(tmp_path, events)) == [UNKNOWN]


def test_invalid_then_valid_snapshot_in_same_turn_is_not_recovered(tmp_path):
    events = _turn("t1", complete=False) + [_bad("provider"), _settings("gpt-6.2-astra", "default"),
                                            _count(2)]
    assert _attributed(_read(tmp_path, events))[1] == UNKNOWN


@pytest.mark.parametrize("shape", ["P2", "P3"])
def test_invalid_then_valid_before_start_allows_recovery(tmp_path, shape):
    events = [_bad("provider"), _settings("gpt-6.2-astra", "default")]
    if shape == "P2":
        # The pre-start valid snapshot is model-only evidence here: an Astra
        # snapshot of another tier arrives after the start.
        events += [_event("task_started", turn_id="t1"), _ctx("t1"),
                   _settings("gpt-6.2-astra", "priority"), _count(1)]
    else:
        # A foreign snapshot clears the pending setting without tainting, so the
        # turn starts with no captured snapshot (P3 shape).
        foreign = _settings("gpt-6.2-astra", "default")
        foreign["payload"]["thread_id"] = "other-thread"
        events += [foreign, _event("task_started", turn_id="t1"), _ctx("t1"), _count(1)]
    facts = _read(tmp_path, events)
    assert _attributed(facts) == [RECOVERED]
    assert _total(facts)["unpriced_tokens"] == 1_000_000
