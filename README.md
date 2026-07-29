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
  than implied).
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

## Privacy

agent-cost makes zero network calls. `agent-cost export`'s JSONL never
includes absolute file paths, rollout paths, prompt/message content, or git
branch names -- only the fields needed to reproduce a cost estimate
(timestamp, agent, session id, model, token kind, token count, mode).

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
- 破損したログ行、読めなくなったファイル、Codex の累積カウンタが逆行するケースなどは、すべて
  `data_quality` に件数として記録し、黙って丸めたり捨てたりしません。
