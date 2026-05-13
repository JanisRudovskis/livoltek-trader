"""Debug-only: fill form with today's plan, click Save Params, capture screenshots.

Captures three screenshots:
  1. After form fill, BEFORE Save click — to confirm form state
  2. ~0.5s after Save click — to catch any confirmation dialog
  3. ~8s after Save click — to see final state (toast/dialog/idle)

The script does NOT interact with any popup that appears, so if a confirmation
modal is in play, no actual write commits. If no popup appears, the Save WILL
commit normally and the inverter will receive the schedule.

Usage: uv run python scripts/livoltek_debug_save.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from livoltek_trader.livoltek import LivoltekClient, LivoltekError
from livoltek_trader.strategy import CyclePair, DailyPlan, TradingWindow

UTC = timezone.utc
OUT = Path("data/exploration")


def _today_one_cycle_plan() -> DailyPlan:
    today = date.today()
    base = datetime.combine(today, datetime.min.time(), tzinfo=UTC)
    charge = TradingWindow(
        start=base + timedelta(hours=11),  # 11:00 UTC = 14:00 Riga
        end=base + timedelta(hours=13),
        avg_eur_per_kwh=0.0812,
    )
    discharge = TradingWindow(
        start=base + timedelta(hours=18),  # 18:00 UTC = 21:00 Riga
        end=base + timedelta(hours=20),
        avg_eur_per_kwh=0.1649,
    )
    cycle = CyclePair(
        charge=charge,
        discharge=discharge,
        gross_revenue_eur=0.77,
        wear_cost_eur=0.50,
        net_profit_eur=0.27,
    )
    return DailyPlan(
        target_date=today,
        cycles=[cycle],
        skipped_reason=None,
        total_net_profit_eur=0.27,
    )


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    plan = _today_one_cycle_plan()
    print(f"Debug plan: 1 cycle Wed 14:00-16:00 Riga, today is {date.today()}")
    try:
        async with LivoltekClient() as client:
            print("Login + nav...")
            await client.login()
            await client.navigate_to_system_mode()
            print("Filling form (save=False)...")
            await client.apply_schedule(plan, save=False)

            page = client.page
            await page.screenshot(path=str(OUT / "save-01-before-click.png"), full_page=True)
            print("Screenshot 1 saved: form filled, BEFORE Save click")

            print("Clicking Save Params...")
            await page.get_by_role("button", name="Save Params.").first.click()
            await asyncio.sleep(0.5)
            await page.screenshot(path=str(OUT / "save-02-immediately-after.png"), full_page=True)
            print("Screenshot 2 saved: immediately after click")

            await asyncio.sleep(8)
            await page.screenshot(path=str(OUT / "save-03-after-8s.png"), full_page=True)
            print("Screenshot 3 saved: 8s after click")

            print("Exit. NO additional clicks were made.")
    except LivoltekError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
