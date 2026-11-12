"""Refit the nights-ahead ranking model -> src/livoltek_trader/night_model.json.

    uv run --with numpy python scripts/train_night_model.py

Fetches LV/FI day-ahead history from Elering and archived ECMWF forecasts
(Open-Meteo Previous Runs API, available from ~March 2024). Builds rows with
the runtime `nights.feature_row`, so training and production features cannot
drift apart. Then fits a ridge regression of (cheapest 3-h night block -
7-night level) and stores the coefficients plus a per-lead residual band.

Refit monthly-ish, or whenever the market changes regime. Read-only: it only
calls public APIs and writes the JSON file. numpy is needed here only; the
runtime prediction is plain Python.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import numpy as np

from livoltek_trader.config import Settings
from livoltek_trader.elering import fetch_price_range
from livoltek_trader.nights import (
    HORIZON,
    LEVEL_NIGHTS,
    MODEL_PATH,
    Weather,
    band_scale,
    cheapest_block,
    feature_row,
    fetch_weather,
    hourly_mwh,
)

FIRST_DAY = date(2024, 3, 1)  # archived 100 m wind forecasts start here
RIDGE_LAMBDA = 300.0
CHUNK_DAYS = 30


async def _prices(settings: Settings, last_day: date) -> tuple[dict, dict]:
    lv: dict = {}
    fi: dict = {}
    d = FIRST_DAY
    async with httpx.AsyncClient(base_url=settings.elering_base_url, timeout=60) as c:
        while d <= last_day:
            end = min(d + timedelta(days=CHUNK_DAYS - 1), last_day)
            got = await fetch_price_range(d, end, regions=("lv", "fi"), settings=settings, client=c)
            lv.update(hourly_mwh(got["lv"]))
            fi.update(hourly_mwh(got["fi"]))
            d = end + timedelta(days=1)
    return lv, fi


async def _weather(settings: Settings, last_day: date) -> Weather:
    wx: Weather = {}
    d = FIRST_DAY
    async with httpx.AsyncClient(timeout=180) as c:
        while d <= last_day:
            end = min(d + timedelta(days=CHUNK_DAYS - 1), last_day)
            for attempt in range(5):
                try:
                    part = await fetch_weather(d, end, settings=settings, client=c)
                    break
                except Exception as exc:  # noqa: BLE001 — the public API times out at times
                    print(f"weather {d}: {exc!s:.80} - retry {attempt + 1}")
                    await asyncio.sleep(20 * (attempt + 1))
            else:
                raise RuntimeError(f"weather chunk {d} kept failing")
            for pt, variables in part.items():
                for var, series in variables.items():
                    wx.setdefault(pt, {}).setdefault(var, {}).update(series)
            print(f"weather {d}..{end} ok", flush=True)
            d = end + timedelta(days=1)
    return wx


def main() -> None:
    settings = Settings()
    today = date.today()
    lv, fi = asyncio.run(_prices(settings, today))
    wx = asyncio.run(_weather(settings, today))

    rows, targets, leads = [], [], []
    d0 = FIRST_DAY + timedelta(days=LEVEL_NIGHTS)
    last_d0 = today - timedelta(days=HORIZON + 2)  # every target night fully priced
    while d0 <= last_d0:
        for k in range(1, HORIZON + 1):
            row = feature_row(d0, k, lv, fi, wx)
            night = cheapest_block(lv, d0 + timedelta(days=k))
            if row is None or night is None:
                continue
            rows.append(row)
            targets.append(night[0] - row["lvl7"])
            leads.append(k)
        d0 += timedelta(days=1)

    features = list(rows[0])
    X = np.array([[r[f] for f in features] for r in rows])
    y = np.array(targets)
    k_arr = np.array(leads)
    level = X[:, features.index("lvl7")]

    mean, scale = X.mean(axis=0), X.std(axis=0)
    scale[scale == 0] = 1.0
    Z = (X - mean) / scale
    intercept = float(y.mean())
    coef = np.linalg.solve(Z.T @ Z + RIDGE_LAMBDA * np.eye(len(features)), Z.T @ (y - intercept))

    resid = y - (intercept + Z @ coef)
    rel = resid / np.array([band_scale(v) for v in level])
    band = {
        str(k): [round(float(np.quantile(rel[k_arr == k], 0.2)), 4),
                 round(float(np.quantile(rel[k_arr == k], 0.8)), 4)]
        for k in range(1, HORIZON + 1)
    }

    out = {
        "trained_at": today.isoformat(),
        "trained_through": last_d0.isoformat(),
        "samples": len(rows),
        "ridge_lambda": RIDGE_LAMBDA,
        "features": features,
        "mean": [round(float(v), 6) for v in mean],
        "scale": [round(float(v), 6) for v in scale],
        "coef": [round(float(v), 6) for v in coef],
        "intercept": round(intercept, 6),
        "band": band,
    }
    Path(MODEL_PATH).write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")

    mae = float(np.abs(resid).mean())
    top = sorted(zip(np.abs(coef), features), reverse=True)[:8]
    print(f"samples {len(rows)}, in-sample MAE {mae:.1f} EUR/MWh, through {last_d0}")
    print("largest effects:", ", ".join(f"{f} {c:.1f}" for c, f in top))
    print(f"wrote {MODEL_PATH}")


if __name__ == "__main__":
    main()
