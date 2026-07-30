# agent-cost

Estimate how many tokens Claude Code and Codex CLI actually used, and roughly
what that cost, by reading the local logs those tools already write to your
machine.

## Quick start

```bash
pip install -e .          # or: pip install agent-cost once published
agent-cost doctor         # sanity-check log locations and the rate catalog
agent-cost report
```

`agent-cost report` scans `~/.claude/projects/**/*.jsonl` and
`~/.codex/state_5.sqlite` (+ its rollout files), turns every token-usage
event into a canonical "fact" (one model, one token kind, one timestamp,
one count), prices each fact against a bundled rate catalog, and prints an
aggregated table:

```
agent-cost report --since 2026-06-01 --until 2026-07-01 --format table
agent-cost report --group-by month,agent --format csv
agent-cost report --format json > usage.json
agent-cost export --agent claude --out facts.jsonl   # raw canonical facts
```

Useful flags: `--since`/`--until` (half-open window; date-only values are
interpreted in `--timezone`, default UTC), `--agent claude,codex`,
`--group-by month,agent,model,token-kind`, `--rates PATH` (use a different
catalog entirely, see below), `--exclude-archived` (Codex threads).

If another program wants to parse agent-cost's output for one or more
specific session ids, see `agent-cost measure` below rather than
scraping `report`.

## What this measures, and what it doesn't

agent-cost only reads data that is already on disk. It never talks to the
network, never calls `gh`, and never resolves branches or PRs.

- **Claude Code**: every `assistant` message's `usage` block is one billing
  event, attributed to the exact model on that event (a session that
  switches models mid-conversation is not folded into one "primary model").
  When Anthropic's prompt-cache TTL breakdown (5-minute vs 1-hour writes) is
  present in the log, it's used; otherwise the cache-write tokens are priced
  at the 5-minute rate as an explicit **lower bound** and flagged
  `lower_bound` rather than guessed at the (more expensive) 1-hour rate.
- **Codex CLI**: rollout files record a *cumulative* token count after each
  turn; agent-cost turns that into per-turn deltas. Codex does not expose a
  separate cache-write signal at all, so cache writes are never reported for
  Codex (not zero -- simply not observable, and left out of the row rather
  than implied). Codex's `output` fact is `output_tokens` alone: cross-checking
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
  `claude-opus-5`'s launch date could not be confirmed from an authoritative
  source, so its rate period's `effective_from` is a placeholder. `gpt-5.6`
  (Sol/Terra/Luna) credits could not be confirmed from the primary source
  (`help.openai.com`'s Codex rate card returns HTTP 403 to automated
  fetches); the values in the catalog come from several independent
  secondary sources that agree with each other and are internally
  consistent with `usd_per_credit`, but are not primary-source-verified --
  re-check them once the rate card is reachable. Update either via a
  custom `--rates` file if you have a confirmed number.

## Updating the rate catalog

Prices live in `agent_cost/rates.json`, not in code. It's a historical
catalog: each model can have several time-bounded rate periods, so a price
change is recorded as a new period rather than overwriting the old one (see
`claude-sonnet-5`'s launch-promo period for a worked example). Every catalog
carries a `catalog_version`, a list of `sources` (the pricing page a rate
came from), and is validated on load (no duplicate model keys or aliases,
no negative rates, no overlapping periods for the same model).

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
    "source_quality": { "ok": 41, "first_event_delta": 2 }
  }
}
```

Rows are grouped by agent/model/token-kind only -- `measure` never buckets
by month, since a query is already scoped to specific sessions. `total` is
the union of every requested `session_id` (not a global report), so it's
the number to attribute to whatever unit of work those sessions represent.
`data_quality.unpriced_tokens` and `.source_quality` are scoped to the
requested sessions; `.malformed_events`/`.skipped_files`/`.negative_deltas`
describe the health of the underlying log read within `--since`/`--until`
and are not attributable to one session.

## Privacy

agent-cost makes zero network calls. `agent-cost export`'s JSONL never
includes absolute file paths, rollout paths, prompt/message content, or git
branch names -- only the fields needed to reproduce a cost estimate:
`occurred_at_utc`, `agent`, `session_id`, `model_raw`, `model_key`,
`token_kind`, `tokens`, `mode`, and `source_quality` (a fixed-vocabulary
caveat about how that one fact was derived, e.g. `"ok"` or Codex's
`"first_event_delta"` -- never null).

## License

MIT. See [LICENSE](LICENSE).

---

## 日本語サマリ

`agent-cost` は Claude Code / Codex CLI がローカルに残すログ（`~/.claude/projects/**/*.jsonl`
と `~/.codex/state_5.sqlite` + rollout ファイル）だけを読み、トークン使用量とおおよそのコストを
見積もる CLI です。ネットワークアクセスは一切行いません。

- 集計の最小単位は「1 イベント = 1 モデル × 1 token 種別」の fact であり、session 単位でモデルを
  丸めません。Claude の prompt cache は TTL 内訳（5分/1時間）が取れればそれを使い、取れない場合は
  5分単価で **下限推計**（`lower_bound`）として明示します。Codex の cache write は観測不能なため
  出力しません（0 とは区別）。
- 出力フィールドは `estimated_cost_usd`（推計であることを明示）。Codex は `credits` も併記します。
  未知のモデル・単価表にない token 種別は `unpriced` として扱い、憶測の価格を出しません。
- 単価表 (`agent_cost/rates.json`) は履歴型カタログで、値上げは新しい期間として追加します。
  `--rates PATH` で別カタログに完全差し替えできます。
- `agent-cost measure --session-id ID [--session-id ID ...] --format json` は他プログラムから
  subprocess で叩くための機械可読な契約です（`protocol_version: "measure/v1"`）。指定した
  session_id が1件も見つからなくても終了コードは0（空集計として表現）、`--session-id` 未指定など
  の入力エラーのみ終了コード2です。
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
