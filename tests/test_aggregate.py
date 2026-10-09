from datetime import datetime, timezone
from decimal import Decimal

from agent_cost.aggregate import build_rows, filter_facts, price_fact
from agent_cost.facts import Fact
from agent_cost.rates import load_rates

UTC = timezone.utc


def _fact(**kwargs):
    base = dict(
        occurred_at_utc=datetime(2026, 6, 1, tzinfo=UTC),
        agent="claude",
        session_id="s1",
        model_raw="claude-opus-4-8",
        model_key="claude-opus-4-8",
        token_kind="input_nocache",
        tokens=1_000_000,
        mode="unknown",
    )
    base.update(kwargs)
    return Fact(**base)


def test_price_fact_known_model_and_kind():
    catalog = load_rates()
    cost, status, credits = price_fact(catalog, _fact())
    assert status == "priced"
    assert cost == Decimal("5.0")
    assert credits is None


def test_price_fact_unknown_model_is_unpriced():
    catalog = load_rates()
    f = _fact(model_key="no-such-model")
    cost, status, credits = price_fact(catalog, f)
    assert cost is None
    assert status == "unpriced"
    assert credits is None


def test_price_fact_cache_write_unknown_is_lower_bound():
    catalog = load_rates()
    f = _fact(token_kind="cache_write_unknown")
    cost, status, _ = price_fact(catalog, f)
    assert status == "lower_bound"
    assert cost == Decimal("6.25")  # priced at the 5m rate, not 1h


def test_price_fact_codex_cache_write_is_unpriced():
    catalog = load_rates()
    f = _fact(
        occurred_at_utc=datetime(2026, 7, 15, tzinfo=UTC),
        agent="codex",
        model_raw="gpt-5.5",
        model_key="gpt-5.5",
        token_kind="cache_write_5m",
    )
    cost, status, credits = price_fact(catalog, f)
    assert cost is None
    assert status == "unpriced"


def test_price_fact_codex_applies_fast_multiplier_to_usd_and_credits():
    catalog = load_rates()
    f = _fact(
        occurred_at_utc=datetime(2026, 7, 15, tzinfo=UTC),
        agent="codex",
        model_raw="gpt-5.5",
        model_key="gpt-5.5",
        token_kind="input_nocache",
        mode="fast",
    )
    cost, status, credits = price_fact(catalog, f)
    assert status == "priced"
    assert cost == Decimal("5.0") * Decimal("2.5")
    assert credits == Decimal("125.0") * Decimal("2.5")


def test_filter_facts_half_open_window():
    facts = [
        _fact(occurred_at_utc=datetime(2026, 6, 1, tzinfo=UTC)),
        _fact(occurred_at_utc=datetime(2026, 6, 15, tzinfo=UTC)),
        _fact(occurred_at_utc=datetime(2026, 7, 1, tzinfo=UTC)),
    ]
    since = datetime(2026, 6, 1, tzinfo=UTC)
    until = datetime(2026, 7, 1, tzinfo=UTC)
    result = list(filter_facts(facts, since_utc=since, until_utc=until))
    assert len(result) == 2  # includes since, excludes until


def test_build_rows_groups_by_month_agent_model_token_kind():
    catalog = load_rates()
    facts = [
        _fact(occurred_at_utc=datetime(2026, 6, 1, tzinfo=UTC), tokens=500_000),
        _fact(occurred_at_utc=datetime(2026, 6, 15, tzinfo=UTC), tokens=500_000),
        _fact(occurred_at_utc=datetime(2026, 7, 1, tzinfo=UTC), tokens=1_000_000),
    ]
    rows, dq = build_rows(facts, catalog)
    by_month = {r.month: r for r in rows}
    assert by_month["2026-06"].tokens == 1_000_000
    assert by_month["2026-06"].estimated_cost_usd == Decimal("5.0")
    assert by_month["2026-07"].tokens == 1_000_000
    assert dq.unpriced_tokens == 0


def test_build_rows_timezone_shifts_month_boundary():
    catalog = load_rates()
    # 2026-06-30T16:00:00 UTC == 2026-07-01T01:00:00 in Asia/Tokyo (+9)
    f = _fact(occurred_at_utc=datetime(2026, 6, 30, 16, 0, 0, tzinfo=UTC))
    rows_utc, _ = build_rows([f], catalog, timezone_name="UTC")
    rows_tokyo, _ = build_rows([f], catalog, timezone_name="Asia/Tokyo")
    assert rows_utc[0].month == "2026-06"
    assert rows_tokyo[0].month == "2026-07"


def test_build_rows_dst_boundary_does_not_crash():
    catalog = load_rates()
    # US DST spring-forward 2026-03-08 in America/New_York.
    f = _fact(occurred_at_utc=datetime(2026, 3, 8, 7, 30, 0, tzinfo=UTC))
    rows, _ = build_rows([f], catalog, timezone_name="America/New_York")
    assert rows[0].month == "2026-03"


def test_build_rows_unknown_model_is_unpriced_and_flagged():
    catalog = load_rates()
    f = _fact(model_key="totally-unknown")
    rows, dq = build_rows([f], catalog)
    assert rows[0].pricing_status == "unpriced"
    assert rows[0].unpriced_tokens == f.tokens
    assert rows[0].estimated_cost_usd == Decimal("0")
    assert dq.unpriced_tokens == f.tokens


def test_build_rows_ungrouped_dimensions_are_none():
    catalog = load_rates()
    facts = [
        _fact(agent="claude", model_key="claude-opus-4-8"),
        _fact(agent="claude", model_key="claude-sonnet-5"),
    ]
    rows, _ = build_rows(facts, catalog, group_by=("agent",))
    assert len(rows) == 1
    row = rows[0]
    assert row.agent == "claude"
    assert row.month is None
    assert row.model is None
    assert row.token_kind is None
    assert row.tokens == facts[0].tokens + facts[1].tokens


def test_build_rows_mixed_priced_and_unpriced_marks_row_unpriced():
    catalog = load_rates()
    facts = [
        _fact(model_key="claude-opus-4-8", tokens=100),
        _fact(model_key="unknown-model", tokens=50),
    ]
    rows, _ = build_rows(facts, catalog, group_by=("agent",))
    assert rows[0].pricing_status == "unpriced"
    assert rows[0].priced_tokens == 100
    assert rows[0].unpriced_tokens == 50


def test_build_rows_all_priced_facts_mark_row_priced():
    # AC-GAP-02: baseline for the ranking -- a row built only from
    # normally-priced facts (no lower_bound, no unpriced) stays "priced".
    catalog = load_rates()
    facts = [
        _fact(model_key="claude-opus-4-8", tokens=100),
        _fact(model_key="claude-opus-4-8", token_kind="output", tokens=50),
    ]
    rows, _ = build_rows(facts, catalog, group_by=("agent",))
    assert rows[0].pricing_status == "priced"


def test_build_rows_mixed_priced_and_lower_bound_marks_row_lower_bound():
    # AC-GAP-02: the half of the _STATUS_RANK ordering that
    # tests/test_aggregate.py had no coverage for -- a row built from a
    # plain priced fact and a lower_bound fact (cache_write_unknown) in
    # the same group-by bucket must be downgraded to "lower_bound", not
    # left at "priced".
    catalog = load_rates()
    facts = [
        _fact(model_key="claude-opus-4-8", token_kind="input_nocache", tokens=100),
        _fact(model_key="claude-opus-4-8", token_kind="cache_write_unknown", tokens=50),
    ]
    rows, _ = build_rows(facts, catalog, group_by=("agent",))
    assert rows[0].pricing_status == "lower_bound"
    assert rows[0].priced_tokens == 150
    assert rows[0].unpriced_tokens == 0


def test_build_rows_mixed_lower_bound_and_unpriced_marks_row_unpriced():
    # AC-GAP-02: unpriced outranks (is worse than) lower_bound, so a row
    # mixing a lower_bound fact with an unpriced one ends up "unpriced".
    catalog = load_rates()
    facts = [
        _fact(model_key="claude-opus-4-8", token_kind="cache_write_unknown", tokens=100),
        _fact(model_key="unknown-model", tokens=50),
    ]
    rows, _ = build_rows(facts, catalog, group_by=("agent",))
    assert rows[0].pricing_status == "unpriced"
    assert rows[0].priced_tokens == 100
    assert rows[0].unpriced_tokens == 50


def test_build_rows_worst_status_ranking_is_order_independent():
    # AC-GAP-02: the row's pricing_status is the worst status among its
    # facts regardless of the order the facts were built in -- build_rows
    # only ever downgrades (never upgrades) a row's status as it iterates,
    # so any permutation of the same three facts must land on "unpriced".
    catalog = load_rates()
    priced = _fact(model_key="claude-opus-4-8", token_kind="input_nocache", tokens=100)
    lower_bound = _fact(model_key="claude-opus-4-8", token_kind="cache_write_unknown", tokens=50)
    unpriced = _fact(model_key="unknown-model", tokens=25)

    for facts in (
        [priced, lower_bound, unpriced],
        [unpriced, lower_bound, priced],
        [lower_bound, priced, unpriced],
    ):
        rows, _ = build_rows(facts, catalog, group_by=("agent",))
        assert rows[0].pricing_status == "unpriced"


# ── prompt-length tiers (prompt_tiers) ──


def _tiered_catalog(tmp_path):
    import json

    def rate(**values):
        return {
            "rate_id": "r1",
            "effective_from": "2025-01-01T00:00:00+00:00",
            "effective_until": None,
            **values,
        }

    data = {
        "schema_version": "2",
        "catalog_version": "test",
        "currency": "USD",
        "unit": "per_mtok",
        "usd_per_credit": "0.04",
        "sources": [],
        "models": [
            {
                "model_key": "tiered",
                "aliases": [],
                "fast_multiplier": "1.0",
                "rates": [
                    rate(
                        input_nocache="1.0",
                        cache_read="0.1",
                        cache_write_5m="1.25",
                        cache_write_1h="2.0",
                        output="5.0",
                        prompt_tiers=[
                            {
                                "prompt_tokens_over": 100000,
                                "input_nocache": "2.0",
                                "cache_read": "0.2",
                                "cache_write_5m": "2.5",
                                "cache_write_1h": "4.0",
                                "output": "10.0",
                            },
                            {
                                "prompt_tokens_over": 200000,
                                "input_nocache": "3.0",
                                "cache_read": "0.3",
                                "cache_write_5m": "3.75",
                                "cache_write_1h": "6.0",
                                "output": "15.0",
                            },
                        ],
                    )
                ],
            },
            {
                "model_key": "flat",
                "aliases": [],
                "fast_multiplier": "1.0",
                "rates": [
                    rate(
                        input_nocache="1.0",
                        cache_read="0.1",
                        cache_write_5m="1.25",
                        cache_write_1h="2.0",
                        output="5.0",
                    )
                ],
            },
        ],
    }
    path = tmp_path / "rates.json"
    path.write_text(json.dumps(data))
    return load_rates(path)


def _tiered_fact(**kwargs):
    base = dict(model_raw="tiered", model_key="tiered", token_kind="output")
    base.update(kwargs)
    return _fact(**base)


def test_price_fact_prompt_tier_boundary_is_strictly_greater_than(tmp_path):
    catalog = _tiered_catalog(tmp_path)
    expected = {
        100000: Decimal("5.0"),  # base: prompt length <= first threshold
        100001: Decimal("10.0"),  # tier 1
        200000: Decimal("10.0"),  # still tier 1 (not > 200000)
        200001: Decimal("15.0"),  # tier 2: the largest threshold exceeded
        1: Decimal("5.0"),
    }
    for prompt_tokens, cost_per_mtok in expected.items():
        cost, status, credits = price_fact(catalog, _tiered_fact(prompt_tokens=prompt_tokens))
        assert (prompt_tokens, cost, status) == (prompt_tokens, cost_per_mtok, "priced")
        assert credits is None


def test_price_fact_prompt_tier_applies_to_every_token_kind(tmp_path):
    catalog = _tiered_catalog(tmp_path)
    for kind, cost_per_mtok in (
        ("input_nocache", Decimal("2.0")),
        ("cache_read", Decimal("0.2")),
        ("cache_write_5m", Decimal("2.5")),
        ("cache_write_1h", Decimal("4.0")),
        ("output", Decimal("10.0")),
    ):
        cost, status, _ = price_fact(catalog, _tiered_fact(token_kind=kind, prompt_tokens=150000))
        assert (kind, cost, status) == (kind, cost_per_mtok, "priced")


def test_price_fact_unknown_prompt_length_on_tiered_model_is_base_lower_bound(tmp_path):
    catalog = _tiered_catalog(tmp_path)
    cost, status, _ = price_fact(catalog, _tiered_fact(prompt_tokens=None))
    assert cost == Decimal("5.0")
    assert status == "lower_bound"


def test_price_fact_cache_write_unknown_on_tiered_model_uses_tier_5m_rate_as_lower_bound(tmp_path):
    catalog = _tiered_catalog(tmp_path)
    cost, status, _ = price_fact(
        catalog, _tiered_fact(token_kind="cache_write_unknown", prompt_tokens=150000)
    )
    assert cost == Decimal("2.5")
    assert status == "lower_bound"
    cost, status, _ = price_fact(
        catalog, _tiered_fact(token_kind="cache_write_unknown", prompt_tokens=50000)
    )
    assert cost == Decimal("1.25")
    assert status == "lower_bound"


def test_price_fact_flat_model_ignores_prompt_tokens(tmp_path):
    catalog = _tiered_catalog(tmp_path)
    for kind in ("input_nocache", "output", "cache_write_unknown"):
        results = {
            price_fact(catalog, _fact(model_raw="flat", model_key="flat", token_kind=kind, prompt_tokens=pt))
            for pt in (None, 1, 100001, 10_000_000)
        }
        assert len(results) == 1, (kind, results)
    # Same check against the packaged catalog's flat entries.
    packaged = load_rates()
    assert price_fact(packaged, _fact(prompt_tokens=500000)) == price_fact(packaged, _fact())


def test_build_rows_sums_each_fact_at_its_own_tier(tmp_path):
    catalog = _tiered_catalog(tmp_path)
    facts = [
        _tiered_fact(prompt_tokens=90000),  # base: 1M output tokens * 5.0
        _tiered_fact(prompt_tokens=150000),  # tier 1: 1M output tokens * 10.0
    ]
    rows, dq = build_rows(facts, catalog)
    assert len(rows) == 1
    assert rows[0].estimated_cost_usd == Decimal("15.0")
    assert rows[0].pricing_status == "priced"
    assert rows[0].tokens == 2_000_000
    assert dq.unpriced_tokens == 0
