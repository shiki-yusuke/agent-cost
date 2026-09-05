"""Price facts and aggregate them into report rows.

Pricing and grouping are pure functions over a list of ``Fact`` and a
``RateCatalog`` -- they know nothing about where the facts came from, so
they're testable independently of any reader.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Optional, Tuple
from zoneinfo import ZoneInfo

from .facts import Fact
from .rates import RateCatalog

GROUP_DIMENSIONS = ("month", "agent", "model", "token-kind")
_STATUS_RANK = {"unpriced": 0, "lower_bound": 1, "priced": 2}


def price_fact(catalog: RateCatalog, fact: Fact) -> Tuple[Optional[Decimal], str, Optional[Decimal]]:
    """Price one fact. Returns ``(estimated_cost_usd, pricing_status, credits)``.

    ``cache_write_unknown`` (a cache write whose TTL couldn't be
    determined) is priced at the 5-minute rate as a *lower bound* --
    never at the more expensive 1-hour rate -- and flagged accordingly.
    An unrecognized model or an unpriced token kind for an otherwise-known
    model both return ``(None, "unpriced", None)`` rather than a guess.
    """
    resolved_key, period = catalog.rate_for(fact.model_key, fact.occurred_at_utc)
    if resolved_key is None or period is None:
        return None, "unpriced", None

    # Astra's verified rates cover only the exact Codex model ID. Generic
    # suffix normalization must not implicitly price unverified snapshots,
    # speed variants or usage from a different agent with this new entry.
    if resolved_key == "gpt-6-astra" and (
        fact.agent != "codex" or fact.model_raw != "gpt-6-astra" or fact.mode == "unknown"
    ):
        return None, "unpriced", None

    rate_field = fact.token_kind
    lower_bound = False
    if fact.token_kind == "cache_write_unknown":
        rate_field = "cache_write_5m"
        lower_bound = True

    rate = period.values.get(rate_field)
    if rate is None:
        return None, "unpriced", None

    entry = catalog.models[resolved_key]
    tokens = Decimal(fact.tokens)
    multiplier = entry.fast_multiplier if fact.mode == "fast" else Decimal("1.0")
    cost = (tokens / Decimal(1_000_000)) * rate * multiplier

    credits = None
    if entry.credits_per_mtok is not None:
        credit_rate = entry.credits_per_mtok.get(rate_field)
        if credit_rate is not None:
            credits = (tokens / Decimal(1_000_000)) * credit_rate * multiplier

    status = "lower_bound" if lower_bound else "priced"
    return cost, status, credits


def filter_facts(
    facts: Iterable[Fact],
    *,
    since_utc: Optional[datetime] = None,
    until_utc: Optional[datetime] = None,
    agents: Optional[set] = None,
):
    """Apply the ``[since, until)`` half-open window and an agent filter."""
    for f in facts:
        if since_utc is not None and f.occurred_at_utc < since_utc:
            continue
        if until_utc is not None and f.occurred_at_utc >= until_utc:
            continue
        if agents is not None and f.agent not in agents:
            continue
        yield f


@dataclass
class DataQuality:
    malformed_events: int = 0
    skipped_files: int = 0
    negative_deltas: int = 0
    unpriced_tokens: int = 0

    def to_dict(self) -> dict:
        return {
            "malformed_events": self.malformed_events,
            "skipped_files": self.skipped_files,
            "negative_deltas": self.negative_deltas,
            "unpriced_tokens": self.unpriced_tokens,
        }


@dataclass
class Row:
    month: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    token_kind: Optional[str] = None
    tokens: int = 0
    priced_tokens: int = 0
    unpriced_tokens: int = 0
    estimated_cost_usd: Decimal = field(default_factory=lambda: Decimal("0"))
    credits: Decimal = field(default_factory=lambda: Decimal("0"))
    pricing_status: str = "priced"

    def to_dict(self) -> dict:
        return {
            "month": self.month,
            "agent": self.agent,
            "model": self.model,
            "token_kind": self.token_kind,
            "tokens": self.tokens,
            "priced_tokens": self.priced_tokens,
            "unpriced_tokens": self.unpriced_tokens,
            "estimated_cost_usd": float(self.estimated_cost_usd),
            "credits": float(self.credits),
            "pricing_status": self.pricing_status,
        }


def _month_key(dt: datetime, tz: ZoneInfo) -> str:
    local = dt.astimezone(tz)
    return f"{local.year:04d}-{local.month:02d}"


def build_rows(
    facts: Iterable[Fact],
    catalog: RateCatalog,
    *,
    group_by: Tuple[str, ...] = GROUP_DIMENSIONS,
    timezone_name: str = "UTC",
) -> Tuple[list, DataQuality]:
    """Group priced facts into rows. Dimensions not in ``group_by`` are
    left ``None`` on every row (per-fact filtering/pricing still happens
    on the full, ungrouped dimension set).

    A row's ``pricing_status`` is the worst status among its facts
    (``unpriced`` beats ``lower_bound`` beats ``priced``), so a row is
    never silently reported as fully priced when part of it wasn't.
    """
    tz = ZoneInfo(timezone_name)
    buckets: dict = {}
    unpriced_tokens_total = 0

    for f in facts:
        cost, status, credits = price_fact(catalog, f)
        if status == "unpriced":
            unpriced_tokens_total += f.tokens

        month = _month_key(f.occurred_at_utc, tz) if "month" in group_by else None
        agent = f.agent if "agent" in group_by else None
        model = f.model_key if "model" in group_by else None
        token_kind = f.token_kind if "token-kind" in group_by else None
        key = (month, agent, model, token_kind)

        row = buckets.get(key)
        if row is None:
            row = Row(month=month, agent=agent, model=model, token_kind=token_kind)
            buckets[key] = row

        row.tokens += f.tokens
        if status == "unpriced":
            row.unpriced_tokens += f.tokens
        else:
            row.priced_tokens += f.tokens
            if cost is not None:
                row.estimated_cost_usd += cost
            if credits is not None:
                row.credits += credits

        if _STATUS_RANK[status] < _STATUS_RANK[row.pricing_status]:
            row.pricing_status = status

    rows = sorted(
        buckets.values(),
        key=lambda r: (r.month or "", r.agent or "", r.model or "", r.token_kind or ""),
    )
    return rows, DataQuality(unpriced_tokens=unpriced_tokens_total)


def rows_totals(rows: Iterable[Row]) -> dict:
    """Sum a set of rows into a single scalar totals dict.

    Sums the underlying Decimal cost/credits fields (not their float
    conversions) so precision isn't lost across many rows before
    converting to float once at the end, for callers (like ``measure``)
    that need one aggregate number across rows that were grouped by
    dimensions they don't care about.
    """
    rows = list(rows)
    estimated_cost_usd = sum((r.estimated_cost_usd for r in rows), Decimal("0"))
    credits = sum((r.credits for r in rows), Decimal("0"))
    return {
        "tokens": sum(r.tokens for r in rows),
        "priced_tokens": sum(r.priced_tokens for r in rows),
        "unpriced_tokens": sum(r.unpriced_tokens for r in rows),
        "estimated_cost_usd": float(estimated_cost_usd),
        "credits": float(credits),
    }
