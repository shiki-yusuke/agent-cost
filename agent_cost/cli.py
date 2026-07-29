"""Command-line entry point: ``agent-cost report / export / rates / doctor``."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from . import __version__
from .aggregate import DataQuality, build_rows, filter_facts
from .config import load_config
from .rates import RatesValidationError, load_rates
from .readers import claude as claude_reader
from .readers import codex as codex_reader
from .renderers import render_csv, render_json, render_table


def _parse_window_bound(value: Optional[str], tz: ZoneInfo) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"[error] invalid date/time: {value!r} ({exc})")
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    # Date-only (or naive datetime) input is interpreted in --timezone.
    return dt.replace(tzinfo=tz).astimezone(timezone.utc)


def _collect_facts(config, *, agents: set, exclude_archived: bool):
    facts: list = []
    dq = DataQuality()

    if "claude" in agents:
        result = claude_reader.read_claude_facts(config.claude_projects_dir)
        facts.extend(result.facts)
        dq.malformed_events += result.malformed_events
        dq.skipped_files += result.skipped_files

    if "codex" in agents and config.codex_db_path.exists():
        result = codex_reader.read_codex_facts(
            config.codex_db_path,
            config.codex_home,
            include_archived=not exclude_archived,
        )
        facts.extend(result.facts)
        dq.malformed_events += result.malformed_events
        dq.skipped_files += result.skipped_files
        dq.negative_deltas += result.negative_deltas

    return facts, dq


def cmd_report(args) -> int:
    config = load_config()
    tz = ZoneInfo(args.timezone)
    since = _parse_window_bound(args.since, tz)
    until = _parse_window_bound(args.until, tz)
    agents = set(args.agent.split(",")) if args.agent else {"claude", "codex"}
    group_by = tuple(args.group_by.split(",")) if args.group_by else (
        "month",
        "agent",
        "model",
        "token-kind",
    )

    try:
        catalog = load_rates(Path(args.rates) if args.rates else None)
    except RatesValidationError as exc:
        print(f"[error] rates catalog invalid: {exc}", file=sys.stderr)
        return 2

    facts, dq = _collect_facts(config, agents=agents, exclude_archived=args.exclude_archived)
    facts = list(filter_facts(facts, since_utc=since, until_utc=until, agents=agents))
    rows, agg_dq = build_rows(facts, catalog, group_by=group_by, timezone_name=args.timezone)
    dq.unpriced_tokens = agg_dq.unpriced_tokens

    payload = {
        "schema_version": "1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
        },
        "timezone": args.timezone,
        "rates": {"catalog_version": catalog.catalog_version, "sha256": catalog.sha256},
        "group_by": list(group_by),
        "data_quality": dq.to_dict(),
        "rows": [r.to_dict() for r in rows],
    }

    renderer = {"table": render_table, "csv": render_csv, "json": render_json}[args.format]
    print(renderer(payload))
    return 0


def cmd_export(args) -> int:
    config = load_config()
    tz = ZoneInfo(args.timezone)
    since = _parse_window_bound(args.since, tz)
    until = _parse_window_bound(args.until, tz)
    agents = set(args.agent.split(",")) if args.agent else {"claude", "codex"}

    facts, _dq = _collect_facts(config, agents=agents, exclude_archived=False)
    facts = list(filter_facts(facts, since_utc=since, until_utc=until, agents=agents))

    out = open(args.out, "w") if args.out else sys.stdout
    try:
        for f in facts:
            record = {
                "occurred_at_utc": f.occurred_at_utc.isoformat(),
                "agent": f.agent,
                "session_id": f.session_id,
                "model_raw": f.model_raw,
                "model_key": f.model_key,
                "token_kind": f.token_kind,
                "tokens": f.tokens,
                "mode": f.mode,
                "source_quality": f.source_quality,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
    finally:
        if args.out:
            out.close()
    return 0


def cmd_rates_show(args) -> int:
    try:
        catalog = load_rates(Path(args.rates) if args.rates else None)
    except RatesValidationError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    if args.model:
        resolved = catalog.resolve_model_key(args.model)
        if not resolved:
            print(f"[unpriced] no rate entry for {args.model!r}")
            return 0
        entry = catalog.models[resolved]
        print(f"model_key: {entry.model_key}  (queried: {args.model})")
        print(f"aliases: {list(entry.aliases)}")
        print(f"fast_multiplier: {entry.fast_multiplier}")
        for period in entry.rates:
            until = period.effective_until.isoformat() if period.effective_until else "(open)"
            print(f"  [{period.rate_id}] {period.effective_from.isoformat()} .. {until}")
            for field_name, value in period.values.items():
                print(f"      {field_name}: {value if value is not None else '(unpriced)'}")
        return 0

    print(f"catalog_version: {catalog.catalog_version}")
    print(f"currency: {catalog.currency}  unit: {catalog.unit}  usd_per_credit: {catalog.usd_per_credit}")
    print(f"models ({len(catalog.models)}):")
    for key in sorted(catalog.models):
        print(f"  - {key}")
    return 0


def cmd_rates_validate(args) -> int:
    path = Path(args.path) if args.path else None
    try:
        catalog = load_rates(path)
    except RatesValidationError as exc:
        print(f"[invalid] {exc}", file=sys.stderr)
        return 1
    print(
        f"[ok] {path or '(packaged default)'}: catalog_version={catalog.catalog_version}, "
        f"{len(catalog.models)} models, sha256={catalog.sha256}"
    )
    return 0


def cmd_doctor(_args) -> int:
    config = load_config()
    ok = True

    print("agent-cost doctor")
    print(f"  version: {__version__}")

    claude_dir = config.claude_projects_dir
    if claude_dir.exists():
        count = sum(1 for _ in claude_dir.glob("*/*.jsonl"))
        print(f"  [ok] Claude projects dir: {claude_dir} ({count} session files)")
    else:
        print(f"  [warn] Claude projects dir not found: {claude_dir}")

    codex_db = config.codex_db_path
    if codex_db.exists():
        print(f"  [ok] Codex state DB found: {codex_db}")
        try:
            snapshot = codex_reader.snapshot_db(codex_db)
            try:
                threads = codex_reader.fetch_threads(snapshot)
                print(f"  [ok] Codex threads readable via sqlite3 ({len(threads)} threads)")
            finally:
                snapshot.unlink(missing_ok=True)
        except Exception as exc:  # surfaced as a doctor finding, not a crash
            ok = False
            print(f"  [error] Codex state DB read failed: {exc}")
    else:
        print(f"  [warn] Codex state DB not found: {codex_db}")

    try:
        catalog = load_rates()
        print(f"  [ok] rates.json valid: catalog_version={catalog.catalog_version}, {len(catalog.models)} models")
    except RatesValidationError as exc:
        ok = False
        print(f"  [error] rates.json invalid: {exc}")

    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-cost",
        description=(
            "Estimate AI coding agent token usage and cost from local logs "
            "(Claude Code, Codex CLI). Fully offline; makes no network calls."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_report = sub.add_parser("report", help="Aggregate usage and estimated cost")
    p_report.add_argument("--since", help="ISO date/datetime, inclusive")
    p_report.add_argument("--until", help="ISO date/datetime, exclusive")
    p_report.add_argument("--timezone", default="UTC", help="IANA zone for date-only input and month grouping")
    p_report.add_argument("--agent", help="comma-separated: claude,codex")
    p_report.add_argument("--group-by", help="comma-separated: month,agent,model,token-kind")
    p_report.add_argument("--format", choices=["table", "csv", "json"], default="table")
    p_report.add_argument("--rates", help="path to a rates.json that fully replaces the packaged catalog")
    p_report.add_argument("--exclude-archived", action="store_true", help="exclude archived Codex threads")
    p_report.set_defaults(func=cmd_report)

    p_export = sub.add_parser("export", help="Export canonical facts as JSONL")
    p_export.add_argument("--agent", help="comma-separated: claude,codex")
    p_export.add_argument("--since")
    p_export.add_argument("--until")
    p_export.add_argument("--timezone", default="UTC")
    p_export.add_argument("--out", help="output path (default: stdout)")
    p_export.set_defaults(func=cmd_export)

    p_rates = sub.add_parser("rates", help="Inspect or validate a rates catalog")
    rates_sub = p_rates.add_subparsers(dest="rates_command", required=True)
    p_rates_show = rates_sub.add_parser("show")
    p_rates_show.add_argument("--model")
    p_rates_show.add_argument("--rates")
    p_rates_show.set_defaults(func=cmd_rates_show)
    p_rates_validate = rates_sub.add_parser("validate")
    p_rates_validate.add_argument("path", nargs="?")
    p_rates_validate.set_defaults(func=cmd_rates_validate)

    p_doctor = sub.add_parser("doctor", help="Check environment and rates catalog health")
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
