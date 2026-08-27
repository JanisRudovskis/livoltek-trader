"""Measure real battery drain physics from the portal's 5-minute history.

Two questions this answers from data that already exists (April 2026 onward):

  1. When does usable history actually start? (probe a few dates)
  2. How many kWh does the battery deliver per SOC point, and what load does the
     house actually draw? From those two, `battery_drain_hours` for ANY load is
     arithmetic: usable_kwh / load_kw.

Night-time episodes are used because there is no PV input then, so the SOC fall
is caused purely by household load and the integration is clean.

Read-only. Usage:
    uv run python scripts/livoltek_measure_drain.py
    uv run python scripts/livoltek_measure_drain.py 2026-08-01 2026-08-26
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from livoltek_trader.livoltek import LivoltekClient, LivoltekError

OUT = Path("data/exploration/history-spike")
FIVE_MIN_URL = (
    "https://evs.livoltek-portal.com/ctrller-manager/energystorage/"
    "reportFormToCString"
)
DAILY_URL = (
    "https://evs.livoltek-portal.com/ctrller-manager/powerstation/reportForm"
)
STEP_H = 5 / 60  # sample interval in hours

# Probe for the start of usable data.
START_PROBES = [
    date(2026, 3, 1), date(2026, 3, 20), date(2026, 4, 1),
    date(2026, 4, 10), date(2026, 4, 20), date(2026, 5, 1),
]


def _rows(payload) -> list[dict]:
    data = (payload or {}).get("data") or []
    return [
        r
        for r in data
        if isinstance(r, dict) and r.get("updateDate") != "Datetime"
    ]


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _drain_episodes(rows: list[dict]) -> list[dict]:
    """Find runs where SOC falls monotonically while the battery discharges.

    Returns one entry per episode with SOC delta, energy delivered (integrated
    from battery power), mean household load, and duration.
    """
    samples = []
    for r in rows:
        soc = _num(r.get("Battery SOC"))
        bp = _num(r.get("Battery power"))
        lp = _num(r.get("Load Power"))
        pv = _num(r.get("PV Power")) or 0.0
        ts = r.get("updateDate")
        if soc is None or bp is None or ts is None:
            continue
        samples.append(
            {"ts": ts, "soc": soc, "bp": bp, "lp": lp or 0.0, "pv": pv}
        )

    episodes: list[dict] = []
    cur: list[dict] = []
    for s in samples:
        discharging = s["bp"] < -0.05 and s["pv"] < 0.20
        if discharging:
            if cur and s["soc"] > cur[-1]["soc"]:
                # SOC went up mid-run: close the episode.
                if len(cur) >= 6:
                    episodes.append(cur)
                cur = []
            cur.append(s)
        else:
            if len(cur) >= 6:
                episodes.append(cur)
            cur = []
    if len(cur) >= 6:
        episodes.append(cur)

    out = []
    for ep in episodes:
        d_soc = ep[0]["soc"] - ep[-1]["soc"]
        if d_soc < 5:
            continue
        kwh_out = sum(-s["bp"] for s in ep) * STEP_H
        load_kw = sum(s["lp"] for s in ep) / len(ep)
        hours = len(ep) * STEP_H
        out.append(
            {
                "from": ep[0]["ts"],
                "to": ep[-1]["ts"],
                "soc_from": ep[0]["soc"],
                "soc_to": ep[-1]["soc"],
                "d_soc": round(d_soc, 1),
                "kwh_delivered": round(kwh_out, 2),
                "kwh_per_soc_pt": round(kwh_out / d_soc, 4),
                "mean_load_kw": round(load_kw, 3),
                "hours": round(hours, 2),
            }
        )
    return out


async def main(argv: list[str]) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if len(argv) >= 2:
        d0 = date.fromisoformat(argv[0])
        d1 = date.fromisoformat(argv[1])
    else:
        d1 = date(2026, 8, 26)
        d0 = d1 - timedelta(days=13)

    try:
        async with LivoltekClient() as client:
            page = client.page
            captured: dict = {}

            def on_request(request) -> None:
                if "reportFormToCString" in request.url and not captured:
                    try:
                        captured["body"] = json.loads(request.post_data or "{}")
                    except Exception:
                        captured["body"] = {}
                    captured["headers"] = {
                        k: v
                        for k, v in (request.headers or {}).items()
                        if k.lower()
                        not in ("host", "content-length", "connection")
                    }

            page.on("request", on_request)
            await client.login()

            if client.HOME_URL_FRAGMENT not in page.url:
                await page.goto(
                    f"https://{client.EU_HOST}/#{client.HOME_URL_FRAGMENT}",
                    wait_until="domcontentloaded",
                )
                await page.wait_for_url(
                    f"**{client.HOME_URL_FRAGMENT}**", timeout=10000
                )
            await page.locator(".deviceIcon").first.click()
            await page.wait_for_url(
                f"**{client.STATION_URL_FRAGMENT}**", timeout=15000
            )
            await asyncio.sleep(1.5)
            await page.locator('img[src*="hp3_online"]').first.click()
            await page.wait_for_url(
                f"**{client.DEVICE_URL_FRAGMENT}**", timeout=15000
            )
            await asyncio.sleep(1.5)
            try:
                await page.get_by_role(
                    "tab", name="Data report", exact=True
                ).click(timeout=8000)
            except PlaywrightTimeoutError:
                pass
            await asyncio.sleep(3.0)

            if not captured:
                print("could not capture auth headers", file=sys.stderr)
                return 1
            headers = captured["headers"]
            device_id = captured["body"].get("id")
            print(f"device id={device_id}, station daily endpoint too\n")

            async def five_min(d: date) -> list[dict]:
                resp = await page.request.post(
                    FIVE_MIN_URL,
                    headers=headers,
                    data={
                        "id": device_id,
                        "timeType": 0,
                        "startTime": f"{d.isoformat()} 00:00:00",
                        "endTime": f"{d.isoformat()} 23:59:59",
                    },
                    timeout=30000,
                )
                return _rows(json.loads(await resp.text()))

            # ---- 1. Where does usable data start? ----
            print("=== DATA START PROBE (SOC samples present?) ===")
            for d in START_PROBES:
                rows = await five_min(d)
                socs = [
                    _num(r.get("Battery SOC"))
                    for r in rows
                    if _num(r.get("Battery SOC")) is not None
                ]
                loads = [
                    _num(r.get("Load Power"))
                    for r in rows
                    if _num(r.get("Load Power")) is not None
                ]
                print(
                    f"  {d}: rows={len(rows):3d} soc_samples={len(socs):3d} "
                    f"load_samples={len(loads):3d} "
                    f"soc_range={[min(socs), max(socs)] if socs else None}"
                )

            # ---- 2. Drain episodes over the requested window ----
            print(f"\n=== DRAIN EPISODES {d0} .. {d1} ===")
            all_eps: list[dict] = []
            d = d0
            while d <= d1:
                rows = await five_min(d)
                eps = _drain_episodes(rows)
                for e in eps:
                    e["day"] = d.isoformat()
                all_eps.extend(eps)
                d += timedelta(days=1)

            all_eps.sort(key=lambda e: -e["d_soc"])
            for e in all_eps[:25]:
                print(
                    f"  {e['from']} -> {e['to']}  "
                    f"SOC {e['soc_from']:.0f}->{e['soc_to']:.0f} "
                    f"({e['d_soc']:.0f}pt)  {e['kwh_delivered']:.2f} kWh  "
                    f"{e['kwh_per_soc_pt']:.4f} kWh/pt  "
                    f"load {e['mean_load_kw']:.2f} kW  {e['hours']:.1f} h"
                )

            (OUT / "drain-episodes.json").write_text(
                json.dumps(all_eps, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            deep = [e for e in all_eps if e["d_soc"] >= 30]
            if deep:
                per_pt = sorted(e["kwh_per_soc_pt"] for e in deep)
                mid = per_pt[len(per_pt) // 2]
                usable_10_100 = mid * 90
                print(
                    f"\n  episodes with >=30 SOC pts: {len(deep)}"
                    f"\n  median kWh per SOC point : {mid:.4f}"
                    f"\n  implied usable 10->100%  : {usable_10_100:.2f} kWh"
                )
                print("\n  implied drain hours by load:")
                for load in (1.0, 1.25, 1.5, 2.0, 2.5, 3.0):
                    print(
                        f"    {load:.2f} kW ({load*24:.0f} kWh/day)"
                        f" -> {usable_10_100 / load:.1f} h"
                    )
            else:
                print("\n  no episodes with >=30 SOC points in this window")

    except LivoltekError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
