"""Spike part 2: learn the history request shape, then probe retention depth.

Part 1 (`livoltek_peek_history.py`) found the two endpoints that carry history:

  energystorage/reportFormToCString  -> 5-minute series, incl. `Battery SOC`
  powerstation/reportForm            -> daily energy aggregates

This script captures the exact REQUEST those endpoints receive (method, headers,
JSON body), then replays it against past dates to find how far back the portal
retains data. That answers the one question deciding whether telemetry can be a
single local pull (long retention) or must be collected nightly (short one).

Auth note: the portal is a Vue SPA that sends a bearer/token HEADER, not just
cookies, so a replay must reuse the captured headers verbatim. Those headers are
kept in memory only and redacted before anything is written to disk.

Read-only: only POSTs the two report endpoints. Never touches Params set.

Usage: uv run python scripts/livoltek_peek_history_range.py
"""

from __future__ import annotations

import asyncio
import calendar
import json
import re
import sys
from datetime import date
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from livoltek_trader.livoltek import LivoltekClient, LivoltekError

OUT = Path("data/exploration/history-spike")

FIVE_MIN = "reportFormToCString"   # timeType 0 — intra-day 5-minute series
DAILY = "powerstation/reportForm"  # timeType 1 — daily aggregates for a month
TARGETS = (FIVE_MIN, DAILY)

# Newest first. If a deep probe returns rows we can calibrate on last winter
# right away instead of waiting for November.
PROBE_DATES = [
    date(2026, 8, 26),   # yesterday — sanity check the replay itself works
    date(2026, 7, 15),
    date(2026, 5, 15),
    date(2026, 3, 15),
    date(2026, 2, 15),   # last winter
    date(2026, 1, 15),
    date(2025, 12, 15),
    date(2025, 6, 15),
]

REDACT = re.compile(r"token|auth|cookie|password|secret|session", re.I)

# Headers Playwright sets itself; passing them through causes conflicts.
SKIP_HEADERS = {"host", "content-length", "connection", ":authority", ":method",
                ":path", ":scheme"}


def _redact_headers(h: dict) -> dict:
    return {k: ("<redacted>" if REDACT.search(k) else v) for k, v in h.items()}


def _body_for(target: str, base: dict, d: date) -> dict:
    body = dict(base)
    if target == FIVE_MIN:
        body["startTime"] = f"{d.isoformat()} 00:00:00"
        body["endTime"] = f"{d.isoformat()} 23:59:59"
    else:
        last = calendar.monthrange(d.year, d.month)[1]
        body["startTime"] = f"{d.year:04d}-{d.month:02d}-01 00:00:00"
        body["endTime"] = f"{d.year:04d}-{d.month:02d}-{last:02d} 23:59:59"
    return body


def _summarise(payload) -> dict:
    if not isinstance(payload, dict):
        return {"error": f"unexpected payload type {type(payload).__name__}"}
    data = payload.get("data") or []
    rows = [r for r in data if isinstance(r, dict)]
    real = [r for r in rows if r.get("updateDate") != "Datetime"]
    stamps = [r.get("updateDate") for r in real if r.get("updateDate")]
    socs = [
        r.get("Battery SOC")
        for r in real
        if isinstance(r.get("Battery SOC"), (int, float))
    ]
    loads = [
        r.get("Load") for r in real if isinstance(r.get("Load"), (int, float))
    ]
    out = {
        "code": payload.get("code"),
        "message": payload.get("message"),
        "total": payload.get("total"),
        "real_rows": len(real),
        "first_stamp": stamps[0] if stamps else None,
        "last_stamp": stamps[-1] if stamps else None,
    }
    if socs:
        out["soc_n"] = len(socs)
        out["soc_range"] = [min(socs), max(socs)]
    if loads:
        out["daily_load_kwh"] = [round(x, 1) for x in loads[:8]]
    return out


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    seen: dict[str, dict] = {}

    try:
        async with LivoltekClient() as client:
            page = client.page

            def on_request(request) -> None:
                for t in TARGETS:
                    if t in request.url and t not in seen:
                        try:
                            body = json.loads(request.post_data or "{}")
                        except Exception:
                            body = {}
                        seen[t] = {
                            "url": request.url,
                            "method": request.method,
                            "body": body,
                            "headers": {
                                k: v
                                for k, v in (request.headers or {}).items()
                                if k.lower() not in SKIP_HEADERS
                            },
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
                print("could not open 'Data report' tab", file=sys.stderr)
            await asyncio.sleep(3.0)

            print("=== CAPTURED REQUEST SHAPES ===")
            for t, r in seen.items():
                print(f"{t}: {r['method']} {r['url']}")
                print(f"   body    : {json.dumps(r['body'], ensure_ascii=False)}")
                print(f"   headers : {sorted(r['headers'].keys())}")
            (OUT / "request-shapes.json").write_text(
                json.dumps(
                    {
                        t: {**r, "headers": _redact_headers(r["headers"])}
                        for t, r in seen.items()
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            if not seen:
                print("NO target requests captured — cannot probe retention.")
                return 1

            results: dict[str, list[dict]] = {}
            for t, r in seen.items():
                results[t] = []
                print(f"\n=== RETENTION PROBE: {t} ===")
                first = True
                for d in PROBE_DATES:
                    body = _body_for(t, r["body"], d)
                    try:
                        resp = await page.request.post(
                            r["url"],
                            headers=r["headers"],
                            data=body,
                            timeout=30000,
                        )
                        raw = await resp.text()
                        if first:
                            # Keep one raw response so an auth/shape problem is
                            # diagnosable without another portal round trip.
                            (OUT / "probe-raw-sample.txt").write_text(
                                f"HTTP {resp.status}\n{raw[:4000]}",
                                encoding="utf-8",
                            )
                            print(f"  [raw sample] HTTP {resp.status} "
                                  f"{raw[:200]}")
                            first = False
                        s = _summarise(json.loads(raw))
                    except Exception as exc:  # noqa: BLE001 — exploratory
                        s = {"error": f"{type(exc).__name__}: {exc}"}
                    s["probe_date"] = d.isoformat()
                    results[t].append(s)
                    if s.get("error"):
                        print(f"  {d}: ERR {s['error']}")
                    else:
                        extra = ""
                        if s.get("soc_range"):
                            extra = f" soc={s['soc_range']} n={s.get('soc_n')}"
                        if s.get("daily_load_kwh"):
                            extra = f" load[:8]={s['daily_load_kwh']}"
                        print(
                            f"  {d}: rows={s['real_rows']:4d} "
                            f"total={s['total']} "
                            f"{s['first_stamp']} .. {s['last_stamp']}{extra}"
                        )

            (OUT / "retention-probe.json").write_text(
                json.dumps(results, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"\nWrote probe results to {OUT / 'retention-probe.json'}")

    except LivoltekError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
