# agent-cost

See Claude Code and Codex CLI token usage and estimated costs from the logs already on your
machine.

Keep pricing gaps visible: unknown models are marked `unpriced`, and cache-write estimates
with missing TTL information are marked `lower_bound`. JSON reports include the rate catalog
version and SHA-256 used for the calculation; the catalog records its pricing sources.

**These are estimates based on the selected rate catalog, not your actual bill.** The CLI makes
no runtime network calls and has zero runtime dependencies. On macOS and Linux with IANA
timezone data, no account, service, or project configuration is required. See the
[platform note](#report-and-export) for minimal Windows Python environments.

Need the result in another program? [`agent-cost measure`](#machine-consumption-agent-cost-measure)
returns versioned `measure/v1` JSON for session IDs you supply. Task and PR attribution remain
the caller's responsibility.

[日本語サマリ](#日本語サマリ) · [Synthetic output example](#synthetic-output-example)

## Try it on your machine

With [`uvx`](https://docs.astral.sh/uv/guides/tools/), no persistent install is needed:

```bash
uvx --from coding-agent-cost agent-cost doctor
uvx --from coding-agent-cost agent-cost report
```

The package runner may need network access to download the tool. The CLI itself does not upload
your logs.

The first command checks the expected local paths, the Codex database, and the bundled rate
catalog. It does not open every Claude JSONL file. The second command performs the real scan,
prints the result, and exposes unreadable inputs in `data_quality.skipped_files`. This path was
exercised on macOS from a clean temporary directory against the published `0.1.0` package on
2026-08-23.

Prefer a persistent command? Install the PyPI distribution, then run the same two commands:

```bash
pip install coding-agent-cost
agent-cost doctor
agent-cost report
```

The PyPI distribution is named [`coding-agent-cost`](https://pypi.org/project/coding-agent-cost/),
while the command remains `agent-cost` and the import remains `agent_cost`. The shorter PyPI
name is unavailable because of PyPI's similarity rule; this project is not affiliated with the
unrelated `agentcost` distribution. Releases are published from a `v*` tag by GitHub Actions
through PyPI's Trusted Publisher; see [docs/release.md](docs/release.md).

## Synthetic output example

This is **synthetic Claude data**, not observed usage, a bill, or evidence of savings. The
CLI output below was reproduced with PyPI package `coding-agent-cost==0.1.0` and source
commit `d170ea301ed0c46351749214bd299e75ae8a7786` (only trailing space padding is omitted).
The [two-event fixture](examples/synthetic-claude.jsonl) contains known-model input, cache
writes without a TTL breakdown, and unknown-model input.

```text
Month    Agent   Model                 Token Kind           Tokens   Priced   Unpriced  Est. Cost (USD)  Credits  Status
-------  ------  --------------------  -------------------  -------  -------  --------  ---------------  -------  -----------
2026-06  claude  claude-opus-4-8       cache_write_unknown  1000000  1000000  0         6.2500           -        lower_bound
2026-06  claude  claude-opus-4-8       input_nocache        1000000  1000000  0         5.0000           -        priced
2026-06  claude  model-not-in-catalog  input_nocache        500      0        500       0.0000           -        unpriced

Total tokens: 2,000,500   Total estimated cost: $11.2500
Rates catalog: 2026-07-29  (sha256=5d86e11b3c95...)
Data quality: malformed_events=0  skipped_files=0  negative_deltas=0  unpriced_tokens=500
```

- `priced`: $5.0000 is a catalog-based estimate for 1,000,000 input tokens, not a confirmed charge.
- `lower_bound`: $6.2500 uses the 5-minute cache-write rate because the TTL is missing. This is
  a lower bound under the catalog and observed token data, not a lower bound on your actual bill.
- `unpriced`: `0.0000` does not mean free. Read `pricing_status` (the `Status` column) together
  with `unpriced_tokens` (`Unpriced`): 500 tokens have no price and are excluded from the dollar
  total. The $11.2500 total also includes the cache-write lower-bound estimate.

<details>
<summary>Reproduce with synthetic data only (Python 3.9+ and uvx)</summary>

Run from this repository's root. Both log locations and the config file are explicitly set
inside a temporary directory. Inherited log-location overrides are removed for the child
process, so it cannot fall back to your real Claude or Codex logs. The temporary data is
removed when the command exits. The package runner may download the pinned package.

```bash
python3 - <<'PY'
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

fixture = Path("examples/synthetic-claude.jsonl").resolve()
with tempfile.TemporaryDirectory(prefix="agent-cost-example-") as directory:
    root = Path(directory)
    project = root / "claude" / "projects" / "synthetic"
    project.mkdir(parents=True)
    (root / "codex").mkdir()
    shutil.copyfile(fixture, project / "session.jsonl")
    config = root / "config.json"
    config.write_text(json.dumps({
        "claude_home": str(root / "claude"),
        "codex_home": str(root / "codex"),
    }))
    env = dict(os.environ)
    for key in ("CLAUDE_HOME", "CODEX_HOME", "PYTHONPATH"):
        env.pop(key, None)
    env["AGENT_COST_CONFIG"] = str(config)
    subprocess.run([
        "uvx", "--from", "coding-agent-cost==0.1.0", "agent-cost",
        "report", "--agent", "claude", "--since", "2026-06-01",
        "--until", "2026-06-02", "--format", "table",
    ], cwd=root, env=env, check=True)
PY
```

Use `--format json` in the same command to see the full catalog SHA-256 and field names.

</details>

## Why this exists

Many usage trackers are designed as rich, convenient dashboards. That can be the right choice
when broad agent coverage and interactive exploration matter most. `agent-cost` is for a
different trust model:

- **Local-first and zero-network:** source logs stay on disk; the package has zero runtime
  dependencies.
- **Auditable:** token facts, the versioned rate catalog, its digest, and data-quality counters
  are available for inspection and machine consumption.
- **Fail-closed pricing:** unknown models remain `unpriced`; ambiguous cache-write TTLs are
  labeled `lower_bound` instead of silently receiving a best-guess price.
- **Explicit attribution boundary:** `measure` accepts named session ids, but agent-cost does
  not infer a task from a branch or PR. A workflow such as
  [`spec-lane`](https://github.com/shiki-yusuke/spec-lane) can own the session-to-task binding
  and consume the versioned JSON result.

This is not a claim that local CLI accounting is universally better than a dashboard. It is a
smaller primitive for environments where data egress, dependency surface, custom metrics, or
task attribution need to remain under the operator's control.

See [Choosing by use case and trust model](https://github.com/shiki-yusuke/agent-cost/blob/main/docs/comparison.md)
for a dated, source-linked comparison with broader CLI trackers, local dashboards, and
observability stacks.

## Report and export

`agent-cost report` turns each local usage event into a canonical fact (one model, one token
kind, one timestamp, one count), prices each fact against the bundled rate catalog, and prints
an aggregated table:

```bash
agent-cost report --since 2026-06-01 --until 2026-07-01 --format table
agent-cost report --group-by month,agent --format csv
agent-cost report --format json > usage.json
agent-cost export --agent claude --out facts.jsonl   # raw canonical facts
```

Useful flags: `--since`/`--until` (half-open window; date-only values are
interpreted in `--timezone`, default UTC), `--agent claude,codex`,
`--group-by month,agent,model,token-kind`, `--rates PATH` (use a different
catalog entirely, see below), `--exclude-archived` (Codex threads).

**Platform note:** `report`, `export`, and `measure` use Python's IANA timezone database even
when the timezone is `UTC`. macOS and typical Linux installations provide it through the
operating system. A minimal Windows Python environment may require `pip install tzdata` first;
`tzdata` is not currently a declared package dependency.

If another program wants to parse agent-cost's output for one or more
specific session ids, see `agent-cost measure` below rather than
scraping `report`.

## Internal billing plan (optional, confidential)

Some organizations bill Claude seats internally with their own rules -- a
fixed subscription per billing window, a usage allowance measured in
list-price dollars, and a discounted rate above it. `report --billing-plan
PATH` applies such a plan to the Claude usage agent-cost already prices at
the public catalog. Nothing changes without the flag, and there is no
default path or environment variable that turns it on.

```bash
agent-cost report --billing-plan ~/private/billing-plan.json \
  --since 2030-01-01T00:00:00+00:00 --until 2030-02-01T00:00:00+00:00 --format json
```

A plan (`plan_schema_version` `"1"`; every value below is made up):

```json
{
  "plan_schema_version": "1",
  "plan_id": "bp-example",
  "nonce": "0123456789abcdef0123456789abcdef",
  "applies_to": {"agent": "claude", "scope": "seat"},
  "basis": "agent-cost-list-price",
  "periods": [
    {"period_id": "p1", "effective_from": "2030-01-01T00:00:00+00:00",
     "window_subscription_usd": "3", "allowance_usd": null, "overage": null},
    {"period_id": "p2", "effective_from": "2030-01-11T00:00:00+00:00",
     "window_subscription_usd": "10", "allowance_usd": "50",
     "overage": {"type": "charge_multiplier", "value": "0.5"}},
    {"period_id": "end", "effective_from": "2030-02-01T00:00:00+00:00", "terminates": true}
  ]
}
```

- `periods` is a list of finite billing windows: window *i* is
  `[effective_from_i, effective_from_{i+1})`, and the last entry must be
  `{period_id, effective_from, terminates: true}` so that no window is
  open-ended. Write one window per billing month (or per contract change).
  `effective_from` needs a UTC offset and no sub-second part.
- `window_subscription_usd` is the fixed amount billed for that window, as
  you write it -- agent-cost never prorates it.
- `allowance_usd` is the window's usage allowance in list-price dollars.
  `null` means the window has no overage concept at all (it is not a zero
  allowance), and then `overage` must be `null` too.
- `overage` prices `max(0, list_cost - allowance)`:
  `{"type": "charge_multiplier", "value": "0.5"}` charges overage × value
  (`0 <= value <= 1`), and `{"type": "block_discount", "block_usd": "5",
  "discount_usd": "1"}` charges overage minus `floor(overage / block_usd) ×
  discount_usd` (`0 < discount_usd <= block_usd`).
- Amounts are decimal strings only (no JSON numbers), at most 28
  significant digits. `plan_id` is `[A-Za-z0-9_-]{1,32}`, `period_id`
  `[A-Za-z0-9_-]{1,16}`, `nonce` 32 lowercase hex characters (e.g.
  `python3 -c 'import secrets; print(secrets.token_hex(16))'`). Unknown or
  duplicate keys are errors. v1 only accepts `scope: "seat"`: an allowance
  shared by a department or org cannot be computed from one person's logs.

The JSON output gains an `internal_billing` block (existing keys are
unchanged), and the table gains a section headed `Internal billing
(CONFIDENTIAL -- do not share)`. For each window that intersects
`[since, until)` it shows `list_cost_usd` (Claude facts in the window,
priced at the packaged catalog), `allowance_usd`, `overage_usd`,
`overage_cost_usd`, `window_subscription_usd`, `internal_cost_usd` --
all as 4-decimal strings -- and four separate status fields:

- `query_coverage`: `full` when `--since`/`--until` cover the whole
  window, otherwise `partial`. It says nothing about whether the logs
  themselves are complete (deleted transcripts, other machines, API or web
  usage are never seen; see `data_quality` at the top level as well).
- `window_state`: `closed` once the window has ended (`until <=
  generated_at`), otherwise `open`.
- `list_cost_pricing`: `priced`, `lower_bound` (some facts were unpriced
  or only priced as a lower bound, or an output fact's tokens are only a
  lower bound -- `source_quality: "output_lower_bound"`) or
  `no_usage_observed`.
- `internal_cost_certainty`: `estimate` only when the window is fully
  queried, closed and `priced`. Otherwise `lower_bound` for windows with no
  overage or `charge_multiplier` (more usage can only raise the amount), and
  `indeterminate` for `block_discount` (more usage can cross a block
  boundary and lower it). This describes what can be derived from the
  observed list cost -- it is not a statement that the number matches an
  actual invoice.

`window_subscription_usd` in each row is the plan's input echoed back, not
an amount allocated to your query. `internal_cost_usd` is the cost of the
whole billing window computed from the usage visible in this report -- it
is not the cost of the `[since, until)` range, so do not sum windows to get
one. `plan_coverage: partial` with `uncovered` ranges flags the parts of
`[since, until)` that no window covers (before the first window or after
`terminates`); usage there is not billed.

`measure` has no billing plan option on purpose: a per-session internal
cost would mean allocating a window-level charge across sessions.
`--billing-plan` cannot be combined with `--rates` (the basis must be the
packaged public catalog), `--format csv`, an `--agent` list without
`claude`, or a missing `--since`/`--until`; any of those, an unreadable or
invalid plan, or a calculation that would need rounding exits 2 with
nothing on stdout. `AGENT_COST_NOW` (an ISO 8601 instant with a UTC offset)
pins `generated_at`, and with it `window_state`, for reproducible runs.

**Keep the plan and its output private.** The plan must be a regular file
owned by you with mode `0600` (or stricter), reached without any symlink in
its path (on macOS `/tmp` and `/var` are symlinks -- keep it under your
home directory), and not inside a git repository or worktree. Error messages
never print plan values or the path. Output produced with the plan is
confidential: agent-cost has no redaction, so mind your terminal scrollback,
shell history, and the permissions of any file you redirect it to, and keep
it out of shared logs, CI, artifacts and ledgers.

## What this measures, and what it doesn't

agent-cost only reads data that is already on disk. It never talks to the
network, never calls `gh`, and never resolves branches or PRs.

- **Claude Code**: every logical assistant message is one billing event,
  attributed to the exact model on that event (a session that switches
  models mid-conversation is not folded into one "primary model"). Claude
  Code's transcript writes one JSONL line per content block of the same
  message, and those lines are deduplicated first -- but only when a row
  carries a full `message.id` + `requestId` pair; a row missing either
  half is emitted on its own (never merged) and flagged
  `source_quality: "identity_missing"` rather than assumed
  billing-accurate. `identity_missing` facts are still priced and included
  in rows/totals; the flag is a warning, not an exclusion or an unpriced
  status. A message's lines don't necessarily repeat an identical `usage`
  block: `model` and the input-side fields (input tokens, cache read,
  cache-write TTL breakdown) stay the same across a message's lines, but
  `output_tokens` typically grows line by line as the response streams in,
  and intermediate lines usually lack `usage.speed` entirely (reported as
  mode `"unknown"`), with only the final line carrying a concrete mode.
  Within a deduplicated group, `model` or an input-side field that
  actually differs across the group's rows, two rows disagreeing on a
  *concrete* mode (`"normal"` vs `"fast"`), or an `output_tokens` value
  that decreases or is non-monotonic across them, is counted in
  `data_quality.conflicting_duplicate_groups` as a real billing
  disagreement -- `output_tokens` growing row by row, and mode
  `"unknown"` mixed with a single concrete mode elsewhere in the group,
  are both the ordinary streaming pattern just described and are not
  flagged, since the reader's own cross-check of real transcripts found
  exactly that pattern in every observed duplicated group. See
  `CHANGELOG.md`'s 0.2.0 entry.
  If the row a group adopts has no `stop_reason` (common in subagent
  transcripts, where a message ending in `tool_use` may never get its final
  line), its `output_tokens` is only the streaming head's value: the
  group's `output` fact is flagged `source_quality: "output_lower_bound"`
  -- the observed lower bound of that message's output tokens, not
  guaranteed to be >= the true count. The group's input-side facts stay
  `"ok"`, and the token amount itself is unchanged (still priced and
  included in rows/totals); the flag is a warning only.
  When Anthropic's prompt-cache TTL breakdown (5-minute vs 1-hour writes) is
  present in the log, it's used; otherwise the cache-write tokens are priced
  at the 5-minute rate as an explicit **lower bound** and flagged
  `lower_bound` rather than guessed at the (more expensive) 1-hour rate.
  Each Claude fact also carries its request's prompt length
  (`prompt_tokens` = `input_tokens` + `cache_read_input_tokens` +
  `cache_creation_input_tokens`), used only to pick the tier of a model
  whose price depends on prompt length (`claude-haiku-5-5`).
- **Prompt-length tiers need a known prompt length.** A fact whose prompt
  length is unknown -- every Codex fact (a delta of cumulative totals, not
  one request), a Claude fact from a conflicting dedup group, or a Claude
  row whose cache-write TTL breakdown exceeds its `cache_creation_input_tokens`
  -- is priced at the model's base (cheapest) tier and flagged
  `lower_bound`. Models without tiers are unaffected.
- **Codex CLI**: rollout files record a *cumulative* token count after each
  turn; agent-cost turns that into per-turn deltas. Under the Codex token-rate
  tariff used here, cache writes are not added as a separate charge. The current
  reader does not emit independent cache-write facts; newer logs can include
  `cache_write_input_tokens`, but this field is not used to add a cache-write
  charge. This differs from older logs where that signal was unavailable.
  Codex's `output` fact is `output_tokens` alone: cross-checking
  real rollout files confirms `total_tokens == input_tokens + output_tokens`
  in every sample, which means `reasoning_output_tokens` is a breakdown of
  output tokens already counted, not an additional charge -- adding it in
  would double it.
- **Anthropic prices are standard (non-batch) API list prices.** The Batch
  API is roughly 50% cheaper, but agent-cost's logs carry no signal for
  whether a request went through Batch, so all Claude usage is priced at
  standard rates; this overstates cost for anyone using Batch.
- **Cost is always an estimate.** The output field is `estimated_cost_usd`,
  never `cost_usd`: it is a list-price calculation from token counts, not a
  bill. For Codex, whose provider bills in credits, the row also carries a
  `credits` figure; `credits × usd_per_credit` is an illustrative USD
  conversion, not what you were actually charged (enterprise allowances, overage rules, and
  fast-mode multipliers with unknown values are exactly why the raw credits
  number is kept alongside the USD estimate rather than only the USD).
- An unrecognized model, or a token kind a model's rate period doesn't
  define, is reported as `unpriced` with `estimated_cost_usd: null`-like
  zero and the tokens broken out in `unpriced_tokens` -- agent-cost never
  invents a price for something it doesn't have a rate for.
- Corrupted log lines, files that vanish mid-read, and Codex cumulative
  counters that go backwards (e.g. after a session reset) are all counted
  in the report's `data_quality` block instead of being silently dropped or
  clamped to zero.
- **Known catalog gaps**, tracked in `agent_cost/rates.json`'s `notes`:
  `gpt-5.6`
  (Sol/Terra/Luna) credits could not be confirmed from the primary source
  (`help.openai.com`'s Codex rate card returns HTTP 403 to automated
  fetches); the values in the catalog come from several independent
  secondary sources that agree with each other and are internally
  consistent with `usd_per_credit`, but are not primary-source-verified --
  re-check them once the rate card is reachable. Update either via a
  custom `--rates` file if you have a confirmed number.

## Updating the rate catalog

**GPT-6 Astra:** Codex Standard/Fast estimates use exact ID `gpt-6-astra` and
owned settings matched to turn/model context in the public Codex 0.153.4 JSONL
format. Explicit `default` uses Standard; `priority` uses 2.5x. Missing/ambiguous
tier or model attribution stays `unpriced`, with tokens preserved. These are
request-setting estimates, not confirmed processing tiers or actual bills.
The catalog observation cutoff is not an official launch time; earlier usage
is `unpriced`. See [evidence, synthetic tests and model-switch limits](docs/astra-pricing.md).
API pricing and legacy message billing are outside this entry.

Prices live in `agent_cost/rates.json`, not in code. It's a historical
catalog: each model can have several time-bounded rate periods, so a price
change is recorded as a new period rather than overwriting the old one (see
`claude-sonnet-5`'s launch-promo period for a worked example). Every catalog
carries a `catalog_version`, a list of `sources` (the pricing page a rate
came from), and is validated on load (no duplicate model keys or aliases,
no negative rates, no overlapping periods for the same model).

A rate period can price by prompt length (catalog `schema_version` `"2"`).
The period's own five values are the base tier, for prompts up to the first
threshold; each entry in `prompt_tiers` applies to prompts *strictly over*
its `prompt_tokens_over`, and a request uses the largest threshold it
exceeds. Prompt length is the request's input + cache-read + cache-write
tokens, and the chosen tier prices every token kind of that request,
output included:

```json
{
  "rate_id": "claude-haiku-5-5-launch-2026-10-07",
  "effective_from": "2026-10-07T00:00:00+00:00",
  "effective_until": null,
  "input_nocache": "0.10", "cache_read": "0.01", "cache_write_5m": "0.125", "cache_write_1h": "0.20", "output": "0.50",
  "prompt_tiers": [
    { "prompt_tokens_over": 100000,
      "input_nocache": "0.50", "cache_read": "0.05", "cache_write_5m": "0.625", "cache_write_1h": "1.0", "output": "2.50" }
  ]
}
```

In a period with `prompt_tiers`, all five values of the base and of every
tier must be present and non-null, thresholds must be positive integers in
ascending order without duplicates, and every value must be greater than or
equal to the same value in the previous tier (the base first). That
monotonicity is what makes the base tier a true lower bound for a fact whose
prompt length is unknown. A `schema_version` `"1"` catalog may not contain
`prompt_tiers`; agent-cost 0.3.0 and earlier reject a `"2"` catalog as
unsupported rather than mispricing it, so a custom `--rates` file that uses
tiers must declare `"2"`.

To use your own catalog instead of the one bundled with the package, pass
`--rates path/to/rates.json` to `report` or `export` -- this fully replaces
the bundled catalog, it does not merge with it. Inspect any catalog with:

```bash
agent-cost rates show                       # list every model_key
agent-cost rates show --model gpt-5.5       # one model's rate history
agent-cost rates validate path/to/rates.json
```

`agent-cost report`'s JSON output always echoes the catalog's
`catalog_version` and the sha256 of the exact rates file used, so a report
can be traced back to the prices that produced it.

## Machine consumption: `agent-cost measure`

`report` is for a person reading a table. `measure` is for another program
calling agent-cost as a subprocess and parsing its stdout -- e.g. a build
orchestrator attributing cost to a specific unit of work it already knows
the session id(s) for.

```bash
agent-cost measure --session-id <id> [--session-id <id> ...] \
  [--since --until --timezone] [--agent claude,codex] [--rates PATH] --format json
```

- One or more `--session-id` is required (repeat the flag for more than
  one); `measure` never scans "everything," only the sessions you name.
- `--since`/`--until` accept a date-only value (interpreted in
  `--timezone`), an offset-qualified ISO 8601 datetime (`+00:00`, `+09:00`,
  ...), or the same datetime with a trailing `Z` instead of an offset
  (`2026-07-31T00:00:00Z`, exactly what `Date.toISOString()` in JS emits)
  -- all three are accepted by `report`/`export`/`measure` alike.
- Exit code is `0` on success -- including when none of the given session
  ids matched any usage at all, which is a valid, representable answer
  (empty totals, `"matched": false` per session), not a failure. Exit code
  `2` means bad input (no `--session-id`, an unparseable `--since`/
  `--until`/`--timezone`, or an invalid `--rates` catalog) -- nothing was
  measured, don't trust any partial output.
- Output is one JSON object on stdout with a `protocol_version` field
  (currently `"measure/v1"`) a caller should check before trusting the
  shape below. Within a major version, only additive changes (new fields)
  are made; a field being removed or changing meaning bumps the version.

```json
{
  "protocol_version": "measure/v1",
  "producer_version": "0.3.0",
  "accounting_basis": "agent-cost-raw-total/v2",
  "generated_at": "...",
  "window": { "since": "...", "until": null },
  "timezone": "UTC",
  "agent": ["claude", "codex"],
  "rates": { "catalog_version": "2026-07-29", "sha256": "..." },
  "session_ids": ["sess-1", "sess-2"],
  "sessions": {
    "sess-1": { "matched": true, "rows": [ /* same row shape as report --format json */ ], "totals": { "tokens": 12345, "priced_tokens": 12345, "unpriced_tokens": 0, "estimated_cost_usd": 0.42, "credits": 0.0 } },
    "sess-2": { "matched": false, "rows": [], "totals": { "tokens": 0, "priced_tokens": 0, "unpriced_tokens": 0, "estimated_cost_usd": 0.0, "credits": 0.0 } }
  },
  "total": { "rows": [ /* union across every requested session_id */ ], "totals": { "...": "..." } },
  "data_quality": {
    "malformed_events": 0,
    "skipped_files": 0,
    "negative_deltas": 0,
    "unpriced_tokens": 0,
    "duplicate_rows_skipped": 0,
    "conflicting_duplicate_groups": 0,
    "missing_dedup_identity_rows": 0,
    "source_quality": { "ok": 41, "first_event_delta": 2, "identity_missing": 0, "output_lower_bound": 1 }
  }
}
```

Rows are grouped by agent/model/token-kind only -- `measure` never buckets
by month, since a query is already scoped to specific sessions. `total` is
the union of every requested `session_id` (not a global report), so it's
the number to attribute to whatever unit of work those sessions represent.
`producer_version` is the agent-cost package version that produced this
payload; `accounting_basis` identifies the token-accounting semantics
behind the numbers (separate from `protocol_version`, which only tracks
the JSON shape) -- a consumer that persists historical measurements should
key comparability on `accounting_basis`, not `producer_version` alone,
since a future release can bump the latter while keeping the former.
`data_quality.unpriced_tokens` and `.source_quality` are scoped to the
requested sessions; so are the three dedup counters
(`duplicate_rows_skipped`, `conflicting_duplicate_groups`,
`missing_dedup_identity_rows`), which are Claude-only and computed over
the intersection of the requested session ids and the `--since`/`--until`
window, never over an unrequested session's rows.
`.malformed_events`/`.skipped_files`/`.negative_deltas` describe the
health of the underlying log read within `--since`/`--until` and are not
attributable to one session.

## Privacy

agent-cost makes zero network calls. `agent-cost export`'s JSONL never
includes absolute file paths, rollout paths, prompt/message content, or git
branch names -- only the fields needed to reproduce a cost estimate:
`occurred_at_utc`, `agent`, `session_id`, `model_raw`, `model_key`,
`token_kind`, `tokens`, `mode`, `source_quality` (a fixed-vocabulary
caveat about how that one fact was derived: `"ok"`, Codex's
`"first_event_delta"`, or Claude's `"identity_missing"` /
`"output_lower_bound"` -- never null), and `prompt_tokens` (the request's
prompt length as an integer, or null when unknown).

## License

MIT. See [LICENSE](LICENSE).

---

## 日本語サマリ

`agent-cost` は Claude Code / Codex CLI がローカルに残すログ（`~/.claude/projects/**/*.jsonl`
と `~/.codex/state_5.sqlite` + rollout ファイル）だけを読み、トークン使用量とおおよそのコストを
見積もる CLI です。ネットワークアクセスは一切行いません。

- 集計の最小単位は「1 イベント = 1 モデル × 1 token 種別」の fact であり、session 単位でモデルを
  丸めません。Claude の prompt cache は TTL 内訳（5分/1時間）が取れればそれを使い、取れない場合は
  5分単価で **下限推計**（`lower_bound`）として明示します。対象の Codex トークン料金体系では
  cache write を別料金として加算せず、現行 reader は独立した cache-write fact を出力しません。
  新しいログに `cache_write_input_tokens` が含まれていても追加料金の計算には使用しません。
  これは、旧ログでその情報を観測できないこととは区別します。
- 出力フィールドは `estimated_cost_usd`（推計であることを明示）。Codex は `credits` も併記します。
  未知のモデル・単価表にない token 種別は `unpriced` として扱い、憶測の価格を出しません。
- 単価表 (`agent_cost/rates.json`) は履歴型カタログで、値上げは新しい期間として追加します。
  `--rates PATH` で別カタログに完全差し替えできます。
- prompt 長で単価が変わるモデル（`claude-haiku-5-5`、100,000 tokens 超で高い単価）は期間の
  `prompt_tiers`（schema_version `"2"`）で表し、リクエストごとの prompt 長（input + cache read +
  cache write）で tier を選びます。prompt 長が不明な fact（Codex 由来、conflicting group、usage
  不整合）は基底 tier（最安）で **下限推計**（`lower_bound`）になります。
- `agent-cost measure --session-id ID [--session-id ID ...] --format json` は他プログラムから
  subprocess で叩くための機械可読な契約です（`protocol_version: "measure/v1"`）。指定した
  session_id が1件も見つからなくても終了コードは0（空集計として表現）、`--session-id` 未指定など
  の入力エラーのみ終了コード2です。
- `report --billing-plan PATH`（任意・機密）は、ローカルの課金プラン（窓ごとの定額・利用枠・超過規則）を
  公開単価で集計した Claude の list_cost に当てて `internal_billing` を出します。フラグ無しの出力は不変です。
- plan ファイルは 0600・symlink 不可・git repo 外に置き、出力は機密成果物として扱います（`measure` には按分になるため無し）。
- 破損したログ行、読めなくなったファイル、Codex の累積カウンタが逆行するケースなどは、すべて
  `data_quality` に件数として記録し、黙って丸めたり捨てたりしません。
- Codex の `output` は `output_tokens` のみです。実 rollout データを突合した結果
  `total_tokens == input_tokens + output_tokens` が常に成立することを確認しており、
  `reasoning_output_tokens` は output の内訳（二重計上してはいけない）と判断しています。
- Anthropic の単価は標準（非 Batch）API 価格です。Batch API は約50%安いですが、ログからは
  Batch 利用かどうか判別できないため、常に標準単価で推計します（Batch 利用者には過大推計）。
- `claude-opus-5` のローンチ日は根拠を確認できず `effective_from` はプレースホルダです。
  `gpt-5.6`（sol/terra/luna）は一次情報（help.openai.com の rate card）が 403 で取得できなかったため、
  相互に整合する複数の二次情報源の値を採用しています（一次情報での裏取りは未完了、詳細は
  `rates.json` の `notes`）。
