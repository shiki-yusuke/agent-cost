"""Load a confidential internal billing plan and apply it to Claude usage.

A billing plan (``plan_schema_version`` "1") is a local JSON file that
describes how a seat's Claude usage is billed internally: a list of finite
billing windows, each with a fixed subscription amount, an optional usage
allowance and, above the allowance, an overage rule. ``report
--billing-plan PATH`` prices Claude facts at the public catalog as usual
(``list_cost``) and then applies the plan per window to get
``internal_cost``. The catalog, the readers and ``price_fact`` are not
changed by any of this.

The plan's values are confidential. Every error raised here is a
``BillingPlanError`` whose message names a field, a period index or a rule
-- never a value, a path or a file name. Importing this module has no side
effects: it reads no environment variable and opens no file until
``load_billing_plan`` is called.
"""

from __future__ import annotations

import decimal
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Optional, Tuple, Union

from .aggregate import price_fact
from .facts import Fact
from .rates import RateCatalog

PLAN_SCHEMA_VERSION = "1"
CALC_SCHEMA_VERSION = "1"
SUPPORTED_BASIS = "agent-cost-list-price"
SUPPORTED_AGENT = "claude"
SUPPORTED_SCOPE = "seat"

_TOP_KEYS = frozenset({"plan_schema_version", "plan_id", "nonce", "applies_to", "basis", "periods"})
_APPLIES_TO_KEYS = frozenset({"agent", "scope"})
_REGULAR_KEYS = frozenset(
    {"period_id", "effective_from", "window_subscription_usd", "allowance_usd", "overage"}
)
_TERMINATES_KEYS = frozenset({"period_id", "effective_from", "terminates"})
_MULTIPLIER_KEYS = frozenset({"type", "value"})
_BLOCK_KEYS = frozenset({"type", "block_usd", "discount_usd"})

_PLAN_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
_PERIOD_ID = re.compile(r"[A-Za-z0-9_-]{1,16}")
_NONCE = re.compile(r"[0-9a-f]{32}")

_MAX_DIGITS = 28
_MAX_ABS_ADJUSTED = 12
_CALC_PREC = 60
_FOUR_PLACES = Decimal("0.0001")
_AMOUNT_FIELDS = (
    "list_cost_usd",
    "allowance_usd",
    "overage_usd",
    "overage_cost_usd",
    "window_subscription_usd",
    "internal_cost_usd",
)


class BillingPlanError(ValueError):
    """The billing plan could not be read, failed validation, or could not
    be applied exactly. The message never contains a plan value or path."""


@dataclass(frozen=True)
class OverageChargeMultiplier:
    """Overage is charged at ``overage * value`` (``0 <= value <= 1``)."""

    value: Decimal


@dataclass(frozen=True)
class OverageBlockDiscount:
    """Overage is charged at ``overage - floor(overage / block_usd) *
    discount_usd`` (``0 < discount_usd <= block_usd``)."""

    block_usd: Decimal
    discount_usd: Decimal


Overage = Union[OverageChargeMultiplier, OverageBlockDiscount]


@dataclass(frozen=True)
class PlanWindow:
    """One regular billing window, half-open ``[effective_from, until)``."""

    period_id: str
    effective_from: datetime
    until: datetime
    window_subscription_usd: Decimal
    # None means "no overage concept within the subscription" (not a zero
    # allowance); ``overage`` is then None too.
    allowance_usd: Optional[Decimal]
    overage: Optional[Overage]


@dataclass(frozen=True)
class BillingPlan:
    plan_schema_version: str
    plan_id: str
    nonce: str
    applies_to_agent: str
    applies_to_scope: str
    basis: str
    # Ascending by effective_from; contiguous by construction.
    windows: Tuple[PlanWindow, ...]
    terminates_period_id: str
    terminates_at: datetime


# ------------------------------------------------------------------ reading


def _reject_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise BillingPlanError("duplicate key in a JSON object")
        obj[key] = value
    return obj


def _has_git_ancestor(real_path: str) -> bool:
    current = os.path.dirname(real_path)
    while True:
        if os.path.lexists(os.path.join(current, ".git")):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _read_plan_bytes(path) -> bytes:
    """Read the plan file after the v5.1 section 5.2 checks."""
    if not hasattr(os, "getuid") or not hasattr(os, "O_NOFOLLOW"):
        raise BillingPlanError("billing plans are not supported on this platform")

    try:
        expanded = os.path.abspath(os.path.expanduser(os.fspath(path)))
        real = os.path.realpath(expanded)
    except OSError:
        raise BillingPlanError("plan path could not be resolved") from None
    if real != expanded:
        raise BillingPlanError("plan path must not go through a symlink")

    try:
        before = os.lstat(expanded)
    except FileNotFoundError:
        raise BillingPlanError("plan file not found") from None
    except PermissionError:
        raise BillingPlanError("plan file is not accessible") from None
    except OSError:
        raise BillingPlanError("plan file could not be inspected") from None
    if not stat.S_ISREG(before.st_mode):
        raise BillingPlanError("plan path is not a regular file")
    if _has_git_ancestor(real):
        raise BillingPlanError("plan file must not be inside a git repository or worktree")

    try:
        fd = os.open(expanded, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        raise BillingPlanError("plan file could not be opened") from None
    close_failed = False
    try:
        try:
            after = os.fstat(fd)
        except OSError:
            raise BillingPlanError("plan file could not be inspected") from None
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino) or not stat.S_ISREG(after.st_mode):
            raise BillingPlanError("plan file changed while it was being opened")
        if after.st_uid != os.getuid():
            raise BillingPlanError("plan file must be owned by the current user")
        if after.st_mode & 0o077:
            raise BillingPlanError("plan file must not be accessible by group or others (use chmod 600)")
        chunks = []
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                raise BillingPlanError("plan file could not be read") from None
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        try:
            os.close(fd)
        except OSError:
            # Raised after the finally block so a pending BillingPlanError
            # is not replaced by this one.
            close_failed = True
    if close_failed:
        raise BillingPlanError("plan file could not be closed")
    return b"".join(chunks)


def load_billing_plan(path: Path) -> BillingPlan:
    """Securely open, parse and validate the plan at ``path``."""
    raw = _read_plan_bytes(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise BillingPlanError("plan file is not valid UTF-8") from None
    return parse_billing_plan(text)


# --------------------------------------------------------------- validation


def _check_keys(obj, expected: frozenset, where: str) -> None:
    keys = set(obj)
    if keys == expected:
        return
    missing = sorted(expected - keys)
    problems = []
    if missing:
        problems.append("missing " + ", ".join(missing))
    if keys - expected:
        problems.append("unknown key(s) present")
    raise BillingPlanError(f"{where}: " + "; ".join(problems))


def _identifier(value, pattern, where: str, rule: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise BillingPlanError(f"{where}: must match {rule}")
    return value


def _decimal(value, where: str) -> Decimal:
    if not isinstance(value, str):
        raise BillingPlanError(f"{where}: must be a decimal string")
    try:
        d = Decimal(value)
    except (InvalidOperation, ValueError):
        raise BillingPlanError(f"{where}: must be a decimal string") from None
    if not d.is_finite():
        raise BillingPlanError(f"{where}: must be finite")
    if d < 0:
        raise BillingPlanError(f"{where}: must be >= 0")
    if len(d.as_tuple().digits) > _MAX_DIGITS:
        raise BillingPlanError(f"{where}: more than {_MAX_DIGITS} significant digits")
    if abs(d.adjusted()) > _MAX_ABS_ADJUSTED:
        raise BillingPlanError(f"{where}: exponent out of range (|adjusted| > {_MAX_ABS_ADJUSTED})")
    return Decimal(0) if d == 0 else d


def _timestamp(value, where: str) -> datetime:
    if not isinstance(value, str):
        raise BillingPlanError(f"{where}: must be an ISO 8601 string")
    # Python 3.9-3.10's fromisoformat() rejects a "Z" suffix; accept it as
    # +00:00 so the accepted syntax does not depend on the Python version.
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise BillingPlanError(f"{where}: must be an ISO 8601 date-time") from None
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise BillingPlanError(f"{where}: must carry a UTC offset")
    if dt.microsecond != 0:
        raise BillingPlanError(f"{where}: must not have sub-second precision")
    return dt.astimezone(timezone.utc)


def _overage(raw, where: str) -> Overage:
    if not isinstance(raw, dict):
        raise BillingPlanError(f"{where}: must be an object or null")
    kind = raw.get("type")
    if kind == "charge_multiplier":
        _check_keys(raw, _MULTIPLIER_KEYS, where)
        value = _decimal(raw["value"], f"{where}.value")
        if value > 1:
            raise BillingPlanError(f"{where}.value: must be <= 1")
        return OverageChargeMultiplier(value=value)
    if kind == "block_discount":
        _check_keys(raw, _BLOCK_KEYS, where)
        block = _decimal(raw["block_usd"], f"{where}.block_usd")
        discount = _decimal(raw["discount_usd"], f"{where}.discount_usd")
        if block <= 0:
            raise BillingPlanError(f"{where}.block_usd: must be > 0")
        if discount <= 0:
            raise BillingPlanError(f"{where}.discount_usd: must be > 0")
        if discount > block:
            raise BillingPlanError(f"{where}.discount_usd: must be <= block_usd")
        return OverageBlockDiscount(block_usd=block, discount_usd=discount)
    raise BillingPlanError(f"{where}.type: must be charge_multiplier or block_discount")


def parse_billing_plan(text: str) -> BillingPlan:
    """Parse and validate plan JSON text (no file access)."""
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except BillingPlanError:
        raise
    except (ValueError, RecursionError):
        raise BillingPlanError("plan is not valid JSON") from None

    if not isinstance(data, dict):
        raise BillingPlanError("plan: top level must be an object")
    _check_keys(data, _TOP_KEYS, "plan")

    if data["plan_schema_version"] != PLAN_SCHEMA_VERSION:
        raise BillingPlanError(f'plan_schema_version: must be "{PLAN_SCHEMA_VERSION}"')
    plan_id = _identifier(data["plan_id"], _PLAN_ID, "plan_id", "[A-Za-z0-9_-]{1,32}")
    nonce = _identifier(data["nonce"], _NONCE, "nonce", "[0-9a-f]{32}")

    applies_to = data["applies_to"]
    if not isinstance(applies_to, dict):
        raise BillingPlanError("applies_to: must be an object")
    _check_keys(applies_to, _APPLIES_TO_KEYS, "applies_to")
    if applies_to["agent"] != SUPPORTED_AGENT:
        raise BillingPlanError(f'applies_to.agent: must be "{SUPPORTED_AGENT}"')
    if applies_to["scope"] != SUPPORTED_SCOPE:
        raise BillingPlanError(f'applies_to.scope: must be "{SUPPORTED_SCOPE}"')
    if data["basis"] != SUPPORTED_BASIS:
        raise BillingPlanError(f'basis: must be "{SUPPORTED_BASIS}"')

    periods = data["periods"]
    if not isinstance(periods, list) or not periods:
        raise BillingPlanError("periods: must be a non-empty list")

    parsed = []  # (period_id, effective_from, raw, where)
    seen_ids = set()
    previous: Optional[datetime] = None
    last_index = len(periods) - 1
    for index, raw in enumerate(periods):
        where = f"periods[{index}]"
        if not isinstance(raw, dict):
            raise BillingPlanError(f"{where}: must be an object")
        is_terminates = "terminates" in raw
        if is_terminates and index != last_index:
            raise BillingPlanError(f"{where}: terminates is only allowed on the last period")
        if not is_terminates and index == last_index:
            raise BillingPlanError(f"{where}: the last period must have terminates: true")
        _check_keys(raw, _TERMINATES_KEYS if is_terminates else _REGULAR_KEYS, where)
        if is_terminates and raw["terminates"] is not True:
            raise BillingPlanError(f"{where}.terminates: must be true")

        period_id = _identifier(raw["period_id"], _PERIOD_ID, f"{where}.period_id", "[A-Za-z0-9_-]{1,16}")
        if period_id in seen_ids:
            raise BillingPlanError(f"{where}.period_id: duplicate period_id")
        seen_ids.add(period_id)

        effective_from = _timestamp(raw["effective_from"], f"{where}.effective_from")
        if previous is not None and effective_from <= previous:
            raise BillingPlanError(f"{where}.effective_from: must be strictly after the previous period")
        previous = effective_from
        parsed.append((period_id, effective_from, raw, where))

    if len(parsed) < 2:
        raise BillingPlanError("periods: at least one regular period is required")

    windows = []
    for (period_id, effective_from, raw, where), (_next_id, next_from, _next_raw, _w) in zip(parsed, parsed[1:]):
        subscription = _decimal(raw["window_subscription_usd"], f"{where}.window_subscription_usd")
        if raw["allowance_usd"] is None:
            if raw["overage"] is not None:
                raise BillingPlanError(f"{where}.overage: must be null when allowance_usd is null")
            allowance = None
            overage = None
        else:
            allowance = _decimal(raw["allowance_usd"], f"{where}.allowance_usd")
            if raw["overage"] is None:
                raise BillingPlanError(f"{where}.overage: required when allowance_usd is set")
            overage = _overage(raw["overage"], f"{where}.overage")
        windows.append(
            PlanWindow(
                period_id=period_id,
                effective_from=effective_from,
                until=next_from,
                window_subscription_usd=subscription,
                allowance_usd=allowance,
                overage=overage,
            )
        )

    terminates_id, terminates_at, _raw, _where = parsed[-1]
    return BillingPlan(
        plan_schema_version=PLAN_SCHEMA_VERSION,
        plan_id=plan_id,
        nonce=nonce,
        applies_to_agent=SUPPORTED_AGENT,
        applies_to_scope=SUPPORTED_SCOPE,
        basis=SUPPORTED_BASIS,
        windows=tuple(windows),
        terminates_period_id=terminates_id,
        terminates_at=terminates_at,
    )


# -------------------------------------------------------------- revision_id


def _canonical_decimal(d: Decimal) -> str:
    # A fresh context, so a caller's low precision or traps cannot round or
    # reject a value that validation accepted (at most 28 digits).
    with decimal.localcontext(decimal.Context(prec=_CALC_PREC, rounding=ROUND_HALF_EVEN, traps=[])):
        return "0" if d == 0 else format(d.normalize(), "f")


def _canonical_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _canonical_overage(overage: Optional[Overage]):
    if overage is None:
        return None
    if isinstance(overage, OverageChargeMultiplier):
        return {"type": "charge_multiplier", "value": _canonical_decimal(overage.value)}
    return {
        "type": "block_discount",
        "block_usd": _canonical_decimal(overage.block_usd),
        "discount_usd": _canonical_decimal(overage.discount_usd),
    }


def revision_id(plan: BillingPlan) -> str:
    """sha256 (lowercase hex) of the validated plan's canonical JSON.

    Identifies this exact plan revision (nonce included), independent of
    the file's whitespace, key order, decimal spelling or UTC offsets.
    """
    periods = [
        {
            "period_id": w.period_id,
            "effective_from": _canonical_time(w.effective_from),
            "window_subscription_usd": _canonical_decimal(w.window_subscription_usd),
            "allowance_usd": None if w.allowance_usd is None else _canonical_decimal(w.allowance_usd),
            "overage": _canonical_overage(w.overage),
        }
        for w in plan.windows
    ]
    periods.append(
        {
            "period_id": plan.terminates_period_id,
            "effective_from": _canonical_time(plan.terminates_at),
            "terminates": True,
        }
    )
    obj = {
        "plan_schema_version": plan.plan_schema_version,
        "plan_id": plan.plan_id,
        "nonce": plan.nonce,
        "applies_to": {"agent": plan.applies_to_agent, "scope": plan.applies_to_scope},
        "basis": plan.basis,
        "periods": periods,
    }
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# -------------------------------------------------------------- computation


def _uncovered(plan: BillingPlan, since: datetime, until: datetime):
    """Parts of ``[since, until)`` that no regular window covers."""
    gaps = []
    cursor = since
    for w in plan.windows:
        if w.until <= cursor:
            continue
        if w.effective_from >= until:
            break
        if w.effective_from > cursor:
            gaps.append((cursor, w.effective_from))
        cursor = w.until
        if cursor >= until:
            break
    if cursor < until:
        gaps.append((cursor, until))
    return gaps


def _certainty(window: PlanWindow, query_coverage: str, window_state: str, pricing: str) -> str:
    if query_coverage == "full" and window_state == "closed" and pricing == "priced":
        return "estimate"
    # g is non-decreasing for "no overage" and charge_multiplier, so a lower
    # bound on list_cost is a lower bound on internal_cost; block_discount's
    # g can drop at a block boundary, so nothing can be said.
    if isinstance(window.overage, OverageBlockDiscount):
        return "indeterminate"
    return "lower_bound"


def _window_result(window, priced_facts, *, since, until, generated_at) -> dict:
    since_w = max(since, window.effective_from)
    until_w = min(until, window.until)

    fact_count = 0
    priced_tokens = 0
    unpriced_tokens = 0
    list_cost = Decimal(0)
    any_unpriced = False
    any_lower_bound = False
    for f, cost, status in priced_facts:
        if not (since_w <= f.occurred_at_utc < until_w):
            continue
        fact_count += 1
        if status == "unpriced":
            any_unpriced = True
            unpriced_tokens += f.tokens
            continue
        if status == "lower_bound":
            any_lower_bound = True
        priced_tokens += f.tokens
        if cost is not None:
            list_cost += cost

    if window.allowance_usd is None:
        overage = Decimal(0)
        overage_cost = Decimal(0)
    else:
        overage = max(Decimal(0), list_cost - window.allowance_usd)
        if isinstance(window.overage, OverageChargeMultiplier):
            overage_cost = overage * window.overage.value
        else:
            # Integer division is exact (no Inexact trap) and, for
            # non-negative operands, equals ROUND_FLOOR of the quotient.
            blocks = overage // window.overage.block_usd
            overage_cost = overage - blocks * window.overage.discount_usd
    internal_cost = window.window_subscription_usd + overage_cost

    if fact_count == 0:
        pricing = "no_usage_observed"
    elif any_unpriced or any_lower_bound:
        pricing = "lower_bound"
    else:
        pricing = "priced"
    query_coverage = "full" if since <= window.effective_from and window.until <= until else "partial"
    window_state = "closed" if window.until <= generated_at else "open"

    return {
        "period_id": window.period_id,
        "from": window.effective_from.isoformat(),
        "until": window.until.isoformat(),
        "fact_count": fact_count,
        "priced_tokens": priced_tokens,
        "unpriced_tokens": unpriced_tokens,
        "list_cost_usd": list_cost,
        "allowance_usd": window.allowance_usd,
        "overage_usd": overage,
        "overage_cost_usd": overage_cost,
        "window_subscription_usd": window.window_subscription_usd,
        "internal_cost_usd": internal_cost,
        "query_coverage": query_coverage,
        "window_state": window_state,
        "list_cost_pricing": pricing,
        "internal_cost_certainty": _certainty(window, query_coverage, window_state, pricing),
    }


def compute_internal_billing(
    plan: BillingPlan,
    facts: Iterable[Fact],
    catalog: RateCatalog,
    *,
    since: datetime,
    until: datetime,
    generated_at: datetime,
) -> dict:
    """Apply ``plan`` to the Claude facts in ``[since, until)``.

    Returns the ``internal_billing`` block with amounts as unrounded
    ``Decimal`` (see ``format_internal_billing`` for the output form). All
    arithmetic runs at prec=60 with Inexact/Rounded trapped, so any
    rounding is an error rather than a silent change of the result.
    """
    since = since.astimezone(timezone.utc)
    until = until.astimezone(timezone.utc)
    generated_at = generated_at.astimezone(timezone.utc)
    windows = []
    with decimal.localcontext() as ctx:
        ctx.prec = _CALC_PREC
        ctx.rounding = ROUND_HALF_EVEN
        ctx.traps[decimal.Inexact] = True
        ctx.traps[decimal.Rounded] = True
        try:
            # Priced in the same trapped context as the sums, so a fact whose
            # price would round at the default precision is an error too.
            # Within normal token counts this is the same Decimal build_rows
            # would have summed.
            priced_facts = []
            for f in facts:
                if f.agent == "claude" and since <= f.occurred_at_utc < until:
                    cost, status, _credits = price_fact(catalog, f)
                    priced_facts.append((f, cost, status))
            for window in plan.windows:
                if max(since, window.effective_from) < min(until, window.until):
                    windows.append(
                        _window_result(
                            window, priced_facts, since=since, until=until, generated_at=generated_at
                        )
                    )
        except decimal.DecimalException:
            raise BillingPlanError("internal billing arithmetic would round; refusing to compute") from None

    uncovered = [{"from": a.isoformat(), "until": b.isoformat()} for a, b in _uncovered(plan, since, until)]
    return {
        "calc_schema_version": CALC_SCHEMA_VERSION,
        "plan_id": plan.plan_id,
        "revision_id": revision_id(plan),
        "basis": plan.basis,
        "catalog_version": catalog.catalog_version,
        "catalog_sha256": catalog.sha256,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "generated_at": generated_at.isoformat(),
        "plan_coverage": "partial" if uncovered else "full",
        "uncovered": uncovered,
        "windows": windows,
    }


def _four_places(d: Decimal) -> str:
    with decimal.localcontext() as ctx:
        ctx.prec = _CALC_PREC
        ctx.traps[decimal.Inexact] = False
        ctx.traps[decimal.Rounded] = False
        return format(d.quantize(_FOUR_PLACES, rounding=ROUND_HALF_EVEN), "f")


def format_internal_billing(block: dict) -> dict:
    """JSON-ready copy of ``compute_internal_billing``'s result: every
    amount as a fixed 4-decimal string (``None`` stays ``None``)."""
    out = dict(block)
    out["uncovered"] = [dict(u) for u in block["uncovered"]]
    out["windows"] = []
    for window in block["windows"]:
        formatted = dict(window)
        for key in _AMOUNT_FIELDS:
            if formatted[key] is not None:
                formatted[key] = _four_places(formatted[key])
        out["windows"].append(formatted)
    return out
