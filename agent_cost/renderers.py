"""Render a report payload as a table, CSV, or JSON string."""

from __future__ import annotations

import csv
import io
import json

_COLUMNS = (
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
)
_HEADERS = (
    "Month",
    "Agent",
    "Model",
    "Token Kind",
    "Tokens",
    "Priced",
    "Unpriced",
    "Est. Cost (USD)",
    "Credits",
    "Status",
)


def _cell(row: dict, column: str) -> str:
    value = row.get(column)
    if value is None:
        return "-"
    if column == "estimated_cost_usd":
        return f"{value:.4f}"
    if column == "credits":
        return f"{value:.2f}" if value else "-"
    return str(value)


def render_table(payload: dict) -> str:
    rows = payload["rows"]
    formatted = [[_cell(row, c) for c in _COLUMNS] for row in rows]
    widths = [len(h) for h in _HEADERS]
    for frow in formatted:
        for i, cell in enumerate(frow):
            widths[i] = max(widths[i], len(cell))

    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(_HEADERS))]
    lines.append("  ".join("-" * w for w in widths))
    for frow in formatted:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(frow)))

    total_tokens = sum((r.get("tokens") or 0) for r in rows)
    total_cost = sum((r.get("estimated_cost_usd") or 0) for r in rows)
    lines.append("")
    lines.append(f"Total tokens: {total_tokens:,}   Total estimated cost: ${total_cost:.4f}")

    rates = payload.get("rates") or {}
    sha = rates.get("sha256") or ""
    lines.append(f"Rates catalog: {rates.get('catalog_version')}  (sha256={sha[:12]}...)")

    dq = payload.get("data_quality") or {}
    if any(dq.values()):
        lines.append(
            "Data quality: "
            f"malformed_events={dq.get('malformed_events', 0)}  "
            f"skipped_files={dq.get('skipped_files', 0)}  "
            f"negative_deltas={dq.get('negative_deltas', 0)}  "
            f"unpriced_tokens={dq.get('unpriced_tokens', 0)}"
        )
    return "\n".join(lines)


def render_csv(payload: dict) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_COLUMNS)
    writer.writeheader()
    for row in payload["rows"]:
        writer.writerow({c: row.get(c) for c in _COLUMNS})
    return buf.getvalue()


def render_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
