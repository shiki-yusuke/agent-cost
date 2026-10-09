# Changelog

## 0.4.0

Adds `claude-haiku-5-5`, whose price depends on each request's prompt length,
and the catalog / fact fields needed to express that. Rows and totals for
every model without prompt tiers are identical to 0.3.0; only
`claude-haiku-5-5` rows differ (they were `unpriced` and are now priced).

### Added

- `claude-haiku-5-5` rate entry (catalog_version `2026-10-09`), effective
  from the release date 2026-10-07 (platform.claude.com model overview), fast
  multiplier 1.0, a separate entry rather than an alias of
  `claude-haiku-4-5`. Per the pricing page (retrieved 2026-10-09): prompts up
  to 100,000 tokens $0.10 / $0.01 / $0.125 / $0.20 / $0.50 per MTok
  (input / cache read / 5m cache write / 1h cache write / output), prompts
  over 100,000 tokens $0.50 / $0.05 / $0.625 / $1 / $2.50; `cache_read` is the
  standard 0.1x. Claude Code's 2.1.293 changelog entry makes Haiku 5.5 the
  default Haiku model, so `model: haiku` rows now carry this raw ID.
- Optional `prompt_tiers` on a rate period: the period's own values are the
  base tier (prompt length <= the first threshold); each tier applies to
  prompts strictly over its `prompt_tokens_over`, and a request uses the
  largest threshold it exceeds. Validation: thresholds are positive ints,
  ascending, no duplicates; in a tiered period every value of the base and of
  every tier is present and non-null; every value is >= the same value in the
  previous tier (so the base is a true lower bound).
- `Fact.prompt_tokens` (appended after `source_quality`, default `None`): the
  request's prompt length, `input_tokens + cache_read_input_tokens +
  cache_creation_input_tokens`, matching the pricing page's Long context
  pricing definition (cache reads and writes count; each request is priced on
  its own, output included). The Claude reader sets it on every fact of a row;
  it is `None` for a conflicting dedup group, for a row whose cache-write TTL
  breakdown exceeds `cache_creation_input_tokens`, and for every Codex fact
  (a delta of cumulative totals, not one request). `export` JSONL records
  carry it as `prompt_tokens` (int or null).
- `rates show --model` prints a period's tiers under a `prompt_tiers:` line,
  e.g. `> 100000: input_nocache=0.50 cache_read=0.05 cache_write_5m=0.625
  cache_write_1h=1.0 output=2.50`.

### Changed

- The packaged `rates.json` is `schema_version` `"2"`. The loader accepts
  `"1"` and `"2"`, and rejects a `"1"` catalog that contains `prompt_tiers`.
  agent-cost 0.3.0 and earlier reject a `"2"` catalog as an unsupported
  schema_version -- it fails to load rather than pricing every request at
  the cheaper tier.
- Pricing: a fact for a tiered period is priced at its tier and is
  `priced`; one whose `prompt_tokens` is unknown is priced at the base tier
  and is `lower_bound`. `cache_write_unknown` keeps its 5-minute-rate
  `lower_bound` treatment, at the selected tier's 5-minute rate. `report` /
  `measure` row shapes are unchanged.

### Upgrade notes

- A downstream consumer that validates `export` JSONL against a fixed schema
  must allow the new `prompt_tokens` key (int or null).
- A custom catalog passed with `--rates` must declare `schema_version` `"2"`
  to use `prompt_tiers`; an existing `"1"` catalog without tiers keeps
  working unchanged.

## 0.3.0

Reader flag-only release: no token amount, dedup adoption or conflict
counting changes. Reports computed with 0.2.2 have the same rows and totals;
only some Claude `output` facts' `source_quality` changes from `"ok"` to the
new value below.

### Added

- `source_quality` value `"output_lower_bound"` (appended to
  `SOURCE_QUALITY_VALUES`): the fact's tokens are the observed lower bound of
  that message's `output_tokens`, not guaranteed to be >= the true count. The
  Claude reader sets it on a dedup group's `output` fact when the row it
  adopts has no valid `stop_reason` (a non-empty string) -- including a final
  row overridden by a later, differing non-final row. In Claude Code subagent
  transcripts roughly 35% of messages ending in `tool_use` never write their
  final line, so the adopted row's `output_tokens` is the streaming head's
  value; top-level transcripts showed no such groups in a 2026-10-07 check.
  Input-side facts from the same group stay `"ok"`, identity-missing rows
  stay `"identity_missing"`, and the Codex reader is unchanged.
- `measure`'s `data_quality.source_quality` now always carries an
  `output_lower_bound` key (0 when absent), like every other value.

### Upgrade notes

- A downstream consumer that validates `source_quality` (in `export` JSONL or
  `measure`'s `data_quality.source_quality`) against a fixed enum must add
  `"output_lower_bound"`.

## 0.2.2

Catalog-only release: no reader or aggregation logic changes. Reports
computed with 0.2.1 differ only for `claude-sonnet-5-5` rows (now priced).

### Added

- `claude-sonnet-5-5` rate entry (catalog_version `2026-09-29`): $2 / $0.20 /
  $2.50 / $4 / $10 per MTok, fast multiplier 1.0, effective from the public
  launch date 2026-09-28. Claude Code's 2.1.284 changelog entry makes Sonnet
  5.5 the default Sonnet model (the `sonnet` alias in agent frontmatter now
  resolves to it), so rows whose raw model ID is `claude-sonnet-5-5` would
  otherwise have been reported as `unpriced`. The values equal
  `claude-sonnet-5`'s ongoing values, but the model is a separate entry, not
  an alias: the pricing page lists the two rows independently. `cache_read`
  is the standard 0.1x for this model (no pricing-page footnote), unlike
  `claude-opus-5-5`'s 0.05x.

## 0.2.1

Catalog-only release: no reader or aggregation logic changes. Reports
computed with 0.2.0 differ only for `claude-opus-5-5` rows (now priced) and
for `claude-opus-5` rows dated 2026-07-01..2026-07-23 (now `unpriced`).

### Added

- `claude-opus-5-5` rate entry (catalog_version `2026-09-23`): $4 / $0.20 /
  $5 / $8 / $20 per MTok, fast multiplier 2.0, effective from the public
  launch date 2026-09-22. Claude Code's 2.1.280 changelog entry makes Opus
  5.5 the default model, so rows whose raw model ID is `claude-opus-5-5`
  would otherwise have been reported as `unpriced`. `cache_read` is 0.05x
  base input for this model (pricing-page footnote), not the standard 0.1x.

### Changed

- `claude-opus-5`'s `effective_from` placeholder (2026-07-01) replaced by the
  official launch date 2026-07-24 (rate_id `claude-opus-5-launch-2026-07-24`).
  The earliest local transcript row for the model is 2026-07-28, so no
  observed event changes from priced to unpriced.

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
