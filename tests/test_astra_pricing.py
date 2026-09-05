"""Codex Astra catalog checks; public-format synthetic data, never real logs.

Source scope and known reader limitations: docs/astra-pricing.md.
"""
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from agent_cost import cli
from agent_cost.aggregate import build_rows, price_fact, rows_totals
from agent_cost.facts import Fact, normalize_model_key
from agent_cost.rates import load_rates
from agent_cost.readers.codex import read_codex_facts

CUTOFF = datetime(2026, 9, 5, 17, 18, 23, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "synthetic-codex-astra.jsonl"


def _fact(kind="input_nocache", mode="normal", model="gpt-6-astra"):
    return Fact(CUTOFF, "codex", "synthetic-astra", model,
                normalize_model_key(model), kind, 1_000_000, mode)


@pytest.mark.parametrize("kind,usd,credits", [
    ("input_nocache", "10", "250"),
    ("cache_read", "1", "25"),
    ("output", "50", "1250"),
])
@pytest.mark.parametrize("mode,multiplier", [("normal", "1"), ("fast", "2.5"), ("unknown", "1")])
def test_standard_fast_and_unknown_mode_prices(kind, usd, credits, mode, multiplier):
    # Registration is tested separately from detection. Unknown must stay unpriced.
    cost, status, actual_credits = price_fact(load_rates(), _fact(kind, mode))
    if mode == "unknown":
        assert (cost, status, actual_credits) == (None, "unpriced", None)
        return
    assert status == "priced"
    assert cost == Decimal(usd) * Decimal(multiplier)
    assert actual_credits == Decimal(credits) * Decimal(multiplier)


def test_catalog_observation_boundary_is_not_backdated():
    catalog = load_rates()
    assert catalog.catalog_version == "2026-09-06"
    entry = catalog.models["gpt-6-astra"]
    assert entry.aliases == ()
    assert entry.rates[0].effective_from == CUTOFF
    assert entry.rates[0].effective_until is None
    for moment, expected in [(CUTOFF - timedelta(microseconds=1), "unpriced"),
                             (CUTOFF, "priced"), (CUTOFF + timedelta(microseconds=1), "priced")]:
        fact = replace(_fact(), occurred_at_utc=moment)
        assert price_fact(catalog, fact)[1] == expected
    # Existing half-open semantics also exclude an explicitly closed period's end.
    closed = replace(entry.rates[0], effective_until=CUTOFF + timedelta(days=1))
    assert closed.covers(closed.effective_until - timedelta(microseconds=1))
    assert not closed.covers(closed.effective_until)


@pytest.mark.parametrize("model", ["unknown-model", "gpt-6-pro", "gpt-6-astra-pro",
    "gpt-6-astra-fast", "gpt-6-astra-latest", "gpt-6-astra@20260906",
    "gpt-6-astra-2026-09-06", "gpt-6-astra[1m]", "gpt-6-astra-200k", "gpt-6-astra-1m"])
def test_unverified_model_ids_stay_unpriced_after_normalization(model):
    rows, quality = build_rows([_fact(model=model)], load_rates())
    assert rows[0].pricing_status == "unpriced"
    assert rows[0].estimated_cost_usd == 0
    assert rows[0].unpriced_tokens == quality.unpriced_tokens == 1_000_000


def test_codex_rate_is_not_applied_to_claude_facts():
    assert price_fact(load_rates(), replace(_fact(), agent="claude")) == (None, "unpriced", None)


@pytest.mark.parametrize("kind", ["cache_write_5m", "cache_write_1h", "cache_write_unknown"])
def test_no_api_cache_write_rate_is_invented(kind):
    assert price_fact(load_rates(), _fact(kind)) == (None, "unpriced", None)


def _synthetic_db(tmp_path, *, service_tier="default", collaboration="default"):
    # Only synthetic files are copied/generated under pytest's temporary root.
    events = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    if service_tier is not None:
        events[1]["payload"]["thread_settings"]["service_tier"] = service_tier
    if collaboration is None:
        events = [event for event in events if event.get("payload", {}).get("type") != "task_started"]
    else:
        events[2]["payload"]["collaboration_mode_kind"] = collaboration
    rollout = tmp_path / "synthetic.jsonl"
    rollout.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    db = tmp_path / "state_5.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, model TEXT, rollout_path TEXT, tokens_used INTEGER)")
        conn.execute("INSERT INTO threads VALUES (?, ?, ?, ?)",
                     ("synthetic-astra", "gpt-6-astra", rollout.name, 6_000_000))
    return db


def test_synthetic_reader_to_aggregation_standard_tokens(tmp_path):
    db = _synthetic_db(tmp_path)
    result = read_codex_facts(db, tmp_path)
    assert (result.malformed_events, result.negative_deltas, result.skipped_files) == (0, 0, 0)
    assert {f.model_raw for f in result.facts} == {"gpt-6-astra"}
    assert {f.token_kind for f in result.facts} == {"input_nocache", "cache_read", "output"}
    assert len(result.facts) == 6
    assert all(f.tokens == 1_000_000 for f in result.facts)
    assert [f.source_quality for f in result.facts] == ["first_event_delta"] * 3 + ["ok"] * 3
    rows, quality = build_rows(result.facts, load_rates())
    by_kind = {r.token_kind: r for r in rows}
    for kind, cost, credits in [("input_nocache", 20, 500), ("cache_read", 2, 50), ("output", 100, 2500)]:
        assert by_kind[kind].estimated_cost_usd == cost
        assert by_kind[kind].credits == credits
        assert by_kind[kind].pricing_status == "priced"
    # No API long-context multiplier, extra reasoning charge, or cache-write charge.
    assert rows_totals(rows)["estimated_cost_usd"] == 122
    assert rows_totals(rows)["tokens"] == 6_000_000
    assert quality.unpriced_tokens == 0


@pytest.mark.parametrize("collaboration,expected_mode", [("default", "fast"), (None, "unknown")])
def test_reader_detects_priority_only_with_matching_turn(tmp_path, collaboration, expected_mode):
    # An owned setting without a turn boundary is not sufficient attribution.
    db = _synthetic_db(tmp_path, service_tier="priority", collaboration=collaboration)
    result = read_codex_facts(db, tmp_path)
    assert {f.mode for f in result.facts} == {expected_mode}
    rows, _ = build_rows(result.facts, load_rates())
    assert rows_totals(rows)["estimated_cost_usd"] == (305 if collaboration else 0)
    assert rows_totals(rows)["unpriced_tokens"] == (0 if collaboration else 6_000_000)


def test_astra_does_not_infer_fast_from_collaboration_name(tmp_path):
    db = _synthetic_db(tmp_path, collaboration="fast-turbo")
    result = read_codex_facts(db, tmp_path)
    assert {f.mode for f in result.facts} == {"normal"}
    rows, _ = build_rows(result.facts, load_rates())
    assert rows_totals(rows)["estimated_cost_usd"] == 122
    assert rows_totals(rows)["credits"] == 3050


def test_measure_reads_isolated_synthetic_astra_with_existing_contract(tmp_path, monkeypatch, capsys):
    _synthetic_db(tmp_path)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"claude_home": str(tmp_path / "empty-claude"), "codex_home": str(tmp_path)}))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("AGENT_COST_CONFIG", str(config))
    assert cli.main(["measure", "--session-id", "synthetic-astra", "--agent", "codex"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["protocol_version"] == "measure/v1"
    assert report["rates"]["catalog_version"] == "2026-09-06"
    assert len(report["rates"]["sha256"]) == 64
    assert report["sessions"]["synthetic-astra"]["totals"]["estimated_cost_usd"] == 122
    assert report["total"]["totals"]["credits"] == 3050


def _event(kind, **payload):
    return {"type": "event_msg", "timestamp": "2026-09-06T01:00:00Z",
            "payload": {"type": kind, **payload}}


def _settings(model="gpt-6-astra", tier="default", owner="synthetic-astra"):
    return _event("thread_settings_applied", thread_id=owner, thread_settings={
        "model": model, "model_provider_id": "openai", "service_tier": tier,
        "collaboration_mode": {"mode": "default", "settings": {"model": model}},
    })


def _context(turn="t1", model="gpt-6-astra"):
    return {"type": "turn_context", "payload": {"turn_id": turn, "model": model}}


def _count(n):
    return _event("token_count", info={"total_token_usage": {
        "input_tokens": n * 1_000_000, "cached_input_tokens": 0, "output_tokens": 0,
    }})


def _start(turn="t1", model="gpt-6-astra", tier="default"):
    return [_settings(model, tier), _event("task_started", turn_id=turn), _context(turn, model)]


def _read_events(tmp_path, events, db_model="gpt-6-astra"):
    from agent_cost.readers.codex import parse_rollout_facts
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(e) if not isinstance(e, str) else e for e in events) + "\n")
    facts, _, _ = parse_rollout_facts(path, model_raw=db_model, session_id="synthetic-astra")
    return facts


def _total(facts):
    rows, _ = build_rows(facts, load_rates())
    return rows_totals(rows)


def test_standard_fast_standard_turns_do_not_reprice_past_deltas(tmp_path):
    events = []
    for i, tier in enumerate(("default", "priority", "default"), 1):
        events += _start(str(i), tier=tier) + [_count(i), _event("task_complete", turn_id=str(i))]
    facts = _read_events(tmp_path, events)
    assert [f.mode for f in facts] == ["normal", "fast", "normal"]
    assert [f.tokens for f in facts] == [1_000_000] * 3
    assert [price_fact(load_rates(), f)[0] for f in facts] == [10, 25, 10]
    assert _total(facts)["estimated_cost_usd"] == 45


@pytest.mark.parametrize("tier", [None, "", "fast", "flex", "standard", "normal", "auto", "PRIORITY", {}, 7])
def test_unsupported_or_null_tier_never_uses_standard(tmp_path, tier):
    facts = _read_events(tmp_path, _start(tier=tier) + [_count(1)])
    assert _total(facts)["unpriced_tokens"] == 1_000_000
    assert _total(facts)["estimated_cost_usd"] == 0


def test_absent_tier_does_not_inherit_previous_priority(tmp_path):
    second = _start("t2")
    del second[0]["payload"]["thread_settings"]["service_tier"]
    facts = _read_events(tmp_path, _start(tier="priority") + [_count(1), _event("task_complete", turn_id="t1")]
                         + second + [_count(2)])
    assert [f.mode for f in facts] == ["fast", "unknown"]
    assert _total(facts)["estimated_cost_usd"] == 25
    assert _total(facts)["unpriced_tokens"] == 1_000_000


@pytest.mark.parametrize("owner", [None, "other-thread"])
def test_unowned_settings_cannot_price_copied_history(tmp_path, owner):
    events = _start()
    events[0]["payload"]["thread_id"] = owner
    assert _total(_read_events(tmp_path, events + [_count(1)]))["unpriced_tokens"] == 1_000_000


@pytest.mark.parametrize("conflict", ["context_model", "context_turn", "nested_model", "provider", "missing_context"])
def test_conflicting_context_is_not_attributed_to_astra(tmp_path, conflict):
    events = _start()
    if conflict == "context_model":
        events[-1]["payload"]["model"] = "gpt-5.5"
    elif conflict == "context_turn":
        events[-1]["payload"]["turn_id"] = "other-turn"
    elif conflict == "nested_model":
        events[0]["payload"]["thread_settings"]["collaboration_mode"]["settings"]["model"] = "gpt-5.5"
    elif conflict == "provider":
        events[0]["payload"]["thread_settings"]["model_provider_id"] = "other-provider"
    else:
        events.pop()
    facts = _read_events(tmp_path, events + [_count(1)])
    assert _total(facts)["unpriced_tokens"] == 1_000_000
    assert {f.model_key for f in facts} == {"(unknown)"}


def test_model_switch_uses_turn_model_not_final_db_model(tmp_path):
    events = []
    for i, model in enumerate(("gpt-5.5", "gpt-6-astra", "gpt-5.5"), 1):
        events += _start(str(i), model=model) + [_count(i), _event("task_complete", turn_id=str(i))]
    facts = _read_events(tmp_path, events, db_model="gpt-5.5")
    assert [f.model_raw for f in facts] == ["gpt-5.5", "gpt-6-astra", "gpt-5.5"]
    assert [price_fact(load_rates(), f)[0] for f in facts] == [5, 10, 5]


@pytest.mark.parametrize("new_model,new_tier", [("gpt-6-astra", "priority"), ("gpt-5.5", "default")])
def test_mid_turn_updates_stay_unpriced_until_next_turn(tmp_path, new_model, new_tier):
    events = _start() + [_count(1), _settings(new_model, new_tier), _count(2),
        _event("task_complete", turn_id="t1")]
    events += _start("t2", new_model, new_tier) + [_count(3)]
    facts = _read_events(tmp_path, events)
    assert price_fact(load_rates(), facts[0])[0] == 10
    assert facts[1].model_key == "(unknown)"
    assert price_fact(load_rates(), facts[1])[1] == "unpriced"
    assert price_fact(load_rates(), facts[2])[0] == (25 if new_model == "gpt-6-astra" else 5)
    assert _total(facts)["tokens"] == 3_000_000


@pytest.mark.parametrize("alias", ["gpt-6-astra-fast", "gpt-6-astra@20260906", "gpt-6-astra-pro"])
def test_reader_aliases_remain_unpriced(tmp_path, alias):
    facts = _read_events(tmp_path, _start(model=alias, tier="priority") + [_count(1)], db_model=alias)
    assert _total(facts)["unpriced_tokens"] == 1_000_000


def test_legacy_astra_without_settings_is_unpriced_even_with_fast_suffix(tmp_path):
    facts = _read_events(tmp_path, [_event("task_started", collaboration_mode_kind="fast-turbo"), _count(1)])
    assert _total(facts)["unpriced_tokens"] == 1_000_000


@pytest.mark.parametrize("command", ["report", "measure"])
def test_cli_mixed_priced_unpriced_contract(tmp_path, monkeypatch, capsys, command):
    _synthetic_db(tmp_path)
    events = _start() + [_count(1), _settings(tier="priority"), _count(2)]
    (tmp_path / "synthetic.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"claude_home": str(tmp_path / "empty"), "codex_home": str(tmp_path)}))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("AGENT_COST_CONFIG", str(config))
    args = [command, "--agent", "codex", "--format", "json"]
    if command == "measure":
        args += ["--session-id", "synthetic-astra"]
    assert cli.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["data_quality"]["unpriced_tokens"] == 1_000_000
    rows = report["rows"] if command == "report" else report["total"]["rows"]
    assert sum(r["tokens"] for r in rows) == 2_000_000
    assert sum(r["estimated_cost_usd"] for r in rows) == 10
    assert any(r["pricing_status"] == "unpriced" for r in rows)
    assert set(rows[0]) == {"month", "agent", "model", "token_kind", "tokens", "priced_tokens",
                            "unpriced_tokens", "estimated_cost_usd", "credits", "pricing_status"}
    assert report.get("protocol_version", report.get("schema_version")) == ("measure/v1" if command == "measure" else "1")


@pytest.mark.parametrize("bad", ["{broken", {"type": "event_msg", "payload": []}, 3])
def test_malformed_event_cannot_leave_stale_standard_setting(tmp_path, bad):
    facts = _read_events(tmp_path, _start() + [_count(1), bad, _count(2)])
    assert _total(facts)["estimated_cost_usd"] == 10
    assert _total(facts)["unpriced_tokens"] == 1_000_000


def test_wrong_turn_end_cannot_authorize_overlapping_turn(tmp_path):
    events = _start() + [_count(1), _event("task_complete", turn_id="unrelated")]
    events += _start("t2", tier="priority") + [_count(2)]
    assert _total(_read_events(tmp_path, events))["unpriced_tokens"] == 1_000_000


def test_first_usage_before_any_settings_is_not_final_db_model(tmp_path):
    facts = _read_events(tmp_path, [_count(1)] + _start() + [_count(2)], db_model="gpt-5.5")
    assert [f.model_key for f in facts] == ["(unknown)", "gpt-6-astra"]
    assert _total(facts)["unpriced_tokens"] == 1_000_000


def test_no_duplicate_charge_for_unchanged_cumulative_counter(tmp_path):
    facts = _read_events(tmp_path, _start(tier="priority") + [_count(1), _count(1), _count(2)])
    assert len(facts) == 2
    assert _total(facts)["estimated_cost_usd"] == 50


@pytest.mark.parametrize("offset,status", [(-1, "unpriced"), (0, "priced")])
def test_reader_at_observation_boundary(tmp_path, offset, status):
    count = _count(1)
    count["timestamp"] = (CUTOFF + timedelta(microseconds=offset)).isoformat()
    facts = _read_events(tmp_path, _start(tier="priority") + [count])
    assert price_fact(load_rates(), facts[0])[1] == status
