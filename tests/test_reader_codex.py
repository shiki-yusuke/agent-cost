import json
import sqlite3

from agent_cost.readers.codex import (
    fetch_threads,
    parse_rollout_facts,
    read_codex_facts,
    snapshot_db,
)

CURRENT_SCHEMA = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    model TEXT,
    model_provider TEXT,
    reasoning_effort TEXT,
    cli_version TEXT,
    cwd TEXT,
    git_branch TEXT,
    git_sha TEXT,
    git_origin_url TEXT,
    created_at_ms INTEGER,
    updated_at_ms INTEGER,
    archived INTEGER DEFAULT 0,
    rollout_path TEXT,
    tokens_used INTEGER,
    title TEXT,
    preview TEXT,
    agent_role TEXT,
    agent_nickname TEXT,
    agent_path TEXT,
    source TEXT
)
"""

LEGACY_SCHEMA = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    model TEXT,
    rollout_path TEXT,
    tokens_used INTEGER
)
"""


def _make_db(path, schema, rows):
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(schema)
        for row in rows:
            columns = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            conn.execute(f"INSERT INTO threads ({columns}) VALUES ({placeholders})", list(row.values()))
        conn.commit()
    finally:
        conn.close()


# ── snapshot_db ──


def test_snapshot_db_creates_readable_copy(tmp_path):
    src = tmp_path / "state.sqlite"
    _make_db(src, CURRENT_SCHEMA, [{"id": "t1", "model": "gpt-5.5", "rollout_path": "x", "tokens_used": 10}])
    snap = snapshot_db(src)
    try:
        threads = fetch_threads(snap)
        assert len(threads) == 1
        assert threads[0]["id"] == "t1"
    finally:
        snap.unlink(missing_ok=True)


def test_snapshot_db_does_not_modify_source(tmp_path):
    src = tmp_path / "state.sqlite"
    _make_db(src, CURRENT_SCHEMA, [{"id": "t1", "model": "gpt-5.5", "rollout_path": "x", "tokens_used": 10}])
    before = src.read_bytes()
    snap = snapshot_db(src)
    snap.unlink(missing_ok=True)
    after = src.read_bytes()
    assert before == after


def test_snapshot_db_works_with_wal_writer_open(tmp_path):
    src = tmp_path / "state.sqlite"
    writer = sqlite3.connect(str(src))
    writer.execute("PRAGMA journal_mode=WAL")
    writer.executescript(CURRENT_SCHEMA)
    writer.execute(
        "INSERT INTO threads (id, model, rollout_path, tokens_used) VALUES (?, ?, ?, ?)",
        ("t1", "gpt-5.5", "x", 10),
    )
    writer.commit()
    try:
        snap = snapshot_db(src)
        try:
            threads = fetch_threads(snap)
            assert len(threads) == 1
            assert threads[0]["id"] == "t1"
        finally:
            snap.unlink(missing_ok=True)
    finally:
        writer.close()


# ── fetch_threads: schema feature detection ──


def test_fetch_threads_legacy_schema_without_created_at_ms_or_archived(tmp_path):
    src = tmp_path / "state.sqlite"
    _make_db(src, LEGACY_SCHEMA, [{"id": "t1", "model": "gpt-5.5", "rollout_path": "x", "tokens_used": 5}])
    snap = snapshot_db(src)
    try:
        threads = fetch_threads(snap, include_archived=False)
        assert len(threads) == 1
    finally:
        snap.unlink(missing_ok=True)


def test_fetch_threads_archived_default_included(tmp_path):
    src = tmp_path / "state.sqlite"
    _make_db(
        src,
        CURRENT_SCHEMA,
        [
            {"id": "t1", "model": "gpt-5.5", "rollout_path": "x", "tokens_used": 1, "archived": 0},
            {"id": "t2", "model": "gpt-5.5", "rollout_path": "y", "tokens_used": 1, "archived": 1},
        ],
    )
    snap = snapshot_db(src)
    try:
        threads = fetch_threads(snap)
        assert {t["id"] for t in threads} == {"t1", "t2"}
        threads_excl = fetch_threads(snap, include_archived=False)
        assert {t["id"] for t in threads_excl} == {"t1"}
    finally:
        snap.unlink(missing_ok=True)


def test_fetch_threads_boundary_time_filter(tmp_path):
    src = tmp_path / "state.sqlite"
    _make_db(
        src,
        CURRENT_SCHEMA,
        [
            {"id": "early", "model": "x", "rollout_path": "x", "tokens_used": 1, "created_at_ms": 1000},
            {"id": "late", "model": "x", "rollout_path": "x", "tokens_used": 1, "created_at_ms": 5000},
        ],
    )
    snap = snapshot_db(src)
    try:
        threads = fetch_threads(snap, since_ms=2000, until_ms=6000)
        assert {t["id"] for t in threads} == {"late"}
    finally:
        snap.unlink(missing_ok=True)


# ── parse_rollout_facts ──


def test_first_total_null_event_is_skipped(tmp_path):
    rollout = tmp_path / "r1.jsonl"
    events = [
        {
            "type": "event_msg",
            "timestamp": "2026-06-01T00:00:00Z",
            "payload": {"type": "token_count", "info": {"total_token_usage": None}},
        },
        {
            "type": "event_msg",
            "timestamp": "2026-06-01T00:01:00Z",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 50, "reasoning_output_tokens": 0}},
            },
        },
    ]
    rollout.write_text("\n".join(json.dumps(e) for e in events) + "\n")

    facts, malformed, negative = parse_rollout_facts(rollout, model_raw="gpt-5.5", session_id="t1")
    assert malformed == 0
    assert negative == 0
    total = sum(f.tokens for f in facts)
    assert total == 150


def test_multiple_cumulative_events_produce_deltas(tmp_path):
    rollout = tmp_path / "r2.jsonl"
    events = [
        {"type": "event_msg", "timestamp": "2026-06-01T00:00:00Z", "payload": {"type": "token_count", "info": {"total_token_usage": None}}},
        {
            "type": "event_msg",
            "timestamp": "2026-06-01T00:01:00Z",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 50, "reasoning_output_tokens": 0}},
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-06-01T00:02:00Z",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 250, "cached_input_tokens": 50, "output_tokens": 90, "reasoning_output_tokens": 10}},
            },
        },
    ]
    rollout.write_text("\n".join(json.dumps(e) for e in events) + "\n")

    facts, malformed, negative = parse_rollout_facts(rollout, model_raw="gpt-5.5", session_id="t1")
    assert malformed == 0
    assert negative == 0

    by_ts = {}
    for f in facts:
        by_ts.setdefault(f.occurred_at_utc.isoformat(), {})[f.token_kind] = f.tokens

    first = by_ts["2026-06-01T00:01:00+00:00"]
    assert first["input_nocache"] == 80  # 100 - 20
    assert first["cache_read"] == 20
    assert first["output"] == 50

    second = by_ts["2026-06-01T00:02:00+00:00"]
    assert second["input_nocache"] == 120  # (250-100) - (50-20) = 150-30
    assert second["cache_read"] == 30
    assert second["output"] == 50  # (90-50) + (10-0)


def test_reasoning_output_tokens_combined_into_output(tmp_path):
    rollout = tmp_path / "r3.jsonl"
    events = [
        {
            "type": "event_msg",
            "timestamp": "2026-06-01T00:00:00Z",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5, "reasoning_output_tokens": 7}},
            },
        },
    ]
    rollout.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    facts, _, _ = parse_rollout_facts(rollout, model_raw="gpt-5.5", session_id="t1")
    output_fact = [f for f in facts if f.token_kind == "output"][0]
    assert output_fact.tokens == 12


def test_negative_delta_is_skipped_and_counted(tmp_path):
    rollout = tmp_path / "r4.jsonl"
    events = [
        {
            "type": "event_msg",
            "timestamp": "2026-06-01T00:00:00Z",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 20, "reasoning_output_tokens": 0}},
            },
        },
        {
            # Reset: cumulative totals go backwards.
            "type": "event_msg",
            "timestamp": "2026-06-01T00:01:00Z",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 10, "cached_input_tokens": 5, "output_tokens": 2, "reasoning_output_tokens": 0}},
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-06-01T00:02:00Z",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 30, "cached_input_tokens": 10, "output_tokens": 12, "reasoning_output_tokens": 0}},
            },
        },
    ]
    rollout.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    facts, malformed, negative = parse_rollout_facts(rollout, model_raw="gpt-5.5", session_id="t1")
    assert malformed == 0
    assert negative == 2  # input/cache channel reset once, output channel reset once

    by_ts = {}
    for f in facts:
        by_ts.setdefault(f.occurred_at_utc.isoformat(), {})[f.token_kind] = f.tokens
    assert "2026-06-01T00:01:00+00:00" not in by_ts
    third = by_ts["2026-06-01T00:02:00+00:00"]
    assert third["input_nocache"] == 15  # (30-10) - (10-5)
    assert third["cache_read"] == 5
    assert third["output"] == 10


def test_corrupted_json_line_counted_as_malformed(tmp_path):
    rollout = tmp_path / "r5.jsonl"
    good = {
        "type": "event_msg",
        "timestamp": "2026-06-01T00:00:00Z",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5, "reasoning_output_tokens": 0}},
        },
    }
    rollout.write_text("{broken\n" + json.dumps(good) + "\n")
    facts, malformed, negative = parse_rollout_facts(rollout, model_raw="gpt-5.5", session_id="t1")
    assert malformed == 1
    assert len(facts) == 2


def test_missing_rollout_file_returns_empty(tmp_path):
    facts, malformed, negative = parse_rollout_facts(tmp_path / "missing.jsonl", model_raw="gpt-5.5", session_id="t1")
    assert facts == []
    assert malformed == 0
    assert negative == 0


def test_fast_mode_true_false_unknown(tmp_path):
    def _facts_for(model, collab_mode):
        rollout = tmp_path / f"r-{model}-{collab_mode}.jsonl"
        events = []
        if collab_mode is not None:
            events.append(
                {"type": "event_msg", "timestamp": "2026-06-01T00:00:00Z", "payload": {"type": "task_started", "collaboration_mode_kind": collab_mode}}
            )
        events.append(
            {
                "type": "event_msg",
                "timestamp": "2026-06-01T00:01:00Z",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5, "reasoning_output_tokens": 0}},
                },
            }
        )
        rollout.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        facts, _, _ = parse_rollout_facts(rollout, model_raw=model, session_id="t1")
        return facts

    fast_by_model_suffix = _facts_for("gpt-5.5-fast", None)
    assert {f.mode for f in fast_by_model_suffix} == {"fast"}

    fast_by_collab = _facts_for("gpt-5.5", "fast-turbo")
    assert {f.mode for f in fast_by_collab} == {"fast"}

    normal = _facts_for("gpt-5.5", "default")
    assert {f.mode for f in normal} == {"normal"}

    unknown = _facts_for("gpt-5.5", "some-new-mode-2027")
    assert {f.mode for f in unknown} == {"unknown"}

    unknown_no_collab = _facts_for("gpt-5.5", None)
    assert {f.mode for f in unknown_no_collab} == {"unknown"}


def test_tokens_used_vs_rollout_diff_flagged(tmp_path):
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    rollout_dir = codex_home / "rollouts"
    rollout_dir.mkdir()
    rollout = rollout_dir / "r1.jsonl"
    events = [
        {
            "type": "event_msg",
            "timestamp": "2026-06-01T00:00:00Z",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5, "reasoning_output_tokens": 0}},
            },
        },
    ]
    rollout.write_text("\n".join(json.dumps(e) for e in events) + "\n")

    db_path = codex_home / "state_5.sqlite"
    _make_db(
        db_path,
        CURRENT_SCHEMA,
        [{"id": "t1", "model": "gpt-5.5", "rollout_path": str(rollout), "tokens_used": 999999, "created_at_ms": 1}],
    )
    result = read_codex_facts(db_path, codex_home)
    assert result.tokens_used_diffs == 1


def test_read_codex_facts_skips_missing_rollout_file(tmp_path):
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    db_path = codex_home / "state_5.sqlite"
    _make_db(
        db_path,
        CURRENT_SCHEMA,
        [{"id": "t1", "model": "gpt-5.5", "rollout_path": "does-not-exist.jsonl", "tokens_used": 5, "created_at_ms": 1}],
    )
    result = read_codex_facts(db_path, codex_home)
    assert result.skipped_files == 1
    assert result.facts == []
