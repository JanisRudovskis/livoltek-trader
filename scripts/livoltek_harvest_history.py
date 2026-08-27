"""Harvest the portal's full history to local JSONL. Prototype for `collect`.

Pulls, for every day in a range:
  - 5-minute inverter series  (energystorage/reportFormToCString, timeType 0)
  - daily energy aggregates   (powerstation/reportForm, timeType 1, per month)

and writes them to `data/telemetry/`. Only a small field subset is kept so the
files stay analysable offline; the portal retains everything anyway, so a wider
pull is always possible later.

Usable data begins 2026-04-01 (the inverter reports nothing before that).

Read-only. Usage:
    uv run python scripts/livoltek_harvest_history.py
    uv run python scripts/livoltek_harvest_history.py 2026-04-01 2026-08-26
"""

from __future__ import annotations

import asyncio
import calendar
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from livoltek_trader.livoltek import LivoltekClient, LivoltekError

OUT = Path("data/telemetry")
BASE = "https://evs.livoltek-portal.com/ctrller-manager"
FIVE_MIN_URL = f"{BASE}/energystorage/reportFormToCString"
DAILY_URL = f"{BASE}/powerstation/reportForm"

DATA_STARTS = date(2026, 4, 1)

# Keep only what analysis needs. Portal row keys are verbatim, spaces included.
FIVE_MIN_FIELDS = (
    "updateDate",
    "Battery SOC",
    "Battery power",
    "Load Power",
    "PV Power",
    "Pac",
    "exportpwr_real",
    "Battery temperature",
)
DAILY_FIELDS = (
    "updateDate",
    "Load",
    "ETotal Charge",
    "ETotal Discharge",
    "ETotal toGrid",
    "SM_PositiveE",
    "SM_NegativeE",
    "Total Profit",
)


def _rows(payload) -> list[dict]:
    data = (payload or {}).get("data") or []
    return [
        r
        for r in data
        if isinstance(r, dict) and r.get("updateDate") != "Datetime"
    ]


def _slim(row: dict, fields: tuple[str, ...]) -> dict:
    return {k: row.get(k) for k in fields if k in row}


async def main(argv: list[str]) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if len(argv) >= 2:
        d0, d1 = date.fromisoformat(argv[0]), date.fromisoformat(argv[1])
    else:
        d0, d1 = DATA_STARTS, date(2026, 8, 26)
    if d0 < DATA_STARTS:
        print(f"clamping start to {DATA_STARTS} (no data before that)")
        d0 = DATA_STARTS

    try:
        async with LivoltekClient() as client:
            page = client.page
            cap: dict = {}

            def on_request(request) -> None:
                # Check each endpoint independently: an earlier `not cap` guard
                # here silently disabled header capture once the station id had
                # been recorded by the other branch.
                if (
                    "reportFormToCString" in request.url
                    and "headers" not in cap
                ):
                    try:
                        cap["body"] = json.loads(request.post_data or "{}")
                    except Exception:
                        cap["body"] = {}
                    cap["headers"] = {
                        k: v
                        for k, v in (request.headers or {}).items()
                        if k.lower()
                        not in ("host", "content-length", "connection")
                    }
                if "powerstation/reportForm" in request.url and "station" not in cap:
                    try:
                        cap["station"] = json.loads(
                            request.post_data or "{}"
                        ).get("id")
                    except Exception:
                        pass

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

            if "headers" not in cap:
                print("could not capture auth headers", file=sys.stderr)
                return 1
            headers = cap["headers"]
            device_id = cap["body"].get("id")
            station_id = cap.get("station")
            print(f"device={device_id} station={station_id}")

            # ---- daily aggregates, one request per month ----
            daily_path = OUT / "daily.jsonl"
            written_daily = 0
            with daily_path.open("w", encoding="utf-8") as fh:
                m = date(d0.year, d0.month, 1)
                while m <= d1:
                    last = calendar.monthrange(m.year, m.month)[1]
                    resp = await page.request.post(
                        DAILY_URL,
                        headers=headers,
                        data={
                            "id": station_id,
                            "timeType": 1,
                            "startTime": f"{m.isoformat()} 00:00:00",
                            "endTime": (
                                f"{m.year:04d}-{m.month:02d}-{last:02d} 23:59:59"
                            ),
                        },
                        timeout=30000,
                    )
                    for r in _rows(json.loads(await resp.text())):
                        fh.write(
                            json.dumps(
                                _slim(r, DAILY_FIELDS), ensure_ascii=False
                            )
                            + "\n"
                        )
                        written_daily += 1
                    print(f"  daily {m.year}-{m.month:02d}: ok")
                    m = date(m.year + (m.month == 12), (m.month % 12) + 1, 1)
                    await asyncio.sleep(0.3)

            # ---- 5-minute series, one request per day ----
            fm_path = OUT / "five_min.jsonl"
            written_fm = 0
            days = 0
            with fm_path.open("w", encoding="utf-8") as fh:
                d = d0
                while d <= d1:
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
                    rows = _rows(json.loads(await resp.text()))
                    for r in rows:
                        fh.write(
                            json.dumps(
                                _slim(r, FIVE_MIN_FIELDS), ensure_ascii=False
                            )
                            + "\n"
                        )
                        written_fm += 1
                    days += 1
                    if days % 20 == 0:
                        print(f"  5-min: {days} days, {written_fm} rows")
                    d += timedelta(days=1)
                    await asyncio.sleep(0.25)

            print(
                f"\nWrote {written_daily} daily rows -> {daily_path}"
                f"\nWrote {written_fm} 5-min rows ({days} days) -> {fm_path}"
            )

    except LivoltekError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
