import json
import sqlite3
from datetime import datetime, timezone

from agent_cost import cli


def _write_claude_session(claude_home, slug, session_name, events):
    project_dir = claude_home / "projects" / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / session_name
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _assistant_event(ts, model, input_tokens, output_tokens, session_id="s1"):
    return {
        "type": "assistant",
        "timestamp": ts,
        "sessionId": session_id,
        "message": {"model": model, "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}},
    }


_CODEX_THREADS_SCHEMA = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    model TEXT,
    rollout_path TEXT,
    tokens_used INTEGER,
    created_at_ms INTEGER,
    archived INTEGER DEFAULT 0
)
"""


def _write_codex_thread(codex_home, *, thread_id, model, rollout_events, tokens_used, created_at_ms):
    rollout_path = codex_home / f"{thread_id}.jsonl"
    rollout_path.write_text("\n".join(json.dumps(e) for e in rollout_events) + "\n")
    db_path = codex_home / "state_5.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='threads'"
        ).fetchone():
            conn.executescript(_CODEX_THREADS_SCHEMA)
        conn.execute(
            "INSERT INTO threads (id, model, rollout_path, tokens_used, created_at_ms) VALUES (?, ?, ?, ?, ?)",
            (thread_id, model, str(rollout_path), tokens_used, created_at_ms),
        )
        conn.commit()
    finally:
        conn.close()


def _token_count_event(ts, *, input_tokens, cached_input_tokens=0, output_tokens=0):
    return {
        "type": "event_msg",
        "timestamp": ts,
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_input_tokens,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": 0,
                }
            },
        },
    }


def _setup_env(tmp_path, monkeypatch):
    claude_home = tmp_path / "claude_home"
    codex_home = tmp_path / "codex_home"
    claude_home.mkdir()
    codex_home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("AGENT_COST_CONFIG", raising=False)
    return claude_home, codex_home


def test_report_json_e2e(tmp_path, monkeypatch, capsys):
    claude_home, _codex_home = _setup_env(tmp_path, monkeypatch)
    _write_claude_session(
        claude_home,
        "-Users-a-work-proj",
        "s1.jsonl",
        [
            _assistant_event("2026-06-01T00:00:00Z", "claude-opus-4-8", 1_000_000, 0),
        ],
    )

    rc = cli.main(["report", "--format", "json"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["schema_version"] == "1"
    assert payload["rates"]["catalog_version"]
    rows = payload["rows"]
    matching = [r for r in rows if r["model"] == "claude-opus-4-8" and r["token_kind"] == "input_nocache"]
    assert len(matching) == 1
    assert matching[0]["estimated_cost_usd"] == 5.0
    assert matching[0]["pricing_status"] == "priced"


def test_report_table_e2e_runs_without_error(tmp_path, monkeypatch, capsys):
    claude_home, _codex_home = _setup_env(tmp_path, monkeypatch)
    _write_claude_session(
        claude_home,
        "-Users-a-work-proj",
        "s1.jsonl",
        [_assistant_event("2026-06-01T00:00:00Z", "claude-opus-4-8", 1000, 500)],
    )
    rc = cli.main(["report", "--format", "table"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Total tokens" in out
    assert "Rates catalog" in out


def test_report_since_until_filters_window(tmp_path, monkeypatch, capsys):
    claude_home, _codex_home = _setup_env(tmp_path, monkeypatch)
    _write_claude_session(
        claude_home,
        "-Users-a-work-proj",
        "s1.jsonl",
        [
            _assistant_event("2026-05-01T00:00:00Z", "claude-opus-4-8", 100, 0),
            _assistant_event("2026-06-15T00:00:00Z", "claude-opus-4-8", 200, 0),
        ],
    )
    rc = cli.main(["report", "--format", "json", "--since", "2026-06-01", "--until", "2026-07-01"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    total_tokens = sum(r["tokens"] for r in payload["rows"])
    assert total_tokens == 200


def test_report_since_until_accept_z_suffix(tmp_path, monkeypatch, capsys):
    # JavaScript's Date.toISOString() always emits a trailing "Z"; a
    # JS-based caller (e.g. lane's TelemetryAdapter) must not be rejected
    # for using it.
    claude_home, _codex_home = _setup_env(tmp_path, monkeypatch)
    _write_claude_session(
        claude_home,
        "-Users-a-work-proj",
        "s1.jsonl",
        [
            _assistant_event("2026-05-01T00:00:00Z", "claude-opus-4-8", 100, 0),
            _assistant_event("2026-06-15T00:00:00Z", "claude-opus-4-8", 200, 0),
        ],
    )
    rc = cli.main(
        [
            "report",
            "--format",
            "json",
            "--since",
            "2026-06-01T00:00:00Z",
            "--until",
            "2026-07-01T00:00:00Z",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert sum(r["tokens"] for r in payload["rows"]) == 200
    assert payload["window"]["since"] == "2026-06-01T00:00:00+00:00"
    assert payload["window"]["until"] == "2026-07-01T00:00:00+00:00"


def test_z_suffix_and_plus_00_00_are_the_same_instant(tmp_path, monkeypatch, capsys):
    claude_home, _codex_home = _setup_env(tmp_path, monkeypatch)
    _write_claude_session(
        claude_home,
        "-Users-a-work-proj",
        "s1.jsonl",
        [_assistant_event("2026-06-15T00:00:00Z", "claude-opus-4-8", 100, 0)],
    )
    rc_z = cli.main(["report", "--format", "json", "--since", "2026-06-15T00:00:00Z"])
    payload_z = json.loads(capsys.readouterr().out)
    rc_offset = cli.main(["report", "--format", "json", "--since", "2026-06-15T00:00:00+00:00"])
    payload_offset = json.loads(capsys.readouterr().out)
    assert rc_z == 0 and rc_offset == 0
    assert payload_z["window"]["since"] == payload_offset["window"]["since"]
    assert sum(r["tokens"] for r in payload_z["rows"]) == sum(r["tokens"] for r in payload_offset["rows"])


def test_export_since_accepts_z_suffix(tmp_path, monkeypatch, capsys):
    claude_home, _codex_home = _setup_env(tmp_path, monkeypatch)
    _write_claude_session(
        claude_home,
        "-Users-a-work-proj",
        "s1.jsonl",
        [_assistant_event("2026-06-15T00:00:00Z", "claude-opus-4-8", 10, 5)],
    )
    rc = cli.main(["export", "--agent", "claude", "--since", "2026-06-01T00:00:00Z"])
    assert rc == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2


def test_since_z_suffix_date_only_still_uses_timezone(tmp_path, monkeypatch, capsys):
    # Date-only input (no "Z", no offset) must keep being interpreted in
    # --timezone -- this fix only teaches fromisoformat about "Z", it must
    # not change date-only handling.
    claude_home, _codex_home = _setup_env(tmp_path, monkeypatch)
    _write_claude_session(
        claude_home,
        "-Users-a-work-proj",
        "s1.jsonl",
        [_assistant_event("2026-05-31T16:00:00Z", "claude-opus-4-8", 100, 0)],  # 2026-06-01 01:00 JST
    )
    rc = cli.main(
        ["report", "--format", "json", "--since", "2026-06-01", "--timezone", "Asia/Tokyo"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert sum(r["tokens"] for r in payload["rows"]) == 100


def test_invalid_since_still_rejected_after_z_suffix_fix(tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    try:
        cli.main(["report", "--since", "not-a-date"])
        raised = False
    except SystemExit:
        raised = True
    assert raised  # unchanged pre-existing behavior for report/export


def test_report_cli_includes_codex_thread_created_before_window(tmp_path, monkeypatch, capsys):
    # Regression (real report/export path, not just the reader unit test):
    # a Codex thread created well before the window must still contribute
    # its in-window usage -- threads.created_at_ms must never hard-filter
    # it out.
    _claude_home, codex_home = _setup_env(tmp_path, monkeypatch)
    _write_codex_thread(
        codex_home,
        thread_id="t1",
        model="gpt-5.5",
        rollout_events=[_token_count_event("2026-06-15T00:00:00Z", input_tokens=1000, output_tokens=200)],
        tokens_used=1200,
        created_at_ms=0,  # long before the window below
    )
    rc = cli.main(
        ["report", "--format", "json", "--agent", "codex", "--since", "2026-06-01", "--until", "2026-07-01"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert sum(r["tokens"] for r in payload["rows"]) == 1200


def test_export_cli_includes_claude_file_modified_after_until(tmp_path, monkeypatch, capsys):
    # Regression (real export path): a session file's mtime reflects its
    # *last* write, which can be well after `until`, but an earlier
    # in-window event in that same file must still be exported.
    claude_home, _codex_home = _setup_env(tmp_path, monkeypatch)
    _write_claude_session(
        claude_home,
        "-Users-a-work-proj",
        "s1.jsonl",
        [_assistant_event("2026-06-10T00:00:00Z", "claude-opus-4-8", 42, 7)],
    )
    # The file's real mtime ("now") is after `until` below.
    rc = cli.main(
        ["export", "--agent", "claude", "--since", "2026-06-01", "--until", "2026-06-15"]
    )
    assert rc == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert sum(json.loads(line)["tokens"] for line in lines) == 49


def test_doctor_runs(tmp_path, monkeypatch, capsys):
    _setup_env(tmp_path, monkeypatch)
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "agent-cost doctor" in out
    assert rc in (0, 1)


def test_rates_validate_packaged_catalog(capsys):
    rc = cli.main(["rates", "validate"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[ok]" in out


def test_rates_show_model(capsys):
    rc = cli.main(["rates", "show", "--model", "claude-opus-4-8"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "claude-opus-4-8" in out


def test_report_json_schema_is_locked(tmp_path, monkeypatch, capsys):
    """Pins the report JSON's shape so a future change to it is deliberate."""
    claude_home, _codex_home = _setup_env(tmp_path, monkeypatch)
    _write_claude_session(
        claude_home,
        "-Users-a-work-proj",
        "s1.jsonl",
        [
            _assistant_event("2026-06-01T00:00:00Z", "claude-opus-4-8", 1_000_000, 0),
            # Unknown model -> unpriced row.
            _assistant_event("2026-06-01T00:01:00Z", "totally-unknown-model-xyz", 500, 0),
            # cache_creation with no TTL breakdown -> cache_write_unknown -> lower_bound row.
            {
                "type": "assistant",
                "timestamp": "2026-06-01T00:02:00Z",
                "sessionId": "s1",
                "message": {
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1_000_000},
                },
            },
        ],
    )

    rc = cli.main(["report", "--format", "json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    assert set(payload.keys()) == {
        "schema_version",
        "generated_at",
        "window",
        "timezone",
        "rates",
        "group_by",
        "data_quality",
        "rows",
    }
    assert payload["schema_version"] == "1"
    assert set(payload["window"].keys()) == {"since", "until"}
    assert set(payload["rates"].keys()) == {"catalog_version", "sha256"}
    assert set(payload["data_quality"].keys()) == {
        "malformed_events",
        "skipped_files",
        "negative_deltas",
        "unpriced_tokens",
    }

    row_columns = {
        "month",
        "agent",
        "model",
        "token_kind",
        "tokens",
        "priced_tokens",
        "unpriced_tokens",
        "estimated_cost_usd",
        "credits",
        "pricing_status",
    }
    assert len(payload["rows"]) > 0
    for row in payload["rows"]:
        assert set(row.keys()) == row_columns
        assert row["pricing_status"] in ("priced", "lower_bound", "unpriced")

    by_model = {r["model"]: r for r in payload["rows"] if r["token_kind"] == "input_nocache"}
    unpriced_row = by_model["totally-unknown-model-xyz"]
    assert unpriced_row["pricing_status"] == "unpriced"
    assert unpriced_row["estimated_cost_usd"] == 0.0
    assert unpriced_row["unpriced_tokens"] == unpriced_row["tokens"] == 500
    assert payload["data_quality"]["unpriced_tokens"] == 500

    lower_bound_rows = [r for r in payload["rows"] if r["token_kind"] == "cache_write_unknown"]
    assert len(lower_bound_rows) == 1
    assert lower_bound_rows[0]["pricing_status"] == "lower_bound"
    assert lower_bound_rows[0]["estimated_cost_usd"] > 0.0


def test_export_jsonl(tmp_path, monkeypatch, capsys):
    claude_home, _codex_home = _setup_env(tmp_path, monkeypatch)
    _write_claude_session(
        claude_home,
        "-Users-a-work-proj",
        "s1.jsonl",
        [_assistant_event("2026-06-01T00:00:00Z", "claude-opus-4-8", 10, 5)],
    )
    out_path = tmp_path / "facts.jsonl"
    rc = cli.main(["export", "--agent", "claude", "--out", str(out_path)])
    assert rc == 0
    lines = out_path.read_text().strip().splitlines()
    assert len(lines) == 2  # input_nocache + output facts
    record = json.loads(lines[0])
    assert record["agent"] == "claude"
    assert record["model_key"] == "claude-opus-4-8"
    assert record["source_quality"] == "ok"
    for line in lines:
        assert json.loads(line)["source_quality"] is not None
    # Privacy: no absolute paths, prompts, or branch names in export.
    assert "cwd" not in record
    assert "jsonl_path" not in record
    assert "branch" not in record


# ── measure ──


def test_measure_requires_at_least_one_session_id(capsys):
    rc = cli.main(["measure", "--format", "json"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "session-id" in err


def test_measure_invalid_timezone_is_input_error(capsys):
    rc = cli.main(["measure", "--session-id", "s1", "--timezone", "Not/AZone"])
    assert rc == 2


def test_measure_invalid_since_is_input_error(capsys):
    rc = cli.main(["measure", "--session-id", "s1", "--since", "not-a-date"])
    assert rc == 2


def test_measure_invalid_rates_path_is_input_error(tmp_path, capsys):
    bad_rates = tmp_path / "bad.json"
    bad_rates.write_text("{}")
    rc = cli.main(["measure", "--session-id", "s1", "--rates", str(bad_rates)])
    assert rc == 2


def test_measure_unknown_session_id_exits_zero_with_empty_result(tmp_path, monkeypatch, capsys):
    _setup_env(tmp_path, monkeypatch)
    rc = cli.main(["measure", "--session-id", "no-such-session"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_ids"] == ["no-such-session"]
    session = payload["sessions"]["no-such-session"]
    assert session["matched"] is False
    assert session["rows"] == []
    assert session["totals"] == {
        "tokens": 0,
        "priced_tokens": 0,
        "unpriced_tokens": 0,
        "estimated_cost_usd": 0.0,
        "credits": 0.0,
    }
    assert payload["total"]["totals"]["tokens"] == 0
    assert payload["data_quality"]["source_quality"] == {"ok": 0, "first_event_delta": 0}


def test_measure_multiple_sessions_claude_and_codex_mixed(tmp_path, monkeypatch, capsys):
    claude_home, codex_home = _setup_env(tmp_path, monkeypatch)
    _write_claude_session(
        claude_home,
        "-Users-a-work-proj",
        "s1.jsonl",
        [_assistant_event("2026-07-15T00:00:00Z", "claude-opus-4-8", 1_000_000, 0, session_id="session-a")],
    )
    _write_codex_thread(
        codex_home,
        thread_id="session-b",
        model="gpt-5.5",
        rollout_events=[_token_count_event("2026-07-15T00:00:00Z", input_tokens=1_000_000, output_tokens=0)],
        tokens_used=1_000_000,
        created_at_ms=0,
    )

    rc = cli.main(
        [
            "measure",
            "--session-id",
            "session-a",
            "--session-id",
            "session-b",
            "--format",
            "json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["protocol_version"] == "measure/v1"
    assert payload["session_ids"] == ["session-a", "session-b"]

    session_a = payload["sessions"]["session-a"]
    assert session_a["matched"] is True
    assert session_a["totals"]["tokens"] == 1_000_000
    assert session_a["totals"]["estimated_cost_usd"] == 5.0
    assert all(r["agent"] == "claude" for r in session_a["rows"])

    session_b = payload["sessions"]["session-b"]
    assert session_b["matched"] is True
    assert session_b["totals"]["tokens"] == 1_000_000
    assert session_b["totals"]["estimated_cost_usd"] == 5.0
    assert session_b["totals"]["credits"] == 125.0
    assert all(r["agent"] == "codex" for r in session_b["rows"])

    # total is the union of both requested sessions, not a global report.
    assert payload["total"]["totals"]["tokens"] == 2_000_000
    assert payload["total"]["totals"]["estimated_cost_usd"] == 10.0
    assert set(r["agent"] for r in payload["total"]["rows"]) == {"claude", "codex"}

    # measure never groups by month -- every row's "month" key is null.
    for row in session_a["rows"] + session_b["rows"] + payload["total"]["rows"]:
        assert row["month"] is None


def test_measure_agent_filter_excludes_other_agents_session(tmp_path, monkeypatch, capsys):
    claude_home, codex_home = _setup_env(tmp_path, monkeypatch)
    _write_claude_session(
        claude_home,
        "-Users-a-work-proj",
        "s1.jsonl",
        [_assistant_event("2026-06-01T00:00:00Z", "claude-opus-4-8", 100, 0, session_id="session-a")],
    )
    _write_codex_thread(
        codex_home,
        thread_id="session-b",
        model="gpt-5.5",
        rollout_events=[_token_count_event("2026-06-01T00:00:00Z", input_tokens=100, output_tokens=0)],
        tokens_used=100,
        created_at_ms=0,
    )
    rc = cli.main(
        [
            "measure",
            "--session-id",
            "session-a",
            "--session-id",
            "session-b",
            "--agent",
            "claude",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions"]["session-a"]["matched"] is True
    assert payload["sessions"]["session-b"]["matched"] is False


def test_measure_unpriced_model_reflected_in_data_quality(tmp_path, monkeypatch, capsys):
    claude_home, _codex_home = _setup_env(tmp_path, monkeypatch)
    _write_claude_session(
        claude_home,
        "-Users-a-work-proj",
        "s1.jsonl",
        [_assistant_event("2026-06-01T00:00:00Z", "totally-unknown-model", 500, 0, session_id="session-a")],
    )
    rc = cli.main(["measure", "--session-id", "session-a"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    session = payload["sessions"]["session-a"]
    assert session["matched"] is True
    assert session["rows"][0]["pricing_status"] == "unpriced"
    assert session["totals"]["unpriced_tokens"] == 500
    assert payload["data_quality"]["unpriced_tokens"] == 500


def test_measure_source_quality_breakdown_scoped_to_requested_sessions(tmp_path, monkeypatch, capsys):
    # A codex thread NOT among the requested session_ids must not leak into
    # the source_quality breakdown -- it's scoped to what was asked for.
    claude_home, codex_home = _setup_env(tmp_path, monkeypatch)
    _write_codex_thread(
        codex_home,
        thread_id="requested-session",
        model="gpt-5.5",
        rollout_events=[_token_count_event("2026-06-01T00:00:00Z", input_tokens=100, output_tokens=0)],
        tokens_used=100,
        created_at_ms=0,
    )
    _write_codex_thread(
        codex_home,
        thread_id="other-session",
        model="gpt-5.5",
        rollout_events=[_token_count_event("2026-06-01T00:00:00Z", input_tokens=999, output_tokens=0)],
        tokens_used=999,
        created_at_ms=0,
    )
    rc = cli.main(["measure", "--session-id", "requested-session", "--agent", "codex"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # Both codex threads' first delta is "first_event_delta"; only the
    # requested one should be counted.
    assert payload["data_quality"]["source_quality"]["first_event_delta"] == 1
    assert payload["total"]["totals"]["tokens"] == 100


def test_measure_window_filters_session_facts(tmp_path, monkeypatch, capsys):
    claude_home, _codex_home = _setup_env(tmp_path, monkeypatch)
    _write_claude_session(
        claude_home,
        "-Users-a-work-proj",
        "s1.jsonl",
        [
            _assistant_event("2026-05-01T00:00:00Z", "claude-opus-4-8", 100, 0, session_id="session-a"),
            _assistant_event("2026-06-15T00:00:00Z", "claude-opus-4-8", 200, 0, session_id="session-a"),
        ],
    )
    rc = cli.main(
        [
            "measure",
            "--session-id",
            "session-a",
            "--since",
            "2026-06-01",
            "--until",
            "2026-07-01",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions"]["session-a"]["totals"]["tokens"] == 200


def test_measure_since_until_accept_z_suffix(tmp_path, monkeypatch, capsys):
    # A JS caller (Date.toISOString()) always emits a "Z" suffix.
    claude_home, _codex_home = _setup_env(tmp_path, monkeypatch)
    _write_claude_session(
        claude_home,
        "-Users-a-work-proj",
        "s1.jsonl",
        [
            _assistant_event("2026-05-01T00:00:00Z", "claude-opus-4-8", 100, 0, session_id="session-a"),
            _assistant_event("2026-06-15T00:00:00Z", "claude-opus-4-8", 200, 0, session_id="session-a"),
        ],
    )
    rc = cli.main(
        [
            "measure",
            "--session-id",
            "session-a",
            "--since",
            "2026-06-01T00:00:00Z",
            "--until",
            "2026-07-01T00:00:00Z",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions"]["session-a"]["totals"]["tokens"] == 200
    assert payload["window"]["since"] == "2026-06-01T00:00:00+00:00"
    assert payload["window"]["until"] == "2026-07-01T00:00:00+00:00"
