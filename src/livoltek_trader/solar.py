"""Open-Meteo PV forecast client.

Fetches the shortwave radiation forecast for the user's site and converts it
to expected daily kWh using an empirically calibrated factor (see
PROJECT memory: pv_system.md). The forecast is the primary signal that lets
the strategy skip grid-charge cycles on sunny days when the battery will be
filled from PV surplus regardless.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from livoltek_trader.config import Settings, get_settings


class PvForecast(BaseModel):
    """Day-ahead PV production estimate for the configured site."""

    model_config = ConfigDict(frozen=True)

    target_date: date
    expected_kwh: float
    shortwave_radiation_mj_m2: float
    sunshine_hours: float
    cloud_cover_pct: float


class OpenMeteoAPIError(RuntimeError):
    """Raised when Open-Meteo returns a malformed payload or HTTP error."""


def _extract(payload: dict[str, Any], target_date: date) -> dict[str, Any]:
    daily = payload.get("daily")
    if not isinstance(daily, dict):
        raise OpenMeteoAPIError(f"missing 'daily' block in payload: {payload!r}")
    times = daily.get("time") or []
    target_str = target_date.isoformat()
    try:
        idx = times.index(target_str)
    except ValueError as exc:
        raise OpenMeteoAPIError(
            f"target date {target_str} not in forecast (got {times!r})"
        ) from exc

    def at(field: str, default: float = 0.0) -> float:
        values = daily.get(field) or []
        if idx >= len(values) or values[idx] is None:
            return default
        return float(values[idx])

    return {
        "shortwave_radiation_mj_m2": at("shortwave_radiation_sum"),
        "sunshine_hours": at("sunshine_duration") / 3600.0,
        "cloud_cover_pct": at("cloud_cover_mean"),
    }


async def fetch_pv_forecast(
    target_date: date,
    *,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> PvForecast:
    """Fetch the day-ahead PV forecast for the configured site and date."""
    settings = settings or get_settings()
    params = {
        "latitude": settings.pv_lat,
        "longitude": settings.pv_lon,
        "daily": "shortwave_radiation_sum,sunshine_duration,cloud_cover_mean",
        "start_date": target_date.isoformat(),
        "end_date": target_date.isoformat(),
        "timezone": "Europe/Riga",
    }

    async def _do(c: httpx.AsyncClient) -> httpx.Response:
        return await c.get("/forecast", params=params)

    try:
        if client is None:
            async with httpx.AsyncClient(
                base_url=settings.open_meteo_base_url,
                timeout=settings.open_meteo_timeout_s,
            ) as owned:
                response = await _do(owned)
        else:
            response = await _do(client)
    except httpx.HTTPError as exc:
        raise OpenMeteoAPIError(f"HTTP transport error: {exc}") from exc

    if response.status_code != 200:
        raise OpenMeteoAPIError(
            f"Open-Meteo returned HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise OpenMeteoAPIError(f"non-JSON response: {exc}") from exc

    fields = _extract(payload, target_date)
    expected_kwh = fields["shortwave_radiation_mj_m2"] * settings.pv_kwh_per_mj_m2
    return PvForecast(target_date=target_date, expected_kwh=expected_kwh, **fields)
