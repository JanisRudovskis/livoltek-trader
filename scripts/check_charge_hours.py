"""Which hours does the planner actually charge at, over a price period?

Answers "does it cover the morning peak / the evening peak?" without guessing.
Run it over a winter range and a summer range and compare — the planner picks
completely different hours in each, because the price shape is different:

    Jan-Feb 2026 : 03h x6, 04h x10, 05h x12 — 29 of 30 cycles cover 06-09h
    September    : 14-15h — covers the evening instead

Neither is a setting; both fall out of which windows score best. In winter the
cheap hours are wind-driven nights, so charging then to cover the morning peak
pays. In September the cheap hours are solar midday, and night and morning are
priced together, so there is nothing to arbitrage before the morning peak.

Usage:
    uv run python scripts/check_charge_hours.py 2026-01-01 2026-02-28
"""
import asyncio, collections
from datetime import date, timedelta
from zoneinfo import ZoneInfo
from livoltek_trader.config import get_settings
from livoltek_trader.elering import fetch_day_ahead
from livoltek_trader.strategy import aggregate_hourly, plan_day
RIGA = ZoneInfo("Europe/Riga")

async def main(argv):
    s = get_settings()
    d0 = date.fromisoformat(argv[0]) if len(argv) >= 2 else date(2026, 1, 1)
    d1 = date.fromisoformat(argv[1]) if len(argv) >= 2 else date(2026, 2, 28)
    hours = collections.Counter()
    covers_morning = days = fired = 0
    d = d0
    while d <= d1:
        try:
            hourly = aggregate_hourly(await fetch_day_ahead(d, settings=s))
        except Exception:
            d += timedelta(days=1); continue
        if len(hourly) >= 20:
            days += 1
            p = plan_day(hourly, d, settings=s, pv_forecast=None)
            if p.cycles: fired += 1
            for c in p.cycles:
                h = c.charge.start.astimezone(RIGA).hour
                hours[h] += 1
                # does its drain window span any of 06:00-09:00 local?
                ds = c.discharge.start.astimezone(RIGA)
                de = c.discharge.end.astimezone(RIGA)
                span = {(ds + timedelta(hours=k)).hour
                        for k in range(int((de-ds).total_seconds()//3600))}
                if span & {6, 7, 8, 9}:
                    covers_morning += 1
        d += timedelta(days=1)
        await asyncio.sleep(0.05)
    print(f"{d0} .. {d1}: {days} days, {fired} with a cycle")
    print("charge start hour (Riga)  ->  times chosen")
    for h in sorted(hours):
        print(f"   {h:02d}h  {'#'*hours[h]} ({hours[h]})")
    print(f"\ncycles whose drain window covers 06-09h: {covers_morning}")

import sys
raise SystemExit(asyncio.run(main(sys.argv[1:])) or 0)
