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
