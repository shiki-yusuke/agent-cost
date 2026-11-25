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
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from importlib import resources
from pathlib import Path
from typing import Optional, Tuple

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
# wrong by a fixed, silent factor. Schema "2" adds optional per-period
# ``prompt_tiers``; it is a separate version (and "1" may not carry tiers)
# so that a reader that predates tiers rejects a tiered catalog as
# unsupported instead of silently pricing every request at the base tier.
# Schema "3" adds the optional top-level ``promotions`` array (time-bounded
# fast-multiplier overrides); "1"/"2" may not carry it, so a reader that
# predates promotions rejects such a catalog instead of ignoring them.
SUPPORTED_SCHEMA_VERSIONS = ("1", "2", "3")
SUPPORTED_CURRENCIES = ("USD",)
SUPPORTED_UNITS = ("per_mtok",)


class RatesValidationError(ValueError):
    """The rates catalog failed structural or business-rule validation."""


@dataclass(frozen=True)
class PromptTier:
    """Rate values for requests whose prompt length is strictly greater
    than ``prompt_tokens_over``."""

    prompt_tokens_over: int
    values: dict


@dataclass(frozen=True)
class RatePeriod:
    rate_id: str
    effective_from: datetime
    effective_until: Optional[datetime]
    # Base tier: prompt length <= the first tier's threshold (or every
    # request, when there are no tiers).
    values: dict
    # Ascending by threshold; a request uses the tier with the largest
    # threshold its prompt length exceeds. Every value in a tiered period
    # is non-null and non-decreasing tier to tier, so ``values`` is the
    # cheapest (a lower bound) when the prompt length is unknown.
    prompt_tiers: Tuple[PromptTier, ...] = ()

    def covers(self, moment: datetime) -> bool:
        if moment < self.effective_from:
            return False
        if self.effective_until is not None and moment >= self.effective_until:
            return False
        return True


PROMOTION_AGENTS = ("codex", "claude")
PROMOTION_MODES = ("fast",)
_PROMOTION_REQUIRED_KEYS = (
    "id",
    "agent",
    "mode",
    "fast_multiplier",
    "effective_from",
    "effective_until",
    "source",
    "note",
)
_PROMOTION_OPTIONAL_KEYS = ("uncertain_from",)


@dataclass(frozen=True)
class Promotion:
    """A time-bounded override of the fast multiplier for one (agent, mode).

    Applies to every model of ``agent`` for facts whose ``occurred_at_utc``
    is in ``[effective_from, effective_until)``. ``[uncertain_from,
    effective_until)``, when set, is the stretch where it is unknown whether
    the promotion still applied: facts there are priced at the promotion
    multiplier but marked ``lower_bound``.
    """

    promotion_id: str
    agent: str
    mode: str
    fast_multiplier: Decimal
    effective_from: datetime
    effective_until: datetime
    uncertain_from: Optional[datetime]
    source: str
    note: str

    def covers(self, moment: datetime) -> bool:
        return self.effective_from <= moment < self.effective_until

    def is_uncertain(self, moment: datetime) -> bool:
        return self.uncertain_from is not None and self.uncertain_from <= moment < self.effective_until


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
    promotions: Tuple[Promotion, ...] = ()

    def promotion_for(self, agent: str, mode: str, occurred_at_utc: datetime) -> Optional[Promotion]:
        """Return the promotion covering ``(agent, mode)`` at the moment, if any.

        Validation guarantees promotions for the same (agent, mode) never
        overlap, so at most one can match.
        """
        for promo in self.promotions:
            if promo.agent == agent and promo.mode == mode and promo.covers(occurred_at_utc):
                return promo
        return None

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


_OVERPRECISE_FRACTION = re.compile(r"[.,]\d{7,}")


# Promotion boundaries accept exactly one textual form so that every
# supported Python (3.9's fromisoformat is much stricter than 3.11+'s)
# parses them identically: ASCII digits, a "T" separator, seconds, an
# optional 1-6 digit "." fraction, and a "Z" or "+HH:MM"/"-HH:MM" offset.
# Anything else (offsets with seconds or fractions, week dates, a space
# separator, ...) is rejected rather than interpreted.
_AWARE_DATETIME = re.compile(
    r"(?P<base>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]{1,6}))?"
    r"(?P<offset>Z|[+-][0-9]{2}:[0-9]{2})"
)


def _parse_aware_datetime(value, *, field_name: str) -> datetime:
    """Parse a promotion boundary: an explicit timezone is required (no
    UTC assumption) and only the ``_AWARE_DATETIME`` form is accepted."""
    if not isinstance(value, str):
        raise RatesValidationError(f"invalid datetime for {field_name}: {value!r}")
    # fromisoformat silently truncates fractional seconds past microseconds,
    # which would move a window boundary; reject what cannot be represented.
    if _OVERPRECISE_FRACTION.search(value):
        raise RatesValidationError(
            f"{field_name} has more than 6 fractional-second digits: {value!r}"
        )
    m = _AWARE_DATETIME.fullmatch(value)
    if m is None:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?", value):
            raise RatesValidationError(f"{field_name} must include a timezone, got {value!r}")
        raise RatesValidationError(
            f"invalid datetime for {field_name}: {value!r} "
            "(expected YYYY-MM-DDTHH:MM:SS[.ffffff](Z|+HH:MM|-HH:MM))"
        )
    # Pad the fraction to 6 digits: Python 3.9's fromisoformat only accepts
    # 3 or 6 fractional digits.
    fraction = m.group("fraction")
    offset = m.group("offset")
    text = m.group("base")
    if fraction is not None:
        text += "." + fraction.ljust(6, "0")
    text += "+00:00" if offset == "Z" else offset
    try:
        dt = datetime.fromisoformat(text)
        return dt.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise RatesValidationError(f"invalid datetime for {field_name}: {value!r}") from exc


def _parse_promotion_multiplier(value, *, field_name: str) -> Decimal:
    # Only JSON strings: a JSON number is rounded to a float by the parser
    # before Decimal sees it (e.g. -1e-400 -> -0.0, 0.99999999999999999 -> 1.0).
    if not isinstance(value, str):
        raise RatesValidationError(f"{field_name} must be a decimal string, got {value!r}")
    try:
        d = Decimal(value)
    except InvalidOperation as exc:
        raise RatesValidationError(f"invalid decimal for {field_name}: {value!r}") from exc
    if not d.is_finite():
        raise RatesValidationError(f"{field_name} must be finite, got {value!r}")
    if d < 0:
        raise RatesValidationError(f"{field_name} must be >= 0, got {d}")
    return d


def _build_promotions(raw_list) -> Tuple[Promotion, ...]:
    if not isinstance(raw_list, list):
        raise RatesValidationError("promotions must be a list")
    allowed = set(_PROMOTION_REQUIRED_KEYS) | set(_PROMOTION_OPTIONAL_KEYS)
    promotions = []
    seen_ids = set()
    for idx, raw in enumerate(raw_list):
        where = f"promotions[{idx}]"
        if not isinstance(raw, dict):
            raise RatesValidationError(f"{where}: must be an object")
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise RatesValidationError(f"{where}: unknown keys {unknown}")
        missing = [k for k in _PROMOTION_REQUIRED_KEYS if k not in raw]
        if missing:
            raise RatesValidationError(f"{where}: missing required keys {missing}")
        promo_id = raw["id"]
        if not isinstance(promo_id, str) or not promo_id:
            raise RatesValidationError(f"{where}: id must be a non-empty string")
        where = f"promotions[{promo_id}]"
        if promo_id in seen_ids:
            raise RatesValidationError(f"duplicate promotion id: {promo_id!r}")
        seen_ids.add(promo_id)
        for k in ("source", "note"):
            if not isinstance(raw[k], str) or not raw[k]:
                raise RatesValidationError(f"{where}: {k} must be a non-empty string")
        if raw["agent"] not in PROMOTION_AGENTS:
            raise RatesValidationError(
                f"{where}: agent must be one of {PROMOTION_AGENTS}, got {raw['agent']!r}"
            )
        if raw["mode"] not in PROMOTION_MODES:
            raise RatesValidationError(
                f"{where}: mode must be one of {PROMOTION_MODES}, got {raw['mode']!r}"
            )
        multiplier = _parse_promotion_multiplier(
            raw["fast_multiplier"], field_name=f"{where}.fast_multiplier"
        )
        effective_from = _parse_aware_datetime(raw["effective_from"], field_name=f"{where}.effective_from")
        effective_until = _parse_aware_datetime(raw["effective_until"], field_name=f"{where}.effective_until")
        if effective_from >= effective_until:
            raise RatesValidationError(f"{where}: effective_until must be after effective_from")
        uncertain_from = None
        # Omitting the key means "no uncertain tail"; an explicit null (or any
        # non-string) is rejected rather than treated as omitted.
        if "uncertain_from" in raw:
            uncertain_from = _parse_aware_datetime(raw["uncertain_from"], field_name=f"{where}.uncertain_from")
            if not (effective_from <= uncertain_from < effective_until):
                raise RatesValidationError(
                    f"{where}: uncertain_from must be within [effective_from, effective_until)"
                )
        promotions.append(
            Promotion(
                promotion_id=promo_id,
                agent=raw["agent"],
                mode=raw["mode"],
                fast_multiplier=multiplier,
                effective_from=effective_from,
                effective_until=effective_until,
                uncertain_from=uncertain_from,
                source=raw["source"],
                note=raw["note"],
            )
        )

    by_scope: dict = {}
    for promo in promotions:
        by_scope.setdefault((promo.agent, promo.mode), []).append(promo)
    for group in by_scope.values():
        group.sort(key=lambda p: p.effective_from)
        for a, b in zip(group, group[1:]):
            if a.effective_until > b.effective_from:
                raise RatesValidationError(
                    f"overlapping promotions {a.promotion_id!r} and {b.promotion_id!r} "
                    f"for agent {a.agent!r} mode {a.mode!r}"
                )
    return tuple(promotions)


def _build_prompt_tiers(tiers_raw, base_values: dict, *, where: str) -> Tuple[PromptTier, ...]:
    if not isinstance(tiers_raw, list):
        raise RatesValidationError(f"{where}: prompt_tiers must be a list")
    if not tiers_raw:
        return ()

    for f in RATE_FIELDS:
        if base_values[f] is None:
            raise RatesValidationError(f"{where}: {f} is required when prompt_tiers is present")

    tiers = []
    previous_values = base_values
    previous_over = None
    for idx, tier_raw in enumerate(tiers_raw):
        tier_where = f"{where}.prompt_tiers[{idx}]"
        if not isinstance(tier_raw, dict):
            raise RatesValidationError(f"{tier_where}: must be an object")
        over = tier_raw.get("prompt_tokens_over")
        if isinstance(over, bool) or not isinstance(over, int) or over <= 0:
            raise RatesValidationError(f"{tier_where}: prompt_tokens_over must be a positive int, got {over!r}")
        if previous_over is not None and over <= previous_over:
            raise RatesValidationError(
                f"{tier_where}: prompt_tokens_over must be ascending without duplicates "
                f"({over} after {previous_over})"
            )
        values = {f: _parse_decimal(tier_raw.get(f), field_name=f"{tier_where}.{f}") for f in RATE_FIELDS}
        for f in RATE_FIELDS:
            if values[f] is None:
                raise RatesValidationError(f"{tier_where}: {f} is required")
            if values[f] < previous_values[f]:
                raise RatesValidationError(
                    f"{tier_where}: {f} {values[f]} is lower than the previous tier's {previous_values[f]}"
                )
        tiers.append(PromptTier(prompt_tokens_over=over, values=values))
        previous_values = values
        previous_over = over
    return tuple(tiers)


def _build_model_entry(raw: dict, *, schema_version: str) -> ModelEntry:
    model_key = raw.get("model_key")
    if not model_key or not isinstance(model_key, str):
        raise RatesValidationError(f"model entry missing model_key: {raw!r}")

    aliases = tuple(raw.get("aliases") or [])

    if schema_version == "3":
        # Schema "3" takes the model multiplier as a JSON string only, like a
        # promotion's: a JSON number is rounded to a float by the parser
        # (0.99999999999999999 -> 1.0), which would distort min(model,
        # promotion) and the lower bound built on it.
        fast_multiplier = _parse_promotion_multiplier(
            raw.get("fast_multiplier", "1.0"), field_name=f"{model_key}.fast_multiplier"
        )
    else:
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
        prompt_tiers: Tuple[PromptTier, ...] = ()
        if "prompt_tiers" in rate_raw:
            if schema_version == "1":
                raise RatesValidationError(
                    f"{model_key}.{rate_id}: prompt_tiers requires schema_version \"2\""
                )
            prompt_tiers = _build_prompt_tiers(
                rate_raw["prompt_tiers"], values, where=f"{model_key}.{rate_id}"
            )
        periods.append(
            RatePeriod(
                rate_id=rate_id,
                effective_from=effective_from,
                effective_until=effective_until,
                values=values,
                prompt_tiers=prompt_tiers,
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
        entry = _build_model_entry(raw, schema_version=schema_version)
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

    promotions: Tuple[Promotion, ...] = ()
    if "promotions" in data:
        if schema_version != "3":
            raise RatesValidationError('promotions requires schema_version "3"')
        promotions = _build_promotions(data["promotions"])

    return RateCatalog(
        schema_version=schema_version,
        catalog_version=catalog_version,
        currency=currency,
        unit=unit,
        usd_per_credit=usd_per_credit,
        sources=sources,
        models=models,
        _alias_index=alias_index,
        promotions=promotions,
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
