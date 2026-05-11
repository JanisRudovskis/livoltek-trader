"""Smoke test the apply_schedule flow with a synthetic 2-cycle plan.

DOES NOT CLICK SAVE — only fills the form so the developer can eyeball the
result. Drops a 15s pause at the end so the form is visible.

Usage: uv run python scripts/livoltek_apply_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timedelta, timezone

from livoltek_trader.livoltek import LivoltekClient, LivoltekError
from livoltek_trader.strategy import CyclePair, DailyPlan, TradingWindow

UTC = timezone.utc


def _fake_plan() -> DailyPlan:
    """Build a 2-cycle plan with charge windows at 02-04 and 13-15 UTC."""
    today = date.today()
    def w(hr_start: int, hr_end: int, price: float) -> TradingWindow:
        return TradingWindow(
            start=datetime.combine(today, datetime.min.time(), tzinfo=UTC)
            + timedelta(hours=hr_start),
            end=datetime.combine(today, datetime.min.time(), tzinfo=UTC)
            + timedelta(hours=hr_end),
            avg_eur_per_kwh=price,
        )

    def cycle(
        c_start: int, c_end: int, c_price: float,
        d_start: int, d_end: int, d_price: float,
    ) -> CyclePair:
        return CyclePair(
            charge=w(c_start, c_end, c_price),
            discharge=w(d_start, d_end, d_price),
            gross_revenue_eur=(d_price - c_price) * 9.22,
            wear_cost_eur=0.50,
            net_profit_eur=(d_price - c_price) * 9.22 - 0.50,
        )

    return DailyPlan(
        target_date=today,
        cycles=[
            cycle(2, 4, 0.05, 18, 20, 0.30),
            cycle(13, 15, 0.08, 21, 23, 0.28),
        ],
        skipped_reason=None,
        total_net_profit_eur=4.20,
    )


async def main() -> int:
    plan = _fake_plan()
    print(f"Smoke plan: {len(plan.cycles)} cycles")
    for i, c in enumerate(plan.cycles, 1):
        print(
            f"  Cycle {i}: charge {c.charge.start.time()}-{c.charge.end.time()} "
            f"-> disch {c.discharge.start.time()}-{c.discharge.end.time()}"
        )
    try:
        async with LivoltekClient() as client:
            print("Logging in + navigating...")
            await client.login()
            await client.navigate_to_system_mode()
            print("Filling form (DRY-RUN — Save will NOT be clicked)...")
            await client.apply_schedule(plan, save=False)
            print("OK. Holding browser open 15s so you can inspect form.")
            await asyncio.sleep(15)
    except LivoltekError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
