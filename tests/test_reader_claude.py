import json
from datetime import datetime, timezone

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
