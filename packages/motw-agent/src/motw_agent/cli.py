"""`motw-agent suggest|remind [--week YYYY-MM-DD] [--dry-run]` (k3s CronJobs, or by hand).

    op run --env-file=.env.op --env-file=.env.mt.op -- uv run motw-agent suggest --dry-run

Exit status: 0 for every expected outcome (posted, already picked, nothing eligible,
reminded, ...), 1 when MT or Slack failed, so the CronJob's Job shows the failure.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from sb_models import ModelConfigError, resolve

from motw_agent.mt import LiveMt, parse_week
from motw_agent.run import Config, Out, remind, suggest
from motw_agent.writer import build_writer
from sb_obs import setup_telemetry

log = logging.getLogger("motw_agent")
RUN_HINT = "op run --env-file=.env.op --env-file=.env.mt.op -- uv run motw-agent ..."


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="motw-agent", description="Match of the Week suggestions")
    sub = p.add_subparsers(dest="command", required=True)
    for name, help_ in (
        ("suggest", "post this week's candidates to the picks channel"),
        ("remind", "nudge approvers if the week is still unpicked"),
    ):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--week", help="any YYYY-MM-DD in the week (default: this week, Eastern)")
        s.add_argument("--dry-run", action="store_true", help="print Slack messages, post none")
    return p


async def _main(args: argparse.Namespace) -> int:
    cfg = Config.from_env()
    week = parse_week(args.week)
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    client = None
    if token:
        from slack_sdk.web.async_client import AsyncWebClient

        client = AsyncWebClient(token=token)
    out = Out(client, cfg.channel, dry_run=args.dry_run)
    writer = build_writer()
    if args.command == "suggest":
        outcome = await suggest(week, LiveMt(), writer, out)
    else:
        outcome = await remind(week, LiveMt(), writer, out, cfg.approvers)
    log.info("motw-agent %s: %s", args.command, outcome, extra={"week": week.isoformat()})
    return 1 if outcome == "error" else 0


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    telemetry = setup_telemetry("motw-agent")
    missing = (
        []
        if args.dry_run
        else [k for k in ("SLACK_BOT_TOKEN", "MOTW_SLACK_CHANNEL") if not os.environ.get(k)]
    )
    if not os.environ.get("MT_AGENT_MCP_TOKEN"):
        missing.append("MT_AGENT_MCP_TOKEN")
    if missing:
        raise SystemExit(f"missing env: {', '.join(missing)}. Run via: {RUN_HINT}")
    try:
        resolve("motw-agent")
    except ModelConfigError as exc:
        raise SystemExit(f"{exc}. Run via: {RUN_HINT}") from exc
    try:
        code = asyncio.run(_main(args))
    except Exception:
        log.exception("motw-agent %s failed", args.command)
        code = 1
    finally:
        telemetry.shutdown()  # a batch job: flush spans and the RunRecord before exit
    raise SystemExit(code)
