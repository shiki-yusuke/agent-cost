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
