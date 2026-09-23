"""Value every candidate charge window by SIMULATING the measured hourly load.

The planner values a cycle against the mean price of a fixed `battery_drain_hours`
window. That constant is a single number for something that varies about 6x —
overnight load runs ~0.3 kW (the battery would last 20+ hours) while a winter
afternoon runs ~2.5 kW (3.6 hours). This script instead walks the real hourly
load forward and prices every kWh the battery actually delivers, which is what
the cycle is really worth.

Used on 2026-09-23 to settle "why no charge before the morning peak?", when the
night (EUR 0.1421) was genuinely cheaper than the morning peak (EUR 0.2817):

  00-02h  delivers 8.89 kWh, EXHAUSTED BY 13h, realised EUR 0.2228/kWh -> -0.304
  15-17h  delivers 8.89 kWh, lasts to    22h, realised EUR 0.2780/kWh -> +0.376

The battery does not wait for the peak. A night charge is spent on the
EUR 0.155-0.175 hours between 02:00 and 06:00; only 0.36 kWh reaches the
EUR 0.28 hour. So the test is not "is the night cheaper than the peak" but
"is it cheaper than the AVERAGE of the hours the battery discharges into",
by enough to cover the ~24% round-trip loss and the margin on both legs.

The fixed-window model agreed with the simulation here (EUR 0.184 vs EUR 0.173,
both below break-even), so it is not yet clear the simulation changes decisions.
Validating that needs a winter load profile — see the December checklist.

Usage: uv run python scripts/sim_charge_windows.py [YYYY-MM-DD]
Needs data/telemetry/five_min.jsonl covering the day and some days around it.
"""
import asyncio, json, pathlib, collections, statistics as st
from datetime import date
from zoneinfo import ZoneInfo
from livoltek_trader.config import get_settings
from livoltek_trader.elering import fetch_day_ahead
from livoltek_trader.strategy import aggregate_hourly
RIGA=ZoneInfo("Europe/Riga"); STEP=5/60; M=0.05
def n(v): return v if isinstance(v,(int,float)) else None

rows=[json.loads(l) for l in pathlib.Path("data/telemetry/five_min.jsonl")
      .read_text(encoding="utf-8").splitlines() if l.strip()]
perday=collections.defaultdict(lambda: collections.defaultdict(float))
for r in rows:
    ts=str(r.get("updateDate",""))
    if len(ts)<16: continue
    perday[ts[:10]][int(ts[11:13])] += (n(r.get("Load Power")) or 0)*STEP

import sys
TODAY = sys.argv[1] if len(sys.argv) > 1 else "2026-09-23"
# mean hourly load from the other complete days
profile={}
for h in range(24):
    vals=[perday[d][h] for d in perday if d!=TODAY and sum(perday[d].values())>5]
    if vals: profile[h]=st.mean(vals)

load={h: (perday[TODAY][h] if h<=8 else profile.get(h,0.4)) for h in range(24)}

async def main():
    s=get_settings(); RTE=s.round_trip_efficiency; CAP=s.cycle_output_kwh
    hourly=aggregate_hourly(await fetch_day_ahead(date.fromisoformat(TODAY),settings=s))
    price={h.start.astimezone(RIGA).hour:h.eur_per_kwh for h in hourly}
    print(f"{TODAY}: measured load where available, recent mean elsewhere")
    print("window       delivered  lasts until  realised EUR/kWh  cost/kWh   NET")
    best=None
    for start in range(0,16):
        if start+1 not in price: continue
        c=(price[start]+price[start+1])/2
        rem=CAP; val=0.0; dl=0.0; last=start+1
        for h in range(start+2, 24):
            if h not in price: break
            take=min(rem, load.get(h,0))
            if take<=0: continue
            val+=take*(price[h]+M); dl+=take; rem-=take; last=h
            if rem<=1e-9: break
        if dl<=0.1: continue
        cost=dl/RTE*(c+M); wear=s.wear_cost_per_cycle_eur*dl/CAP
        net=val-cost-wear
        if best is None or net>best[0]: best=(net,start,dl,last)
        if start<=5 or net>0:
            print(f"  {start:02d}-{start+2:02d}h @{c:.4f}  {dl:5.2f} kWh   {last:02d}h"
                  f"        {val/dl:.4f}         {cost/dl:.4f}   {net:+.3f}")
    print(f"\n  best window: {best[1]:02d}-{best[1]+2:02d}h, net {best[0]:+.3f}, "
          f"delivers {best[2]:.2f} kWh through {best[3]:02d}h")
    print(f"  total simulated daily load: {sum(load.values()):.1f} kWh")

asyncio.run(main())
