"""Daily entry point: fetch prices and PV forecast, plan, notify, optionally apply.

Usage:
    livoltek-trader                       # dry-run for today (compute + notify, no portal write)
    livoltek-trader --execute             # full pipeline with portal Save
    livoltek-trader --date 2026-05-12     # plan for a specific date

Default is dry-run for safety. `--execute` is required to actually push the
schedule to the inverter.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date

import structlog

from livoltek_trader.config import Settings, get_settings
from livoltek_trader.elering import ElerinAPIError, fetch_day_ahead
from livoltek_trader.livoltek import LivoltekClient
from livoltek_trader.notify import (
    NtfyClient,
    NtfyError,
    format_error_message,
    format_plan_message,
)
from livoltek_trader.solar import OpenMeteoAPIError, fetch_pv_forecast
from livoltek_trader.strategy import aggregate_hourly, plan_day

log = structlog.get_logger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="livoltek-trader")
    p.add_argument(
        "--date",
        help="Target date YYYY-MM-DD (default: today)",
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="Push schedule to the inverter. Without this flag, the run is "
        "dry-run only (plan computed, ntfy sent, no portal contact).",
    )
    return p.parse_args(argv)


async def _try_notify(ntfy: NtfyClient, *args, **kwargs) -> None:
    try:
        await ntfy.send(*args, **kwargs)
    except NtfyError as exc:
        log.error("main.ntfy_failed", error=str(exc))


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    target_date = date.fromisoformat(args.date) if args.date else date.today()
    log.info(
        "main.start",
        target_date=target_date.isoformat(),
        execute=bool(args.execute),
    )

    ntfy = NtfyClient(settings)

    try:
        pv = await fetch_pv_forecast(target_date, settings=settings)
        periods = await fetch_day_ahead(target_date, settings=settings)
    except (ElerinAPIError, OpenMeteoAPIError) as exc:
        log.error("main.fetch_failed", error=str(exc))
        title, body, prio = format_error_message("fetch", exc)
        await _try_notify(ntfy, body, title=title, priority=prio, tags=["warning"])
        return 1

    hourly = aggregate_hourly(periods)
    plan = plan_day(hourly, target_date, settings=settings, pv_forecast=pv)
    log.info(
        "main.plan_ready",
        cycles=len(plan.cycles),
        net=plan.total_net_profit_eur,
        skipped=plan.skipped_reason,
    )

    title, body, tags = format_plan_message(
        plan, pv_forecast=pv, hourly_prices=hourly, settings=settings
    )
    if not args.execute:
        title = f"[DRY-RUN] {title}"
    await _try_notify(ntfy, body, title=title, tags=tags)

    if not args.execute:
        log.info("main.dry_run_complete")
        return 0

    try:
        async with LivoltekClient(settings) as client:
            await client.login()
            await client.navigate_to_system_mode()
            await client.apply_schedule(plan, save=True)
    except Exception as exc:
        # Catch broadly: Playwright timeouts, network errors, etc. inherit from
        # Exception but NOT from LivoltekError. Without this catch they would
        # escape asyncio.run(), kill the cron with no ntfy, and leave only a
        # bare "app crashed" line in Railway logs.
        log.exception(
            "main.portal_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        title, body, prio = format_error_message("portal", exc)
        await _try_notify(ntfy, body, title=title, priority=prio, tags=["warning"])
        return 1

    log.info("main.execute_complete")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    return asyncio.run(_run(args, settings))


if __name__ == "__main__":
    sys.exit(main())
