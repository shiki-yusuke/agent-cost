import json

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
