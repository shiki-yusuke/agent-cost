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

条件C (PR レビュー3点目・追加分): claude-fable-5-1 の effective_from
       (2026-08-30T00:00:00+00:00) より前の timestamp の usage は、
       resolver が None を返すだけでなく、価格計算の最終出力
       (build_rows -> Row.to_dict()) で pricing_status == "unpriced"
       かつ確定金額が計上されない。境界ちょうどは priced になる。

条件D (PR レビュー3点目・追加分): catalog は
       catalog_version == "2026-09-09" であり、sources の中に url
       "https://platform.claude.com/docs/en/about-claude/pricing" の
       entry があって、その note に
       "sha256=d79ad28567196bd55dd50e2fd89341b9da9774a45c1d02fcf387078507fd15e0"
       を含む。

条件E (PR レビュー3点目・追加分): claude-sonnet-5 の
       2026-09-01T00:00:00+00:00 より前の period の rate_id は
       "claude-sonnet-5-launch-promo"、同じ境界以降の period の
       rate_id は "claude-sonnet-5-standard-2026-09-01" である
       (同額でも別 rate_id へ改名したら落ちる)。

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


# --- 条件C: claude-fable-5-1 は effective_from 前だと最終出力でも unpriced ---


def test_condition_c_claude_fable_5_1_before_effective_from_is_unpriced_in_final_output():
    """条件C: effective_from (2026-08-30T00:00:00+00:00) より前の usage は、
    resolver が None を返すだけでなく build_rows -> Row.to_dict() の最終出力
    でも pricing_status == "unpriced" かつ確定金額 0.0 であること。

    落ちる変更の例: price_fact/build_rows が resolver の None を無視して
    直近の(未来の)rate period にフォールバックし、境界より前の usage を
    誤って priced として金額を計上してしまう。
    """
    catalog = load_rates()
    facts = [
        _fact(
            "claude-fable-5-1",
            "input_nocache",
            1_000_000,
            occurred_at="2026-08-29T23:59:59+00:00",
        )
    ]
    rows, dq = build_rows(facts, catalog)
    assert len(rows) == 1
    row_dict = rows[0].to_dict()
    assert row_dict["pricing_status"] == "unpriced"
    assert row_dict["priced_tokens"] == 0
    assert row_dict["unpriced_tokens"] == 1_000_000
    assert row_dict["estimated_cost_usd"] == 0.0
    assert dq.unpriced_tokens == 1_000_000


def test_condition_c_claude_fable_5_1_at_effective_from_boundary_is_priced_contrast_case():
    """対照: effective_from ちょうど (2026-08-30T00:00:00+00:00) の usage は
    build_rows -> Row.to_dict() の最終出力で pricing_status == "priced" と
    なり、input_nocache の単価 (10.0/MTok, test_a1 で確認済み) 通りの確定
    金額が計上されること。

    落ちる変更の例: 境界の比較演算子を off-by-one で変更し、境界ちょうどが
    誤って unpriced 側に倒れてしまう。
    """
    catalog = load_rates()
    facts = [
        _fact(
            "claude-fable-5-1",
            "input_nocache",
            1_000_000,
            occurred_at="2026-08-30T00:00:00+00:00",
        )
    ]
    rows, dq = build_rows(facts, catalog)
    assert len(rows) == 1
    row_dict = rows[0].to_dict()
    assert row_dict["pricing_status"] == "priced"
    assert row_dict["priced_tokens"] == 1_000_000
    assert row_dict["unpriced_tokens"] == 0
    assert row_dict["estimated_cost_usd"] == 10.0
    assert dq.unpriced_tokens == 0


# --- 条件D: catalog_version と pricing source の sha256 note ----------------


def test_condition_d_catalog_version_and_pricing_source_sha256_note():
    """条件D: catalog_version == "2026-09-09" であり、sources に url
    "https://platform.claude.com/docs/en/about-claude/pricing" の entry が
    あって、その note に指定の sha256 文字列を含むこと。

    落ちる変更の例: catalog_version の更新を忘れる、または sources の
    entry を追加/差し替えた際に note の sha256 を書き換え忘れる・別の
    ダイジェストを貼り付けてしまう。
    """
    catalog = load_rates()
    assert catalog.catalog_version == "2026-09-09"

    matches = [
        s
        for s in catalog.sources
        if s.get("url") == "https://platform.claude.com/docs/en/about-claude/pricing"
    ]
    assert matches, catalog.sources
    assert any(
        "sha256=d79ad28567196bd55dd50e2fd89341b9da9774a45c1d02fcf387078507fd15e0"
        in s.get("note", "")
        for s in matches
    )


# --- 条件E: claude-sonnet-5 は promo/standard で rate_id が別名 -------------


def test_condition_e_claude_sonnet_5_rate_id_differs_before_and_after_promo_boundary():
    """条件E: 2026-09-01T00:00:00+00:00 より前の period の rate_id は
    "claude-sonnet-5-launch-promo"、同じ境界以降 (境界ちょうど含む) の
    period の rate_id は "claude-sonnet-5-standard-2026-09-01" である。

    落ちる変更の例: promo 期間と standard 期間の単価が同額のため、
    rate_id を使い回した/リネームし忘れた(単価だけを見るテストでは
    検出できない回帰)。
    """
    catalog = load_rates()

    resolved, before = catalog.rate_for(
        "claude-sonnet-5", dt("2026-08-31T23:59:59+00:00")
    )
    assert resolved == "claude-sonnet-5"
    assert before is not None
    assert before.rate_id == "claude-sonnet-5-launch-promo"

    for occurred_at in ("2026-09-01T00:00:00+00:00", "2026-12-01T00:00:00+00:00"):
        resolved, after = catalog.rate_for("claude-sonnet-5", dt(occurred_at))
        assert resolved == "claude-sonnet-5"
        assert after is not None
        assert after.rate_id == "claude-sonnet-5-standard-2026-09-01", occurred_at
