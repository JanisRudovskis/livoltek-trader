"""Offline analysis of harvested telemetry. No portal access.

Measures the physical constants the strategy currently guesses:
  - kWh delivered per SOC point (discharge) -> real `cycle_output_kwh`
  - kWh absorbed per SOC point (charge)     -> real `battery_capacity_kwh`
  - round-trip efficiency
  - daily load distribution, and how well yesterday's load predicts today's
    (this decides whether `expected_daily_load_kwh` can stop being a constant)

Usage: uv run python scripts/analyze_telemetry.py
"""

from __future__ import annotations

import json
import statistics as st
from pathlib import Path

IN = Path("data/telemetry")
STEP_H = 5 / 60


def _num(v):
    return v if isinstance(v, (int, float)) else None


def load_five_min() -> list[dict]:
    rows = []
    for line in (IN / "five_min.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        soc = _num(r.get("Battery SOC"))
        bp = _num(r.get("Battery power"))
        if soc is None or bp is None:
            continue
        rows.append(
            {
                "ts": r.get("updateDate"),
                "soc": soc,
                "bp": bp,
                "lp": _num(r.get("Load Power")) or 0.0,
                "pv": _num(r.get("PV Power")) or 0.0,
                "temp": _num(r.get("Battery temperature")),
            }
        )
    return rows


def episodes(rows: list[dict], *, charging: bool) -> list[dict]:
    """Contiguous runs of charging (or discharging) with monotone SOC."""
    out, cur = [], []
    for s in rows:
        if charging:
            active = s["bp"] > 0.05
            monotone = not cur or s["soc"] >= cur[-1]["soc"]
        else:
            # Exclude PV so a discharge episode is purely load-driven.
            active = s["bp"] < -0.05 and s["pv"] < 0.20
            monotone = not cur or s["soc"] <= cur[-1]["soc"]
        if active and monotone:
            cur.append(s)
        else:
            if len(cur) >= 6:
                out.append(cur)
            cur = [s] if active else []
    if len(cur) >= 6:
        out.append(cur)

    res = []
    for ep in out:
        d_soc = abs(ep[-1]["soc"] - ep[0]["soc"])
        if d_soc < 30:
            continue
        kwh = sum(abs(s["bp"]) for s in ep) * STEP_H
        res.append(
            {
                "from": ep[0]["ts"],
                "to": ep[-1]["ts"],
                "d_soc": d_soc,
                "kwh": kwh,
                "per_pt": kwh / d_soc,
                "mean_load_kw": sum(s["lp"] for s in ep) / len(ep),
                "hours": len(ep) * STEP_H,
            }
        )
    return res


def main() -> int:
    rows = load_five_min()
    print(f"5-min samples with SOC+power: {len(rows)}")

    dis = episodes(rows, charging=False)
    chg = episodes(rows, charging=True)

    out_pt = st.median(e["per_pt"] for e in dis) if dis else None
    in_pt = st.median(e["per_pt"] for e in chg) if chg else None

    print(f"\n=== DISCHARGE episodes (>=30 SOC pts, no PV): {len(dis)} ===")
    if out_pt:
        vals = sorted(e["per_pt"] for e in dis)
        print(f"  kWh per SOC point: median {out_pt:.4f}  "
              f"p10 {vals[len(vals)//10]:.4f}  p90 {vals[-max(1,len(vals)//10)]:.4f}")
        print(f"  usable 10->100%  : {out_pt*90:.2f} kWh")

    print(f"\n=== CHARGE episodes (>=30 SOC pts): {len(chg)} ===")
    if in_pt:
        print(f"  kWh per SOC point: median {in_pt:.4f}")
        print(f"  absorbed 10->100%: {in_pt*90:.2f} kWh")

    if out_pt and in_pt:
        print(f"\n  measured round-trip efficiency: {out_pt/in_pt:.3f}")
        print(f"  config assumes                : 0.900")
        print(f"  config battery_capacity_kwh   : 10.24  (measured in  {in_pt*90:.2f})")
        print(f"  config cycle_output_kwh       : 9.216  (measured out {out_pt*90:.2f})")

    # ---- daily load ----
    daily = []
    for line in (IN / "daily.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        lo = _num(r.get("Load"))
        if lo is None or lo <= 0:
            continue
        daily.append((r.get("updateDate"), lo, _num(r.get("SM_PositiveE")) or 0.0))
    daily.sort()

    loads = [d[1] for d in daily]
    print(f"\n=== DAILY LOAD ({len(loads)} days with data) ===")
    print(f"  min {min(loads):.1f}  median {st.median(loads):.1f}  "
          f"mean {st.mean(loads):.1f}  max {max(loads):.1f}  "
          f"stdev {st.stdev(loads):.1f}")
    print(f"  grid import median: {st.median(d[2] for d in daily):.1f} kWh")

    # How well does each predictor estimate today's load?
    print("\n=== PREDICTING TODAY'S LOAD (mean abs error, kWh) ===")
    errs_fixed, errs_yday, errs_3d = [], [], []
    for i in range(3, len(loads)):
        actual = loads[i]
        errs_fixed.append(abs(actual - 22.0))
        errs_yday.append(abs(actual - loads[i - 1]))
        errs_3d.append(abs(actual - st.mean(loads[i - 3 : i])))
    print(f"  fixed 22.0 kWh   : {st.mean(errs_fixed):.2f}")
    print(f"  yesterday's load : {st.mean(errs_yday):.2f}")
    print(f"  mean of last 3 d : {st.mean(errs_3d):.2f}")

    if out_pt:
        print("\n=== DRAIN HOURS from measured capacity ===")
        usable = out_pt * 90
        for kwh_day in (24, 30, 36, 42, 48, 54, 60):
            print(f"  {kwh_day:2d} kWh/day ({kwh_day/24:.2f} kW)"
                  f" -> {usable/(kwh_day/24):.1f} h")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
