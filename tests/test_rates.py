from datetime import datetime, timezone
from decimal import Decimal

import pytest

from agent_cost.rates import RatesValidationError, load_rates

UTC = timezone.utc


def dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def test_packaged_catalog_loads_and_validates():
    catalog = load_rates()
    assert catalog.catalog_version
    assert "claude-opus-4-8" in catalog.models
    assert "gpt-5.5" in catalog.models


def test_claude_opus_rates():
    catalog = load_rates()
    _, period = catalog.rate_for("claude-opus-4-8", dt("2026-06-01T00:00:00+00:00"))
    assert period is not None
    assert period.values["input_nocache"] == Decimal("5.0")
    assert period.values["cache_write_5m"] == Decimal("6.25")
    assert period.values["cache_read"] == Decimal("0.50")
    assert period.values["output"] == Decimal("25.0")


def test_gpt_5_5_cache_write_is_unpriced():
    catalog = load_rates()
    _, period = catalog.rate_for("gpt-5.5", dt("2026-07-15T00:00:00+00:00"))
    assert period.values["cache_write_5m"] is None


def test_sonnet_5_historical_switch_before_and_after_cutover():
    catalog = load_rates()
    _, promo = catalog.rate_for("claude-sonnet-5", dt("2026-08-31T23:00:00+00:00"))
    assert promo.values["input_nocache"] == Decimal("2.0")

    _, standard = catalog.rate_for("claude-sonnet-5", dt("2026-09-01T00:00:00+00:00"))
    assert standard.values["input_nocache"] == Decimal("3.0")

    _, still_standard = catalog.rate_for("claude-sonnet-5", dt("2026-12-01T00:00:00+00:00"))
    assert still_standard.values["input_nocache"] == Decimal("3.0")


def test_alias_resolution():
    catalog = load_rates()
    resolved, period = catalog.rate_for("gpt-5.5-codex", dt("2026-07-15T00:00:00+00:00"))
    assert resolved == "gpt-5.5"
    assert period.values["input_nocache"] == Decimal("5.0")


def test_unknown_model_is_unresolved():
    catalog = load_rates()
    resolved, period = catalog.rate_for("totally-unknown-model", dt("2026-07-15T00:00:00+00:00"))
    assert resolved is None
    assert period is None


def test_unknown_period_for_known_model_before_effective_from():
    catalog = load_rates()
    resolved, period = catalog.rate_for("gpt-5.5", dt("2020-01-01T00:00:00+00:00"))
    assert resolved == "gpt-5.5"
    assert period is None


def _minimal_catalog(models):
    return {
        "schema_version": "1",
        "catalog_version": "test",
        "currency": "USD",
        "unit": "per_mtok",
        "usd_per_credit": "0.04",
        "sources": [],
        "models": models,
    }


def _rate(rate_id, effective_from, effective_until=None, **overrides):
    base = {
        "rate_id": rate_id,
        "effective_from": effective_from,
        "effective_until": effective_until,
        "input_nocache": "1.0",
        "cache_read": "0.1",
        "cache_write_5m": "1.25",
        "cache_write_1h": "2.0",
        "output": "5.0",
    }
    base.update(overrides)
    return base


def test_duplicate_model_key_raises(tmp_path):
    data = _minimal_catalog(
        [
            {"model_key": "m1", "aliases": [], "rates": [_rate("r1", "2025-01-01T00:00:00+00:00")]},
            {"model_key": "m1", "aliases": [], "rates": [_rate("r2", "2025-01-01T00:00:00+00:00")]},
        ]
    )
    path = tmp_path / "rates.json"
    path.write_text(__import__("json").dumps(data))
    with pytest.raises(RatesValidationError, match="duplicate model_key"):
        load_rates(path)


def test_alias_colliding_with_model_key_raises(tmp_path):
    data = _minimal_catalog(
        [
            {"model_key": "m1", "aliases": ["m2"], "rates": [_rate("r1", "2025-01-01T00:00:00+00:00")]},
            {"model_key": "m2", "aliases": [], "rates": [_rate("r1", "2025-01-01T00:00:00+00:00")]},
        ]
    )
    path = tmp_path / "rates.json"
    path.write_text(__import__("json").dumps(data))
    with pytest.raises(RatesValidationError, match="collides"):
        load_rates(path)


def test_duplicate_alias_raises(tmp_path):
    data = _minimal_catalog(
        [
            {"model_key": "m1", "aliases": ["alias-x"], "rates": [_rate("r1", "2025-01-01T00:00:00+00:00")]},
            {"model_key": "m2", "aliases": ["alias-x"], "rates": [_rate("r1", "2025-01-01T00:00:00+00:00")]},
        ]
    )
    path = tmp_path / "rates.json"
    path.write_text(__import__("json").dumps(data))
    with pytest.raises(RatesValidationError, match="duplicate alias"):
        load_rates(path)


def test_negative_rate_value_raises(tmp_path):
    data = _minimal_catalog(
        [
            {
                "model_key": "m1",
                "aliases": [],
                "rates": [_rate("r1", "2025-01-01T00:00:00+00:00", input_nocache="-1.0")],
            },
        ]
    )
    path = tmp_path / "rates.json"
    path.write_text(__import__("json").dumps(data))
    with pytest.raises(RatesValidationError, match=">= 0"):
        load_rates(path)


def test_overlapping_rate_periods_raise(tmp_path):
    data = _minimal_catalog(
        [
            {
                "model_key": "m1",
                "aliases": [],
                "rates": [
                    _rate("r1", "2025-01-01T00:00:00+00:00", "2025-06-01T00:00:00+00:00"),
                    _rate("r2", "2025-05-01T00:00:00+00:00", None),
                ],
            },
        ]
    )
    path = tmp_path / "rates.json"
    path.write_text(__import__("json").dumps(data))
    with pytest.raises(RatesValidationError, match="overlapping"):
        load_rates(path)


def test_adjacent_non_overlapping_periods_are_ok(tmp_path):
    data = _minimal_catalog(
        [
            {
                "model_key": "m1",
                "aliases": [],
                "rates": [
                    _rate("r1", "2025-01-01T00:00:00+00:00", "2025-06-01T00:00:00+00:00"),
                    _rate("r2", "2025-06-01T00:00:00+00:00", None),
                ],
            },
        ]
    )
    path = tmp_path / "rates.json"
    path.write_text(__import__("json").dumps(data))
    catalog = load_rates(path)
    assert len(catalog.models["m1"].rates) == 2


def test_rates_path_fully_replaces_default(tmp_path):
    data = _minimal_catalog(
        [{"model_key": "only-model", "aliases": [], "rates": [_rate("r1", "2025-01-01T00:00:00+00:00")]}]
    )
    path = tmp_path / "rates.json"
    path.write_text(__import__("json").dumps(data))
    catalog = load_rates(path)
    assert list(catalog.models.keys()) == ["only-model"]
    assert "claude-opus-4-8" not in catalog.models


def test_sha256_is_stable_for_same_content(tmp_path):
    data = _minimal_catalog(
        [{"model_key": "m1", "aliases": [], "rates": [_rate("r1", "2025-01-01T00:00:00+00:00")]}]
    )
    path = tmp_path / "rates.json"
    path.write_text(__import__("json").dumps(data))
    c1 = load_rates(path)
    c2 = load_rates(path)
    assert c1.sha256 == c2.sha256
    assert len(c1.sha256) == 64
