"""Tests for ``source_quality="output_lower_bound"`` (E0-a4 S1).

Spec: when the row a dedup group adopts (``_select_adopted_row``) has no
valid ``message.stop_reason`` (a non-empty ``str``), that group's
``token_kind="output"`` fact is flagged ``"output_lower_bound"`` -- its
``output_tokens`` is the streaming head's value, a lower bound on the true
count. Input-side facts from the same group stay ``"ok"``. The decision is
"is the *adopted* row final", not "does the group contain a final row", so
a final row followed by a differing non-final row (adoption switches to
the last row) is flagged too. Identity-missing standalone rows keep
``"identity_missing"``. Token amounts, adoption and conflict counting are
unchanged -- only the flag is added.

All fixtures are synthetic and written to ``tmp_path``.
"""

import json

import pytest

from agent_cost import cli
from agent_cost.facts import SOURCE_QUALITY_VALUES
from agent_cost.readers.claude import parse_session_detailed

_ABSENT = object()


def _event(
    ts,
    output,
    *,
    stop_reason=_ABSENT,
    msg_id="msg-1",
    req_id="req-1",
    session_id="s-olb",
    input_tokens=10,
    cache_read=20,
    cache_write_5m=30,
):
    message = {
        "id": msg_id,
        "model": "claude-sonnet-5",
        "usage": {
            "input_tokens": input_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write_5m,
            "cache_creation": {"ephemeral_5m_input_tokens": cache_write_5m, "ephemeral_1h_input_tokens": 0},
            "output_tokens": output,
        },
    }
    if msg_id is None:
        del message["id"]
    if stop_reason is not _ABSENT:
        message["stop_reason"] = stop_reason
    event = {
        "type": "assistant",
        "timestamp": ts,
        "sessionId": session_id,
        "requestId": req_id,
        "message": message,
    }
    if req_id is None:
        del event["requestId"]
    return event


def _write(path, events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return path


def _parse(tmp_path, events):
    return parse_session_detailed(_write(tmp_path / "session.jsonl", events))


def _quality_by_kind(facts):
    return {f.token_kind: f.source_quality for f in facts}


def _tokens_by_kind(facts):
    return {f.token_kind: f.tokens for f in facts}


INPUT_KINDS = ("input_nocache", "cache_read", "cache_write_5m")


def test_output_lower_bound_is_last_source_quality_value():
    assert SOURCE_QUALITY_VALUES[-1] == "output_lower_bound"
    assert SOURCE_QUALITY_VALUES[:3] == ("ok", "first_event_delta", "identity_missing")


def test_group_without_final_row_flags_only_output(tmp_path):
    """AC1: no row in the group has a stop_reason -> adopted = last row,
    its output fact is output_lower_bound, input-side facts stay ok."""
    result = _parse(
        tmp_path,
        [
            _event("2026-10-01T00:00:00Z", 1, stop_reason=None),
            _event("2026-10-01T00:00:01Z", 1),
        ],
    )
    quality = _quality_by_kind(result.facts)
    assert quality["output"] == "output_lower_bound"
    for kind in INPUT_KINDS:
        assert quality[kind] == "ok"
    assert _tokens_by_kind(result.facts) == {"input_nocache": 10, "cache_read": 20, "cache_write_5m": 30, "output": 1}
    assert result.duplicate_rows_skipped == 1
    assert result.conflicting_duplicate_groups == 0


def test_single_row_group_without_final_flags_output(tmp_path):
    result = _parse(tmp_path, [_event("2026-10-01T00:00:00Z", 4)])
    quality = _quality_by_kind(result.facts)
    assert quality["output"] == "output_lower_bound"
    for kind in INPUT_KINDS:
        assert quality[kind] == "ok"


def test_group_ending_in_final_row_is_all_ok(tmp_path):
    """AC2: the last row carries a stop_reason -> every fact is ok."""
    result = _parse(
        tmp_path,
        [
            _event("2026-10-01T00:00:00Z", 1),
            _event("2026-10-01T00:00:01Z", 2),
            _event("2026-10-01T00:00:02Z", 57, stop_reason="tool_use"),
        ],
    )
    assert {f.source_quality for f in result.facts} == {"ok"}
    assert _tokens_by_kind(result.facts)["output"] == 57


def test_final_row_followed_by_differing_row_flags_output(tmp_path):
    """AC2b: a final row followed by a different non-final row switches
    adoption to the last row (no stop_reason) -> output_lower_bound."""
    result = _parse(
        tmp_path,
        [
            _event("2026-10-01T00:00:00Z", 5, stop_reason="end_turn"),
            _event("2026-10-01T00:00:01Z", 8),
        ],
    )
    quality = _quality_by_kind(result.facts)
    assert quality["output"] == "output_lower_bound"
    for kind in INPUT_KINDS:
        assert quality[kind] == "ok"
    assert _tokens_by_kind(result.facts)["output"] == 8


def test_final_row_followed_by_identical_row_stays_ok(tmp_path):
    """AC2c: a final row followed by an identical (non-final) row keeps the
    final row adopted -> ok, even though the group's last row isn't final."""
    result = _parse(
        tmp_path,
        [
            _event("2026-10-01T00:00:00Z", 5, stop_reason="end_turn"),
            _event("2026-10-01T00:00:01Z", 5),
        ],
    )
    assert {f.source_quality for f in result.facts} == {"ok"}
    assert _tokens_by_kind(result.facts)["output"] == 5


@pytest.mark.parametrize("stop_reason", ["", None, 0, 1, 1.5, False, ["end_turn"], _ABSENT])
def test_invalid_stop_reason_only_group_flags_output(tmp_path, stop_reason):
    """AC2c: stop_reason "" / None / numbers / non-str only -> not final."""
    result = _parse(
        tmp_path,
        [
            _event("2026-10-01T00:00:00Z", 3, stop_reason=stop_reason),
            _event("2026-10-01T00:00:01Z", 6, stop_reason=stop_reason),
        ],
    )
    quality = _quality_by_kind(result.facts)
    assert quality["output"] == "output_lower_bound"
    for kind in INPUT_KINDS:
        assert quality[kind] == "ok"
    assert _tokens_by_kind(result.facts)["output"] == 6


def test_final_row_with_zero_output_is_ok(tmp_path):
    """AC2c: a final row with output 0 emits no output fact; the rest is ok."""
    result = _parse(
        tmp_path,
        [
            _event("2026-10-01T00:00:00Z", 0),
            _event("2026-10-01T00:00:01Z", 0, stop_reason="end_turn"),
        ],
    )
    assert "output" not in _tokens_by_kind(result.facts)
    assert {f.source_quality for f in result.facts} == {"ok"}


@pytest.mark.parametrize("missing", ["msg_id", "req_id"])
def test_identity_missing_row_stays_identity_missing(tmp_path, missing):
    """AC2d: a standalone row without a full id pair is not a group, so it
    keeps identity_missing on every fact (including output)."""
    kwargs = {missing: None}
    result = _parse(tmp_path, [_event("2026-10-01T00:00:00Z", 9, **kwargs)])
    assert result.missing_dedup_identity_rows == 1
    assert "output" in _tokens_by_kind(result.facts)
    assert {f.source_quality for f in result.facts} == {"identity_missing"}


def test_flag_is_per_group(tmp_path):
    """One group without a final row, one with: only the former's output is flagged."""
    result = _parse(
        tmp_path,
        [
            _event("2026-10-01T00:00:00Z", 2, msg_id="msg-a", req_id="req-a"),
            _event("2026-10-01T00:00:01Z", 40, msg_id="msg-b", req_id="req-b", stop_reason="end_turn"),
        ],
    )
    output_facts = [f for f in result.facts if f.token_kind == "output"]
    assert [(f.tokens, f.source_quality) for f in output_facts] == [(2, "output_lower_bound"), (40, "ok")]
    assert all(f.source_quality == "ok" for f in result.facts if f.token_kind != "output")


# ---------------------------------------------------------------------------
# CLI (AC3): measure's data_quality.source_quality always carries the key.
# ---------------------------------------------------------------------------


def _setup_claude_session(tmp_path, monkeypatch, events):
    claude_home = tmp_path / "claude_home"
    codex_home = tmp_path / "codex_home"
    project_dir = claude_home / "projects" / "olb"
    project_dir.mkdir(parents=True)
    codex_home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("AGENT_COST_CONFIG", raising=False)
    _write(project_dir / "session.jsonl", events)


def _measure_source_quality(capsys):
    rc = cli.main(["measure", "--session-id", "s-olb", "--since", "2026-09-01"])
    assert rc == 0
    return json.loads(capsys.readouterr().out)["data_quality"]["source_quality"]


def test_measure_counts_output_lower_bound(tmp_path, monkeypatch, capsys):
    _setup_claude_session(
        tmp_path,
        monkeypatch,
        [
            _event("2026-10-01T00:00:00Z", 1, msg_id="msg-a", req_id="req-a"),
            _event("2026-10-01T00:00:01Z", 30, msg_id="msg-b", req_id="req-b", stop_reason="end_turn"),
        ],
    )
    sq = _measure_source_quality(capsys)
    assert set(sq.keys()) == set(SOURCE_QUALITY_VALUES)
    assert sq["output_lower_bound"] == 1
    assert sq["ok"] == 7
    assert sq["identity_missing"] == 0


def test_measure_reports_zero_output_lower_bound_when_absent(tmp_path, monkeypatch, capsys):
    _setup_claude_session(
        tmp_path,
        monkeypatch,
        [_event("2026-10-01T00:00:00Z", 30, stop_reason="end_turn")],
    )
    sq = _measure_source_quality(capsys)
    assert sq == {"ok": 4, "first_event_delta": 0, "identity_missing": 0, "output_lower_bound": 0}
