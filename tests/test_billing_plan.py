"""Spec: agent_cost.billing_plan (agent-cost 0.5.0, plan_schema_version "1").

Every amount, allowance and discount here is fictional (subscription 10,
allowance 50, block 3 / discount 2, multiplier 0.5, ...), and so are the
dates. ``SENTINEL`` is planted in every rejected fixture to check that no
error message (nor its formatted traceback) echoes a plan value, the plan's
path or its file name.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from agent_cost import billing_plan as bp
from agent_cost.facts import Fact
from agent_cost.rates import load_rates

SENTINEL = "7777.7777"
PLAN_FILE_NAME = "plan-secret-name.json"
NONCE = "0123456789abcdef0123456789abcdef"


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def _base_plan() -> dict:
    return {
        "plan_schema_version": "1",
        "plan_id": "bp-test",
        "nonce": NONCE,
        "applies_to": {"agent": "claude", "scope": "seat"},
        "basis": "agent-cost-list-price",
        "periods": [
            {
                "period_id": "p1",
                "effective_from": "2030-01-01T00:00:00+00:00",
                "window_subscription_usd": SENTINEL,
                "allowance_usd": None,
                "overage": None,
            },
            {
                "period_id": "p2",
                "effective_from": "2030-01-11T00:00:00+00:00",
                "window_subscription_usd": "10",
                "allowance_usd": "50",
                "overage": {"type": "charge_multiplier", "value": "0.5"},
            },
            {"period_id": "end", "effective_from": "2030-02-01T00:00:00+00:00", "terminates": True},
        ],
    }


def _write_plan(directory: Path, content, *, mode: int = 0o600, name: str = PLAN_FILE_NAME) -> Path:
    path = directory / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    elif isinstance(content, str):
        path.write_text(content)
    else:
        path.write_text(json.dumps(content))
    os.chmod(path, mode)
    return path


def _assert_no_leak(exc: BaseException, path: Path) -> None:
    rendered = str(exc) + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    message = str(exc)
    assert SENTINEL not in rendered
    assert str(path) not in rendered
    assert str(path.parent) not in message
    assert PLAN_FILE_NAME not in rendered
    assert "plan-secret-name" not in rendered


def _load_rejected(directory: Path, content) -> bp.BillingPlanError:
    path = _write_plan(directory, content)
    with pytest.raises(bp.BillingPlanError) as info:
        bp.load_billing_plan(path)
    _assert_no_leak(info.value, path)
    return info.value


# ---------------------------------------------------------------- happy path


def test_valid_plan_loads(tmp_path):
    plan = bp.load_billing_plan(_write_plan(tmp_path, _base_plan()))
    assert isinstance(plan, bp.BillingPlan)
    assert plan.plan_id == "bp-test"
    assert plan.nonce == NONCE
    assert plan.basis == "agent-cost-list-price"
    assert [w.period_id for w in plan.windows] == ["p1", "p2"]
    p1, p2 = plan.windows
    assert p1.effective_from == _utc("2030-01-01T00:00:00+00:00")
    assert p1.until == _utc("2030-01-11T00:00:00+00:00")
    assert p2.until == _utc("2030-02-01T00:00:00+00:00")
    assert p1.window_subscription_usd == Decimal(SENTINEL)
    assert p1.allowance_usd is None and p1.overage is None
    assert p2.allowance_usd == Decimal("50")
    assert isinstance(p2.overage, bp.OverageChargeMultiplier)
    assert p2.overage.value == Decimal("0.5")
    assert plan.terminates_period_id == "end"
    assert plan.terminates_at == _utc("2030-02-01T00:00:00+00:00")


def test_frozen_dataclasses(tmp_path):
    plan = bp.load_billing_plan(_write_plan(tmp_path, _base_plan()))
    with pytest.raises(Exception):
        plan.plan_id = "other"  # type: ignore[misc]
    with pytest.raises(Exception):
        plan.windows[0].allowance_usd = Decimal("1")  # type: ignore[misc]


def test_block_discount_overage_parses(tmp_path):
    data = _base_plan()
    data["periods"][1]["overage"] = {"type": "block_discount", "block_usd": "5", "discount_usd": "1"}
    plan = bp.load_billing_plan(_write_plan(tmp_path, data))
    overage = plan.windows[1].overage
    assert isinstance(overage, bp.OverageBlockDiscount)
    assert overage.block_usd == Decimal("5") and overage.discount_usd == Decimal("1")


def test_block_discount_equal_discount_and_block_is_allowed(tmp_path):
    data = _base_plan()
    data["periods"][1]["overage"] = {"type": "block_discount", "block_usd": "5", "discount_usd": "5"}
    bp.load_billing_plan(_write_plan(tmp_path, data))


def test_multiplier_bounds_zero_and_one_are_allowed(tmp_path):
    for value in ("0", "1"):
        data = _base_plan()
        data["periods"][1]["overage"] = {"type": "charge_multiplier", "value": value}
        bp.parse_billing_plan(json.dumps(data))


def test_offset_is_normalized_to_utc():
    data = _base_plan()
    data["periods"][0]["effective_from"] = "2030-01-01T09:00:00+09:00"
    plan = bp.parse_billing_plan(json.dumps(data))
    assert plan.windows[0].effective_from == _utc("2030-01-01T00:00:00+00:00")
    assert plan.windows[0].effective_from.utcoffset().total_seconds() == 0


def test_whole_second_offset_is_normalized_to_utc():
    data = _base_plan()
    data["periods"][0]["effective_from"] = "2030-01-01T00:00:00+00:00:30"
    plan = bp.parse_billing_plan(json.dumps(data))
    assert plan.windows[0].effective_from == _utc("2029-12-31T23:59:30+00:00")


def test_fractional_second_offset_rejected_as_sub_second():
    # The local time has no microseconds, but the UTC instant does.
    data = _base_plan()
    data["periods"][0]["effective_from"] = "2030-01-01T00:00:00+00:00:30.500000"
    with pytest.raises(bp.BillingPlanError) as info:
        bp.parse_billing_plan(json.dumps(data))
    assert "sub-second" in str(info.value)


def test_offset_overflowing_utc_rejected_as_billing_plan_error():
    data = _base_plan()
    data["periods"][0]["effective_from"] = "0001-01-01T00:00:00+23:59"
    with pytest.raises(bp.BillingPlanError) as info:
        bp.parse_billing_plan(json.dumps(data))
    rendered = "".join(traceback.format_exception(type(info.value), info.value, info.value.__traceback__))
    assert "OverflowError" not in rendered


def test_exponent_notation_and_negative_zero_accepted():
    data = _base_plan()
    data["periods"][1]["allowance_usd"] = "5E+1"
    data["periods"][1]["window_subscription_usd"] = "-0"
    plan = bp.parse_billing_plan(json.dumps(data))
    assert plan.windows[1].allowance_usd == Decimal("50")
    assert plan.windows[1].window_subscription_usd == Decimal("0")


def test_decimal_limits_at_the_edge_are_accepted():
    data = _base_plan()
    data["periods"][1]["allowance_usd"] = "1234567890.123456789012345678"  # 28 digits, adjusted 9
    bp.parse_billing_plan(json.dumps(data))
    data["periods"][1]["allowance_usd"] = "1E+12"
    bp.parse_billing_plan(json.dumps(data))
    data["periods"][1]["allowance_usd"] = "1E-12"
    bp.parse_billing_plan(json.dumps(data))


# ------------------------------------------------------- parse / validate: reject


def _mutate(fn):
    data = _base_plan()
    fn(data)
    return data


def _set_period(index, key, value):
    def apply(data):
        data["periods"][index][key] = value

    return apply


def _del_period(index, key):
    def apply(data):
        del data["periods"][index][key]

    return apply


def _set_top(key, value):
    def apply(data):
        data[key] = value

    return apply


def _del_top(key):
    def apply(data):
        del data[key]

    return apply


def _set_applies(key, value):
    def apply(data):
        data["applies_to"][key] = value

    return apply


def _replace_periods(periods):
    def apply(data):
        data["periods"] = periods

    return apply


def _terminating(pid="end", ts="2030-02-01T00:00:00+00:00"):
    return {"period_id": pid, "effective_from": ts, "terminates": True}


def _regular(pid, ts, sub=SENTINEL, allowance=None, overage=None):
    return {
        "period_id": pid,
        "effective_from": ts,
        "window_subscription_usd": sub,
        "allowance_usd": allowance,
        "overage": overage,
    }


REJECTED_MUTATIONS = {
    # key sets
    "top_unknown_key": _set_top(SENTINEL, SENTINEL),
    "top_missing_key": _del_top("basis"),
    "applies_to_unknown_key": _set_applies("tier", SENTINEL),
    "applies_to_missing_key": lambda d: d["applies_to"].pop("scope"),
    "period_unknown_key": _set_period(1, "note", SENTINEL),
    "period_missing_key": _del_period(1, "allowance_usd"),
    "terminates_extra_key": _set_period(2, "window_subscription_usd", SENTINEL),
    "overage_extra_key_multiplier": _set_period(
        1, "overage", {"type": "charge_multiplier", "value": "0.5", "block_usd": SENTINEL}
    ),
    "overage_extra_key_block": _set_period(
        1,
        "overage",
        {"type": "block_discount", "block_usd": "5", "discount_usd": "1", "value": SENTINEL},
    ),
    "overage_missing_key_block": _set_period(1, "overage", {"type": "block_discount", "block_usd": SENTINEL}),
    "overage_missing_type": _set_period(1, "overage", {"value": SENTINEL}),
    "overage_unknown_type": _set_period(1, "overage", {"type": "tiered", "value": SENTINEL}),
    "overage_not_object": _set_period(1, "overage", SENTINEL),
    # types
    "amount_is_json_number": _set_period(1, "window_subscription_usd", 7777.7777),
    "amount_is_json_bool": _set_period(1, "allowance_usd", True),
    "amount_is_non_decimal_string": _set_period(1, "allowance_usd", "abc" + SENTINEL),
    "amount_nan": _set_period(1, "allowance_usd", "NaN"),
    "amount_infinity": _set_period(1, "allowance_usd", "Infinity"),
    "amount_snan": _set_period(1, "allowance_usd", "sNaN"),
    "amount_negative": _set_period(1, "allowance_usd", "-" + SENTINEL),
    "amount_too_many_digits": _set_period(1, "allowance_usd", "1.2345678901234567890123456789" + "7777"),
    "amount_exponent_too_large": _set_period(1, "allowance_usd", "7.7777E+13"),
    "amount_exponent_too_small": _set_period(1, "allowance_usd", "7.7777E-13"),
    "multiplier_value_number": _set_period(1, "overage", {"type": "charge_multiplier", "value": 0.5}),
    "multiplier_above_one": _set_period(1, "overage", {"type": "charge_multiplier", "value": SENTINEL}),
    "multiplier_negative": _set_period(1, "overage", {"type": "charge_multiplier", "value": "-0.5"}),
    "block_discount_greater_than_block": _set_period(
        1, "overage", {"type": "block_discount", "block_usd": "5", "discount_usd": SENTINEL}
    ),
    "block_usd_zero": _set_period(
        1, "overage", {"type": "block_discount", "block_usd": "0", "discount_usd": SENTINEL}
    ),
    "discount_usd_zero": _set_period(
        1, "overage", {"type": "block_discount", "block_usd": SENTINEL, "discount_usd": "0"}
    ),
    "terminates_false": _set_period(2, "terminates", False),
    "terminates_string": _set_period(2, "terminates", "true"),
    "terminates_number": _set_period(2, "terminates", 1),
    # allowance / overage pairing
    "allowance_null_overage_set": _set_period(0, "overage", {"type": "charge_multiplier", "value": "0.5"}),
    "allowance_set_overage_null": _set_period(1, "overage", None),
    # effective_from
    "effective_from_no_offset": _set_period(1, "effective_from", "2030-01-11T00:00:00"),
    "effective_from_date_only": _set_period(1, "effective_from", "2030-01-11"),
    "effective_from_microseconds": _set_period(1, "effective_from", "2030-01-11T00:00:00.000001+00:00"),
    "effective_from_unparseable": _set_period(1, "effective_from", "soon-" + SENTINEL),
    "effective_from_not_string": _set_period(1, "effective_from", 7777),
    "effective_from_descending": _set_period(1, "effective_from", "2029-12-01T00:00:00+00:00"),
    "effective_from_duplicate": _set_period(1, "effective_from", "2030-01-01T00:00:00+00:00"),
    "effective_from_equal_instant_other_offset": _set_period(1, "effective_from", "2030-01-01T09:00:00+09:00"),
    "terminates_before_last_regular": _set_period(2, "effective_from", "2030-01-05T00:00:00+00:00"),
    # periods structure
    "last_not_terminates": _replace_periods(
        [_regular("p1", "2030-01-01T00:00:00+00:00"), _regular("p2", "2030-01-11T00:00:00+00:00")]
    ),
    "terminates_in_middle": _replace_periods(
        [
            _regular("p1", "2030-01-01T00:00:00+00:00"),
            _terminating("mid", "2030-01-11T00:00:00+00:00"),
            _regular("p2", "2030-01-21T00:00:00+00:00"),
            _terminating("end", "2030-02-01T00:00:00+00:00"),
        ]
    ),
    "terminates_twice": _replace_periods(
        [
            _regular("p1", "2030-01-01T00:00:00+00:00"),
            _terminating("end1", "2030-01-11T00:00:00+00:00"),
            _terminating("end2", "2030-02-01T00:00:00+00:00"),
        ]
    ),
    "period_id_duplicate": _set_period(1, "period_id", "p1"),
    "period_id_duplicate_with_terminates": _set_period(2, "period_id", "p1"),
    "no_regular_windows": _replace_periods([_terminating()]),
    "empty_periods": _replace_periods([]),
    "periods_not_list": _set_top("periods", {"p1": SENTINEL}),
    "period_not_object": _replace_periods([SENTINEL, _terminating()]),
    # constants / identifiers
    "basis_wrong": _set_top("basis", "invoice-" + SENTINEL),
    "applies_to_agent_codex": _set_applies("agent", "codex"),
    "applies_to_scope_org": _set_applies("scope", "org"),
    "applies_to_not_object": _set_top("applies_to", SENTINEL),
    "schema_version_two": _set_top("plan_schema_version", "2"),
    "schema_version_number": _set_top("plan_schema_version", 1),
    "plan_id_too_long": _set_top("plan_id", "a" * 33),
    "plan_id_bad_chars": _set_top("plan_id", "bp " + SENTINEL),
    "plan_id_empty": _set_top("plan_id", ""),
    "nonce_uppercase": _set_top("nonce", NONCE.upper()),
    "nonce_short": _set_top("nonce", NONCE[:31]),
    "nonce_not_string": _set_top("nonce", 7777),
    "period_id_too_long": _set_period(1, "period_id", "p" * 17),
    "period_id_bad_chars": _set_period(1, "period_id", SENTINEL),
    "terminates_period_id_bad": _set_period(2, "period_id", "end." + SENTINEL),
}


@pytest.mark.parametrize("case", sorted(REJECTED_MUTATIONS))
def test_invalid_plan_rejected_without_leaking(case, tmp_path):
    data = _mutate(REJECTED_MUTATIONS[case])
    _load_rejected(tmp_path, data)


def test_top_level_not_object_rejected(tmp_path):
    _load_rejected(tmp_path, json.dumps([SENTINEL]))


def test_invalid_json_rejected(tmp_path):
    _load_rejected(tmp_path, '{"plan_id": "' + SENTINEL + '",')


def test_json_nan_literal_rejected(tmp_path):
    text = json.dumps(_base_plan()).replace('"' + SENTINEL + '"', "NaN")
    _load_rejected(tmp_path, text)


def test_invalid_utf8_rejected(tmp_path):
    _load_rejected(tmp_path, b'{"plan_id": "\xff\xfe' + SENTINEL.encode() + b'"}')


def test_duplicate_top_level_key_rejected(tmp_path):
    text = json.dumps(_base_plan())
    text = text.replace('"plan_id": "bp-test"', '"plan_id": "bp-test", "plan_id": "' + SENTINEL + '"')
    assert text.count('"plan_id"') == 2
    _load_rejected(tmp_path, text)


def test_duplicate_nested_key_rejected(tmp_path):
    text = json.dumps(_base_plan())
    text = text.replace('"allowance_usd": "50"', '"allowance_usd": "50", "allowance_usd": "' + SENTINEL + '"')
    assert text.count('"allowance_usd"') == 3
    _load_rejected(tmp_path, text)


def test_duplicate_key_in_applies_to_rejected(tmp_path):
    text = json.dumps(_base_plan())
    text = text.replace('"scope": "seat"', '"scope": "seat", "scope": "seat"')
    _load_rejected(tmp_path, text)


def test_error_message_names_the_field(tmp_path):
    exc = _load_rejected(tmp_path, _mutate(_set_period(1, "allowance_usd", 50)))
    assert "periods[1].allowance_usd" in str(exc)


def test_error_message_names_the_rule_for_ordering(tmp_path):
    exc = _load_rejected(tmp_path, _mutate(REJECTED_MUTATIONS["effective_from_descending"]))
    assert "effective_from" in str(exc)


@pytest.mark.parametrize(
    "case, rule",
    [
        ("block_usd_zero", "periods[1].overage.block_usd: must be > 0"),
        ("discount_usd_zero", "periods[1].overage.discount_usd: must be > 0"),
        ("block_discount_greater_than_block", "periods[1].overage.discount_usd: must be <= block_usd"),
    ],
)
def test_block_discount_rules_name_their_own_rule(case, rule, tmp_path):
    # Each case breaks exactly one rule, so dropping that rule's check makes
    # the case fall through to another message (or be accepted).
    exc = _load_rejected(tmp_path, _mutate(REJECTED_MUTATIONS[case]))
    assert str(exc) == rule


def test_billing_plan_error_is_value_error():
    assert issubclass(bp.BillingPlanError, ValueError)


def test_parse_billing_plan_rejects_without_file():
    with pytest.raises(bp.BillingPlanError):
        bp.parse_billing_plan(json.dumps(_mutate(_set_top("basis", "x"))))


# ------------------------------------------------------------ secure open


def test_mode_0644_rejected(tmp_path):
    path = _write_plan(tmp_path, _base_plan(), mode=0o644)
    with pytest.raises(bp.BillingPlanError) as info:
        bp.load_billing_plan(path)
    _assert_no_leak(info.value, path)


def test_mode_0640_and_0601_rejected(tmp_path):
    for mode in (0o640, 0o601, 0o620):
        path = _write_plan(tmp_path, _base_plan(), mode=mode)
        with pytest.raises(bp.BillingPlanError):
            bp.load_billing_plan(path)


def test_mode_0400_accepted(tmp_path):
    bp.load_billing_plan(_write_plan(tmp_path, _base_plan(), mode=0o400))


def test_owned_by_current_user_accepted(tmp_path):
    path = _write_plan(tmp_path, _base_plan())
    assert os.stat(path).st_uid == os.getuid()
    bp.load_billing_plan(path)


def test_missing_file_rejected(tmp_path):
    path = tmp_path / PLAN_FILE_NAME
    with pytest.raises(bp.BillingPlanError) as info:
        bp.load_billing_plan(path)
    _assert_no_leak(info.value, path)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_unreadable_file_rejected(tmp_path):
    path = _write_plan(tmp_path, _base_plan(), mode=0o000)
    try:
        with pytest.raises(bp.BillingPlanError) as info:
            bp.load_billing_plan(path)
        _assert_no_leak(info.value, path)
    finally:
        os.chmod(path, 0o600)


def test_symlink_leaf_rejected(tmp_path):
    target = _write_plan(tmp_path, _base_plan(), name="real.json")
    link = tmp_path / PLAN_FILE_NAME
    link.symlink_to(target)
    with pytest.raises(bp.BillingPlanError) as info:
        bp.load_billing_plan(link)
    _assert_no_leak(info.value, link)


def test_symlinked_parent_dir_rejected(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    _write_plan(real_dir, _base_plan())
    link_dir = tmp_path / "linked"
    link_dir.symlink_to(real_dir, target_is_directory=True)
    path = link_dir / PLAN_FILE_NAME
    with pytest.raises(bp.BillingPlanError) as info:
        bp.load_billing_plan(path)
    _assert_no_leak(info.value, path)


def test_directory_rejected(tmp_path):
    path = tmp_path / PLAN_FILE_NAME
    path.mkdir(mode=0o700)
    with pytest.raises(bp.BillingPlanError) as info:
        bp.load_billing_plan(path)
    _assert_no_leak(info.value, path)


def test_fifo_rejected(tmp_path):
    path = tmp_path / PLAN_FILE_NAME
    os.mkfifo(path, 0o600)
    with pytest.raises(bp.BillingPlanError) as info:
        bp.load_billing_plan(path)
    _assert_no_leak(info.value, path)


def test_git_dir_in_ancestor_rejected(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    path = _write_plan(sub, _base_plan())
    with pytest.raises(bp.BillingPlanError) as info:
        bp.load_billing_plan(path)
    _assert_no_leak(info.value, path)


def test_git_file_in_ancestor_rejected(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /elsewhere\n")
    path = _write_plan(worktree, _base_plan())
    with pytest.raises(bp.BillingPlanError) as info:
        bp.load_billing_plan(path)
    _assert_no_leak(info.value, path)


def test_plan_named_dot_git_itself_is_not_an_ancestor(tmp_path):
    # Only ancestors count: a sibling ".git" next to tmp_path's parent is not
    # present, and the file's own directory has no ".git" entry.
    path = _write_plan(tmp_path, _base_plan())
    bp.load_billing_plan(path)


def test_fstat_mismatch_after_open_rejected(tmp_path, monkeypatch):
    path = _write_plan(tmp_path, _base_plan())
    decoy = _write_plan(tmp_path, _base_plan(), name="decoy.json")
    real_lstat = os.lstat

    def swapped_lstat(p, *args, **kwargs):
        if os.fspath(p) == str(path):
            return real_lstat(decoy)
        return real_lstat(p, *args, **kwargs)

    monkeypatch.setattr(bp.os, "lstat", swapped_lstat)
    with pytest.raises(bp.BillingPlanError) as info:
        bp.load_billing_plan(path)
    _assert_no_leak(info.value, path)


def _oserror_on(path: Path) -> OSError:
    return OSError(5, "simulated I/O error", str(path))


def test_realpath_oserror_is_billing_plan_error(tmp_path, monkeypatch):
    path = _write_plan(tmp_path, _base_plan())

    def failing_realpath(p, *args, **kwargs):
        raise _oserror_on(path)

    monkeypatch.setattr(bp.os.path, "realpath", failing_realpath)
    with pytest.raises(bp.BillingPlanError) as info:
        bp.load_billing_plan(path)
    _assert_no_leak(info.value, path)


def test_fstat_oserror_is_billing_plan_error(tmp_path, monkeypatch):
    path = _write_plan(tmp_path, _base_plan())

    def failing_fstat(fd):
        raise _oserror_on(path)

    monkeypatch.setattr(bp.os, "fstat", failing_fstat)
    with pytest.raises(bp.BillingPlanError) as info:
        bp.load_billing_plan(path)
    _assert_no_leak(info.value, path)


def test_read_oserror_is_billing_plan_error(tmp_path, monkeypatch):
    path = _write_plan(tmp_path, _base_plan())

    def failing_read(fd, n):
        raise _oserror_on(path)

    monkeypatch.setattr(bp.os, "read", failing_read)
    with pytest.raises(bp.BillingPlanError) as info:
        bp.load_billing_plan(path)
    _assert_no_leak(info.value, path)


def test_close_oserror_is_billing_plan_error(tmp_path, monkeypatch):
    path = _write_plan(tmp_path, _base_plan())
    real_close = os.close

    def failing_close(fd):
        real_close(fd)
        raise _oserror_on(path)

    monkeypatch.setattr(bp.os, "close", failing_close)
    with pytest.raises(bp.BillingPlanError) as info:
        bp.load_billing_plan(path)
    _assert_no_leak(info.value, path)


def test_close_oserror_does_not_replace_an_earlier_error(tmp_path, monkeypatch):
    path = _write_plan(tmp_path, _base_plan(), mode=0o644)
    real_close = os.close

    def failing_close(fd):
        real_close(fd)
        raise _oserror_on(path)

    monkeypatch.setattr(bp.os, "close", failing_close)
    with pytest.raises(bp.BillingPlanError) as info:
        bp.load_billing_plan(path)
    assert "chmod 600" in str(info.value)
    _assert_no_leak(info.value, path)


def test_tilde_path_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_plan(tmp_path, _base_plan())
    plan = bp.load_billing_plan(Path("~") / PLAN_FILE_NAME)
    assert plan.plan_id == "bp-test"


# ------------------------------------------------------------- revision_id


def _expected_revision_id(data: dict) -> str:
    """Independent re-statement of the v6.2 section 3 canonical form."""

    def dec(text):
        d = Decimal(text)
        return "0" if d == 0 else format(d.normalize(), "f")

    def ts(text):
        return _utc(text).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    periods = []
    for p in sorted(data["periods"], key=lambda p: _utc(p["effective_from"])):
        if p.get("terminates") is True:
            periods.append({"period_id": p["period_id"], "effective_from": ts(p["effective_from"]), "terminates": True})
            continue
        overage = p["overage"]
        if overage is not None:
            overage = {k: (v if k == "type" else dec(v)) for k, v in overage.items()}
        periods.append(
            {
                "period_id": p["period_id"],
                "effective_from": ts(p["effective_from"]),
                "window_subscription_usd": dec(p["window_subscription_usd"]),
                "allowance_usd": None if p["allowance_usd"] is None else dec(p["allowance_usd"]),
                "overage": overage,
            }
        )
    obj = {
        "plan_schema_version": data["plan_schema_version"],
        "plan_id": data["plan_id"],
        "nonce": data["nonce"],
        "applies_to": dict(data["applies_to"]),
        "basis": data["basis"],
        "periods": periods,
    }
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_revision_id_matches_canonical_form():
    data = _base_plan()
    plan = bp.parse_billing_plan(json.dumps(data))
    rid = bp.revision_id(plan)
    assert rid == _expected_revision_id(data)
    assert len(rid) == 64 and rid == rid.lower()
    int(rid, 16)


def test_revision_id_matches_canonical_form_with_block_discount():
    data = _base_plan()
    data["periods"][1]["overage"] = {"type": "block_discount", "block_usd": "5.0", "discount_usd": "1.00"}
    plan = bp.parse_billing_plan(json.dumps(data))
    assert bp.revision_id(plan) == _expected_revision_id(data)


def test_revision_id_ignores_json_formatting_and_key_order():
    data = _base_plan()
    compact = bp.revision_id(bp.parse_billing_plan(json.dumps(data, separators=(",", ":"))))
    pretty = bp.revision_id(bp.parse_billing_plan(json.dumps(data, indent=4)))

    def reverse_keys(obj):
        if isinstance(obj, dict):
            return {k: reverse_keys(obj[k]) for k in reversed(list(obj))}
        if isinstance(obj, list):
            return [reverse_keys(v) for v in obj]
        return obj

    reordered = bp.revision_id(bp.parse_billing_plan(json.dumps(reverse_keys(data), indent=1)))
    assert compact == pretty == reordered


def test_revision_id_uses_normalized_values():
    data = _base_plan()
    base = bp.revision_id(bp.parse_billing_plan(json.dumps(data)))
    data["periods"][1]["allowance_usd"] = "5E+1"
    data["periods"][1]["window_subscription_usd"] = "10.000"
    data["periods"][0]["effective_from"] = "2030-01-01T09:00:00+09:00"
    assert bp.revision_id(bp.parse_billing_plan(json.dumps(data))) == base


def test_revision_id_is_independent_of_the_caller_context():
    import decimal

    data = _base_plan()
    data["periods"][1]["allowance_usd"] = "1234567890.123456789012345678"  # 28 digits
    plan = bp.parse_billing_plan(json.dumps(data))
    base = bp.revision_id(plan)
    assert base == _expected_revision_id(data)
    with decimal.localcontext() as ctx:
        ctx.prec = 10
        assert bp.revision_id(plan) == base
        ctx.traps[decimal.Inexact] = True
        ctx.traps[decimal.Rounded] = True
        assert bp.revision_id(plan) == base


def test_revision_id_changes_with_nonce_value_and_windows():
    data = _base_plan()
    base = bp.revision_id(bp.parse_billing_plan(json.dumps(data)))

    other_nonce = copy.deepcopy(data)
    other_nonce["nonce"] = "f" * 32
    other_value = copy.deepcopy(data)
    other_value["periods"][1]["allowance_usd"] = "51"
    other_window = copy.deepcopy(data)
    other_window["periods"].insert(2, _regular("p3", "2030-01-21T00:00:00+00:00", sub="1", allowance=None))
    other_overage = copy.deepcopy(data)
    other_overage["periods"][1]["overage"] = {"type": "block_discount", "block_usd": "5", "discount_usd": "1"}

    ids = {
        bp.revision_id(bp.parse_billing_plan(json.dumps(d)))
        for d in (other_nonce, other_value, other_window, other_overage)
    }
    assert base not in ids
    assert len(ids) == 4


# ------------------------------------------------------------- computation

_TEST_RATES = {
    "schema_version": "2",
    "catalog_version": "test-catalog",
    "currency": "USD",
    "unit": "per_mtok",
    "usd_per_credit": "0",
    "models": [
        {
            "model_key": "claude-test",
            "aliases": [],
            "fast_multiplier": "1.0",
            "rates": [
                {
                    "rate_id": "claude-test-1",
                    "effective_from": "2020-01-01T00:00:00+00:00",
                    "effective_until": None,
                    "input_nocache": "1.0",
                    "cache_read": "1.0",
                    "cache_write_5m": "1.0",
                    "cache_write_1h": "1.0",
                    "output": None,
                }
            ],
        }
    ],
}

A_FROM = _utc("2030-01-01T00:00:00+00:00")
B_FROM = _utc("2030-01-11T00:00:00+00:00")
END = _utc("2030-02-01T00:00:00+00:00")
AFTER_END = _utc("2030-03-01T00:00:00+00:00")


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "rates.json"
    path.write_text(json.dumps(_TEST_RATES))
    return load_rates(path)


def _plan_with(overage_b=None, allowance_b="50", sub_a="3", sub_b="10"):
    if overage_b is None and allowance_b is not None:
        overage_b = {"type": "charge_multiplier", "value": "0.5"}
    data = _base_plan()
    data["periods"][0]["window_subscription_usd"] = sub_a
    data["periods"][1]["window_subscription_usd"] = sub_b
    data["periods"][1]["allowance_usd"] = allowance_b
    data["periods"][1]["overage"] = overage_b
    return bp.parse_billing_plan(json.dumps(data))


def _block(block="5", discount="1"):
    return {"type": "block_discount", "block_usd": block, "discount_usd": discount}


def _mult(value="0.5"):
    return {"type": "charge_multiplier", "value": value}


def _fact(
    ts, usd=None, *, tokens=None, agent="claude", kind="input_nocache", model="claude-test", source_quality="ok"
):
    """``usd`` dollars of input at the test rate of $1 / MTok."""
    if tokens is None:
        tokens = int(Decimal(usd) * 1_000_000)
    when = ts if isinstance(ts, datetime) else _utc(ts)
    return Fact(
        occurred_at_utc=when,
        agent=agent,
        session_id="s1",
        model_raw=model,
        model_key=model,
        token_kind=kind,
        tokens=tokens,
        mode="normal",
        source_quality=source_quality,
    )


def _compute(plan, facts, catalog, since=A_FROM, until=END, generated_at=AFTER_END):
    return bp.compute_internal_billing(
        plan, facts, catalog, since=since, until=until, generated_at=generated_at
    )


def _window(result, period_id):
    matches = [w for w in result["windows"] if w["period_id"] == period_id]
    assert len(matches) == 1
    return matches[0]


def test_block_metadata(catalog):
    plan = _plan_with()
    result = _compute(plan, [], catalog)
    assert result["calc_schema_version"] == "1"
    assert result["plan_id"] == "bp-test"
    assert result["revision_id"] == bp.revision_id(plan)
    assert result["basis"] == "agent-cost-list-price"
    assert result["catalog_version"] == "test-catalog"
    assert result["catalog_sha256"] == catalog.sha256
    assert result["since"] == "2030-01-01T00:00:00+00:00"
    assert result["until"] == "2030-02-01T00:00:00+00:00"
    assert result["generated_at"] == "2030-03-01T00:00:00+00:00"
    assert result["plan_coverage"] == "full"
    assert result["uncovered"] == []
    assert [w["period_id"] for w in result["windows"]] == ["p1", "p2"]


def test_under_allowance_has_no_overage(catalog):
    result = _compute(_plan_with(), [_fact("2030-01-15T00:00:00+00:00", "40")], catalog)
    w = _window(result, "p2")
    assert w["list_cost_usd"] == Decimal("40")
    assert w["overage_usd"] == Decimal("0")
    assert w["overage_cost_usd"] == Decimal("0")
    assert w["internal_cost_usd"] == Decimal("10")


def test_exactly_allowance_has_no_overage(catalog):
    result = _compute(_plan_with(), [_fact("2030-01-15T00:00:00+00:00", "50")], catalog)
    w = _window(result, "p2")
    assert w["overage_usd"] == Decimal("0")
    assert w["internal_cost_usd"] == Decimal("10")


def test_charge_multiplier(catalog):
    result = _compute(_plan_with(_mult("0.5")), [_fact("2030-01-15T00:00:00+00:00", "60")], catalog)
    w = _window(result, "p2")
    assert w["list_cost_usd"] == Decimal("60")
    assert w["allowance_usd"] == Decimal("50")
    assert w["overage_usd"] == Decimal("10")
    assert w["overage_cost_usd"] == Decimal("5")
    assert w["internal_cost_usd"] == Decimal("15")
    assert w["window_subscription_usd"] == Decimal("10")


@pytest.mark.parametrize(
    "list_cost, overage, overage_cost",
    [
        ("54.9999", "4.9999", "4.9999"),  # under one block: no discount
        ("55", "5", "4"),  # exactly one block: one discount
        ("60", "10", "8"),  # exactly two blocks: two discounts
        ("62.5", "12.5", "10.5"),  # two whole blocks plus a remainder
        ("54.999999", "4.999999", "4.999999"),  # rounds to 5.0000 but is still < 1 block
    ],
)
def test_block_discount_floor(catalog, list_cost, overage, overage_cost):
    result = _compute(_plan_with(_block("5", "1")), [_fact("2030-01-15T00:00:00+00:00", list_cost)], catalog)
    w = _window(result, "p2")
    assert w["overage_usd"] == Decimal(overage)
    assert w["overage_cost_usd"] == Decimal(overage_cost)
    assert w["internal_cost_usd"] == Decimal("10") + Decimal(overage_cost)


def test_block_discount_boundary_is_decided_before_output_rounding(catalog):
    result = _compute(_plan_with(_block("5", "1")), [_fact("2030-01-15T00:00:00+00:00", "54.999999")], catalog)
    formatted = bp.format_internal_billing(result)
    w = [x for x in formatted["windows"] if x["period_id"] == "p2"][0]
    # The overage prints as 5.0000 but was not counted as a whole block.
    assert w["overage_usd"] == "5.0000"
    assert w["overage_cost_usd"] == "5.0000"
    assert w["internal_cost_usd"] == "15.0000"


def test_allowance_null_has_no_overage(catalog):
    result = _compute(_plan_with(), [_fact("2030-01-05T00:00:00+00:00", "1000")], catalog)
    w = _window(result, "p1")
    assert w["allowance_usd"] is None
    assert w["list_cost_usd"] == Decimal("1000")
    assert w["overage_usd"] == Decimal("0")
    assert w["overage_cost_usd"] == Decimal("0")
    assert w["internal_cost_usd"] == Decimal("3")


def test_codex_facts_are_excluded(catalog):
    facts = [
        _fact("2030-01-15T00:00:00+00:00", "40"),
        _fact("2030-01-15T00:00:00+00:00", "100", agent="codex"),
    ]
    w = _window(_compute(_plan_with(), facts, catalog), "p2")
    assert w["list_cost_usd"] == Decimal("40")
    assert w["fact_count"] == 1
    assert w["priced_tokens"] == 40_000_000


def test_fact_on_window_boundary_belongs_to_later_window(catalog):
    facts = [_fact(B_FROM, "7")]
    result = _compute(_plan_with(), facts, catalog)
    assert _window(result, "p1")["fact_count"] == 0
    assert _window(result, "p1")["list_cost_usd"] == Decimal("0")
    assert _window(result, "p2")["fact_count"] == 1
    assert _window(result, "p2")["list_cost_usd"] == Decimal("7")


def test_fact_at_until_is_excluded(catalog):
    facts = [_fact("2030-01-20T00:00:00+00:00", "7")]
    result = _compute(_plan_with(), facts, catalog, since=B_FROM, until=_utc("2030-01-20T00:00:00+00:00"))
    assert _window(result, "p2")["fact_count"] == 0


def test_facts_outside_query_are_excluded(catalog):
    facts = [_fact("2030-01-12T00:00:00+00:00", "7"), _fact("2030-01-25T00:00:00+00:00", "11")]
    result = _compute(
        _plan_with(), facts, catalog, since=_utc("2030-01-20T00:00:00+00:00"), until=END
    )
    assert _window(result, "p2")["list_cost_usd"] == Decimal("11")


def test_non_intersecting_windows_are_not_output(catalog):
    result = _compute(_plan_with(), [], catalog, since=_utc("2030-01-15T00:00:00+00:00"), until=END)
    assert [w["period_id"] for w in result["windows"]] == ["p2"]
    # U == B_FROM: p2 starts exactly at U, so it does not intersect [S, U).
    result = _compute(_plan_with(), [], catalog, since=A_FROM, until=B_FROM)
    assert [w["period_id"] for w in result["windows"]] == ["p1"]


def test_no_intersecting_window_gives_empty_list(catalog):
    result = _compute(_plan_with(), [], catalog, since=END, until=AFTER_END, generated_at=AFTER_END)
    assert result["windows"] == []
    assert result["plan_coverage"] == "partial"


def test_window_bounds_are_the_whole_window(catalog):
    result = _compute(_plan_with(), [], catalog, since=_utc("2030-01-15T00:00:00+00:00"), until=END)
    w = _window(result, "p2")
    assert w["from"] == "2030-01-11T00:00:00+00:00"
    assert w["until"] == "2030-02-01T00:00:00+00:00"


def test_partial_query_coverage(catalog):
    result = _compute(_plan_with(), [], catalog, since=_utc("2030-01-15T00:00:00+00:00"), until=END)
    assert _window(result, "p2")["query_coverage"] == "partial"
    result = _compute(_plan_with(), [], catalog, since=B_FROM, until=_utc("2030-01-31T00:00:00+00:00"))
    assert _window(result, "p2")["query_coverage"] == "partial"


def test_full_coverage_when_both_ends_match(catalog):
    result = _compute(_plan_with(), [_fact("2030-01-15T00:00:00+00:00", "1")], catalog, since=B_FROM, until=END)
    w = _window(result, "p2")
    assert w["query_coverage"] == "full"
    assert w["window_state"] == "closed"
    assert w["list_cost_pricing"] == "priced"
    assert w["internal_cost_certainty"] == "estimate"


def test_window_open_when_until_after_generated_at(catalog):
    result = _compute(_plan_with(), [], catalog, generated_at=_utc("2030-01-20T00:00:00+00:00"))
    assert _window(result, "p1")["window_state"] == "closed"
    assert _window(result, "p2")["window_state"] == "open"


def test_window_closed_when_until_equals_generated_at(catalog):
    result = _compute(_plan_with(), [], catalog, generated_at=END)
    assert _window(result, "p2")["window_state"] == "closed"
    assert _window(result, "p1")["window_state"] == "closed"


def test_query_until_in_future(catalog):
    # U > generated_at: the query reaches into the future; the window is
    # still open and the computation proceeds normally.
    now = _utc("2030-01-20T00:00:00+00:00")
    facts = [_fact("2030-01-15T00:00:00+00:00", "60")]
    result = _compute(_plan_with(), facts, catalog, since=A_FROM, until=END, generated_at=now)
    w = _window(result, "p2")
    assert w["window_state"] == "open"
    assert w["query_coverage"] == "full"
    assert w["internal_cost_certainty"] == "lower_bound"
    assert w["internal_cost_usd"] == Decimal("15")


def test_no_usage_observed(catalog):
    result = _compute(_plan_with(), [], catalog)
    for pid in ("p1", "p2"):
        w = _window(result, pid)
        assert w["fact_count"] == 0
        assert w["list_cost_pricing"] == "no_usage_observed"
        assert w["list_cost_usd"] == Decimal("0")


@pytest.mark.parametrize(
    "allowance, overage, expected",
    [
        (None, None, "lower_bound"),
        ("50", _mult("0.5"), "lower_bound"),
        ("50", _block("5", "1"), "indeterminate"),
    ],
)
def test_no_usage_observed_certainty(catalog, allowance, overage, expected):
    plan = _plan_with(overage, allowance_b=allowance)
    w = _window(_compute(plan, [], catalog, since=B_FROM, until=END), "p2")
    assert w["query_coverage"] == "full" and w["window_state"] == "closed"
    assert w["list_cost_pricing"] == "no_usage_observed"
    assert w["internal_cost_certainty"] == expected


def test_unpriced_only(catalog):
    facts = [_fact("2030-01-15T00:00:00+00:00", tokens=1234, kind="output")]
    w = _window(_compute(_plan_with(), facts, catalog), "p2")
    assert w["fact_count"] == 1
    assert w["unpriced_tokens"] == 1234
    assert w["priced_tokens"] == 0
    assert w["list_cost_usd"] == Decimal("0")
    assert w["list_cost_pricing"] == "lower_bound"


def test_unknown_model_is_unpriced(catalog):
    facts = [_fact("2030-01-15T00:00:00+00:00", tokens=99, model="claude-unknown")]
    w = _window(_compute(_plan_with(), facts, catalog), "p2")
    assert w["unpriced_tokens"] == 99
    assert w["list_cost_pricing"] == "lower_bound"


def test_lower_bound_only(catalog):
    facts = [_fact("2030-01-15T00:00:00+00:00", "2", kind="cache_write_unknown")]
    w = _window(_compute(_plan_with(), facts, catalog), "p2")
    assert w["list_cost_pricing"] == "lower_bound"
    assert w["list_cost_usd"] == Decimal("2")
    assert w["priced_tokens"] == 2_000_000
    assert w["unpriced_tokens"] == 0


def test_mixed_priced_lower_bound_unpriced(catalog):
    facts = [
        _fact("2030-01-15T00:00:00+00:00", "40"),
        _fact("2030-01-16T00:00:00+00:00", "15", kind="cache_write_unknown"),
        _fact("2030-01-17T00:00:00+00:00", tokens=500, kind="output"),
    ]
    w = _window(_compute(_plan_with(), facts, catalog), "p2")
    assert w["fact_count"] == 3
    assert w["list_cost_usd"] == Decimal("55")
    assert w["priced_tokens"] == 55_000_000
    assert w["unpriced_tokens"] == 500
    assert w["list_cost_pricing"] == "lower_bound"
    assert w["overage_usd"] == Decimal("5")


def test_certainty_estimate(catalog):
    for overage, allowance in ((None, None), (_mult(), "50"), (_block(), "50")):
        plan = _plan_with(overage, allowance_b=allowance)
        facts = [_fact("2030-01-15T00:00:00+00:00", "60")]
        w = _window(_compute(plan, facts, catalog, since=B_FROM, until=END), "p2")
        assert w["internal_cost_certainty"] == "estimate"


def test_certainty_lower_bound_for_null_and_multiplier(catalog):
    for overage, allowance in ((None, None), (_mult(), "50")):
        plan = _plan_with(overage, allowance_b=allowance)
        facts = [_fact("2030-01-15T00:00:00+00:00", "60")]
        # partial
        w = _window(_compute(plan, facts, catalog, since=_utc("2030-01-12T00:00:00+00:00"), until=END), "p2")
        assert w["internal_cost_certainty"] == "lower_bound"
        # open
        w = _window(_compute(plan, facts, catalog, since=B_FROM, until=END, generated_at=_utc("2030-01-20T00:00:00+00:00")), "p2")
        assert w["internal_cost_certainty"] == "lower_bound"
        # unpriced
        facts_u = facts + [_fact("2030-01-15T00:00:00+00:00", tokens=1, kind="output")]
        w = _window(_compute(plan, facts_u, catalog, since=B_FROM, until=END), "p2")
        assert w["internal_cost_certainty"] == "lower_bound"


def test_certainty_indeterminate_for_block_discount(catalog):
    plan = _plan_with(_block())
    facts = [_fact("2030-01-15T00:00:00+00:00", "60")]
    w = _window(_compute(plan, facts, catalog, since=_utc("2030-01-12T00:00:00+00:00"), until=END), "p2")
    assert w["internal_cost_certainty"] == "indeterminate"
    w = _window(_compute(plan, facts, catalog, since=B_FROM, until=END, generated_at=_utc("2030-01-20T00:00:00+00:00")), "p2")
    assert w["internal_cost_certainty"] == "indeterminate"
    facts_u = facts + [_fact("2030-01-15T00:00:00+00:00", tokens=1, kind="output")]
    w = _window(_compute(plan, facts_u, catalog, since=B_FROM, until=END), "p2")
    assert w["internal_cost_certainty"] == "indeterminate"
    facts_lb = [_fact("2030-01-15T00:00:00+00:00", "60", kind="cache_write_unknown")]
    w = _window(_compute(plan, facts_lb, catalog, since=B_FROM, until=END), "p2")
    assert w["internal_cost_certainty"] == "indeterminate"


@pytest.fixture
def catalog_output(tmp_path):
    rates = copy.deepcopy(_TEST_RATES)
    rates["models"][0]["rates"][0]["output"] = "1.0"
    path = tmp_path / "rates-output.json"
    path.write_text(json.dumps(rates))
    return load_rates(path)


def _output_quality_facts(source_quality):
    return [
        _fact("2030-01-15T00:00:00+00:00", "40"),
        _fact("2030-01-16T00:00:00+00:00", "3", kind="output", source_quality=source_quality),
    ]


def test_output_lower_bound_fact_makes_window_lower_bound(catalog_output):
    # The fact prices as "priced", but its tokens are only a lower bound, so
    # list_cost and internal_cost are too.
    facts = _output_quality_facts("output_lower_bound")
    w = _window(_compute(_plan_with(), facts, catalog_output, since=B_FROM, until=END), "p2")
    assert w["query_coverage"] == "full"
    assert w["window_state"] == "closed"
    assert w["list_cost_usd"] == Decimal("43")
    assert w["priced_tokens"] == 43_000_000
    assert w["unpriced_tokens"] == 0
    assert w["list_cost_pricing"] == "lower_bound"
    assert w["internal_cost_certainty"] == "lower_bound"


@pytest.mark.parametrize("source_quality", ["ok", "first_event_delta", "identity_missing"])
def test_other_source_quality_keeps_window_priced(catalog_output, source_quality):
    facts = _output_quality_facts(source_quality)
    w = _window(_compute(_plan_with(), facts, catalog_output, since=B_FROM, until=END), "p2")
    assert w["list_cost_usd"] == Decimal("43")
    assert w["priced_tokens"] == 43_000_000
    assert w["list_cost_pricing"] == "priced"
    assert w["internal_cost_certainty"] == "estimate"


def test_plan_coverage_partial_before_and_after(catalog):
    since = _utc("2029-12-25T00:00:00+00:00")
    until = _utc("2030-02-05T00:00:00+00:00")
    result = _compute(_plan_with(), [], catalog, since=since, until=until)
    assert result["plan_coverage"] == "partial"
    assert result["uncovered"] == [
        {"from": "2029-12-25T00:00:00+00:00", "until": "2030-01-01T00:00:00+00:00"},
        {"from": "2030-02-01T00:00:00+00:00", "until": "2030-02-05T00:00:00+00:00"},
    ]
    assert [w["period_id"] for w in result["windows"]] == ["p1", "p2"]


def test_plan_coverage_partial_only_after(catalog):
    result = _compute(_plan_with(), [], catalog, since=B_FROM, until=AFTER_END)
    assert result["plan_coverage"] == "partial"
    assert result["uncovered"] == [{"from": "2030-02-01T00:00:00+00:00", "until": "2030-03-01T00:00:00+00:00"}]


def test_plan_coverage_full_inside(catalog):
    result = _compute(
        _plan_with(), [], catalog, since=_utc("2030-01-05T00:00:00+00:00"), until=_utc("2030-01-20T00:00:00+00:00")
    )
    assert result["plan_coverage"] == "full"
    assert result["uncovered"] == []


def test_facts_in_uncovered_range_are_not_billed(catalog):
    facts = [_fact("2029-12-28T00:00:00+00:00", "5"), _fact("2030-02-02T00:00:00+00:00", "5")]
    result = _compute(
        _plan_with(), facts, catalog, since=_utc("2029-12-25T00:00:00+00:00"), until=_utc("2030-02-05T00:00:00+00:00")
    )
    assert all(w["fact_count"] == 0 for w in result["windows"])


def test_partial_report_echoes_full_window_subscription(catalog):
    result = _compute(
        _plan_with(sub_b="10"), [], catalog, since=_utc("2030-01-30T00:00:00+00:00"), until=END
    )
    w = _window(result, "p2")
    assert w["query_coverage"] == "partial"
    assert w["window_subscription_usd"] == Decimal("10")
    assert w["internal_cost_usd"] == Decimal("10")


def test_amounts_are_decimal(catalog):
    w = _window(_compute(_plan_with(), [_fact("2030-01-15T00:00:00+00:00", "60")], catalog), "p2")
    for key in (
        "list_cost_usd",
        "allowance_usd",
        "overage_usd",
        "overage_cost_usd",
        "window_subscription_usd",
        "internal_cost_usd",
    ):
        assert isinstance(w[key], Decimal), key
    for key in ("fact_count", "priced_tokens", "unpriced_tokens"):
        assert type(w[key]) is int, key


def test_arithmetic_trap_is_a_billing_plan_error(catalog):
    # A 28-digit multiplier times an overage carrying ~40 significant digits
    # would need more than prec=60 digits: Inexact/Rounded must trap.
    data = _base_plan()
    data["periods"][1]["allowance_usd"] = "1.234567890123456789012345678E-12"
    data["periods"][1]["overage"] = {"type": "charge_multiplier", "value": "0.1234567890123456789012345678"}
    plan = bp.parse_billing_plan(json.dumps(data))
    facts = [_fact("2030-01-15T00:00:00+00:00", tokens=1_234_567)]
    with pytest.raises(bp.BillingPlanError) as info:
        _compute(plan, facts, catalog)
    assert "0.1234567890123456789012345678" not in str(info.value)


@pytest.fixture
def catalog_1234(tmp_path):
    rates = copy.deepcopy(_TEST_RATES)
    rates["models"][0]["rates"][0]["input_nocache"] = "1.234"
    path = tmp_path / "rates-1234.json"
    path.write_text(json.dumps(rates))
    return load_rates(path)


def test_price_fact_rounding_traps_inside_compute(catalog_1234):
    # p1 has no allowance, so the only arithmetic that can round is
    # price_fact's tokens / 1e6 * 1.234: 61 significant digits > prec 60.
    facts = [_fact("2030-01-05T00:00:00+00:00", tokens=10**57 + 1)]
    with pytest.raises(bp.BillingPlanError) as info:
        _compute(_plan_with(), facts, catalog_1234)
    assert "would round" in str(info.value)


def test_price_fact_runs_at_calc_precision_under_a_trapping_caller(catalog_1234):
    import decimal

    # 31 significant digits: inexact at the default prec 28, exact at 60.
    facts = [_fact("2030-01-05T00:00:00+00:00", tokens=10**27 + 1)]
    with decimal.localcontext() as ctx:
        ctx.prec = 28
        ctx.traps[decimal.Inexact] = True
        ctx.traps[decimal.Rounded] = True
        result = _compute(_plan_with(), facts, catalog_1234)
    assert _window(result, "p1")["list_cost_usd"] == Decimal("1234000000000000000000.000001234")


def test_price_fact_at_normal_token_counts_does_not_trap(catalog_1234):
    from agent_cost.aggregate import price_fact

    fact = _fact("2030-01-05T00:00:00+00:00", tokens=10**12 + 1)
    result = _compute(_plan_with(), [fact], catalog_1234)
    w = _window(result, "p1")
    assert w["list_cost_usd"] == Decimal("1234000.000001234")
    # Same Decimal a plain report sums in the default context.
    assert w["list_cost_usd"] == price_fact(catalog_1234, fact)[0]


def test_compute_does_not_leak_context_changes(catalog):
    import decimal

    before = decimal.getcontext().copy()
    _compute(_plan_with(_block()), [_fact("2030-01-15T00:00:00+00:00", "57.5")], catalog)
    after = decimal.getcontext()
    assert after.prec == before.prec
    assert after.traps[decimal.Inexact] == before.traps[decimal.Inexact]


# ------------------------------------------------------------- formatting


_FOUR_DP = r"^\d+\.\d{4}$"


def test_format_internal_billing_strings(catalog):
    import re

    result = _compute(
        _plan_with(),
        [_fact("2030-01-05T00:00:00+00:00", "1.23456"), _fact("2030-01-15T00:00:00+00:00", "60.00005")],
        catalog,
    )
    formatted = bp.format_internal_billing(result)
    json.dumps(formatted)  # JSON-serializable
    p1 = [w for w in formatted["windows"] if w["period_id"] == "p1"][0]
    p2 = [w for w in formatted["windows"] if w["period_id"] == "p2"][0]
    assert p1["allowance_usd"] is None
    assert p1["overage_usd"] == "0.0000"
    assert p1["overage_cost_usd"] == "0.0000"
    assert p1["list_cost_usd"] == "1.2346"
    assert p2["list_cost_usd"] == "60.0000"  # 60.00005 -> half-even -> 60.0000
    assert p2["allowance_usd"] == "50.0000"
    assert p2["window_subscription_usd"] == "10.0000"
    for w in (p1, p2):
        for key in ("list_cost_usd", "overage_usd", "overage_cost_usd", "window_subscription_usd", "internal_cost_usd"):
            assert re.match(_FOUR_DP, w[key]), (key, w[key])
        assert type(w["fact_count"]) is int
    # The Decimal result is not mutated by formatting.
    assert isinstance(_window(result, "p2")["list_cost_usd"], Decimal)
    assert formatted["plan_coverage"] == "full"
    assert formatted["uncovered"] == []
    assert formatted["revision_id"] == result["revision_id"]


def test_format_large_amount_has_no_exponent(catalog):
    data = _base_plan()
    data["periods"][1]["window_subscription_usd"] = "1E+12"
    plan = bp.parse_billing_plan(json.dumps(data))
    formatted = bp.format_internal_billing(_compute(plan, [], catalog))
    p2 = [w for w in formatted["windows"] if w["period_id"] == "p2"][0]
    assert p2["window_subscription_usd"] == "1000000000000.0000"


# ------------------------------------------------------------- import hygiene


def test_module_top_level_has_no_ambient_io():
    source = Path(bp.__file__).read_text()
    top_level = [line for line in source.splitlines() if line and not line[0].isspace()]
    for line in top_level:
        assert "os.environ" not in line
        assert "open(" not in line
        assert "getenv" not in line
