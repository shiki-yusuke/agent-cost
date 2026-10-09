from datetime import datetime, timezone

import pytest

from agent_cost.facts import Fact, normalize_model_key

UTC = timezone.utc


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("claude-opus-4-7", "claude-opus-4-7"),
        ("claude-opus-4-7-1m", "claude-opus-4-7"),
        ("claude-opus-4-7[1m]", "claude-opus-4-7"),
        ("claude-opus-4-7@20260101", "claude-opus-4-7"),
        ("claude-opus-4.7", "claude-opus-4-7"),
        ("gpt-5.5-fast-200k", "gpt-5.5"),
        ("gpt-5.5", "gpt-5.5"),
        (None, "(unknown)"),
        ("", "(unknown)"),
    ],
)
def test_normalize_model_key(raw, expected):
    assert normalize_model_key(raw) == expected


def test_fact_requires_tz_aware_datetime():
    with pytest.raises(ValueError):
        Fact(
            occurred_at_utc=datetime(2026, 1, 1),
            agent="claude",
            session_id="s1",
            model_raw="claude-opus-4-8",
            model_key="claude-opus-4-8",
            token_kind="output",
            tokens=10,
        )


def test_fact_rejects_negative_tokens():
    with pytest.raises(ValueError):
        Fact(
            occurred_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
            agent="claude",
            session_id="s1",
            model_raw="claude-opus-4-8",
            model_key="claude-opus-4-8",
            token_kind="output",
            tokens=-1,
        )


def test_fact_rejects_invalid_agent():
    with pytest.raises(ValueError):
        Fact(
            occurred_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
            agent="bogus",
            session_id="s1",
            model_raw="x",
            model_key="x",
            token_kind="output",
            tokens=1,
        )


def test_fact_rejects_invalid_token_kind():
    with pytest.raises(ValueError):
        Fact(
            occurred_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
            agent="claude",
            session_id="s1",
            model_raw="x",
            model_key="x",
            token_kind="bogus",
            tokens=1,
        )


def _prompt_fact(**kwargs):
    base = dict(
        occurred_at_utc=datetime(2026, 10, 8, tzinfo=UTC),
        agent="claude",
        session_id="s1",
        model_raw="claude-haiku-5-5",
        model_key="claude-haiku-5-5",
        token_kind="output",
        tokens=1,
    )
    base.update(kwargs)
    return Fact(**base)


def test_fact_prompt_tokens_defaults_to_none():
    assert _prompt_fact().prompt_tokens is None


def test_fact_prompt_tokens_accepts_non_negative_int():
    assert _prompt_fact(prompt_tokens=0).prompt_tokens == 0
    assert _prompt_fact(prompt_tokens=156000).prompt_tokens == 156000


def test_fact_rejects_negative_prompt_tokens():
    with pytest.raises(ValueError):
        _prompt_fact(prompt_tokens=-1)


@pytest.mark.parametrize("value", [True, False])
def test_fact_rejects_bool_prompt_tokens(value):
    with pytest.raises(ValueError):
        _prompt_fact(prompt_tokens=value)


def test_fact_prompt_tokens_is_appended_after_source_quality():
    # Existing positional construction (9 fields up to source_quality) keeps working.
    f = Fact(datetime(2026, 10, 8, tzinfo=UTC), "claude", "s1", "m", "m", "output", 1, "normal", "ok")
    assert f.source_quality == "ok"
    assert f.prompt_tokens is None
