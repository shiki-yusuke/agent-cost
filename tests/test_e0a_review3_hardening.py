"""E0-a acceptance-condition hardening tests (architect review round 3).

architect レビューで、既存テストが次の2条件を弱くしか検証していないと
指摘された。本ファイルはそれぞれ否定側が落ちる形で検証する。

条件A: claude-fable-5 の entry は E0-a (claude-fable-5-1 追加) 前後で
       rate_id・effective_from・effective_until・5単価・aliases・
       fast_multiplier のすべてが変わっていない。

条件B: rates.json に存在しないモデルの usage は、resolver が
       (None, None) を返すだけでなく、集計後の最終出力
       (agent_cost.aggregate.build_rows -> Row.to_dict()) で
       pricing_status == "unpriced" となり、確定した金額が計上されない。

このファイルは新規作成のみで、既存テストファイル・実装ファイルは
一切変更していない。
"""

from datetime import datetime, timezone
from decimal import Decimal

from agent_cost.aggregate import build_rows
from agent_cost.facts import Fact
from agent_cost.rates import load_rates

UTC = timezone.utc


def dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def _fact(model_key, token_kind, tokens, occurred_at="2026-09-05T00:00:00+00:00"):
    return Fact(
        occurred_at_utc=dt(occurred_at),
        agent="claude",
        session_id="s1",
        model_raw=model_key,
        model_key=model_key,
        token_kind=token_kind,
        tokens=tokens,
    )


# --- 条件A: claude-fable-5 は E0-a 前後で完全不変 ---------------------------
#
# 以下の期待値は、E0-a 適用前の main checkout
# /Users/a13714/oss-space/agent-cost/agent_cost/rates.json （読み取り専用、
# 実行時にはこのファイルを読まない）の model_key == "claude-fable-5" エント
# リ（38行目の model_key から 54行目の閉じ括弧まで、rate period は1件のみ）
# を手で転記した literal である。

_PRE_E0A_FABLE_5_ALIASES = ()
_PRE_E0A_FABLE_5_FAST_MULTIPLIER = Decimal("1.0")
_PRE_E0A_FABLE_5_RATE_ID = "claude-fable-5-launch-2026-06-09"
_PRE_E0A_FABLE_5_EFFECTIVE_FROM = dt("2026-06-09T00:00:00+00:00")
_PRE_E0A_FABLE_5_EFFECTIVE_UNTIL = None
_PRE_E0A_FABLE_5_VALUES = {
    "input_nocache": Decimal("10.0"),
    "cache_read": Decimal("1.00"),
    "cache_write_5m": Decimal("12.50"),
    "cache_write_1h": Decimal("20.0"),
    "output": Decimal("50.0"),
}


def test_condition_a_claude_fable_5_entry_metadata_is_fully_unchanged():
    """条件A: claude-fable-5 の aliases / fast_multiplier / 期間数が不変。

    落ちる変更の例: claude-fable-5-1 を追加する際に claude-fable-5 の
    aliases や fast_multiplier を誤って書き換える(コピペミス)、または
    period をもう1件足してしまう。
    """
    catalog = load_rates()
    entry = catalog.models["claude-fable-5"]
    assert entry.aliases == _PRE_E0A_FABLE_5_ALIASES
    assert entry.fast_multiplier == _PRE_E0A_FABLE_5_FAST_MULTIPLIER
    assert len(entry.rates) == 1


def test_condition_a_claude_fable_5_single_rate_period_is_fully_unchanged():
    """条件A: rate_id・effective_from・effective_until・5単価すべてが不変。

    落ちる変更の例: E0-a が claude-fable-5 の cache_read を 1.00 から
    別値に書き換える、あるいは effective_until に値を入れて既存の
    open-ended な期間を打ち切ってしまう。
    """
    catalog = load_rates()
    entry = catalog.models["claude-fable-5"]
    assert len(entry.rates) == 1
    period = entry.rates[0]
    assert period.rate_id == _PRE_E0A_FABLE_5_RATE_ID
    assert period.effective_from == _PRE_E0A_FABLE_5_EFFECTIVE_FROM
    assert period.effective_until == _PRE_E0A_FABLE_5_EFFECTIVE_UNTIL
    for field, expected in _PRE_E0A_FABLE_5_VALUES.items():
        assert period.values[field] == expected, field


# --- 条件B: 未知モデルは集計後の最終出力で pricing_status == "unpriced" ----


def test_condition_b_unknown_model_is_unpriced_in_aggregated_output_not_only_resolver():
    """条件B: build_rows -> Row.to_dict() を通した最終出力で
    pricing_status == "unpriced" となり、estimated_cost_usd に確定金額が
    計上されないこと(resolver が None を返すことだけを見るテストでは
    検出できない回帰を捕まえる)。

    落ちる変更の例: aggregate.price_fact が unpriced を返しても
    build_rows がその status を row.pricing_status に反映し損ね、
    デフォルトの "priced" のまま最終出力に出てしまう
    (_STATUS_RANK 適用漏れ)。
    """
    catalog = load_rates()
    facts = [_fact("claude-nonexistent-9", "input_nocache", 500)]
    rows, dq = build_rows(facts, catalog)
    assert len(rows) == 1
    row_dict = rows[0].to_dict()
    assert row_dict["pricing_status"] == "unpriced"
    assert row_dict["priced_tokens"] == 0
    assert row_dict["unpriced_tokens"] == 500
    assert row_dict["estimated_cost_usd"] == 0.0
    assert dq.unpriced_tokens == 500


def test_condition_b_claude_fable_5_1_1h_cache_write_is_priced_contrast_case():
    """対照: 同じ経路(build_rows -> Row.to_dict())で、TTL識別可能な
    claude-fable-5-1 の cache_write_1h は pricing_status == "priced" と
    なり、確定金額が計上されること。

    落ちる変更の例: build_rows 側の変更で cache_write_1h が誤って
    lower_bound/unpriced 側に倒れる、または金額が計上されなくなる。
    """
    catalog = load_rates()
    facts = [_fact("claude-fable-5-1", "cache_write_1h", 1_000_000)]
    rows, dq = build_rows(facts, catalog)
    assert len(rows) == 1
    row_dict = rows[0].to_dict()
    assert row_dict["pricing_status"] == "priced"
    assert row_dict["priced_tokens"] == 1_000_000
    assert row_dict["unpriced_tokens"] == 0
    assert row_dict["estimated_cost_usd"] == 20.0
    assert dq.unpriced_tokens == 0
