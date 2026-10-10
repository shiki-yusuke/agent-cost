import decimal
import itertools
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_cost import billing_plan, cli
from agent_cost.facts import SOURCE_QUALITY_VALUES

# These tests exercise report/measure/export/pricing behavior, not the
# Claude reader's dedup logic (that lives in tests/test_reader_dedup.py) --
# so every synthetic event here gets its own unique (message.id, requestId)
# pair by default, keeping it out of the "identity_missing" / dedup path
# entirely and preserving each test's original intent (a plain, singular
# "ok" billing event) under the full-pair-only dedup contract.
_assistant_event_ids = itertools.count(1)


def _write_claude_session(claude_home, slug, session_name, events):
    project_dir = claude_home / "projects" / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / session_name
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _assistant_event(
    ts, model, input_tokens, output_tokens, session_id="s1", message_id=None, request_id=None
):
    seq = next(_assistant_event_ids)
    if message_id is None:
        message_id = f"synthetic-msg-{seq}"
    if request_id is None:
        request_id = f"synthetic-req-{seq}"
    return {
        "type": "assistant",
        "timestamp": ts,
        "sessionId": session_id,
        "requestId": request_id,
        "message": {
            "id": message_id,
            "model": model,
            "stop_reason": "end_turn",
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        },
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
        "duplicate_rows_skipped",
        "conflicting_duplicate_groups",
        "missing_dedup_identity_rows",
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
    assert payload["data_quality"]["source_quality"] == {
        "ok": 0,
        "first_event_delta": 0,
        "identity_missing": 0,
        "output_lower_bound": 0,
    }


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


def test_measure_json_schema_is_locked(tmp_path, monkeypatch, capsys):
    """Pins the measure JSON's shape so a future change to it is deliberate.

    A known gap between report and measure until now: report has had
    test_report_json_schema_is_locked since the JSON format existed, but measure --
    a separate, independently hand-built payload in cmd_measure -- never got the same
    pin. This follows that test's own conventions (exact key-set assertions, one
    session exercising all three pricing_status values) so a shape change to either
    command's JSON is caught the same way.
    """
    claude_home, _codex_home = _setup_env(tmp_path, monkeypatch)
    _write_claude_session(
        claude_home,
        "-Users-a-work-proj",
        "s1.jsonl",
        [
            _assistant_event("2026-06-01T00:00:00Z", "claude-opus-4-8", 1_000_000, 0, session_id="session-a"),
            # Unknown model -> unpriced row.
            _assistant_event("2026-06-01T00:01:00Z", "totally-unknown-model-xyz", 500, 0, session_id="session-a"),
            # cache_creation with no TTL breakdown -> cache_write_unknown -> lower_bound row.
            {
                "type": "assistant",
                "timestamp": "2026-06-01T00:02:00Z",
                "sessionId": "session-a",
                "message": {
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1_000_000},
                },
            },
        ],
    )

    rc = cli.main(["measure", "--session-id", "session-a", "--session-id", "no-such-session", "--format", "json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    assert set(payload.keys()) == {
        "protocol_version",
        "producer_version",
        "accounting_basis",
        "generated_at",
        "window",
        "timezone",
        "agent",
        "rates",
        "session_ids",
        "sessions",
        "total",
        "data_quality",
    }
    assert payload["protocol_version"] == "measure/v1"
    assert payload["accounting_basis"] == "agent-cost-raw-total/v2"
    assert set(payload["window"].keys()) == {"since", "until"}
    assert set(payload["rates"].keys()) == {"catalog_version", "sha256"}
    assert set(payload["data_quality"].keys()) == {
        "malformed_events",
        "skipped_files",
        "negative_deltas",
        "unpriced_tokens",
        "duplicate_rows_skipped",
        "conflicting_duplicate_groups",
        "missing_dedup_identity_rows",
        "source_quality",
    }
    assert set(payload["data_quality"]["source_quality"].keys()) == set(SOURCE_QUALITY_VALUES)

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
    totals_columns = {"tokens", "priced_tokens", "unpriced_tokens", "estimated_cost_usd", "credits"}
    session_columns = {"matched", "rows", "totals"}

    assert set(payload["sessions"].keys()) == {"session-a", "no-such-session"}
    for session in payload["sessions"].values():
        assert set(session.keys()) == session_columns
        assert set(session["totals"].keys()) == totals_columns
        for row in session["rows"]:
            assert set(row.keys()) == row_columns
            assert row["pricing_status"] in ("priced", "lower_bound", "unpriced")
            # measure never groups by month.
            assert row["month"] is None

    assert set(payload["total"].keys()) == {"rows", "totals"}
    assert set(payload["total"]["totals"].keys()) == totals_columns

    session_a = payload["sessions"]["session-a"]
    assert session_a["matched"] is True
    by_status = {r["pricing_status"] for r in session_a["rows"]}
    assert by_status == {"priced", "unpriced", "lower_bound"}

    no_such = payload["sessions"]["no-such-session"]
    assert no_such["matched"] is False
    assert no_such["rows"] == []
    assert no_such["totals"] == {
        "tokens": 0,
        "priced_tokens": 0,
        "unpriced_tokens": 0,
        "estimated_cost_usd": 0.0,
        "credits": 0.0,
    }


def test_export_jsonl_includes_prompt_tokens(tmp_path, monkeypatch, capsys):
    claude_home, codex_home = _setup_env(tmp_path, monkeypatch)
    _write_claude_session(
        claude_home,
        "-Users-a-work-proj",
        "s1.jsonl",
        [_assistant_event("2026-06-01T00:00:00Z", "claude-opus-4-8", 10, 5)],
    )
    _write_codex_thread(
        codex_home,
        thread_id="t1",
        model="gpt-5.5",
        rollout_events=[_token_count_event("2026-06-15T00:00:00Z", input_tokens=1000, output_tokens=200)],
        tokens_used=1200,
        created_at_ms=0,
    )
    out_path = tmp_path / "facts.jsonl"
    rc = cli.main(["export", "--out", str(out_path)])
    assert rc == 0
    records = [json.loads(line) for line in out_path.read_text().strip().splitlines()]
    claude_records = [r for r in records if r["agent"] == "claude"]
    codex_records = [r for r in records if r["agent"] == "codex"]
    assert claude_records and codex_records
    assert all("prompt_tokens" in r for r in records)
    assert all(r["prompt_tokens"] == 10 for r in claude_records)
    assert all(r["prompt_tokens"] is None for r in codex_records)


def test_rates_show_model_prints_prompt_tiers(capsys):
    rc = cli.main(["rates", "show", "--model", "claude-haiku-5-5"])
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    assert "      prompt_tiers:" in lines
    assert (
        "        > 100000: input_nocache=0.50 cache_read=0.05 cache_write_5m=0.625 cache_write_1h=1.0 output=2.50"
        in lines
    )
    assert lines.index("      output: 0.50") < lines.index("      prompt_tiers:")


def test_rates_show_model_without_tiers_prints_no_prompt_tiers(capsys):
    rc = cli.main(["rates", "show", "--model", "claude-haiku-4-5"])
    assert rc == 0
    assert "prompt_tiers" not in capsys.readouterr().out


def test_rates_validate_packaged_catalog_with_prompt_tiers_exits_zero(capsys):
    rc = cli.main(["rates", "validate"])
    assert rc == 0
    assert "catalog_version=2026-10-09" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# report --billing-plan (0.5.0). Every amount and date below is fictional.

_PLAN_SINCE = "2026-06-01T00:00:00+00:00"
_PLAN_UNTIL = "2026-07-01T00:00:00+00:00"


def _billing_plan_dict():
    return {
        "plan_schema_version": "1",
        "plan_id": "bp-cli",
        "nonce": "0123456789abcdef0123456789abcdef",
        "applies_to": {"agent": "claude", "scope": "seat"},
        "basis": "agent-cost-list-price",
        "periods": [
            {
                "period_id": "p1",
                "effective_from": "2026-06-01T00:00:00+00:00",
                "window_subscription_usd": "3",
                "allowance_usd": None,
                "overage": None,
            },
            {
                "period_id": "p2",
                "effective_from": "2026-06-11T00:00:00+00:00",
                "window_subscription_usd": "10",
                "allowance_usd": "50",
                "overage": {"type": "charge_multiplier", "value": "0.5"},
            },
            {"period_id": "end", "effective_from": "2026-07-01T00:00:00+00:00", "terminates": True},
        ],
    }


def _write_billing_plan(tmp_path, data=None, mode=0o600):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(data if data is not None else _billing_plan_dict()))
    os.chmod(path, mode)
    return path


def _billing_setup(tmp_path, monkeypatch):
    claude_home, codex_home = _setup_env(tmp_path, monkeypatch)
    monkeypatch.delenv("AGENT_COST_NOW", raising=False)
    _write_claude_session(
        claude_home,
        "-Users-a-work-proj",
        "s1.jsonl",
        [
            # 12M input tokens of claude-opus-4-8 at $5 / MTok = $60 in p2.
            _assistant_event("2026-06-15T00:00:00Z", "claude-opus-4-8", 12_000_000, 0),
            # $5 in p1 (allowance null).
            _assistant_event("2026-06-05T00:00:00Z", "claude-opus-4-8", 1_000_000, 0),
        ],
    )
    return claude_home, codex_home, _write_billing_plan(tmp_path)


def _billing_args(plan_path, *extra):
    return [
        "report",
        "--billing-plan",
        str(plan_path),
        "--since",
        _PLAN_SINCE,
        "--until",
        _PLAN_UNTIL,
        *extra,
    ]


def test_billing_plan_json_has_internal_billing(tmp_path, monkeypatch, capsys):
    _claude_home, _codex_home, plan_path = _billing_setup(tmp_path, monkeypatch)
    rc = cli.main(_billing_args(plan_path, "--format", "json"))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    # Existing keys are unchanged; internal_billing is added.
    assert set(payload) == {
        "schema_version",
        "generated_at",
        "window",
        "timezone",
        "rates",
        "group_by",
        "data_quality",
        "rows",
        "internal_billing",
    }
    ib = payload["internal_billing"]
    assert ib["calc_schema_version"] == "1"
    assert ib["plan_id"] == "bp-cli"
    assert re.match(r"^[0-9a-f]{64}$", ib["revision_id"])
    assert ib["basis"] == "agent-cost-list-price"
    assert ib["catalog_version"] == payload["rates"]["catalog_version"]
    assert ib["catalog_sha256"] == payload["rates"]["sha256"]
    assert ib["since"] == _PLAN_SINCE
    assert ib["until"] == _PLAN_UNTIL
    assert ib["generated_at"] == payload["generated_at"]
    assert ib["plan_coverage"] == "full"
    assert ib["uncovered"] == []

    windows = {w["period_id"]: w for w in ib["windows"]}
    assert [w["period_id"] for w in ib["windows"]] == ["p1", "p2"]
    p1, p2 = windows["p1"], windows["p2"]
    assert p1["allowance_usd"] is None
    assert p1["list_cost_usd"] == "5.0000"
    assert p1["overage_usd"] == "0.0000"
    assert p1["internal_cost_usd"] == "3.0000"
    assert p2["list_cost_usd"] == "60.0000"
    assert p2["allowance_usd"] == "50.0000"
    assert p2["overage_usd"] == "10.0000"
    assert p2["overage_cost_usd"] == "5.0000"
    assert p2["window_subscription_usd"] == "10.0000"
    assert p2["internal_cost_usd"] == "15.0000"
    assert p2["fact_count"] >= 1 and isinstance(p2["fact_count"], int)
    assert p2["query_coverage"] == "full"
    assert p2["window_state"] == "closed"
    assert p2["list_cost_pricing"] == "priced"
    assert p2["internal_cost_certainty"] == "estimate"
    for w in ib["windows"]:
        for key in ("list_cost_usd", "overage_usd", "overage_cost_usd", "window_subscription_usd", "internal_cost_usd"):
            assert isinstance(w[key], str) and re.match(r"^\d+\.\d{4}$", w[key]), (key, w[key])


def test_billing_plan_table_has_confidential_section(tmp_path, monkeypatch, capsys):
    _claude_home, _codex_home, plan_path = _billing_setup(tmp_path, monkeypatch)
    rc = cli.main(_billing_args(plan_path, "--format", "table"))
    assert rc == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    header = [l for l in lines if l.startswith("Internal billing (CONFIDENTIAL -- do not share):")]
    assert len(header) == 1
    assert "plan bp-cli rev " in header[0]
    assert "basis agent-cost-list-price" in header[0]
    assert "plan_coverage full" in header[0]
    after = lines[lines.index(header[0]) :]
    assert any(l.startswith("p2 ") and "15.0000" in l and "estimate" in l for l in after)
    assert any(l.startswith("p1 ") and " - " in l for l in after)  # null allowance -> "-"
    # The confidential section comes after the regular report.
    assert out.index("Total tokens:") < out.index("Internal billing (CONFIDENTIAL")


def test_billing_plan_table_lists_uncovered_ranges(tmp_path, monkeypatch, capsys):
    _claude_home, _codex_home, plan_path = _billing_setup(tmp_path, monkeypatch)
    rc = cli.main(
        [
            "report",
            "--billing-plan",
            str(plan_path),
            "--since",
            "2026-05-25T00:00:00+00:00",
            "--until",
            _PLAN_UNTIL,
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "plan_coverage partial" in out
    assert "uncovered: 2026-05-25T00:00:00+00:00 .. 2026-06-01T00:00:00+00:00" in out


def test_report_without_billing_plan_has_no_internal_billing(tmp_path, monkeypatch, capsys):
    _billing_setup(tmp_path, monkeypatch)
    rc = cli.main(["report", "--format", "json", "--since", _PLAN_SINCE, "--until", _PLAN_UNTIL])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "internal_billing" not in payload

    rc = cli.main(["report", "--format", "table"])
    assert rc == 0
    assert "Internal billing" not in capsys.readouterr().out


def test_billing_plan_combination_rules_exit_2_with_empty_stdout(tmp_path, monkeypatch, capsys):
    _claude_home, _codex_home, plan_path = _billing_setup(tmp_path, monkeypatch)
    rates_path = tmp_path / "rates.json"
    rates_path.write_text((Path(cli.__file__).parent / "rates.json").read_text())
    p = str(plan_path)
    cases = [
        _billing_args(plan_path, "--rates", str(rates_path)),
        _billing_args(plan_path, "--format", "csv"),
        _billing_args(plan_path, "--agent", "codex"),
        ["report", "--billing-plan", p, "--until", _PLAN_UNTIL],
        ["report", "--billing-plan", p, "--since", _PLAN_SINCE],
        ["report", "--billing-plan", p],
        ["report", "--billing-plan", p, "--since", _PLAN_UNTIL, "--until", _PLAN_UNTIL],
        ["report", "--billing-plan", p, "--since", _PLAN_UNTIL, "--until", _PLAN_SINCE],
    ]
    for argv in cases:
        rc = cli.main(argv)
        captured = capsys.readouterr()
        assert rc == 2, argv
        assert captured.out == "", argv
        assert captured.err.startswith("[error] --billing-plan: "), argv
        assert str(tmp_path) not in captured.err


def test_billing_plan_agent_list_including_claude_is_allowed(tmp_path, monkeypatch, capsys):
    _claude_home, _codex_home, plan_path = _billing_setup(tmp_path, monkeypatch)
    rc = cli.main(_billing_args(plan_path, "--format", "json", "--agent", "codex,claude"))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "internal_billing" in payload


def test_billing_plan_invalid_plan_exit_2_with_empty_stdout(tmp_path, monkeypatch, capsys):
    _claude_home, _codex_home, _plan_path = _billing_setup(tmp_path, monkeypatch)
    bad = _billing_plan_dict()
    bad["periods"][1]["allowance_usd"] = 4242.4242
    bad_path = _write_billing_plan(tmp_path, bad)
    rc = cli.main(_billing_args(bad_path, "--format", "json"))
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "4242.4242" not in captured.err
    assert str(tmp_path) not in captured.err
    assert "plan.json" not in captured.err


def test_billing_plan_insecure_file_exit_2_with_empty_stdout(tmp_path, monkeypatch, capsys):
    _claude_home, _codex_home, plan_path = _billing_setup(tmp_path, monkeypatch)
    os.chmod(plan_path, 0o644)
    rc = cli.main(_billing_args(plan_path, "--format", "json"))
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert str(tmp_path) not in captured.err


def test_billing_plan_missing_file_exit_2_with_empty_stdout(tmp_path, monkeypatch, capsys):
    _billing_setup(tmp_path, monkeypatch)
    rc = cli.main(_billing_args(tmp_path / "nope.json", "--format", "json"))
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert str(tmp_path) not in captured.err


def test_billing_plan_empty_path_exit_2_with_empty_stdout(tmp_path, monkeypatch, capsys):
    _billing_setup(tmp_path, monkeypatch)
    rc = cli.main(_billing_args("", "--format", "json"))
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert captured.err.startswith("[error] --billing-plan: ")


def test_billing_plan_with_empty_rates_is_rejected(tmp_path, monkeypatch, capsys):
    _claude_home, _codex_home, plan_path = _billing_setup(tmp_path, monkeypatch)
    rc = cli.main(_billing_args(plan_path, "--format", "json", "--rates", ""))
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert captured.err == "[error] --billing-plan: cannot be combined with --rates\n"


@pytest.mark.parametrize(
    "error",
    [
        billing_plan.BillingPlanError("internal billing arithmetic would round; refusing to compute"),
        decimal.InvalidOperation([decimal.InvalidOperation]),
    ],
)
def test_billing_plan_format_failure_exit_2_without_traceback(tmp_path, monkeypatch, capsys, error):
    _claude_home, _codex_home, plan_path = _billing_setup(tmp_path, monkeypatch)

    def failing_format(block):
        raise error

    monkeypatch.setattr(cli, "format_internal_billing", failing_format)
    rc = cli.main(_billing_args(plan_path, "--format", "json"))
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert captured.err.startswith("[error] --billing-plan: ")
    assert "Traceback" not in captured.err
    assert str(tmp_path) not in captured.err


def test_agent_cost_now_drives_generated_at_and_window_state(tmp_path, monkeypatch, capsys):
    _claude_home, _codex_home, plan_path = _billing_setup(tmp_path, monkeypatch)

    monkeypatch.setenv("AGENT_COST_NOW", "2026-06-20T09:00:00+09:00")
    rc = cli.main(_billing_args(plan_path, "--format", "json"))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["generated_at"] == "2026-06-20T00:00:00+00:00"
    ib = payload["internal_billing"]
    assert ib["generated_at"] == "2026-06-20T00:00:00+00:00"
    states = {w["period_id"]: w["window_state"] for w in ib["windows"]}
    assert states == {"p1": "closed", "p2": "open"}
    assert {w["period_id"]: w["internal_cost_certainty"] for w in ib["windows"]}["p2"] == "lower_bound"

    # until_w == generated_at -> closed.
    monkeypatch.setenv("AGENT_COST_NOW", _PLAN_UNTIL)
    rc = cli.main(_billing_args(plan_path, "--format", "json"))
    assert rc == 0
    ib = json.loads(capsys.readouterr().out)["internal_billing"]
    assert {w["period_id"]: w["window_state"] for w in ib["windows"]} == {"p1": "closed", "p2": "closed"}


def test_agent_cost_now_applies_without_billing_plan(tmp_path, monkeypatch, capsys):
    _setup_env(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENT_COST_NOW", "2026-06-20T00:00:00+00:00")
    assert cli.main(["report", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["generated_at"] == "2026-06-20T00:00:00+00:00"
    assert cli.main(["measure", "--session-id", "x"]) == 0
    assert json.loads(capsys.readouterr().out)["generated_at"] == "2026-06-20T00:00:00+00:00"


def test_agent_cost_now_keeps_sub_second_precision(tmp_path, monkeypatch, capsys):
    _setup_env(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENT_COST_NOW", "2026-06-20T00:00:00.123456+00:00")
    assert cli.main(["report", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["generated_at"] == "2026-06-20T00:00:00.123456+00:00"


def test_invalid_agent_cost_now_exit_2(tmp_path, monkeypatch, capsys):
    _claude_home, _codex_home, plan_path = _billing_setup(tmp_path, monkeypatch)
    for value in ("not-a-time", "2026-06-20T00:00:00"):
        monkeypatch.setenv("AGENT_COST_NOW", value)
        for argv in (
            _billing_args(plan_path, "--format", "json"),
            ["report", "--format", "json"],
            ["measure", "--session-id", "x"],
        ):
            rc = cli.main(argv)
            captured = capsys.readouterr()
            assert rc == 2, (value, argv)
            assert captured.out == "", (value, argv)
            assert "AGENT_COST_NOW" in captured.err


def test_report_help_mentions_billing_plan(capsys):
    with pytest.raises(SystemExit):
        cli.main(["report", "--help"])
    out = capsys.readouterr().out
    assert "--billing-plan PATH" in out
