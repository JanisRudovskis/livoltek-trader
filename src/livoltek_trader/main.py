"""Daily entry point: fetch prices and PV forecast, plan, notify, optionally apply.

Usage:
    livoltek-trader                       # dry-run for TOMORROW (Riga local)
    livoltek-trader --execute             # full pipeline with portal Save
    livoltek-trader --date 2026-05-12     # plan for a specific date

Default target is the next calendar day in Riga local time — we run the
cron at 22:30 Riga the evening before, so by then Nord Pool has published
tomorrow's prices and the trader can write the schedule to the inverter.

Default is dry-run for safety. `--execute` is required to actually push the
schedule to the inverter.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog

RIGA_TZ = ZoneInfo("Europe/Riga")

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
from livoltek_trader.telemetry import DailyTotals, trailing_mean_load_kwh

log = structlog.get_logger(__name__)

LOAD_LOOKBACK_DAYS = 7
"""Days of measured household load to average for the PV gate.

Seven smooths out a single anomalous day (an empty house, a party) while still
tracking the seasonal trend that a fixed constant cannot. Measured against 149
days of history a 3-day mean scored the same as a 7-day one, so the exact
number is not sensitive; what matters is that it is measured at all.
"""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="livoltek-trader")
    p.add_argument(
        "--date",
        help="Target date YYYY-MM-DD (default: tomorrow Riga-local)",
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="Push schedule to the inverter. Without this flag, the run is "
        "dry-run only (plan computed, ntfy sent, no portal contact).",
    )
    return p.parse_args(argv)


def _default_target_date() -> date:
    """Tomorrow's calendar date in Riga local time.

    The cron fires at 22:30 Riga (~19:30 UTC summer). By then Nord Pool
    has published the next day's prices, so we plan for tomorrow. Use
    Riga local — not UTC — so a 22:30 Riga run during DST doesn't
    accidentally pick "today" via UTC's rollover lagging Riga's.
    """
    return (datetime.now(RIGA_TZ).date() + timedelta(days=1))


async def _try_notify(ntfy: NtfyClient, *args, **kwargs) -> None:
    try:
        await ntfy.send(*args, **kwargs)
    except NtfyError as exc:
        cause = exc.__cause__
        log.error(
            "main.ntfy_failed",
            error=str(exc),
            cause_type=type(cause).__name__ if cause else None,
            cause_repr=repr(cause) if cause else None,
        )


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    target_date = (
        date.fromisoformat(args.date) if args.date else _default_target_date()
    )
    # Log the hardware constants that actually took effect. They are measured
    # values living as defaults in config.py, but pydantic-settings lets any
    # of them be overridden by an environment variable on the host. A stale
    # override silently changes what the planner considers profitable — on
    # 2026-09-01 Railway still carried BATTERY_CAPACITY_KWH=10.24 from an old
    # .env.example, pinning the planner to pre-measurement numbers. Printing
    # them makes that drift obvious in the first line of every run.
    log.info(
        "main.start",
        target_date=target_date.isoformat(),
        execute=bool(args.execute),
        battery_capacity_kwh=settings.battery_capacity_kwh,
        cycle_output_kwh=round(settings.cycle_output_kwh, 3),
        battery_drain_hours=settings.battery_drain_hours,
        min_net_profit_eur=settings.min_net_profit_per_cycle_eur,
    )

    ntfy = NtfyClient(settings)

    # An Open-Meteo outage must not cost the whole night's run, but it must not
    # be planned through either: without PV we cannot tell whether the sun will
    # fill the battery for free, and paying for a grid charge that displaces
    # nothing is the expensive mistake. So the run continues and `plan_day`
    # suppresses cycles, leaving the inverter on plain Self-use.
    pv = None
    pv_failed = False
    try:
        pv = await fetch_pv_forecast(target_date, settings=settings)
    except OpenMeteoAPIError as exc:
        pv_failed = True
        log.warning(
            "main.pv_forecast_failed_skipping_cycles",
            error=str(exc),
            error_type=type(exc).__name__,
            cause_type=type(exc.__cause__).__name__ if exc.__cause__ else None,
            cause_repr=repr(exc.__cause__) if exc.__cause__ else None,
        )

    # Prices are not optional — without them there is nothing to plan.
    try:
        periods = await fetch_day_ahead(target_date, settings=settings)
    except ElerinAPIError as exc:
        log.error("main.fetch_failed", error=str(exc))
        title, body, prio = format_error_message("fetch", exc)
        await _try_notify(ntfy, body, title=title, priority=prio, tags=["warning"])
        return 1

    # Read-only pass over the portal's own daily table. Two things come from
    # it: the load figure the PV gate should use instead of a hard-coded annual
    # guess, and the actuals line that goes into the notification. Both are
    # nice-to-have, so any failure here degrades to configured defaults rather
    # than costing the night's schedule. Kept as a SEPARATE browser session
    # from the write path so a read problem can never disturb the write.
    totals: list[DailyTotals] = []
    try:
        async with LivoltekClient(settings) as reader:
            await reader.login()
            totals = await reader.read_daily_totals(
                target_date - timedelta(days=LOAD_LOOKBACK_DAYS + 1),
                target_date - timedelta(days=1),
            )
    except Exception as exc:  # noqa: BLE001 — Playwright raises broadly
        log.warning(
            "main.daily_totals_read_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )

    measured_load = trailing_mean_load_kwh(totals, days=LOAD_LOOKBACK_DAYS)
    latest = totals[-1] if totals else None
    log.info(
        "main.measured_load",
        trailing_mean_kwh=round(measured_load, 2) if measured_load else None,
        configured_kwh=settings.expected_daily_load_kwh,
        days_read=len(totals),
    )

    hourly = aggregate_hourly(periods)
    plan = plan_day(
        hourly,
        target_date,
        settings=settings,
        pv_forecast=pv,
        pv_forecast_failed=pv_failed,
        measured_daily_load_kwh=measured_load,
    )
    log.info(
        "main.plan_ready",
        cycles=len(plan.cycles),
        net=plan.total_net_profit_eur,
        skipped=plan.skipped_reason,
    )

    title, body, tags = format_plan_message(
        plan,
        pv_forecast=pv,
        hourly_prices=hourly,
        settings=settings,
        yesterday=latest,
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
