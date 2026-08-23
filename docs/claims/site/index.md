# agent-cost pricing domain claim corpus

> **All claims below carry their own `review_status`; anything still
> `unreviewed` is AI-generated content that has not had human sign-off.**
> Treat it accordingly.

Regenerate with:

```bash
evidence-docs generate . --generated-at 2026-08-15T00:00:00Z --repo-commit 1c74d44903e67e1f65bb28a0b0d39443f48f657b
```

generated_at: `2026-08-15T00:00:00Z` / repo_commit: `1c74d44903e67e1f65bb28a0b0d39443f48f657b`

## Topics

- [T-01 — rates.json catalog structural validation rules](#t-01)
- [T-02 — cache_write_unknown lower-bound pricing semantics](#t-02)
- [T-03 — unpriced fact/row handling (never guessing a price)](#t-03)
- [T-04 — Decimal used exclusively for money-shaped values](#t-04)
- [T-05 — measure/v1 versioned-tracking contract, cross-repo ownership split](#t-05)

<a id="t-01"></a>

## T-01 — rates.json catalog structural validation rules

What `agent_cost.rates.load_rates()` rejects outright when loading a rate catalog (unsupported schema_version/currency/unit, duplicate model_key, colliding/duplicate aliases, overlapping rate periods for the same model, negative rate values) versus what it accepts, before any pricing decision is made.

| ID | claim_kind | epistemic_status | conformance_status |
|---|---|---|---|
| [OBS-001](#obs-001) | `constraint` | 🟢 `execution_verified` | ✅ `matched` |
| [OBS-002](#obs-002) | `invariant` | 🟢 `execution_verified` | ✅ `matched` |
| [OBS-003](#obs-003) | `invariant` | 🟢 `execution_verified` | ✅ `matched` |

<a id="obs-001"></a>
### OBS-001 · `constraint` · 🟢 `execution_verified` · ✅ `matched`

load_rates() rejects a catalog outright (raising RatesValidationError) if schema_version is not "1", currency is not "USD", or unit is not "per_mtok" -- these are closed allow-lists (SUPPORTED_SCHEMA_VERSIONS / SUPPORTED_CURRENCIES / SUPPORTED_UNITS), not warnings, so a catalog in an unexpected unit/currency can never be silently priced as if it were USD-per-mtok.

**provenance:**

- `test` [`tests/test_rates.py`](../../../tests/test_rates.py) — test_unsupported_schema_version_raises
- `test` [`tests/test_rates.py`](../../../tests/test_rates.py) — test_unsupported_currency_raises
- `test` [`tests/test_rates.py`](../../../tests/test_rates.py) — test_unsupported_unit_raises
- `source` [`agent_cost/rates.py`](../../../agent_cost/rates.py) — _validate_and_build

<a id="obs-002"></a>
### OBS-002 · `invariant` · 🟢 `execution_verified` · ✅ `matched`

A catalog is rejected at load time if two models share the same model_key, if one model's alias equals another model's own model_key (alias/model_key namespaces cannot collide), or if the same alias string is registered under two different models -- all three are structural invariants of _build_model_entry/_validate_and_build's alias_index construction, not just a documentation convention.

**provenance:**

- `test` [`tests/test_rates.py`](../../../tests/test_rates.py) — test_duplicate_model_key_raises
- `test` [`tests/test_rates.py`](../../../tests/test_rates.py) — test_alias_colliding_with_model_key_raises
- `test` [`tests/test_rates.py`](../../../tests/test_rates.py) — test_duplicate_alias_raises

<a id="obs-003"></a>
### OBS-003 · `invariant` · 🟢 `execution_verified` · ✅ `matched`

Two rate periods for the same model are rejected as "overlapping" if one's effective_until (or an open-ended period's implicit datetime.max) is strictly after the next one's effective_from; a period ending exactly when the next one begins (touching, not overlapping) is accepted. Periods are sorted by effective_from before this adjacent-pair check runs.

**provenance:**

- `test` [`tests/test_rates.py`](../../../tests/test_rates.py) — test_overlapping_rate_periods_raise
- `test` [`tests/test_rates.py`](../../../tests/test_rates.py) — test_adjacent_non_overlapping_periods_are_ok
- `source` [`agent_cost/rates.py`](../../../agent_cost/rates.py) — _build_model_entry

<a id="t-02"></a>

## T-02 — cache_write_unknown lower-bound pricing semantics

When a prompt-cache write's TTL breakdown (5-minute vs 1-hour) cannot be determined from the source log, agent-cost records it as the `cache_write_unknown` token kind and prices it at the cheaper 5-minute rate as an explicit lower bound, flagged `lower_bound`, rather than guessing at the more expensive 1-hour rate. Codex CLI never produces this token kind at all, since it exposes no cache-write signal.

| ID | claim_kind | epistemic_status | conformance_status |
|---|---|---|---|
| [OBS-004](#obs-004) | `behavior` | 🟢 `execution_verified` | ✅ `matched` |
| [OBS-005](#obs-005) | `behavior` | 🟢 `execution_verified` | ✅ `matched` |
| [OBS-006](#obs-006) | `decision_record` | 🟦 `recorded_decision` | ✅ `matched` |
| [OBS-007](#obs-007) | `behavior` | 🟠 `single_source_observation` | ✅ `matched` |

<a id="obs-004"></a>
### OBS-004 · `behavior` · 🟢 `execution_verified` · ✅ `matched`

parse_session_facts emits cache_write_5m/cache_write_1h facts when a Claude Code event's usage.cache_creation dict is present and its two ephemeral counters sum to the event's cache_creation_input_tokens exactly; when usage.cache_creation is absent entirely, the whole cache_creation_input_tokens count is instead emitted as a single cache_write_unknown fact. (The tested cases only cover a full TTL breakdown that exactly accounts for the total, and no breakdown at all -- see this topic's gap record for the partial-breakdown case the code also handles but no test exercises.)

**provenance:**

- `test` [`tests/test_reader_claude.py`](../../../tests/test_reader_claude.py) — test_cache_creation_ttl_breakdown_present
- `test` [`tests/test_reader_claude.py`](../../../tests/test_reader_claude.py) — test_cache_creation_without_ttl_breakdown_is_unknown
- `source` [`agent_cost/readers/claude.py`](../../../agent_cost/readers/claude.py) — parse_session_facts

<a id="obs-005"></a>
### OBS-005 · `behavior` · 🟢 `execution_verified` · ✅ `matched`

price_fact() prices a cache_write_unknown fact by substituting the cache_write_5m rate field (never cache_write_1h, the more expensive one) and setting pricing_status to "lower_bound" rather than "priced", so a caller can tell "priced normally" apart from "priced as an explicit floor because the TTL was unknown".

**provenance:**

- `test` [`tests/test_aggregate.py`](../../../tests/test_aggregate.py) — test_price_fact_cache_write_unknown_is_lower_bound
- `source` [`agent_cost/aggregate.py`](../../../agent_cost/aggregate.py) — price_fact

<a id="obs-006"></a>
### OBS-006 · `decision_record` · 🟦 `recorded_decision` · ✅ `matched`

Pricing an unknown-TTL cache write at the 5-minute rate as an explicit lower bound -- instead of guessing the more expensive 1-hour rate, or averaging the two -- is a deliberate design decision, recorded in both price_fact's own docstring and the README's "What this measures" section, so a reader of the output number knows it is a floor, not a best-effort midpoint.

**provenance:**

- `source` [`agent_cost/aggregate.py`](../../../agent_cost/aggregate.py) — price_fact (module docstring)
- `spec` [`README.md`](../../../README.md) — ## What this measures, and what it doesn't

<a id="obs-007"></a>
### OBS-007 · `behavior` · 🟠 `single_source_observation` · ✅ `matched`

The Codex CLI reader (parse_rollout_facts) never emits a cache_write_5m, cache_write_1h, or cache_write_unknown fact for any event -- it only ever constructs Fact objects with token_kind "input_nocache", "cache_read", or "output". This matches the README's claim that Codex exposes no cache-write signal at all, but is recorded here from a direct, single-pass static read of the reader module rather than from a test that asserts the negative (no test in tests/test_reader_codex.py checks that "cache_write" never appears in a fact's token_kind).

**provenance:**

- `source` [`agent_cost/readers/codex.py`](../../../agent_cost/readers/codex.py) — parse_rollout_facts

<a id="t-03"></a>

## T-03 — unpriced fact/row handling (never guessing a price)

How agent-cost handles a fact it has no rate for -- an unrecognized model_key, or a token kind a known model's rate period doesn't define -- and how that "unpriced" status propagates from a single fact up to a grouped row and the top-level data_quality block, without ever inventing a cost number.

| ID | claim_kind | epistemic_status | conformance_status |
|---|---|---|---|
| [OBS-008](#obs-008) | `behavior` | 🟢 `execution_verified` | ✅ `matched` |
| [OBS-009](#obs-009) | `behavior` | 🟢 `execution_verified` | ✅ `matched` |
| [OBS-010](#obs-010) | `invariant` | 🟠 `single_source_observation` | ✅ `matched` |
| [OBS-011](#obs-011) | `invariant` | 🟢 `execution_verified` | ✅ `matched` |

<a id="obs-008"></a>
### OBS-008 · `behavior` · 🟢 `execution_verified` · ✅ `matched`

price_fact() returns (None, "unpriced", None) -- never a guessed number -- both when the fact's model_key does not resolve against the catalog at all, and when the model resolves but the specific token-kind rate field for the covering rate period is None (e.g. OpenAI models carry no cache_write_5m rate at all, so a cache-write fact against gpt-5.5 is unpriced even though the model itself is known).

**provenance:**

- `test` [`tests/test_aggregate.py`](../../../tests/test_aggregate.py) — test_price_fact_unknown_model_is_unpriced
- `test` [`tests/test_aggregate.py`](../../../tests/test_aggregate.py) — test_price_fact_codex_cache_write_is_unpriced
- `test` [`tests/test_rates.py`](../../../tests/test_rates.py) — test_gpt_5_5_cache_write_is_unpriced

<a id="obs-009"></a>
### OBS-009 · `behavior` · 🟢 `execution_verified` · ✅ `matched`

build_rows() marks an entire grouped row "unpriced" as soon as any one of the facts folded into that bucket is unpriced, even if other facts in the same bucket priced normally -- a row is never reported as fully priced when part of its token count actually wasn't.

**provenance:**

- `test` [`tests/test_aggregate.py`](../../../tests/test_aggregate.py) — test_build_rows_mixed_priced_and_unpriced_marks_row_unpriced

<a id="obs-010"></a>
### OBS-010 · `invariant` · 🟠 `single_source_observation` · ✅ `matched`

build_rows()'s row.pricing_status ranking is defined in code as a full three-way order -- unpriced (rank 0) beats lower_bound (rank 1) beats priced (rank 2), via _STATUS_RANK -- so a row containing both a lower_bound fact and a plain priced fact should end up "lower_bound", not "priced". Only the unpriced-over-priced half of this ordering is exercised by an existing test (test_build_rows_mixed_priced_and_unpriced_marks_row_unpriced); no test in tests/test_aggregate.py builds a row from a mix of a lower_bound fact and a priced fact, so the lower_bound-vs-priced half of the ranking is confirmed only by reading _STATUS_RANK and build_rows() directly, not by execution.

**provenance:**

- `source` [`agent_cost/aggregate.py`](../../../agent_cost/aggregate.py) — _STATUS_RANK

<a id="obs-011"></a>
### OBS-011 · `invariant` · 🟢 `execution_verified` · ✅ `matched`

DataQuality.unpriced_tokens (the report-wide total returned alongside build_rows()'s row list) and a single row's own unpriced_tokens field are both accumulated from the exact same set of unpriced facts in one pass, so the two numbers stay consistent with each other by construction rather than by a separate reconciliation step.

**provenance:**

- `test` [`tests/test_aggregate.py`](../../../tests/test_aggregate.py) — test_build_rows_unknown_model_is_unpriced_and_flagged

<a id="t-04"></a>

## T-04 — Decimal used exclusively for money-shaped values

Rate catalog values, per-fact cost/credit calculations, and cross-row aggregation all use decimal.Decimal rather than float, converting to float only once, at the very end, for JSON serialization -- and never for the arithmetic itself.

| ID | claim_kind | epistemic_status | conformance_status |
|---|---|---|---|
| [OBS-012](#obs-012) | `constraint` | 🟢 `execution_verified` | ✅ `matched` |
| [OBS-013](#obs-013) | `behavior` | 🟢 `execution_verified` | ✅ `matched` |
| [OBS-014](#obs-014) | `decision_record` | 🟦 `recorded_decision` | ✅ `matched` |

<a id="obs-012"></a>
### OBS-012 · `constraint` · 🟢 `execution_verified` · ✅ `matched`

Every numeric rate field in rates.json (input_nocache, cache_read, cache_write_5m, cache_write_1h, output, fast_multiplier, credits_per_mtok.*, usd_per_credit) is parsed via _parse_decimal into a decimal.Decimal, never a Python float; _parse_decimal also rejects any value that parses to a negative Decimal, regardless of which field it came from.

**provenance:**

- `test` [`tests/test_rates.py`](../../../tests/test_rates.py) — test_claude_opus_rates
- `test` [`tests/test_rates.py`](../../../tests/test_rates.py) — test_negative_rate_value_raises
- `source` [`agent_cost/rates.py`](../../../agent_cost/rates.py) — _parse_decimal

<a id="obs-013"></a>
### OBS-013 · `behavior` · 🟢 `execution_verified` · ✅ `matched`

price_fact()'s cost/credit formula ((tokens / 1_000_000) * rate * fast_multiplier) is computed entirely in Decimal arithmetic, including the fast-mode multiplier; tests assert the exact resulting Decimal value (e.g. Decimal("5.0") * Decimal("2.5")), not an approximately-equal float comparison, which would not distinguish Decimal arithmetic from float arithmetic that happened to round to the same displayed value.

**provenance:**

- `test` [`tests/test_aggregate.py`](../../../tests/test_aggregate.py) — test_price_fact_known_model_and_kind
- `test` [`tests/test_aggregate.py`](../../../tests/test_aggregate.py) — test_price_fact_codex_applies_fast_multiplier_to_usd_and_credits

<a id="obs-014"></a>
### OBS-014 · `decision_record` · 🟦 `recorded_decision` · ✅ `matched`

rows_totals() deliberately sums each row's Decimal estimated_cost_usd/credits fields first and converts to float exactly once at the end, rather than summing already-float-converted per-row values, specifically so that precision is not lost across many small rows before the final conversion -- stated directly in the function's own docstring as the reason for that ordering.

**provenance:**

- `source` [`agent_cost/aggregate.py`](../../../agent_cost/aggregate.py) — rows_totals (docstring)

<a id="t-05"></a>

## T-05 — measure/v1 versioned-tracking contract, cross-repo ownership split

agent-cost measure --format json is a cross-language conformance contract (measure/v1). agent-cost owns the version number, prose contract, and implementation; the JSON Schema single source of truth and conformance fixtures live in a different repository (ai-agent-skills-playbook) and are vendored read-only into agent-cost's own test suite at a pinned commit, which agent-cost's own tests check its live output against.

| ID | claim_kind | epistemic_status | conformance_status |
|---|---|---|---|
| [OBS-015](#obs-015) | `decision_record` | 🟦 `recorded_decision` | ✅ `matched` |
| [OBS-016](#obs-016) | `behavior` | 🟢 `execution_verified` | ✅ `matched` |
| [OBS-017](#obs-017) | `behavior` | 🟢 `execution_verified` | ✅ `matched` |

<a id="obs-015"></a>
### OBS-015 · `decision_record` · 🟦 `recorded_decision` · ✅ `matched`

measure/v1's version number, prose contract, and implementation are owned end-to-end by agent-cost (agent_cost/cli.py's MEASURE_PROTOCOL_VERSION constant and cmd_measure), while the JSON Schema single source of truth and its conformance fixtures live in a different repository, ai-agent-skills-playbook (contracts/measure/v1/measure-output.schema.json), and are vendored byte-for-byte, read-only, into agent-cost's own test tree at a pinned commit (tests/fixtures/measure-v1-contract/, per its own UPSTREAM pin file) -- this repo never edits the playbook's doc or vice versa.

**provenance:**

- `source` [`agent_cost/cli.py`](../../../agent_cost/cli.py) — MEASURE_PROTOCOL_VERSION
- `test` [`tests/fixtures/measure-v1-contract/UPSTREAM`](../../../tests/fixtures/measure-v1-contract/UPSTREAM) — Vendored commit / Vendored into
- `test` [`tests/test_measure_v1_contract.py`](../../../tests/test_measure_v1_contract.py) — module docstring
- `review_memory` `ai-agent-skills-playbook:docs/protocols/measure-v1.md@e412ad140f42446b7a0769d4a27fb56d67ab60ca` — “This document is not the contract's normative source” section

<a id="obs-016"></a>
### OBS-016 · `behavior` · 🟢 `execution_verified` · ✅ `matched`

agent-cost's own hand-written structural checker (check_measure_payload in tests/test_measure_v1_contract.py) agrees with the vendored contract's own accept/reject labels: all 3 vendored "accept" fixtures pass it with zero issues, and all 6 vendored "reject" fixtures produce at least one issue (with the expected reason code where one is asserted, e.g. protocol_version mismatch, a matched:false session entry with non-zero totals, or total.rows disagreeing with the sessions-level re-aggregation).

**provenance:**

- `test` [`tests/test_measure_v1_contract.py`](../../../tests/test_measure_v1_contract.py) — test_vendored_accept_fixtures_pass_the_structural_check
- `test` [`tests/test_measure_v1_contract.py`](../../../tests/test_measure_v1_contract.py) — test_vendored_reject_fixtures_fail_the_structural_check

<a id="obs-017"></a>
### OBS-017 · `behavior` · 🟢 `execution_verified` · ✅ `matched`

A live payload from cli.main(["measure", ...]), generated fresh by the current implementation against a synthetic Claude Code session, has exactly the same top-level/rates/window/data_quality/ source_quality/session-entry/row key sets as the vendored accept-matched-normal.json fixture, and passes check_measure_payload with zero issues -- this is checked against the external contract's fixture, not only against a hardcoded expectation internal to this repo (that internal-only pin is tests/test_cli.py::test_measure_json_schema_is_locked, a distinct test).

**provenance:**

- `test` [`tests/test_measure_v1_contract.py`](../../../tests/test_measure_v1_contract.py) — test_live_measure_output_matches_vendored_contract_key_shape
