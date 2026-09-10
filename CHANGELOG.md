# Changelog

## 0.2.0

**Breaking for anyone comparing raw numbers across versions**: Claude Code
facts from 0.1.x and 0.2.0+ are not directly comparable. See "Fixed" below.

### Fixed

- Claude reader over-counting: Claude Code's JSONL transcript writes one
  line per *content block* of an assistant message, not one line per
  message, and every one of those lines carries the same `message.usage`
  block. `agent_cost/readers/claude.py`'s `parse_session_facts` treated each
  line as its own billing event, so a single message could be counted
  3-7x. `parse_session_facts` (and the new `parse_session_detailed`) now
  deduplicate rows that share the same logical message within one session
  file. This is a bug fix, not a behavior change anyone should have relied
  on: 0.1.x's Claude token totals were inflated.

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
  were collapsed, how many dedup groups disagreed on token counts across
  their duplicate lines, and how many rows had neither a full `message.id`
  + `requestId` pair to dedup on at all). All three are scoped to match
  the rest of that command's output: `report`'s to its `--since`/`--until`
  window, `measure`'s to its requested `--session-id`s *and* window -- an
  unrequested session's conflict never affects a requested session's
  counters.
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
above. If you need a corrected historical figure, re-run `measure` against
the same local logs under 0.2.0+; don't assume a fixed multiplier, since
the amount of over-counting depends on how many content blocks each
message happened to have.
