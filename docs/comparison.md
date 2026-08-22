# Choosing by use case and trust model

This comparison is about fit, not a feature-count ranking. It was checked against the linked
public documentation and manifests on 2026-08-23; capabilities and packaging can change.

## Short answer

| If you need... | A useful starting point |
|---|---|
| Broad reporting across many coding agents from one CLI | [ccusage](https://github.com/ccusage/ccusage) |
| Rich local charts, replay, or project/tool exploration | [token-tracker](https://github.com/JedIV/token-tracker), [claude-usage](https://github.com/phuryn/claude-usage), or [AgentMeter](https://github.com/LyleMi/AgentMeter) |
| Fleet-level metrics, logs, and traces | [agent-observability](https://github.com/KB1SLN-Labs/agent-observability) or another OpenTelemetry stack |
| A small subprocess contract for custom accounting and task workflows | `agent-cost` |

These categories overlap. The right choice depends on which party may read the logs, whether
runtime price lookup is acceptable, how unsupported prices should behave, and whether another
system already owns the session-to-task mapping.

## Focused comparison

| Tool | Primary shape | Local/network model | Pricing uncertainty | Machine use and attribution |
|---|---|---|---|---|
| `agent-cost` | Claude Code + Codex CLI accounting CLI | Reads local logs; no runtime network calls; zero runtime dependencies | `unpriced` and `lower_bound` are explicit row states; rate catalog version and SHA-256 are emitted | Table/CSV/JSON/JSONL; `measure/v1` accepts an explicit session set. The caller, not agent-cost, binds those sessions to a task |
| [ccusage](https://github.com/ccusage/ccusage) | Broad multi-agent CLI and status-line reporting | Local logs; [pricing code](https://github.com/ccusage/ccusage/blob/main/rust/crates/ccusage-core/src/pricing.rs) supports remote price updates and an offline embedded snapshot | Its [cost modes](https://github.com/ccusage/ccusage/blob/main/docs/guide/cost-modes.md) support different calculation/display needs; missing prices are warned about and excluded | JSON plus daily, weekly, monthly, session, project, and instance views |
| [token-tracker](https://github.com/JedIV/token-tracker) | Local Claude/Codex web dashboard backed by SQLite | Its README states that data stays on the machine; installation is a cloned Python application | Its [pricing lookup](https://github.com/JedIV/token-tracker/blob/main/tracker/pricing.py) falls back to the `_default` entry in the [price catalog](https://github.com/JedIV/token-tracker/blob/main/prices.json), favoring dashboard coverage | Local JSON API with project, session, subagent, entrypoint, and MCP-oriented analysis |
| [AgentMeter](https://github.com/LyleMi/AgentMeter) | Multi-agent local Web UI and TUI | Packaged local binary; project states no proxy, cloud service, or telemetry | Its [pricing documentation](https://github.com/LyleMi/AgentMeter/blob/main/docs/pricing-sources.md) describes explicit unpriced handling | Session/project/source views and local APIs; suited to interactive audit and privacy views |
| [agent-observability](https://github.com/KB1SLN-Labs/agent-observability) | Self-hosted OpenTelemetry, Prometheus, Loki, Tempo, and Grafana stack | Local/self-hosted services; access to collected prompts and logs is part of the operator's trust boundary | Pricing is a dashboard concern rather than a versioned accounting-output contract | Stronger fit for team/fleet metrics, logs, traces, skills, MCP, and effort analysis |

Other useful approaches include [codex-usage-tracker](https://github.com/CasperKristiansson/codex-usage-tracker)
for a Codex-focused CLI plus dashboard, and [codex-usage](https://github.com/hashmil/codex-usage)
for a small local Codex-only report. Their narrower or richer scopes may be a better match than
agent-cost for a given workflow.

## Why agent-cost draws a narrower boundary

The project was not created because token trackers or dashboards were unknown. Similar tools
were evaluated, and an earlier internal approach used Notion for comparable visualization.
The constraint in operational use was different: keep the runtime supply-chain surface small,
send no log data outside the machine, preserve custom metrics, and let an explicit workflow own
task attribution.

That leads to a layered design:

```text
local observations
  -> auditable normalized facts
  -> explicit pricing status
  -> caller-selected sessions
  -> task-attribution policy
  -> optional dashboard / Notion / spec-lane
```

Dashboards remain useful above this layer. `agent-cost` is the accounting primitive underneath
when the measurement boundary itself needs to be inspectable and replaceable.

## Important boundaries

- `estimated_cost_usd` is a list-price estimate, not an invoice or billing record.
- `agent-cost` does not infer that a branch, PR, time window, or session belongs to a task.
- A numeric `0.0` beside `pricing_status: unpriced` is not a zero-cost claim; inspect
  `unpriced_tokens` and `data_quality`.
- Zero runtime dependencies and zero runtime network calls reduce a trust surface; they do not
  eliminate the need to trust the package, Python runtime, operating system, and source logs.
