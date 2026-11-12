"""Rank the coming nights by expected LV price — which night to charge the EV.

Tonight's prices are known at the 21:00 run. The next six nights are forecast
from weather (wind decides most of a Baltic night price) plus the calendar and
the recent price level.

Why this model, measured on 2026-11-12. The backtest used walk-forward
monthly refits over 615 evenings (2025-03 .. 2026-11), with archived ECMWF
forecasts at the lead times that would really have been available.

| Picking the cheapest of the next 7 nights | Hit rate | Cheapest in its top 2 |
|-------------------------------------------|----------|-----------------------|
| This ridge regression                     | 44 %     | 68 %                  |
| LightGBM                                  | 44 %     | 65 %                  |
| Calendar and level only                   | 31 %     | –                     |
| Always Saturday night                     | 24 %     | –                     |
| Random                                    | 14 %     | –                     |

The ridge regression's mean regret was 13 EUR/MWh against 42 for a random
pick, with a median of about 2. Because ridge did as well as LightGBM, it is
used here: prediction is a dot product in plain Python, with no numpy and no
native libraries on the host.

The ranking is useful; the price level is rough. Level MAE on the cheapest
3-hour block was 25-34 EUR/MWh against a mean of 63.

Weather comes from Open-Meteo's Previous Runs API at lead k+1 days for
night k. Every hour of the night is then forecast at least a day before the
21:00 decision, which is the same alignment the training script uses.

Refit with `uv run --with numpy python scripts/train_night_model.py`. The
coefficients live in `night_model.json` next to this file.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import fmean
from typing import Iterable

import httpx

from livoltek_trader.config import Settings
from livoltek_trader.elering import RIGA_TZ, PricePeriod, fetch_price_range
from livoltek_trader.solar import OpenMeteoAPIError

Hourly = dict[datetime, float]
"""UTC hour start -> EUR/MWh."""
Weather = dict[str, dict[str, dict[datetime, float]]]
"""point -> Open-Meteo variable -> UTC hour start -> value."""

WIND_POINTS: dict[str, tuple[float, float]] = {
    "LT_coast": (55.9, 21.4),
    "LV_kurzeme": (56.8, 21.4),
    "EE_west": (58.7, 23.0),
    "FI_ostro_S": (62.5, 21.6),
    "FI_ostro_N": (64.7, 25.0),
    "SE_south": (56.3, 13.8),
    "PL_north": (54.5, 17.0),
}
TEMP_POINTS: dict[str, tuple[float, float]] = {
    "Riga": (56.95, 24.1),
    "Helsinki": (60.2, 24.9),
    "Vilnius": (54.7, 25.3),
}
LEADS = range(2, 8)
NIGHT_START_HOUR = 21
NIGHT_HOURS = 10  # 21:00 -> 07:00
BLOCK_HOURS = 3  # ~33 kWh at 11 kW
HORIZON = 6
LEVEL_NIGHTS = 7
BAND_FLOOR_EUR_MWH = 30.0
WEATHER_MODEL = "ecmwf_ifs025"
MODEL_PATH = Path(__file__).with_name("night_model.json")
UTC = timezone.utc


def power_proxy(kmh: float) -> float:
    """Normalised wind-turbine output for a 100 m wind speed.

    Zero below cut-in (3 m/s), cubic up to rated speed (12 m/s), then flat at 1,
    and zero above cut-out (25 m/s)."""
    v = kmh / 3.6
    if v >= 25.0:
        return 0.0
    return min(max((v**3 - 27.0) / (1728.0 - 27.0), 0.0), 1.0)


def night_hours(d: date) -> list[datetime]:
    start = datetime.combine(d, time(NIGHT_START_HOUR), tzinfo=RIGA_TZ).astimezone(UTC)
    return [start + timedelta(hours=i) for i in range(NIGHT_HOURS)]


def cheapest_block(prices: Hourly, d: date) -> tuple[float, datetime] | None:
    """Cheapest BLOCK_HOURS run inside the night starting on `d` (Riga 21:00-07:00)."""
    hours = night_hours(d)
    ps = [prices.get(h) for h in hours]
    if any(p is None for p in ps):
        return None
    best = min(range(NIGHT_HOURS - BLOCK_HOURS + 1), key=lambda i: sum(ps[i : i + BLOCK_HOURS]))
    return fmean(ps[best : best + BLOCK_HOURS]), hours[best]


def _day_mean(prices: Hourly, d: date) -> float | None:
    t = datetime.combine(d, time(0), tzinfo=RIGA_TZ).astimezone(UTC)
    end = datetime.combine(d + timedelta(days=1), time(0), tzinfo=RIGA_TZ).astimezone(UTC)
    vals = []
    while t < end:
        if t not in prices:
            return None
        vals.append(prices[t])
        t += timedelta(hours=1)
    return fmean(vals)


def _night_weather(wx: Weather, d: date, lead: int) -> dict[str, float] | None:
    hours = night_hours(d)
    out: dict[str, float] = {}
    try:
        for pt in WIND_POINTS:
            series = wx[pt][f"wind_speed_100m_previous_day{lead}"]
            out[f"wind_{pt}"] = fmean(power_proxy(series[h]) for h in hours)
        temps = []
        for pt in TEMP_POINTS:
            series = wx[pt][f"temperature_2m_previous_day{lead}"]
            temps.append(fmean(series[h] for h in hours))
    except KeyError:
        return None
    out["temp"] = fmean(temps)
    return out


def feature_row(
    d0: date, k: int, lv: Hourly, fi: Hourly, wx: Weather
) -> dict[str, float] | None:
    """Features for night `d0 + k`, as known at 21:00 on `d0`. None if data is missing."""
    hist = [cheapest_block(lv, d0 - timedelta(days=i)) for i in range(LEVEL_NIGHTS)]
    tomorrow = _day_mean(lv, d0 + timedelta(days=1))
    fi_days = [_day_mean(fi, d0 - timedelta(days=i)) for i in range(LEVEL_NIGHTS)]
    night = d0 + timedelta(days=k)
    tonight_wx = _night_weather(wx, d0, 2)
    night_wx = _night_weather(wx, night, k + 1)
    if (
        any(h is None for h in hist)
        or tomorrow is None
        or any(f is None for f in fi_days)
        or tonight_wx is None
        or night_wx is None
    ):
        return None

    level = fmean(h[0] for h in hist)
    row = {
        "lvl7": level,
        "tonight_rel": hist[0][0] - level,
        "tom_rel": tomorrow - level,
        "fi_rel": fmean(fi_days) - level,
        "known_rel": 0.0,
    }
    if k == 1:  # tomorrow's 21:00-24:00 is already in the day-ahead result
        known = [lv.get(h) for h in night_hours(night)[:3]]
        if all(p is not None for p in known):
            row["known_rel"] = fmean(known) - level
    for j in range(2, HORIZON + 1):
        row[f"k{j}"] = float(k == j)
    for wd in range(1, 7):
        row[f"wd{wd}"] = float(night.weekday() == wd)
    row["msin"] = math.sin(2 * math.pi * night.month / 12)
    row["mcos"] = math.cos(2 * math.pi * night.month / 12)
    row.update(night_wx)
    row["tonight_wind"] = fmean(tonight_wx[f"wind_{p}"] for p in WIND_POINTS)
    row["d_temp"] = night_wx["temp"] - tonight_wx["temp"]
    return row


def band_scale(level_eur_mwh: float) -> float:
    return math.sqrt(max(level_eur_mwh, BAND_FLOOR_EUR_MWH))


@dataclass(frozen=True)
class NightModel:
    """Ridge regression of (night price - 7-night level) on standardised features."""

    features: tuple[str, ...]
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    coef: tuple[float, ...]
    intercept: float
    band: dict[int, tuple[float, float]]
    """Per lead k: 20th/80th percentile of residual / sqrt(max(level, floor)).

    Errors grow with the price level, but slower than the level itself. Measured
    20-80 % coverage was 52-66 % across price regimes, against 0.43-0.80 for
    proportional scaling and 0.49-0.62 for a fixed EUR/MWh band."""

    @classmethod
    def load(cls, path: Path = MODEL_PATH) -> NightModel:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            features=tuple(raw["features"]),
            mean=tuple(raw["mean"]),
            scale=tuple(raw["scale"]),
            coef=tuple(raw["coef"]),
            intercept=float(raw["intercept"]),
            band={int(k): (float(v[0]), float(v[1])) for k, v in raw["band"].items()},
        )

    def predict(self, row: dict[str, float]) -> float:
        z = sum(
            c * (row[f] - m) / s
            for f, m, s, c in zip(self.features, self.mean, self.scale, self.coef)
        )
        return row["lvl7"] + self.intercept + z

    def interval(self, row: dict[str, float], k: int) -> tuple[float, float]:
        p50 = self.predict(row)
        spread = band_scale(row["lvl7"])
        lo, hi = self.band[k]
        return p50 + lo * spread, p50 + hi * spread


@dataclass(frozen=True)
class NightOutlook:
    night: date
    """The evening the night starts (21:00 Riga)."""
    eur_per_kwh: float
    """Cheapest 3-hour block: exact for the known night, forecast otherwise."""
    low: float | None = None
    high: float | None = None
    block_start: datetime | None = None

    @property
    def known(self) -> bool:
        return self.block_start is not None


def outlook(d0: date, lv: Hourly, fi: Hourly, wx: Weather, model: NightModel) -> list[NightOutlook]:
    tonight = cheapest_block(lv, d0)
    if tonight is None:
        return []
    out = [NightOutlook(d0, tonight[0] / 1000, block_start=tonight[1])]
    for k in range(1, HORIZON + 1):
        row = feature_row(d0, k, lv, fi, wx)
        if row is None:
            continue
        lo, hi = model.interval(row, k)
        out.append(
            NightOutlook(d0 + timedelta(days=k), model.predict(row) / 1000, lo / 1000, hi / 1000)
        )
    return out


def hourly_mwh(periods: Iterable[PricePeriod]) -> Hourly:
    """Average 15-minute periods into hours, in EUR/MWh."""
    acc: dict[datetime, list[float]] = {}
    for p in periods:
        hour = p.start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        acc.setdefault(hour, []).append(p.eur_per_mwh)
    return {h: fmean(v) for h, v in acc.items()}


async def fetch_weather(
    start: date,
    end: date,
    *,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> Weather:
    """Archived forecasts at leads 2-7 days for every point, UTC dates start..end."""
    points = {**WIND_POINTS, **TEMP_POINTS}
    variables = [
        f"{v}_previous_day{n}" for v in ("wind_speed_100m", "temperature_2m") for n in LEADS
    ]
    params = {
        "latitude": ",".join(str(lat) for lat, _ in points.values()),
        "longitude": ",".join(str(lon) for _, lon in points.values()),
        "hourly": ",".join(variables),
        "models": WEATHER_MODEL,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "timezone": "GMT",
    }
    url = f"{settings.open_meteo_previous_runs_url}/forecast"
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=settings.open_meteo_timeout_s) as owned:
                response = await owned.get(url, params=params)
        else:
            response = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise OpenMeteoAPIError(f"previous-runs transport error: {exc}") from exc
    if response.status_code != 200:
        raise OpenMeteoAPIError(
            f"previous-runs HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise OpenMeteoAPIError(f"previous-runs returned non-JSON: {exc}") from exc
    if isinstance(payload, dict):
        payload = [payload]
    if len(payload) != len(points):
        raise OpenMeteoAPIError(f"expected {len(points)} locations, got {len(payload)}")

    wx: Weather = {}
    for name, loc in zip(points, payload):
        hourly = loc["hourly"]
        times = [datetime.fromisoformat(t).replace(tzinfo=UTC) for t in hourly["time"]]
        wx[name] = {
            var: {t: v for t, v in zip(times, hourly.get(var, [])) if v is not None}
            for var in variables
        }
    return wx


async def forecast_nights(
    d0: date, *, settings: Settings, model: NightModel | None = None
) -> list[NightOutlook]:
    """Tonight (known) plus the next six nights, as seen at the evening of `d0`."""
    model = model or NightModel.load()
    periods = await fetch_price_range(
        d0 - timedelta(days=LEVEL_NIGHTS - 1),
        d0 + timedelta(days=1),
        regions=(settings.elering_region, "fi"),
        settings=settings,
    )
    wx = await fetch_weather(d0, d0 + timedelta(days=HORIZON + 1), settings=settings)
    return outlook(
        d0,
        hourly_mwh(periods[settings.elering_region]),
        hourly_mwh(periods["fi"]),
        wx,
        model,
    )
