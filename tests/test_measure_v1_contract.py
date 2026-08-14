"""Conformance test against the vendored measure/v1 cross-language contract.

This is deliberately a *different* check from test_cli.py's
test_measure_json_schema_is_locked. That test pins measure's JSON shape entirely
internally (a hand-written expectation checked against this repo's own
implementation) -- it would happily stay green even if agent-cost's real output
drifted away from what ai-agent-skills-playbook's measure/v1 contract actually
pins, since nothing here reads that contract.

This test does read it: tests/fixtures/measure-v1-contract/ is a byte-for-byte
vendored copy of that contract's schema + fixtures (see the UPSTREAM file in
that directory for the pinned commit and re-vendoring instructions). It re-
implements the schema's required-key/enum checks by hand (no jsonschema
dependency -- agent-cost is dependency-free by design) and checks two
directions:

1. Every vendored accept fixture (a real, previously-captured `measure`
   payload) still passes those checks, and every vendored reject fixture still
   fails them -- proves this repo's hand-written checker agrees with the
   contract's own verify-fixtures.mjs, which produced the fixtures'
   accept/reject labels in the first place.
2. A *live* payload from cli.main(["measure", ...]) -- generated fresh, right
   here, by the actual current implementation -- has the exact same row/totals/
   session-entry key sets as the vendored accept fixtures. If aggregate.py's
   Row/rows_totals or cli.py's cmd_measure ever renames or drops a key, this
   fails even though test_measure_json_schema_is_locked's own hardcoded
   expectation could, in principle, be edited to match the drift unnoticed --
   this test's expectation comes from the external contract instead.
"""

import json
from pathlib import Path

from agent_cost import cli

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "measure-v1-contract" / "fixtures"

# Mirrors contracts/shared/personal-dimensions.mjs's FORBIDDEN_PERSONAL_DIMENSION_KEYS
# (ai-agent-skills-playbook) -- kept as an independent copy the same way spec-lane's
# packages/core/src/agent-metrics-goodhart.ts keeps its own, since this repo has no
# mechanism to import a .mjs file across the repo boundary. If you are extending the
# denylist, extend both.
FORBIDDEN_PERSONAL_DIMENSION_KEYS = {
    "author",
    "reviewer",
    "assignee",
    "owner",
    "user_id",
    "username",
    "email",
    "display_name",
    "handle",
    "chat_id",
    "real_name",
}

ROW_COLUMNS = {
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
TOTALS_COLUMNS = {"tokens", "priced_tokens", "unpriced_tokens", "estimated_cost_usd", "credits"}
SESSION_ENTRY_COLUMNS = {"matched", "rows", "totals"}
TOP_LEVEL_REQUIRED = {
    "protocol_version",
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
TOKEN_KIND_ENUM = {"input_nocache", "cache_read", "cache_write_5m", "cache_write_1h", "cache_write_unknown", "output"}
PRICING_STATUS_ENUM = {"unpriced", "lower_bound", "priced"}
AGENT_ENUM = {"claude", "codex"}


def _scan_personal_dimensions(value, path=""):
    violations = []
    if isinstance(value, list):
        for i, item in enumerate(value):
            violations.extend(_scan_personal_dimensions(item, f"{path}[{i}]"))
        return violations
    if isinstance(value, dict):
        for key, sub in value.items():
            here = f"{path}.{key}" if path else key
            if key in FORBIDDEN_PERSONAL_DIMENSION_KEYS:
                violations.append(here)
            violations.extend(_scan_personal_dimensions(sub, here))
    return violations


def _check_row(label, row, issues):
    missing = ROW_COLUMNS - row.keys()
    if missing:
        issues.append(f"{label}: row missing key(s) {sorted(missing)}")
        return
    if row["month"] is not None:
        issues.append(f"{label}: row.month must be null (measure never groups by month), got {row['month']!r}")
    if row["agent"] not in AGENT_ENUM:
        issues.append(f"{label}: row.agent {row['agent']!r} not in {AGENT_ENUM}")
    if row["token_kind"] not in TOKEN_KIND_ENUM:
        issues.append(f"{label}: row.token_kind {row['token_kind']!r} not in {TOKEN_KIND_ENUM}")
    if row["pricing_status"] not in PRICING_STATUS_ENUM:
        issues.append(f"{label}: row.pricing_status {row['pricing_status']!r} not in {PRICING_STATUS_ENUM}")
    if row["tokens"] != row["priced_tokens"] + row["unpriced_tokens"]:
        issues.append(f"{label}: row.tokens != priced_tokens + unpriced_tokens")


def _check_totals(label, totals, issues):
    missing = TOTALS_COLUMNS - totals.keys()
    if missing:
        issues.append(f"{label}: totals missing key(s) {sorted(missing)}")


def check_measure_payload(payload):
    """Hand-written structural check mirroring measure-output.schema.json's `required`
    keys and enums (not a full JSON Schema implementation -- this repo stays
    dependency-free by design, per pyproject.toml's `dependencies = []`).
    Deliberately does NOT check for *extra*/unknown keys anywhere (no
    additionalProperties:false equivalent) -- see the vendored schema's own
    description ("Open vs. closed schema") for why measure/v1 allows additive
    fields within v1 and a strict closed check would false-positive against
    real, current output.
    """
    issues = []
    if not isinstance(payload, dict):
        return ["payload is not an object"]

    missing_top = TOP_LEVEL_REQUIRED - payload.keys()
    if missing_top:
        issues.append(f"$: missing top-level key(s) {sorted(missing_top)}")

    if payload.get("protocol_version") != "measure/v1":
        issues.append(f"$.protocol_version: expected \"measure/v1\", got {payload.get('protocol_version')!r}")

    window = payload.get("window")
    if isinstance(window, dict):
        if {"since", "until"} - window.keys():
            issues.append("$.window: missing since/until")
    elif "window" in payload:
        issues.append("$.window: not an object")

    rates = payload.get("rates")
    if isinstance(rates, dict):
        if {"catalog_version", "sha256"} - rates.keys():
            issues.append("$.rates: missing catalog_version/sha256")
    elif "rates" in payload:
        issues.append("$.rates: not an object")

    dq = payload.get("data_quality")
    if isinstance(dq, dict):
        dq_required = {"malformed_events", "skipped_files", "negative_deltas", "unpriced_tokens", "source_quality"}
        missing_dq = dq_required - dq.keys()
        if missing_dq:
            issues.append(f"$.data_quality: missing key(s) {sorted(missing_dq)}")
        sq = dq.get("source_quality")
        if isinstance(sq, dict):
            if {"ok", "first_event_delta"} - sq.keys():
                issues.append("$.data_quality.source_quality: missing ok/first_event_delta")
        elif "source_quality" in dq:
            issues.append("$.data_quality.source_quality: not an object")
    elif "data_quality" in payload:
        issues.append("$.data_quality: not an object")

    sessions = payload.get("sessions")
    session_ids = payload.get("session_ids")
    if isinstance(sessions, dict) and isinstance(session_ids, list):
        if set(sessions.keys()) != set(session_ids):
            issues.append("session_ids_sessions_key_mismatch: session_ids and sessions.keys() differ")
        for sid, entry in sessions.items():
            label = f"sessions.{sid}"
            if not isinstance(entry, dict):
                issues.append(f"{label}: not an object")
                continue
            missing = SESSION_ENTRY_COLUMNS - entry.keys()
            if missing:
                issues.append(f"{label}: missing key(s) {sorted(missing)}")
                continue
            for row in entry["rows"]:
                _check_row(label, row, issues)
            _check_totals(label, entry["totals"], issues)

    total = payload.get("total")
    if isinstance(total, dict):
        if {"rows", "totals"} - total.keys():
            issues.append("$.total: missing rows/totals")
        else:
            for row in total["rows"]:
                _check_row("total", row, issues)
            _check_totals("total", total["totals"], issues)
    elif "total" in payload:
        issues.append("$.total: not an object")

    for v in _scan_personal_dimensions(payload):
        issues.append(f"personal_dimension_forbidden_key: {v}")

    return issues


def _load_fixture(filename):
    return json.loads((FIXTURES_DIR / filename).read_text())


def _manifest():
    return _load_fixture("expected-results.json")


def test_vendored_accept_fixtures_pass_the_structural_check():
    manifest = _manifest()
    accept_ids = [f["id"] for f in manifest["fixtures"] if f["expected"] == "accept"]
    assert len(accept_ids) == 3, "expected exactly 3 accept fixtures per the #51 conformance plan"
    for entry in manifest["fixtures"]:
        if entry["expected"] != "accept":
            continue
        payload = _load_fixture(entry["files"]["record"])
        issues = check_measure_payload(payload)
        assert issues == [], f"{entry['id']}: {issues}"


def test_vendored_reject_fixtures_fail_the_structural_check():
    manifest = _manifest()
    reject_ids = [f["id"] for f in manifest["fixtures"] if f["expected"] == "reject"]
    assert len(reject_ids) == 3, "expected exactly 3 reject fixtures per the #51 conformance plan"
    for entry in manifest["fixtures"]:
        if entry["expected"] != "reject":
            continue
        payload = _load_fixture(entry["files"]["record"])
        issues = check_measure_payload(payload)
        assert issues != [], f"{entry['id']}: expected at least one issue, got none"
        if entry.get("reason_code") == "$.protocol_version":
            assert any(i.startswith("$.protocol_version") for i in issues)
        if entry.get("reason_code") == "$.total.totals":
            assert any(i.startswith("total: totals missing key") for i in issues)
        if entry.get("all_forbidden_keys_flagged"):
            for key in FORBIDDEN_PERSONAL_DIMENSION_KEYS:
                assert f"personal_dimension_forbidden_key: {key}" in issues, key


def test_live_measure_output_matches_vendored_contract_key_shape(tmp_path, monkeypatch, capsys):
    """The real, current implementation's row/totals/session-entry keys must equal
    exactly the vendored accept fixture's -- not a hardcoded literal repeated a
    third time (test_cli.py's schema-lock test already does that internally),
    but the external contract this repo doesn't control the other side of.
    """
    claude_home = tmp_path / "claude_home"
    codex_home = tmp_path / "codex_home"
    claude_home.mkdir()
    codex_home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("AGENT_COST_CONFIG", raising=False)

    project_dir = claude_home / "projects" / "-Users-a-work-proj"
    project_dir.mkdir(parents=True)
    event = {
        "type": "assistant",
        "timestamp": "2026-06-01T00:00:00Z",
        "sessionId": "session-live",
        "message": {"model": "claude-opus-4-8", "usage": {"input_tokens": 1000, "output_tokens": 500}},
    }
    (project_dir / "s1.jsonl").write_text(json.dumps(event) + "\n")

    rc = cli.main(["measure", "--session-id", "session-live", "--format", "json"])
    assert rc == 0
    live_payload = json.loads(capsys.readouterr().out)

    vendored = _load_fixture("accept-matched-normal.json")

    assert set(live_payload.keys()) == set(vendored.keys())
    assert set(live_payload["rates"].keys()) == set(vendored["rates"].keys())
    assert set(live_payload["window"].keys()) == set(vendored["window"].keys())
    assert set(live_payload["data_quality"].keys()) == set(vendored["data_quality"].keys())
    assert set(live_payload["data_quality"]["source_quality"].keys()) == set(
        vendored["data_quality"]["source_quality"].keys()
    )

    live_session = live_payload["sessions"]["session-live"]
    vendored_session = vendored["sessions"]["session-a"]
    assert set(live_session.keys()) == set(vendored_session.keys())
    assert set(live_session["totals"].keys()) == set(vendored_session["totals"].keys())
    assert live_session["rows"], "fixture must exercise at least one row"
    for row in live_session["rows"]:
        assert set(row.keys()) == set(vendored_session["rows"][0].keys())

    assert check_measure_payload(live_payload) == []
