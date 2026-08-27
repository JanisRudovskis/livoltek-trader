"""Backtest the planner against real historical Nord Pool prices.

Answers the question the spec can only estimate: how often does a grid-charge
cycle actually clear the gate on real Latvian winter days, and how much did
the OLD (free-pairing) valuation overstate it?

PV is ignored — the PV-skip gate is evaluated separately and in deep winter it
never fires. This isolates the price side.

Usage:
    uv run python scripts/backtest_strategy.py 2026-01-01 2026-02-28
"""

from __future__ import annotations

import asyncio
import statistics as st
import sys
from datetime import date, timedelta

from livoltek_trader.config import get_settings
from livoltek_trader.elering import ElerinAPIError, fetch_day_ahead
from livoltek_trader.strategy import (
    TradingWindow,
    _build_cycle,
    _derive_drain_window,
    aggregate_hourly,
    plan_day,
)


def _old_algorithm_total(hourly, settings) -> tuple[int, float, float]:
    """Faithful replay of the pre-2026-08-27 valuation, for comparison.

    Free (charge, discharge) pairing, gross = spread x output, exclusion by
    shared clock hours only. This is what produced the inflated log numbers.

    Returns (cycles_chosen, claimed_total, honest_total) where honest_total
    re-values the SAME chosen charge windows against their real drain windows.
    That difference is what the old code actually did to the battery.
    """
    n = settings.hours_per_cycle
    out = settings.cycle_output_kwh
    wear = settings.wear_cost_per_cycle_eur
    gate = settings.min_net_profit_per_cycle_eur

    blocks = []
    for i in range(len(hourly) - n + 1):
        chunk = hourly[i : i + n]
        if (chunk[-1].start - chunk[0].start) != timedelta(hours=n - 1):
            continue
        blocks.append(
            (
                chunk[0].start,
                chunk[-1].start + timedelta(hours=1),
                sum(h.eur_per_kwh for h in chunk) / n,
            )
        )

    cands = []
    for c in blocks:
        for d in blocks:
            if d[0] < c[1]:
                continue
            net = (d[2] - c[2]) * out - wear
            if net >= gate:
                cands.append((net, c, d))
    cands.sort(key=lambda t: (-t[0], t[1][0], t[2][0]))

    price_by_hour = {h.start: h.eur_per_kwh for h in hourly}
    used: set = set()
    total, honest, count = 0.0, 0.0, 0
    for net, c, d in cands:
        if count >= settings.max_cycles_per_day:
            break
        hrs = set()
        for w in (c, d):
            h = w[0]
            while h < w[1]:
                hrs.add(h)
                h += timedelta(hours=1)
        if hrs.isdisjoint(used):
            used |= hrs
            total += net
            count += 1
            charge = TradingWindow(start=c[0], end=c[1], avg_eur_per_kwh=c[2])
            drain = _derive_drain_window(
                price_by_hour, charge, settings.battery_drain_hours
            )
            if drain is None:
                # Charge so late the battery drains into the next day; charge
                # cost is certain, delivery value is not. Count the cost only.
                honest -= (
                    settings.battery_capacity_kwh * c[2]
                    + settings.wear_cost_per_cycle_eur
                )
            else:
                honest += _build_cycle(charge, drain, settings).net_profit_eur
    return count, total, honest


async def main(argv: list[str]) -> int:
    if len(argv) >= 2:
        d0, d1 = date.fromisoformat(argv[0]), date.fromisoformat(argv[1])
    else:
        d0, d1 = date(2026, 1, 1), date(2026, 2, 28)

    settings = get_settings()
    short = settings.model_copy(update={"battery_drain_hours": 4})
    print(
        f"constants: bought {settings.battery_capacity_kwh:.2f} kWh, "
        f"delivered {settings.cycle_output_kwh:.2f} kWh, "
        f"drain {settings.battery_drain_hours} h, "
        f"gate {settings.min_net_profit_per_cycle_eur:.2f} EUR\n"
    )

    rows = []
    d = d0
    while d <= d1:
        try:
            periods = await fetch_day_ahead(d, settings=settings)
        except ElerinAPIError as exc:
            print(f"{d}: fetch failed — {exc}")
            d += timedelta(days=1)
            continue
        hourly = aggregate_hourly(periods)
        if len(hourly) < 20:
            d += timedelta(days=1)
            continue

        new = plan_day(hourly, d, settings)
        new4 = plan_day(hourly, d, short)
        old_n, old_total, old_honest = _old_algorithm_total(hourly, settings)

        prices = [h.eur_per_kwh for h in hourly]
        rows.append(
            {
                "day": d,
                "lo": min(prices),
                "hi": max(prices),
                "new_n": len(new.cycles),
                "new": new.total_net_profit_eur,
                "new4_n": len(new4.cycles),
                "new4": new4.total_net_profit_eur,
                "old_n": old_n,
                "old": old_total,
                "old_honest": old_honest,
            }
        )
        d += timedelta(days=1)
        await asyncio.sleep(0.15)

    if not rows:
        print("no data")
        return 1

    print(f"{'day':<12}{'spot lo-hi':<16}{'drain7':>14}{'drain4':>14}{'OLD':>14}")
    for r in rows:
        print(
            f"{r['day'].isoformat():<12}"
            f"{r['lo']:.3f}-{r['hi']:.3f}    "
            f"{r['new_n']}x {r['new']:>6.2f}    "
            f"{r['new4_n']}x {r['new4']:>6.2f}    "
            f"{r['old_n']}x {r['old']:>6.2f}"
        )

    n = len(rows)
    fired7 = sum(1 for r in rows if r["new_n"])
    fired4 = sum(1 for r in rows if r["new4_n"])
    tot7 = sum(r["new"] for r in rows)
    tot4 = sum(r["new4"] for r in rows)
    tot_old = sum(r["old"] for r in rows)
    print(f"\n{n} days")
    print(f"  drain=7: fired {fired7}/{n} days, total EUR {tot7:.2f} "
          f"({tot7/n*30:.2f}/month)")
    print(f"  drain=4: fired {fired4}/{n} days, total EUR {tot4:.2f} "
          f"({tot4/n*30:.2f}/month)")
    tot_old_honest = sum(r["old_honest"] for r in rows)
    old_cycles = sum(r["old_n"] for r in rows)
    new_cycles = sum(r["new_n"] for r in rows)
    print(f"  OLD code claimed:      total EUR {tot_old:.2f} "
          f"({tot_old/n*30:.2f}/month)")
    if tot7 > 0:
        print(f"  OLD overstatement vs drain=7: {tot_old/tot7:.1f}x")
    print(
        f"\n  OLD plan RE-VALUED honestly: EUR {tot_old_honest:.2f} "
        f"({tot_old_honest/n*30:.2f}/month)"
    )
    print(f"  cycles scheduled: OLD {old_cycles}, NEW {new_cycles} "
          f"(wear EUR {old_cycles*0.5:.2f} vs EUR {new_cycles*0.5:.2f})")
    print(
        f"  => the fix is worth EUR {(tot7 - tot_old_honest)/n*30:.2f}/month, "
        f"mostly by NOT trading"
    )
    daily = [r["new"] for r in rows if r["new_n"]]
    if daily:
        print(f"  on firing days: median EUR {st.median(daily):.2f}, "
              f"max EUR {max(daily):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
