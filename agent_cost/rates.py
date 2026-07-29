"""Load, validate, and query the historical rate catalog (rates.json).

The catalog is a plain JSON document describing, per model, one or more
time-bounded rate periods (so a price change shows up as a new period
rather than overwriting history). All rate values are decimal strings in
the JSON and are parsed with `decimal.Decimal` throughout -- float is
never used for money.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from importlib import resources
from pathlib import Path
from typing import Optional

RATE_FIELDS = (
    "input_nocache",
    "cache_read",
    "cache_write_5m",
    "cache_write_1h",
    "output",
)

# The only schema_version/currency/unit values this version of agent-cost
# knows how to interpret. A catalog claiming anything else is rejected
# outright rather than silently treated as USD-per-mtok -- a catalog in a
# different currency or unit priced as if it were USD-per-mtok would be
# wrong by a fixed, silent factor.
SUPPORTED_SCHEMA_VERSIONS = ("1",)
SUPPORTED_CURRENCIES = ("USD",)
SUPPORTED_UNITS = ("per_mtok",)


class RatesValidationError(ValueError):
    """The rates catalog failed structural or business-rule validation."""


@dataclass(frozen=True)
class RatePeriod:
    rate_id: str
    effective_from: datetime
    effective_until: Optional[datetime]
    values: dict

    def covers(self, moment: datetime) -> bool:
        if moment < self.effective_from:
            return False
        if self.effective_until is not None and moment >= self.effective_until:
            return False
        return True


@dataclass(frozen=True)
class ModelEntry:
    model_key: str
    aliases: tuple
    fast_multiplier: Decimal
    rates: tuple
    credits_per_mtok: Optional[dict] = None


@dataclass
class RateCatalog:
    schema_version: str
    catalog_version: str
    currency: str
    unit: str
    usd_per_credit: Decimal
    sources: list
    models: dict = field(default_factory=dict)
    sha256: str = ""
    _alias_index: dict = field(default_factory=dict)

    def resolve_model_key(self, model_key: str) -> Optional[str]:
        """Map a fact's ``model_key`` to a canonical catalog ``model_key``.

        Tries an exact match first, then the catalog's alias table (this
        is separate from -- and applied after -- the generic suffix
        stripping in ``facts.normalize_model_key``).
        """
        if model_key in self.models:
            return model_key
        return self._alias_index.get(model_key)

    def rate_for(self, model_key: str, occurred_at_utc: datetime):
        """Return ``(resolved_model_key, RatePeriod)``, either of which may
        be ``None`` when the model or the specific period is not priced."""
        resolved = self.resolve_model_key(model_key)
        if resolved is None:
            return None, None
        entry = self.models[resolved]
        for period in entry.rates:
            if period.covers(occurred_at_utc):
                return resolved, period
        return resolved, None


def _parse_decimal(value, *, field_name: str) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation as exc:
        raise RatesValidationError(f"invalid decimal for {field_name}: {value!r}") from exc
    if d < 0:
        raise RatesValidationError(f"{field_name} must be >= 0, got {d}")
    return d


def _parse_datetime(value, *, field_name: str) -> datetime:
    try:
        text = value
        if isinstance(text, str) and text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    except (ValueError, TypeError) as exc:
        raise RatesValidationError(f"invalid datetime for {field_name}: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _build_model_entry(raw: dict) -> ModelEntry:
    model_key = raw.get("model_key")
    if not model_key or not isinstance(model_key, str):
        raise RatesValidationError(f"model entry missing model_key: {raw!r}")

    aliases = tuple(raw.get("aliases") or [])

    fast_multiplier = _parse_decimal(
        raw.get("fast_multiplier", "1.0"), field_name=f"{model_key}.fast_multiplier"
    )
    if fast_multiplier is None:
        fast_multiplier = Decimal("1.0")

    credits_raw = raw.get("credits_per_mtok")
    credits_per_mtok = None
    if credits_raw is not None:
        credits_per_mtok = {
            f: _parse_decimal(credits_raw.get(f), field_name=f"{model_key}.credits_per_mtok.{f}")
            for f in RATE_FIELDS
        }

    periods = []
    for rate_raw in raw.get("rates") or []:
        rate_id = rate_raw.get("rate_id")
        if not rate_id:
            raise RatesValidationError(f"{model_key}: rate entry missing rate_id")
        if "effective_from" not in rate_raw:
            raise RatesValidationError(f"{model_key}.{rate_id}: missing effective_from")
        effective_from = _parse_datetime(
            rate_raw["effective_from"], field_name=f"{model_key}.{rate_id}.effective_from"
        )
        effective_until = (
            _parse_datetime(rate_raw["effective_until"], field_name=f"{model_key}.{rate_id}.effective_until")
            if rate_raw.get("effective_until")
            else None
        )
        if effective_until is not None and effective_until <= effective_from:
            raise RatesValidationError(
                f"{model_key}.{rate_id}: effective_until must be after effective_from"
            )
        values = {
            f: _parse_decimal(rate_raw.get(f), field_name=f"{model_key}.{rate_id}.{f}")
            for f in RATE_FIELDS
        }
        periods.append(
            RatePeriod(
                rate_id=rate_id,
                effective_from=effective_from,
                effective_until=effective_until,
                values=values,
            )
        )

    periods.sort(key=lambda p: p.effective_from)
    open_end = datetime.max.replace(tzinfo=timezone.utc)
    for a, b in zip(periods, periods[1:]):
        a_end = a.effective_until or open_end
        if a_end > b.effective_from:
            raise RatesValidationError(
                f"{model_key}: overlapping rate periods {a.rate_id!r} and {b.rate_id!r}"
            )

    return ModelEntry(
        model_key=model_key,
        aliases=aliases,
        fast_multiplier=fast_multiplier,
        rates=tuple(periods),
        credits_per_mtok=credits_per_mtok,
    )


def _validate_and_build(data: dict) -> RateCatalog:
    if not isinstance(data, dict):
        raise RatesValidationError("rates catalog root must be an object")

    catalog_version = data.get("catalog_version")
    if not catalog_version:
        raise RatesValidationError("missing catalog_version")

    schema_version = data.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise RatesValidationError(
            f"unsupported schema_version: {schema_version!r} (supported: {SUPPORTED_SCHEMA_VERSIONS})"
        )

    currency = data.get("currency")
    if currency not in SUPPORTED_CURRENCIES:
        raise RatesValidationError(
            f"unsupported currency: {currency!r} (supported: {SUPPORTED_CURRENCIES})"
        )

    unit = data.get("unit")
    if unit not in SUPPORTED_UNITS:
        raise RatesValidationError(f"unsupported unit: {unit!r} (supported: {SUPPORTED_UNITS})")

    usd_per_credit = _parse_decimal(data.get("usd_per_credit"), field_name="usd_per_credit")
    if usd_per_credit is None:
        usd_per_credit = Decimal("0")
    sources = data.get("sources") or []

    models: dict = {}
    for raw in data.get("models") or []:
        entry = _build_model_entry(raw)
        if entry.model_key in models:
            raise RatesValidationError(f"duplicate model_key: {entry.model_key}")
        models[entry.model_key] = entry

    alias_index: dict = {}
    for entry in models.values():
        for alias in entry.aliases:
            if alias == entry.model_key:
                raise RatesValidationError(f"{entry.model_key}: alias cannot equal its own model_key")
            if alias in models:
                # Aliases live in a separate namespace from model_keys by
                # construction; anything else would let alias resolution
                # loop back on itself (an alias cycle).
                raise RatesValidationError(
                    f"alias {alias!r} on {entry.model_key} collides with an existing model_key"
                )
            if alias in alias_index:
                raise RatesValidationError(f"duplicate alias: {alias!r}")
            alias_index[alias] = entry.model_key

    return RateCatalog(
        schema_version=schema_version,
        catalog_version=catalog_version,
        currency=currency,
        unit=unit,
        usd_per_credit=usd_per_credit,
        sources=sources,
        models=models,
        _alias_index=alias_index,
    )


def load_rates(path: Optional[Path] = None) -> RateCatalog:
    """Load and validate a rate catalog.

    ``path=None`` loads the catalog packaged with agent-cost
    (``agent_cost/rates.json``). A caller-supplied path fully replaces
    the packaged catalog -- it is never merged with it.
    """
    if path is not None:
        raw_bytes = Path(path).read_bytes()
    else:
        raw_bytes = resources.files("agent_cost").joinpath("rates.json").read_bytes()
    try:
        data = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise RatesValidationError(f"invalid JSON: {exc}") from exc
    catalog = _validate_and_build(data)
    catalog.sha256 = hashlib.sha256(raw_bytes).hexdigest()
    return catalog
