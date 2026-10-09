import json
from datetime import datetime, timezone

import pytest

from agent_cost.readers.claude import parse_session_facts, read_claude_facts


def _event(**kwargs):
    return json.dumps(kwargs)


def write_jsonl(path, lines):
    path.write_text("\n".join(lines) + "\n")


def test_multiple_models_in_one_session_attribute_per_event(tmp_path):
    jsonl = tmp_path / "s1.jsonl"
    write_jsonl(
        jsonl,
        [
            _event(
                type="assistant",
                timestamp="2026-06-01T00:00:00Z",
                sessionId="s1",
                message={"model": "claude-opus-4-8", "usage": {"input_tokens": 100, "output_tokens": 50}},
            ),
            _event(
                type="assistant",
                timestamp="2026-06-01T00:05:00Z",
                sessionId="s1",
                message={"model": "claude-sonnet-5", "usage": {"input_tokens": 200, "output_tokens": 80}},
            ),
        ],
    )
    facts, malformed = parse_session_facts(jsonl)
    assert malformed == 0
    models_used = {f.model_key for f in facts}
    assert models_used == {"claude-opus-4-8", "claude-sonnet-5"}
    opus_input = [f for f in facts if f.model_key == "claude-opus-4-8" and f.token_kind == "input_nocache"]
    assert opus_input[0].tokens == 100
    sonnet_input = [f for f in facts if f.model_key == "claude-sonnet-5" and f.token_kind == "input_nocache"]
    assert sonnet_input[0].tokens == 200


def test_month_crossing_facts_keep_their_own_timestamp(tmp_path):
    jsonl = tmp_path / "s2.jsonl"
    write_jsonl(
        jsonl,
        [
            _event(
                type="assistant",
                timestamp="2026-05-31T23:59:00Z",
                sessionId="s2",
                message={"model": "claude-opus-4-8", "usage": {"input_tokens": 10, "output_tokens": 5}},
            ),
            _event(
                type="assistant",
                timestamp="2026-06-01T00:01:00Z",
                sessionId="s2",
                message={"model": "claude-opus-4-8", "usage": {"input_tokens": 20, "output_tokens": 8}},
            ),
        ],
    )
    facts, _ = parse_session_facts(jsonl)
    months = sorted({f.occurred_at_utc.strftime("%Y-%m") for f in facts})
    assert months == ["2026-05", "2026-06"]


def test_cache_creation_ttl_breakdown_present(tmp_path):
    jsonl = tmp_path / "s3.jsonl"
    write_jsonl(
        jsonl,
        [
            _event(
                type="assistant",
                timestamp="2026-06-01T00:00:00Z",
                sessionId="s3",
                message={
                    "model": "claude-opus-4-8",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_creation_input_tokens": 300,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 200,
                            "ephemeral_1h_input_tokens": 100,
                        },
                    },
                },
            ),
        ],
    )
    facts, _ = parse_session_facts(jsonl)
    kinds = {f.token_kind: f.tokens for f in facts}
    assert kinds["cache_write_5m"] == 200
    assert kinds["cache_write_1h"] == 100
    assert "cache_write_unknown" not in kinds


def test_cache_creation_without_ttl_breakdown_is_unknown(tmp_path):
    jsonl = tmp_path / "s4.jsonl"
    write_jsonl(
        jsonl,
        [
            _event(
                type="assistant",
                timestamp="2026-06-01T00:00:00Z",
                sessionId="s4",
                message={
                    "model": "claude-opus-4-8",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_creation_input_tokens": 300,
                    },
                },
            ),
        ],
    )
    facts, _ = parse_session_facts(jsonl)
    kinds = {f.token_kind: f.tokens for f in facts}
    assert kinds["cache_write_unknown"] == 300
    assert "cache_write_5m" not in kinds
    assert "cache_write_1h" not in kinds


def test_cache_creation_partial_ttl_breakdown_leftover_is_unknown(tmp_path):
    # Regression for GAP-01: cache_creation is a dict (TTL breakdown present)
    # but its two counters sum to less than cache_creation_input_tokens. The
    # shortfall (leftover) must still surface as cache_write_unknown,
    # alongside the 5m/1h facts for the portion that *was* broken down.
    jsonl = tmp_path / "s3b.jsonl"
    write_jsonl(
        jsonl,
        [
            _event(
                type="assistant",
                timestamp="2026-06-01T00:00:00Z",
                sessionId="s3b",
                message={
                    "model": "claude-opus-4-8",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_creation_input_tokens": 300,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 150,
                            "ephemeral_1h_input_tokens": 100,
                        },
                    },
                },
            ),
        ],
    )
    facts, _ = parse_session_facts(jsonl)
    kinds = {f.token_kind: f.tokens for f in facts}
    assert kinds["cache_write_5m"] == 150
    assert kinds["cache_write_1h"] == 100
    assert kinds["cache_write_unknown"] == 50


def test_mixed_speed_modes(tmp_path):
    jsonl = tmp_path / "s5.jsonl"
    write_jsonl(
        jsonl,
        [
            _event(
                type="assistant",
                timestamp="2026-06-01T00:00:00Z",
                sessionId="s5",
                message={"model": "claude-opus-4-8", "usage": {"input_tokens": 10, "output_tokens": 5, "speed": "fast"}},
            ),
            _event(
                type="assistant",
                timestamp="2026-06-01T00:01:00Z",
                sessionId="s5",
                message={"model": "claude-opus-4-8", "usage": {"input_tokens": 10, "output_tokens": 5, "speed": "standard"}},
            ),
            _event(
                type="assistant",
                timestamp="2026-06-01T00:02:00Z",
                sessionId="s5",
                message={"model": "claude-opus-4-8", "usage": {"input_tokens": 10, "output_tokens": 5}},
            ),
        ],
    )
    facts, _ = parse_session_facts(jsonl)
    modes = sorted({f.mode for f in facts})
    assert modes == ["fast", "normal", "unknown"]


def test_malformed_json_line_is_counted_and_skipped(tmp_path):
    jsonl = tmp_path / "s6.jsonl"
    jsonl.write_text(
        "\n".join(
            [
                "{not valid json",
                _event(
                    type="assistant",
                    timestamp="2026-06-01T00:00:00Z",
                    sessionId="s6",
                    message={"model": "claude-opus-4-8", "usage": {"input_tokens": 10, "output_tokens": 5}},
                ),
            ]
        )
        + "\n"
    )
    facts, malformed = parse_session_facts(jsonl)
    assert malformed == 1
    assert len(facts) == 2


def test_fact_total_equals_raw_usage_total(tmp_path):
    jsonl = tmp_path / "s7.jsonl"
    write_jsonl(
        jsonl,
        [
            _event(
                type="assistant",
                timestamp="2026-06-01T00:00:00Z",
                sessionId="s7",
                message={
                    "model": "claude-opus-4-8",
                    "usage": {
                        "input_tokens": 111,
                        "cache_read_input_tokens": 222,
                        "cache_creation_input_tokens": 333,
                        "output_tokens": 444,
                    },
                },
            ),
            _event(
                type="assistant",
                timestamp="2026-06-01T00:01:00Z",
                sessionId="s7",
                message={
                    "model": "claude-sonnet-5",
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 20,
                        "cache_creation_input_tokens": 30,
                        "output_tokens": 40,
                    },
                },
            ),
        ],
    )
    facts, _ = parse_session_facts(jsonl)
    raw_total = (111 + 222 + 333 + 444) + (10 + 20 + 30 + 40)
    fact_total = sum(f.tokens for f in facts)
    assert fact_total == raw_total


def test_file_modified_after_until_still_yields_in_window_facts(tmp_path):
    # Regression: a session file is append-only and its mtime reflects the
    # *last* write, which can be long after an earlier in-window event was
    # recorded (e.g. the same file gets a new message after the report
    # window closes). An until-side mtime skip would drop that file
    # entirely and silently lose real in-window data.
    projects = tmp_path / "projects"
    proj = projects / "-Users-a-work-proj"
    proj.mkdir(parents=True)
    write_jsonl(
        proj / "session.jsonl",
        [
            _event(
                type="assistant",
                timestamp="2026-06-10T00:00:00Z",  # inside the window below
                sessionId="s1",
                message={"model": "claude-opus-4-8", "usage": {"input_tokens": 42, "output_tokens": 7}},
            )
        ],
    )
    # The file's actual mtime (just written, "now") is well after `until`
    # below -- this is the scenario the until-side skip used to break.
    since = datetime(2026, 6, 1, tzinfo=timezone.utc)
    until = datetime(2026, 6, 15, tzinfo=timezone.utc)
    result = read_claude_facts(projects, since_utc=since, until_utc=until)
    assert sum(f.tokens for f in result.facts) == 49


def test_read_claude_facts_walks_project_dirs(tmp_path):
    projects = tmp_path / "projects"
    proj1 = projects / "-Users-a-work-proj1"
    proj1.mkdir(parents=True)
    write_jsonl(
        proj1 / "session.jsonl",
        [
            _event(
                type="assistant",
                timestamp="2026-06-01T00:00:00Z",
                sessionId="s1",
                message={"model": "claude-opus-4-8", "usage": {"input_tokens": 5, "output_tokens": 5}},
            )
        ],
    )
    result = read_claude_facts(projects)
    assert len(result.facts) == 2
    assert result.malformed_events == 0
    assert result.skipped_files == 0


# ── prompt_tokens (prompt length for prompt-length-tiered rates) ──


def _haiku_row(msg_id, req_id, *, input_tokens=1000, cache_read=150000, cache_creation=5000,
               eph_5m=5000, eph_1h=0, output=10, stop_reason="end_turn", ts="2026-10-08T00:00:00Z"):
    message = {
        "model": "claude-haiku-5-5",
        "stop_reason": stop_reason,
        "usage": {
            "input_tokens": input_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
            "cache_creation": {"ephemeral_5m_input_tokens": eph_5m, "ephemeral_1h_input_tokens": eph_1h},
            "output_tokens": output,
        },
    }
    event = {"type": "assistant", "timestamp": ts, "sessionId": "s-prompt", "message": message}
    if msg_id is not None:
        message["id"] = msg_id
    if req_id is not None:
        event["requestId"] = req_id
    return json.dumps(event)


def test_prompt_tokens_is_attached_to_every_fact_of_a_row(tmp_path):
    jsonl = tmp_path / "p1.jsonl"
    write_jsonl(jsonl, [_haiku_row("msg-1", "req-1")])
    facts, malformed = parse_session_facts(jsonl)
    assert malformed == 0
    assert {f.token_kind for f in facts} == {"input_nocache", "cache_read", "cache_write_5m", "output"}
    assert [f.prompt_tokens for f in facts] == [156000] * len(facts)


def test_prompt_tokens_streaming_group_uses_adopted_row_value(tmp_path):
    jsonl = tmp_path / "p2.jsonl"
    write_jsonl(
        jsonl,
        [
            _haiku_row("msg-1", "req-1", output=3, stop_reason=None),
            _haiku_row("msg-1", "req-1", output=10, ts="2026-10-08T00:00:01Z"),
        ],
    )
    facts, _ = parse_session_facts(jsonl)
    assert len(facts) == 4
    assert {f.token_kind: f.tokens for f in facts}["output"] == 10
    assert all(f.prompt_tokens == 156000 for f in facts)


def test_prompt_tokens_conflicting_group_is_none(tmp_path):
    from agent_cost.readers.claude import parse_session_detailed

    jsonl = tmp_path / "p3.jsonl"
    write_jsonl(
        jsonl,
        [
            _haiku_row("msg-1", "req-1", input_tokens=10, cache_read=90000, cache_creation=0, eph_5m=0),
            _haiku_row("msg-1", "req-1", input_tokens=10, cache_read=110000, cache_creation=0, eph_5m=0,
                       ts="2026-10-08T00:00:01Z"),
        ],
    )
    result = parse_session_detailed(jsonl)
    assert result.conflicting_duplicate_groups == 1
    assert result.facts
    assert all(f.prompt_tokens is None for f in result.facts)


def test_prompt_tokens_identity_missing_row_gets_its_own_value(tmp_path):
    jsonl = tmp_path / "p4.jsonl"
    write_jsonl(jsonl, [_haiku_row(None, None, input_tokens=20, cache_read=0, cache_creation=0, eph_5m=0, output=5)])
    facts, _ = parse_session_facts(jsonl)
    assert facts
    assert all(f.source_quality == "identity_missing" for f in facts)
    assert all(f.prompt_tokens == 20 for f in facts)


def test_prompt_tokens_missing_fields_count_as_zero(tmp_path):
    jsonl = tmp_path / "p5.jsonl"
    write_jsonl(
        jsonl,
        [
            _event(
                type="assistant",
                timestamp="2026-10-08T00:00:00Z",
                sessionId="s-prompt",
                requestId="req-1",
                message={"id": "msg-1", "model": "claude-haiku-5-5", "stop_reason": "end_turn",
                         "usage": {"input_tokens": 42, "output_tokens": 7}},
            )
        ],
    )
    facts, _ = parse_session_facts(jsonl)
    assert [f.prompt_tokens for f in facts] == [42, 42]


def test_prompt_tokens_is_none_when_ttl_breakdown_exceeds_cache_creation_total(tmp_path):
    jsonl = tmp_path / "p6.jsonl"
    write_jsonl(jsonl, [_haiku_row("msg-1", "req-1", cache_creation=5000, eph_5m=4000, eph_1h=2000)])
    facts, malformed = parse_session_facts(jsonl)
    assert malformed == 0
    assert facts  # the row's facts are still emitted
    assert all(f.prompt_tokens is None for f in facts)


@pytest.mark.parametrize("ttl_field", ["eph_5m", "eph_1h"])
def test_prompt_tokens_is_none_when_a_ttl_breakdown_field_is_negative(tmp_path, ttl_field):
    # Prompt length 100001 (over the 100K tier threshold); the negative TTL
    # field keeps the breakdown sum under cache_creation_input_tokens, so only
    # the per-field sign check catches it.
    jsonl = tmp_path / "p7.jsonl"
    overrides = {"eph_5m": 0, "eph_1h": 0, ttl_field: -1}
    write_jsonl(
        jsonl,
        [_haiku_row("msg-1", "req-1", input_tokens=1, cache_read=100000, cache_creation=0, **overrides)],
    )
    facts, malformed = parse_session_facts(jsonl)
    assert malformed == 0
    assert facts  # the row's facts are still emitted
    assert all(f.prompt_tokens is None for f in facts)


@pytest.mark.parametrize(
    "fields",
    [
        {"input_tokens": -1, "cache_read": 100002, "cache_creation": 0, "eph_5m": 0},
        {"input_tokens": 100002, "cache_read": -1, "cache_creation": 0, "eph_5m": 0},
        {"input_tokens": 1, "cache_read": 100001, "cache_creation": -1, "eph_5m": 0},
    ],
    ids=["input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"],
)
def test_prompt_tokens_is_none_when_a_top_level_field_is_negative(tmp_path, fields):
    jsonl = tmp_path / "p8.jsonl"
    write_jsonl(jsonl, [_haiku_row("msg-1", "req-1", **fields)])
    facts, malformed = parse_session_facts(jsonl)
    assert malformed == 0
    assert facts
    assert all(f.prompt_tokens is None for f in facts)


def test_prompt_tokens_is_none_when_one_row_of_a_streaming_group_is_inconsistent(tmp_path):
    from agent_cost.readers.claude import parse_session_detailed

    # Both rows yield the same tokens (cache_write_5m=110000), so the group is
    # an ordinary streaming progression; but the first row's TTL breakdown
    # exceeds its cache_creation_input_tokens, so its prompt length is None
    # and the group's prompt length isn't settled.
    jsonl = tmp_path / "p9.jsonl"
    write_jsonl(
        jsonl,
        [
            _haiku_row("msg-1", "req-1", input_tokens=10, cache_read=0, cache_creation=90000, eph_5m=110000,
                       output=3, stop_reason=None),
            _haiku_row("msg-1", "req-1", input_tokens=10, cache_read=0, cache_creation=110000, eph_5m=110000,
                       output=10, ts="2026-10-08T00:00:01Z"),
        ],
    )
    result = parse_session_detailed(jsonl)
    assert result.conflicting_duplicate_groups == 0
    assert result.duplicate_rows_skipped == 1
    assert {f.token_kind: f.tokens for f in result.facts} == {"input_nocache": 10, "cache_write_5m": 110000, "output": 10}
    assert all(f.prompt_tokens is None for f in result.facts)
