# GPT-6 Astra: Codex token-rate maintenance

Scope: estimates for **Codex standard/Fast token-based usage with exact model ID
`gpt-6-astra`**. This is not an API tariff, a legacy per-message tariff, a
ChatGPT Work log reader, or an invoice calculator. The logs do not establish
which commercial agreement or authentication/billing route applies; the operator
must confirm that the selected Codex token tariff fits the usage.

## Price evidence and time boundary

Official page bodies were checked on **2026-09-06 JST**, recorded at
**2026-09-05T17:18:23Z**:

- [Work / Codex credit rate card](https://help.openai.com/en/articles/11481834-chatgpt-rate-card):
  per 1M tokens, uncached input **250 credits**, cached input **25**, output
  **1,250**; Astra Fast uses **2.5x** Standard. Codex has no cache-write charge.
- [Enterprise USD token rate card](https://help.openai.com/en/articles/20001415-chatgpt-rate-card-enterprise-token-based-pricing):
  Astra **$10 / $1 / $50** respectively. Codex Astra has no extra multiplier
  above 272K input tokens. Contract terms can differ.
- [API model page](https://developers.openai.com/api/docs/models/gpt-6-astra):
  checked to distinguish products. Its long-context multipliers, cache-write
  tariff and 2x API Fast rate are **not** used in the Codex entry.

`catalog_version` is `2026-09-06` (the local confirmation date). The catalog
requires an `effective_from`; the Astra period starts at the recorded observation
cutoff `2026-09-05T17:18:23Z`, inclusive. **This is a conservative catalog policy,
not an official launch time or tariff commencement time.** Earlier events remain
`unpriced`, including any real Astra use before this observation. No end date was
established; `effective_until: null` keeps the observed rate open until a reviewed
update. It does not promise the provider will never change prices.

The existing `usd_per_credit=0.04` matches these two published standard tables;
credits and USD remain estimates, not a universal credit purchase or cash value.
No cache-write facts are emitted by this Codex reader. Cache-write catalog fields
remain null rather than inventing an API cache-write rate or a synthetic zero row.

## Public-format compatibility and limits

Public source retrieval: **2026-09-05T17:24:44Z** (2026-09-06 JST).
Latest stable release at inspection: [Codex 0.153.4](https://github.com/openai/codex/releases/tag/rust-v0.153.4),
commit `3d2ee51ca2d5db578f328aa75e20aa22c0197c9a`. No real logs were read.

- [Model catalog](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/models-manager/models.json)
  identifies `gpt-6-astra` and labels service tier `priority` as Fast. No aliases,
  Pro IDs, dated IDs or `-fast` IDs are added here. The pricing guard rejects
  unverified raw IDs even if the existing generic normalizer strips their suffix.
- [Protocol](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/protocol/src/protocol.rs)
  defines `TokenUsage`, `TokenUsageInfo`, `TokenCountEvent`, `TurnContextItem`
  and `ThreadSettingsSnapshot`. The existing reader consumes
  `event_msg` / `token_count` / `info.total_token_usage`: input includes cached
  input, so uncached input is their difference. Output is not increased by the
  reasoning breakdown. The newer `cache_write_input_tokens` is ignored for this
  Codex tariff. Cumulative counts are differenced, not summed as independent usage.
- [Persistence policy](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/rollout/src/policy.rs)
  retains token-count events as well as newer token-usage records. This reader
  uses the former and does not add the latter again.
- The [initial thread schema](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/state/migrations/0001_threads.sql)
  and [model-column migration](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/state/migrations/0020_threads_model_reasoning_effort.sql)
  establish the `id`, `rollout_path`, `tokens_used`, `model` columns used here.
  The synthetic SQLite database contains only this consumed subset; the fixture
  likewise uses relevant public fields, not a complete producer-deserialization test.

### Standard/Fast attribution (request-based estimate)

Additional producer-source inspection uses the same pinned 0.153.4 commit:

- [config_types.rs, default request value](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/protocol/src/config_types.rs#L532)
  defines `default` as an explicit choice of no service tier (no model-default
  substitution). The Astra model catalog labels `priority` as Fast.
- [thread_settings.rs, apply/emit](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/session/thread_settings.rs#L86)
  emits a thread-owned snapshot after applying settings. Copied snapshots retain
  the original thread owner in the protocol; ownership must match the DB session.
- [turn_input.rs, turn start](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/session/turn_input.rs#L161)
  emits settings before spawning a new task. Its steering path documents that
  the active turn retains its old context while subsequent turns see updates.
- [step_settings.rs, resolved settings](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/session/step_settings.rs#L35)
  distinguishes configured choices from effective request values. Feature/model
  support can filter a configured tier; captured steps are not changed by later
  settings replacements. [turn.rs, request dispatch](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/session/turn.rs#L2262)
  uses the step's service tier for the request.

These are **request-setting estimates**, not confirmation of the backend's actual
processing tier or invoice. For an Astra-containing JSONL stream, the reader
requires an owned `thread_settings_applied` snapshot, an active `task_started`
(or `turn_started`) with a turn ID, and a matching `turn_context` model/turn ID.
The snapshot provider must be `openai`; nested collaboration-model metadata must
not contradict its model. Only explicit `service_tier="default"` selects Standard;
`"priority"` selects Fast at 2.5x. Missing, null, other or malformed tier values
remain unknown. Missing tier in a new snapshot does not inherit an earlier tier.
The reader intentionally does not assume that omitted/null means Standard even
though the producer may omit an unset optional field. Collaboration names and
model suffixes do not establish Astra's billing mode.

Settings are captured for each turn in file order. They never reprice earlier
facts. Matching turn completion clears the active context. Mid-turn settings
updates, overlapping turns, mismatched contexts/completions, foreign/unowned
snapshots and malformed records invalidate attribution. Subsequent deltas stay
`unpriced` until a safe new turn/context is established. Identical mid-turn
snapshot repetitions are also conservatively invalidated; the reader cannot
prove whether they are checkpoints or updates to an in-flight request.

Astra -> other-model -> Astra transitions are supported between completed turns
with consistent snapshots and contexts. A streaming pre-pass selects this guarded
path whenever Astra appears in the DB model or model/settings events, so the DB's
final model is not applied to earlier usage. An unresolved model is represented
by the existing `(unknown)` model, not guessed as Astra or the final DB model.
Known Astra with unknown mode also remains `unpriced`; its tokens are preserved
in `unpriced_tokens`, including after report/measure aggregation. Numeric zero
for that excluded estimate is not a free-usage claim. No JSON fields or protocol
versions change. Non-Astra-only streams retain the legacy reader path.

Cumulative per-channel delta calculation is unchanged. Cache writes and reasoning
breakdowns are not added as extra Codex charges; newer token-usage records are
not counted again alongside token_count. First deltas retain `first_event_delta`:
missing earlier history can still overstate tokens. Missing/corrupt token events,
concurrent file changes, alternate storage layouts and copied/forked history are
not fully reconstructed by this bounded reader. They are not real-log validated.
The standard/Fast synthetic fixture checks the supported readable JSONL shape,
not every storage mode or producer-deserialization requirement.

The snapshot does not prove the actual provider tier, billing route or contract.
API-key sessions need a separate explicit billing-route choice and API conditions;
provider `openai` alone is not such evidence. No API-specific tariff or new
billing-route contract is introduced here.

## Separate GPT-5.6 discrepancies (no rates changed)

Same official credit and USD sources/time as above. Triples are **uncached input /
cached input / output**, per 1M tokens:

| Model | Existing USD | Official USD | Existing credits | Official credits |
| --- | --- | --- | --- | --- |
| Sol | 5 / 0.50 / 30 | 4 / 0.40 / 20 | 125 / 12.5 / 750 | 100 / 10 / 500 |
| Terra | 2.50 / 0.25 / 15 | 2 / 0.20 / 12 | 62.5 / 6.25 / 375 | 50 / 5 / 300 |
| Luna | 1 / 0.10 / 6 | 0.20 / 0.02 / 1.20 | 25 / 2.5 / 150 | 5 / 0.5 / 30 |

The USD card also states GPT-5.6 Fast is 2.5x; existing family entries use 1.0.
Sol has promotional conditions through at least November 21, 2026, with different
eligibility wording for purchased credits and USD token usage. The retrieved
pages do not establish a precise historical start or every variant's Fast
eligibility. Preserve all existing entries, historical periods and multipliers;
a separate correction needs period/eligibility evidence. The old catalog note
about failed primary retrieval describes its original provenance, not this check.

## Verification and delivery

`tests/test_astra_pricing.py` covers exact per-channel standard/Fast/unknown-mode
prices, the observation boundary, unsupported IDs, catalog loading, synthetic
reader-to-aggregation, and isolated `measure/v1`. Its fixture is entirely
synthetic and copied to pytest temporary directories; no personal log path is used.

Run the focused checks from the repository root:

```bash
python3 -m pytest -q tests/test_astra_pricing.py
python3 -m agent_cost.cli rates validate
```

Local verification on Python 3.9.6: **177 tests passed**, including the existing
112 tests and 65 Astra cases. Prior tests that intentionally recorded incorrect
Fast/unknown behavior now assert corrected Fast pricing or unpriced preservation.
For the full suite, the default config path was redirected to an absent file
under a temporary directory before pytest ran, so tests that clear
`AGENT_COST_CONFIG` did not read a personal config. All 21 existing model entries
match the base catalog; 1,296 historical pricing comparisons across their period
boundaries, token kinds, modes and aliases match. Catalog and whitespace checks
pass. All fixtures are synthetic; this is not real-log validation.

Intentional output differences: an owned priority snapshot plus matching turn
now costs $305 / 7,625 credits for the six-million-token fixture (previously
$122 / 3,050). Missing/ambiguous Astra mode now produces zero priced cost and
preserved unpriced tokens instead of a Standard estimate. Safe model switches
use each turn's model rather than the DB's final model. Other catalog prices
and unrelated reader inputs remain unchanged.

Minimum release proposal: review this bounded change and its limitations, then
separately authorize a patch version (candidate `0.1.1`), build/test the wheel
outside the checkout with synthetic data, and publish only after approval.
No version bump or publication is part of this change. The README example in
PR #7 stays pinned to published `0.1.0`; a newer catalog naturally has a different
digest, so do not silently relabel that captured example.
