# Changelog

## 0.2.0

**Breaking for anyone comparing raw numbers across versions**: Claude Code
facts from 0.1.x and 0.2.0+ are not directly comparable. See "Fixed" below.

### Fixed

- Claude reader over-counting: Claude Code's JSONL transcript writes one
  line per *content block* of an assistant message, not one line per
  message, and every one of those lines carries a `message.usage` block
  for the *same* logical message -- but not always an identical one:
  input-side fields (`input_tokens`, `cache_read_input_tokens`, the
  `cache_creation` TTL breakdown) and `model` stay constant across a
  message's lines, while `output_tokens` grows line by line as the
  response streams in, and `usage.speed` (which determines `mode`) is
  typically absent on every line but the last, which alone carries the
  concrete mode. `agent_cost/readers/claude.py`'s `parse_session_facts`
  treated each line as its own billing event regardless, so a single
  message could be counted 3-7x. `parse_session_facts` (and the new
  `parse_session_detailed`) now deduplicate rows that share the same
  logical message within one session file. This is a bug fix, not a
  behavior change anyone should have relied on: 0.1.x's Claude token
  totals were inflated.

  Dedup only applies when a row carries **both** a `message.id` and a
  `requestId` (both non-empty strings) -- that full pair is the dedup key.
  A single identifier alone is not enough to dedup on, since two genuinely
  different messages could coincidentally share just one half of the pair;
  a row missing either half is emitted individually (never merged with
  another row) and flagged both in `data_quality.missing_dedup_identity_rows`
  and, on the fact itself, via a new `source_quality` value,
  `"identity_missing"` (see `agent_cost/facts.py`'s `SOURCE_QUALITY_VALUES`).
  Within a dedup group, the emitted fact's `occurred_at_utc` is the
  *adopted* row's own timestamp (the row whose usage is actually billed),
  not the group's first-seen timestamp -- using an earlier placeholder
  row's timestamp could shift real tokens across a month or rate-period
  boundary they don't belong to. Only the group's *position* in the
  output stays first-seen-ordered.

### Added

- `data_quality.duplicate_rows_skipped`, `data_quality.conflicting_duplicate_groups`,
  and `data_quality.missing_dedup_identity_rows` in `report` and `measure`
  JSON output -- diagnostics for the dedup above (how many duplicate lines
  were collapsed, how many dedup groups actually disagreed on billing, and
  how many rows had neither a full `message.id` + `requestId` pair to dedup
  on at all). All three are scoped to match the rest of that command's
  output: `report`'s to its `--since`/`--until` window, `measure`'s to its
  requested `--session-id`s *and* window -- an unrequested session's
  conflict never affects a requested session's counters.

  `conflicting_duplicate_groups` does **not** flag ordinary Claude Code
  streaming, per the pattern described under "Fixed" above: `model` and
  the input-side fields (`input_tokens`, `cache_read_input_tokens`, the
  `cache_creation` TTL breakdown) staying identical across a group's rows
  while `output_tokens` grows row by row (the final, `stop_reason`-bearing
  row carrying the largest value) and intermediate rows report `mode` as
  `"unknown"` (absent `usage.speed`) are both counted only in
  `duplicate_rows_skipped`, not as a conflict. `"unknown"` mode is a
  wildcard for conflict purposes -- it means "no speed reported on this
  row", not "this row ran at some other speed" -- so `"unknown"` mixed
  with a single concrete mode (`"normal"`/`"fast"`) is not a conflict, but
  two *different* concrete modes are. A group is flagged as conflicting
  only when `model` or an input-side field actually differs across its
  rows, its concrete modes disagree, or `output_tokens` decreases or is
  non-monotonic across them (all three are actual billing disagreements,
  not streaming progress). The emitted fact's `mode` prefers a group's one
  concrete mode over an adopted row's `"unknown"`, so a fact never reports
  an unknown mode when the group actually knows it.
- A new `source_quality` value, `"identity_missing"` (see "Fixed" above),
  alongside the existing `"ok"` and `"first_event_delta"`.
- `measure`'s JSON output now also carries `producer_version` (the
  installed `agent-cost` version) and `accounting_basis` (currently
  `"agent-cost-raw-total/v2"`) at the top level, alongside
  `protocol_version`. `protocol_version` itself is unchanged
  (`"measure/v1"`) -- these are additive fields, not a shape-breaking
  change, so an existing consumer that only checks `protocol_version`
  keeps working. A consumer that persists historical measurements should
  key on `accounting_basis`, since Claude numbers computed under
  `agent-cost-raw-total/v1` (0.1.x) semantics are not comparable to
  `agent-cost-raw-total/v2` (0.2.0+) numbers for the same session.

### Not comparable across versions

Do not diff or backfill a 0.1.x Claude measurement against a 0.2.0+ one as
if they measure the same thing -- the 0.1.x number is inflated by the bug
above. Don't assume a fixed multiplier to "correct" it, either, since the
amount of over-counting depends on how many content blocks each message
happened to have.

If a consumer already recorded a 0.1.x measurement, apply a correction on
the consumer side (e.g. a recorded note or adjustment factor keyed on
`accounting_basis`) rather than re-running `measure` and overwriting the
stored value -- the original figure is what was actually acted on at the
time, and overwriting it erases that record. If you do choose to
re-measure, keep both numbers: record the new `producer_version` and
`accounting_basis` alongside the new figure, and do not overwrite the old
value.
