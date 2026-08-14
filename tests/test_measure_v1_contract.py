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
implements the schema's required-key/enum checks *and* two semantic MUSTs the
schema alone cannot express -- a matched:false session entry must have empty
rows/all-zero totals, and total.rows must equal the (agent, model,
token_kind)-dimensional re-aggregation of every session's own rows, not just a
scalar totals-sum match -- by hand (no jsonschema dependency -- agent-cost is
dependency-free by design). token_kind is deliberately NOT enforced as a
closed enum here: an unrecognized value is a warning on stderr, never an
issue, since the vendored schema itself treats token_kind as an open string
(sol review must3 -- a closed enum here would make this repo's own test the
breaking change agent-cost's additive-fields policy is supposed to prevent).
Checks two directions:

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
import sys
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
# Informational only (sol review must3) -- token_kind is an OPEN string in the vendored
# schema, not a closed enum: an unrecognized value is a warning, never an issue pushed into
# check_measure_payload's return value, so it can never fail a fixture.
KNOWN_TOKEN_KINDS = {"input_nocache", "cache_read", "cache_write_5m", "cache_write_1h", "cache_write_unknown", "output"}
PRICING_STATUS_ENUM = {"unpriced", "lower_bound", "priced"}
AGENT_ENUM = {"claude", "codex"}
PRICING_STATUS_RANK = {"unpriced": 0, "lower_bound": 1, "priced": 2}


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
    if row["token_kind"] not in KNOWN_TOKEN_KINDS:
        # Warning only, not an issue -- see KNOWN_TOKEN_KINDS's own comment.
        print(
            f"[warn] {label}: row.token_kind {row['token_kind']!r} is not in the currently-known "
            f"set {sorted(KNOWN_TOKEN_KINDS)} -- informational only, not a rejection.",
            file=sys.stderr,
        )
    if row["pricing_status"] not in PRICING_STATUS_ENUM:
        issues.append(f"{label}: row.pricing_status {row['pricing_status']!r} not in {PRICING_STATUS_ENUM}")
    if row["tokens"] != row["priced_tokens"] + row["unpriced_tokens"]:
        issues.append(f"{label}: row.tokens != priced_tokens + unpriced_tokens")


def _check_totals(label, totals, issues):
    missing = TOTALS_COLUMNS - totals.keys()
    if missing:
        issues.append(f"{label}: totals missing key(s) {sorted(missing)}")


def _check_matched_false_is_empty(label, entry, issues):
    """sol review must2: a matched:false session entry MUST have an empty rows array and
    all-zero totals -- "no usage matched" and "usage matched but netted to zero" are
    different facts."""
    if entry.get("matched") is not False:
        return
    rows = entry.get("rows")
    if isinstance(rows, list) and len(rows) > 0:
        issues.append(f"{label}: matched is false but rows has {len(rows)} entrie(s)")
    totals = entry.get("totals")
    if isinstance(totals, dict):
        for key in TOTALS_COLUMNS:
            if totals.get(key, 0) != 0:
                issues.append(f"{label}: matched is false but totals.{key} = {totals.get(key)!r}")


def _aggregate_rows_by_dimension(rows):
    """Groups rows by (agent, model, token_kind), summing numeric fields and taking the
    worst pricing_status per bucket -- mirrors agent_cost/aggregate.py's own build_rows
    bucketing/ranking (sol review must2)."""
    buckets = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = (row.get("agent"), row.get("model"), row.get("token_kind"))
        bucket = buckets.setdefault(
            key,
            {
                "tokens": 0,
                "priced_tokens": 0,
                "unpriced_tokens": 0,
                "estimated_cost_usd": 0.0,
                "credits": 0.0,
                "pricing_status": "priced",
            },
        )
        for field in ("tokens", "priced_tokens", "unpriced_tokens", "estimated_cost_usd", "credits"):
            value = row.get(field)
            if isinstance(value, (int, float)):
                bucket[field] += value
        status = row.get("pricing_status")
        if status in PRICING_STATUS_RANK and PRICING_STATUS_RANK[status] < PRICING_STATUS_RANK[bucket["pricing_status"]]:
            bucket["pricing_status"] = status
    return buckets


def _check_total_rows_match_dimensional_aggregate(sessions, total_rows, issues):
    """sol review must2: total.rows MUST equal the (agent, model, token_kind)-dimensional
    re-aggregation of the union of every sessions[*].rows -- a scalar totals-sum match
    (checked separately, e.g. by test_measure_json_schema_is_locked's cousins) is not
    enough: two payloads can share identical totals while total.rows attributes the same
    tokens to the wrong bucket."""
    all_session_rows = []
    for entry in sessions.values():
        if isinstance(entry, dict) and isinstance(entry.get("rows"), list):
            all_session_rows.extend(entry["rows"])
    expected = _aggregate_rows_by_dimension(all_session_rows)
    actual = _aggregate_rows_by_dimension(total_rows if isinstance(total_rows, list) else [])

    for key in set(expected) | set(actual):
        agent, model, token_kind = key
        label = f"total.rows (agent={agent}, model={model}, token_kind={token_kind})"
        exp = expected.get(key)
        act = actual.get(key)
        if exp is None:
            issues.append(f"{label}: present in total.rows but not in the union of sessions[*].rows")
            continue
        if act is None:
            issues.append(f"{label}: missing from total.rows but present in the union of sessions[*].rows")
            continue
        for field in ("tokens", "priced_tokens", "unpriced_tokens"):
            if exp[field] != act[field]:
                issues.append(f"{label}.{field} = {act[field]}, expected {exp[field]} (recomputed from sessions)")
        for field in ("estimated_cost_usd", "credits"):
            if abs(exp[field] - act[field]) > 1e-9:
                issues.append(f"{label}.{field} = {act[field]}, expected {exp[field]} (recomputed from sessions)")
        if exp["pricing_status"] != act["pricing_status"]:
            issues.append(
                f"{label}.pricing_status = {act['pricing_status']!r}, expected {exp['pricing_status']!r}"
            )


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
            _check_matched_false_is_empty(label, entry, issues)

    total = payload.get("total")
    if isinstance(total, dict):
        if {"rows", "totals"} - total.keys():
            issues.append("$.total: missing rows/totals")
        else:
            for row in total["rows"]:
                _check_row("total", row, issues)
            _check_totals("total", total["totals"], issues)
            if isinstance(sessions, dict):
                _check_total_rows_match_dimensional_aggregate(sessions, total["rows"], issues)
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
    assert len(reject_ids) == 6, "expected exactly 6 reject fixtures per the #51 sol-review round"
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
        if entry.get("reason_code") == "matched_false_must_have_zero_totals":
            assert any("matched is false but totals." in i for i in issues), issues
        if entry.get("reason_code") == "total_rows_dimension_mismatch":
            assert any(i.startswith("total.rows (agent=") for i in issues), issues
        if entry.get("reason_code") == "$.sessions.session-a":
            assert any(i.startswith("sessions.session-a: missing key") for i in issues), issues
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
