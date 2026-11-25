"""Catalog promotions (schema_version "3"): time-bounded fast-multiplier
overrides per (agent, mode). Synthetic data only; the packaged catalog
carries no promotion and must price exactly as before."""

import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from importlib import resources

import pytest

from agent_cost.aggregate import price_fact
from agent_cost.facts import Fact
from agent_cost.rates import RatesValidationError, load_rates

UTC = timezone.utc
ONE_US = timedelta(microseconds=1)

FROM = datetime(2026, 11, 25, 2, 5, tzinfo=UTC)
UNCERTAIN = datetime(2026, 12, 7, 10, 0, tzinfo=UTC)
UNTIL = datetime(2026, 12, 8, 12, 0, tzinfo=UTC)

PROMO = {
    "id": "codex-fast-standard-rate-2026-12",
    "agent": "codex",
    "mode": "fast",
    "fast_multiplier": "1.0",
    "effective_from": "2026-11-25T02:05:00Z",
    "uncertain_from": "2026-12-07T10:00:00Z",
    "effective_until": "2026-12-08T12:00:00Z",
    "source": "Codex in-app notice",
    "note": "observation cutoff, not the official start",
}


def _bundled():
    return json.loads(resources.files("agent_cost").joinpath("rates.json").read_text())


def _write(tmp_path, data):
    path = tmp_path / "rates.json"
    path.write_text(json.dumps(data))
    return path


def _catalog_with(tmp_path, promotions, schema_version="3", data=None):
    data = copy.deepcopy(data if data is not None else _bundled())
    data["schema_version"] = schema_version
    data["promotions"] = promotions
    return _write(tmp_path, data)


@pytest.fixture
def promo_catalog(tmp_path):
    return load_rates(_catalog_with(tmp_path, [dict(PROMO)]))


def _fact(model="gpt-6-astra", at=FROM, mode="fast", agent="codex", kind="input_nocache"):
    return Fact(
        occurred_at_utc=at,
        agent=agent,
        session_id="s1",
        model_raw=model,
        model_key=model,
        token_kind=kind,
        tokens=1_000_000,
        mode=mode,
    )


# ── boundaries (r2 acceptance table) ──

_BOUNDARIES = [
    ("before_start", FROM - ONE_US, False, "priced"),
    ("at_start", FROM, True, "priced"),
    ("before_uncertain", UNCERTAIN - ONE_US, True, "priced"),
    ("at_uncertain", UNCERTAIN, True, "lower_bound"),
    ("before_end", UNTIL - ONE_US, True, "lower_bound"),
    ("at_end", UNTIL, False, "priced"),
]


@pytest.mark.parametrize("label,at,promoted,status", _BOUNDARIES, ids=[b[0] for b in _BOUNDARIES])
def test_astra_fast_boundaries(promo_catalog, label, at, promoted, status):
    cost, got_status, credits = price_fact(promo_catalog, _fact("gpt-6-astra", at))
    if promoted:
        assert (cost, credits) == (Decimal("10"), Decimal("250"))
    else:
        assert (cost, credits) == (Decimal("25"), Decimal("625"))
    assert got_status == status


@pytest.mark.parametrize("label,at,promoted,status", _BOUNDARIES, ids=[b[0] for b in _BOUNDARIES])
def test_ordinary_codex_fast_boundaries(promo_catalog, label, at, promoted, status):
    # gpt-6.2-sol: input 2.00/MTok, 50 credits/MTok, fast_multiplier 2.0.
    cost, got_status, credits = price_fact(promo_catalog, _fact("gpt-6.2-sol", at))
    if promoted:
        assert (cost, credits) == (Decimal("2"), Decimal("50"))
    else:
        assert (cost, credits) == (Decimal("4"), Decimal("100"))
    assert got_status == status


def test_gpt_6_2_astra_fast_is_promoted(promo_catalog):
    plain = load_rates()
    base_cost, _, base_credits = price_fact(plain, _fact("gpt-6.2-astra", FROM, mode="normal"))
    cost, status, credits = price_fact(promo_catalog, _fact("gpt-6.2-astra", FROM))
    assert (cost, credits, status) == (base_cost, base_credits, "priced")


def test_promotion_never_raises_a_lower_model_multiplier(tmp_path):
    data = _bundled()
    data["models"] = [
        {
            "model_key": "half-fast",
            "aliases": [],
            "fast_multiplier": "0.5",
            "credits_per_mtok": {
                "input_nocache": "100",
                "cache_read": None,
                "cache_write_5m": None,
                "cache_write_1h": None,
                "output": None,
            },
            "rates": [
                {
                    "rate_id": "r1",
                    "effective_from": "2026-01-01T00:00:00Z",
                    "effective_until": None,
                    "input_nocache": "4.0",
                    "cache_read": None,
                    "cache_write_5m": None,
                    "cache_write_1h": None,
                    "output": None,
                }
            ],
        }
    ]
    catalog = load_rates(_catalog_with(tmp_path, [dict(PROMO)], data=data))
    expected = {FROM: "priced", UNCERTAIN: "lower_bound", UNTIL - ONE_US: "lower_bound"}
    for at, status in expected.items():
        # The model multiplier wins, but the uncertain tail stays lower_bound.
        assert price_fact(catalog, _fact("half-fast", at)) == (Decimal("2"), status, Decimal("50"))


@pytest.mark.parametrize("at", [FROM, UNCERTAIN])
def test_claude_fast_is_unaffected(promo_catalog, at):
    fact = _fact("claude-opus-5-5", at, agent="claude")
    assert price_fact(promo_catalog, fact) == price_fact(load_rates(), fact)
    assert price_fact(promo_catalog, fact)[0] == Decimal("8")


@pytest.mark.parametrize("mode", ["normal", "unknown"])
@pytest.mark.parametrize("model", ["gpt-6.2-sol", "gpt-6-astra"])
@pytest.mark.parametrize("at", [FROM, UNCERTAIN])
def test_normal_and_unknown_modes_are_unaffected(promo_catalog, mode, model, at):
    fact = _fact(model, at, mode=mode)
    assert price_fact(promo_catalog, fact) == price_fact(load_rates(), fact)


def test_strict_astra_guard_still_applies_during_promotion(promo_catalog):
    fact = Fact(
        occurred_at_utc=FROM,
        agent="codex",
        session_id="s1",
        model_raw="gpt-6-astra-fast",
        model_key="gpt-6-astra",
        token_kind="input_nocache",
        tokens=1_000_000,
        mode="fast",
    )
    assert price_fact(promo_catalog, fact) == (None, "unpriced", None)


def test_cache_write_unknown_lower_bound_is_kept_in_certain_window(tmp_path):
    catalog = load_rates(_catalog_with(tmp_path, [dict(PROMO, agent="claude")]))
    fact = _fact("claude-opus-5-5", FROM, agent="claude", kind="cache_write_unknown")
    cost, status, _ = price_fact(catalog, fact)
    assert status == "lower_bound"
    assert cost == Decimal("5.0")  # cache_write_5m 5.0 x min(2.0, 1.0)


def test_promotion_without_uncertain_from_is_priced_until_end(tmp_path):
    promo = {k: v for k, v in PROMO.items() if k != "uncertain_from"}
    catalog = load_rates(_catalog_with(tmp_path, [promo]))
    assert price_fact(catalog, _fact("gpt-6-astra", UNTIL - ONE_US)) == (
        Decimal("10"),
        "priced",
        Decimal("250"),
    )


# ── schema versions ──


def test_bundled_catalog_has_no_promotions_and_is_unchanged():
    catalog = load_rates()
    assert catalog.schema_version == "2"
    assert catalog.promotions == ()
    assert price_fact(catalog, _fact("gpt-6-astra", FROM)) == (Decimal("25"), "priced", Decimal("625"))


def test_schema_3_without_promotions_loads(tmp_path):
    data = _bundled()
    data["schema_version"] = "3"
    catalog = load_rates(_write(tmp_path, data))
    assert catalog.promotions == ()
    assert price_fact(catalog, _fact("gpt-6-astra", FROM))[0] == Decimal("25")


@pytest.mark.parametrize("schema_version", ["1", "2"])
def test_promotions_require_schema_3(tmp_path, schema_version):
    data = _bundled()
    if schema_version == "1":
        data["models"] = [m for m in data["models"] if not any("prompt_tiers" in r for r in m["rates"])]
    path = _catalog_with(tmp_path, [dict(PROMO)], schema_version=schema_version, data=data)
    with pytest.raises(RatesValidationError, match='schema_version "3"'):
        load_rates(path)


def test_schema_1_catalog_still_loads(tmp_path):
    data = _bundled()
    data["schema_version"] = "1"
    data["models"] = [m for m in data["models"] if not any("prompt_tiers" in r for r in m["rates"])]
    catalog = load_rates(_write(tmp_path, data))
    assert catalog.schema_version == "1"
    assert catalog.promotions == ()


# ── validation rejects (fail-closed) ──


def _without(key):
    return {k: v for k, v in PROMO.items() if k != key}


_NO_TAIL = _without("uncertain_from")


_REJECTS = {
    "not_a_list": (dict(PROMO), "must be a list"),
    "element_not_object": (["x"], "must be an object"),
    "unknown_key": ([dict(PROMO, model="gpt-6-astra")], "unknown keys"),
    "missing_id": ([_without("id")], "missing required keys"),
    "missing_note": ([_without("note")], "missing required keys"),
    "missing_until": ([_without("effective_until")], "missing required keys"),
    "empty_id": ([dict(PROMO, id="")], "id must be"),
    "duplicate_id": (
        [
            dict(_NO_TAIL, effective_until="2026-11-26T00:00:00Z"),
            dict(PROMO, agent="claude"),
        ],
        "duplicate promotion id",
    ),
    "unknown_agent": ([dict(PROMO, agent="gemini")], "agent must be"),
    "mode_normal": ([dict(PROMO, mode="normal")], "mode must be"),
    "mode_unknown": ([dict(PROMO, mode="unknown")], "mode must be"),
    "multiplier_not_numeric": ([dict(PROMO, fast_multiplier="cheap")], "invalid decimal"),
    "multiplier_bool": ([dict(PROMO, fast_multiplier=True)], "decimal string"),
    "multiplier_null": ([dict(PROMO, fast_multiplier=None)], "decimal string"),
    # JSON numbers are rejected: the parser rounds them to float before Decimal.
    "multiplier_json_int": ([dict(PROMO, fast_multiplier=1)], "decimal string"),
    "multiplier_json_float": ([dict(PROMO, fast_multiplier=1.0)], "decimal string"),
    "multiplier_tiny_negative_number": ([dict(PROMO, fast_multiplier=-1e-400)], "decimal string"),
    "multiplier_tiny_negative_string": ([dict(PROMO, fast_multiplier="-1e-400")], ">= 0"),
    "multiplier_negative": ([dict(PROMO, fast_multiplier="-1")], ">= 0"),
    "multiplier_nan": ([dict(PROMO, fast_multiplier="NaN")], "finite"),
    "multiplier_infinity": ([dict(PROMO, fast_multiplier="Infinity")], "finite"),
    "naive_from": ([dict(PROMO, effective_from="2026-11-25T02:05:00")], "timezone"),
    "naive_until": ([dict(PROMO, effective_until="2026-12-08T12:00:00")], "timezone"),
    "naive_uncertain": ([dict(PROMO, uncertain_from="2026-12-07T10:00:00")], "timezone"),
    "bad_datetime": ([dict(PROMO, effective_from="soon")], "invalid datetime"),
    "until_7_fraction_digits": (
        [dict(PROMO, effective_until="2026-12-08T12:00:00.0000001Z")],
        "fractional-second",
    ),
    "from_comma_7_fraction_digits": (
        [dict(PROMO, effective_from="2026-11-25T02:05:00,0000001+00:00")],
        "fractional-second",
    ),
    "uncertain_7_fraction_digits": (
        [dict(PROMO, uncertain_from="2026-12-07T10:00:00.1234567Z")],
        "fractional-second",
    ),
    "uncertain_null": ([dict(PROMO, uncertain_from=None)], "invalid datetime"),
    "uncertain_number": ([dict(PROMO, uncertain_from=0)], "invalid datetime"),
    "from_equals_until": (
        [dict(_NO_TAIL, effective_until="2026-11-25T02:05:00Z")],
        "after effective_from",
    ),
    "from_after_until": (
        [dict(_NO_TAIL, effective_until="2026-11-24T00:00:00Z")],
        "after effective_from",
    ),
    "uncertain_before_from": ([dict(PROMO, uncertain_from="2026-11-25T02:04:59Z")], "uncertain_from"),
    "uncertain_at_until": ([dict(PROMO, uncertain_from="2026-12-08T12:00:00Z")], "uncertain_from"),
    "overlap_same_scope": (
        [
            dict(PROMO),
            dict(_NO_TAIL, id="p2", effective_from="2026-12-08T11:00:00Z",
                 effective_until="2026-12-20T00:00:00Z"),
        ],
        "overlapping promotions",
    ),
}


@pytest.mark.parametrize("promotions,match", list(_REJECTS.values()), ids=list(_REJECTS))
def test_invalid_promotions_are_rejected(tmp_path, promotions, match):
    with pytest.raises(RatesValidationError, match=match):
        load_rates(_catalog_with(tmp_path, promotions))


def test_adjacent_and_cross_agent_promotions_are_accepted(tmp_path):
    promotions = [
        dict(PROMO),
        dict(_NO_TAIL, id="p2", effective_from="2026-12-08T12:00:00Z",
             effective_until="2026-12-20T00:00:00Z", fast_multiplier="1.5"),
        dict(PROMO, id="p3", agent="claude"),
    ]
    catalog = load_rates(_catalog_with(tmp_path, promotions))
    assert len(catalog.promotions) == 3
    # p2 starts exactly where the first promotion ends: 1.5 < 2.5 applies.
    assert price_fact(catalog, _fact("gpt-6-astra", UNTIL))[0] == Decimal("15")


def test_exact_decimal_multiplier_string_is_kept(tmp_path):
    # A string keeps its digits (a JSON number 0.999999 + float noise would not).
    promo = dict(PROMO, fast_multiplier="0.999999")
    catalog = load_rates(_catalog_with(tmp_path, [promo]))
    assert catalog.promotions[0].fast_multiplier == Decimal("0.999999")
    cost, status, credits = price_fact(catalog, _fact("gpt-6-astra", UNCERTAIN))
    assert status == "lower_bound"
    assert cost == Decimal("9.99999") and credits == Decimal("249.99975")


def test_six_fraction_digits_are_accepted(tmp_path):
    promo = dict(PROMO, effective_until="2026-12-08T12:00:00.000001Z")
    catalog = load_rates(_catalog_with(tmp_path, [promo]))
    assert catalog.promotions[0].effective_until == UNTIL + ONE_US
    assert price_fact(catalog, _fact("gpt-6-astra", UNTIL)) == (Decimal("10"), "lower_bound", Decimal("250"))


# ── r2: precision, model multiplier, datetime form, overflow ──

TWENTY_NINE_NINES = "0." + "9" * 29


@pytest.mark.parametrize("value", [TWENTY_NINE_NINES, "0.9999999", "12345", "1e0", "+1.0", ".5", "1."])
def test_overlong_or_odd_promotion_multiplier_is_rejected(tmp_path, value):
    # <= 4 integer and <= 6 fractional digits keeps every product and later
    # aggregate sum exact, so a lower_bound can never round above the real cost.
    with pytest.raises(RatesValidationError, match="must look like N"):
        load_rates(_catalog_with(tmp_path, [dict(PROMO, fast_multiplier=value)]))


def test_bundled_catalog_prices_are_identical_to_default_precision_arithmetic():
    import decimal as _decimal

    catalog = load_rates()
    checked = 0
    for key, entry in catalog.models.items():
        for period in entry.rates:
            at = period.effective_from
            for kind in ("input_nocache", "cache_read", "cache_write_5m", "cache_write_1h", "output"):
                for mode in ("normal", "fast"):
                    for agent in ("codex", "claude"):
                        fact = _fact(key, at, mode=mode, agent=agent, kind=kind)
                        got = price_fact(catalog, fact)
                        if got[1] != "priced":
                            continue
                        rate = period.values[kind]
                        mult = entry.fast_multiplier if mode == "fast" else Decimal("1.0")
                        with _decimal.localcontext(_decimal.Context()):
                            want_cost = (Decimal(1_000_000) / Decimal(1_000_000)) * rate * mult
                            want_credits = None
                            if entry.credits_per_mtok and entry.credits_per_mtok.get(kind) is not None:
                                want_credits = Decimal(1) * entry.credits_per_mtok[kind] * mult
                        assert (str(got[0]), str(got[2])) == (str(want_cost), str(want_credits)), (key, kind, mode)
                        checked += 1
    assert checked > 50


def _schema3_with_model_multiplier(tmp_path, value, promotions=()):
    data = _bundled()
    for m in data["models"]:
        if m["model_key"] == "gpt-6-astra":
            m["fast_multiplier"] = value
    return _catalog_with(tmp_path, list(promotions), data=data)


@pytest.mark.parametrize("value", [0.99999999999999999, 2, 2.5, None, True])
def test_schema_3_rejects_non_string_model_fast_multiplier(tmp_path, value):
    with pytest.raises(RatesValidationError, match="gpt-6-astra.fast_multiplier must be a decimal string"):
        load_rates(_schema3_with_model_multiplier(tmp_path, value))


@pytest.mark.parametrize("value", ["NaN", "-1", "fast"])
def test_schema_3_rejects_invalid_model_fast_multiplier_string(tmp_path, value):
    with pytest.raises(RatesValidationError, match="gpt-6-astra.fast_multiplier"):
        load_rates(_schema3_with_model_multiplier(tmp_path, value))


def test_schema_3_model_multiplier_string_keeps_its_digits(tmp_path):
    path = _schema3_with_model_multiplier(
        tmp_path, "0.999999", [dict(PROMO, fast_multiplier="1.0")]
    )
    catalog = load_rates(path)
    assert catalog.models["gpt-6-astra"].fast_multiplier == Decimal("0.999999")
    cost, status, credits = price_fact(catalog, _fact("gpt-6-astra", UNCERTAIN))
    assert status == "lower_bound"
    assert cost == Decimal("9.99999") and credits == Decimal("249.99975")


def test_schema_3_overlong_model_multiplier_is_rejected(tmp_path):
    with pytest.raises(RatesValidationError, match="must look like N"):
        load_rates(_schema3_with_model_multiplier(tmp_path, "0.99999999999999999"))


@pytest.mark.parametrize("schema_version", ["1", "2"])
def test_schema_1_and_2_still_accept_numeric_model_fast_multiplier(tmp_path, schema_version):
    data = _bundled()
    data["schema_version"] = schema_version
    if schema_version == "1":
        data["models"] = [m for m in data["models"] if not any("prompt_tiers" in r for r in m["rates"])]
    for m in data["models"]:
        if m["model_key"] == "gpt-6-astra":
            m["fast_multiplier"] = 2.5
    catalog = load_rates(_write(tmp_path, data))
    assert catalog.models["gpt-6-astra"].fast_multiplier == Decimal("2.5")


_BAD_FORMS = {
    "offset_with_fraction_seconds": "2026-12-08T12:00:00-00:00:00.5",
    "offset_with_seconds": "2026-12-08T12:00:00+01:00:30",
    "offset_without_colon": "2026-12-08T12:00:00+0100",
    "hour_only_offset": "2026-12-08T12:00:00+01",
    "offset_minutes_60": "2026-12-08T12:00:00+00:60",
    "offset_hours_24": "2026-12-08T12:00:00+24:00",
    "no_seconds": "2026-12-08T12:00Z",
    "space_separator": "2026-12-08 12:00:00Z",
    "lowercase_z": "2026-12-08T12:00:00z",
    "comma_fraction": "2026-12-08T12:00:00,5Z",
    "empty_fraction": "2026-12-08T12:00:00.Z",
    "basic_format": "20261208T120000Z",
    "week_date": "2026-W50-1T12:00:00Z",
    "non_ascii_digits": "２026-12-08T12:00:00Z",
    "trailing_newline": "2026-12-08T12:00:00Z\n",
    "offset_out_of_range": "2026-12-08T12:00:00+24:00",
}


@pytest.mark.parametrize("value", list(_BAD_FORMS.values()), ids=list(_BAD_FORMS))
def test_non_canonical_datetime_forms_are_rejected(tmp_path, value):
    with pytest.raises(RatesValidationError, match="invalid datetime"):
        load_rates(_catalog_with(tmp_path, [dict(PROMO, effective_until=value)]))


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-12-08T12:00:00.1Z", UNTIL + timedelta(microseconds=100_000)),
        ("2026-12-08T12:00:00.12Z", UNTIL + timedelta(microseconds=120_000)),
        ("2026-12-08T12:00:00.12345Z", UNTIL + timedelta(microseconds=123_450)),
        ("2026-12-08T21:00:00.5+09:00", UNTIL + timedelta(microseconds=500_000)),
        ("2026-12-08T11:00:00-01:00", UNTIL),
    ],
)
def test_short_fractions_and_offsets_are_normalized(tmp_path, value, expected):
    catalog = load_rates(_catalog_with(tmp_path, [dict(PROMO, effective_until=value)]))
    assert catalog.promotions[0].effective_until == expected


def test_short_fraction_parses_identically_without_fromisoformat_extensions(monkeypatch):
    # Python 3.9's fromisoformat accepts only 3 or 6 fractional digits; the
    # loader must hand it a 6-digit fraction whatever the input length.
    import agent_cost.rates as rates_mod

    seen = []
    real = datetime

    class _Strict(datetime):
        @classmethod
        def fromisoformat(cls, text):
            seen.append(text)
            return real.fromisoformat(text)

    monkeypatch.setattr(rates_mod, "datetime", _Strict)
    rates_mod._parse_aware_datetime("2026-12-08T12:00:00.1Z", field_name="x")
    assert seen == ["2026-12-08T12:00:00.100000+00:00"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("effective_until", "9999-12-31T23:59:59-01:00"),
        ("effective_from", "0001-01-01T00:00:00+01:00"),
    ],
)
def test_utc_conversion_overflow_is_a_validation_error(tmp_path, field, value):
    with pytest.raises(RatesValidationError, match="invalid datetime"):
        load_rates(_catalog_with(tmp_path, [dict(_NO_TAIL, **{field: value})]))


def test_rates_validate_reports_overflow_without_traceback(tmp_path, capsys):
    from agent_cost.cli import main

    path = _catalog_with(tmp_path, [dict(_NO_TAIL, effective_until="9999-12-31T23:59:59-01:00")])
    rc = main(["rates", "validate", str(path)])
    assert rc != 0
    out = capsys.readouterr()
    assert "invalid datetime" in out.out + out.err
