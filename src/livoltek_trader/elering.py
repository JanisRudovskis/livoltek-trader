"""Elering Nord Pool day-ahead price client (region: lv).

The public Elering dashboard at https://dashboard.elering.ee/api exposes
Nord Pool spot prices for the Baltic + FI bidding zones in 15-minute
resolution. The server returns timestamps as UTC Unix epoch and prices
as EUR/MWh; we convert to EUR/kWh at parse time and expose timezone-aware
UTC datetimes to callers.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict

from livoltek_trader.config import Settings, get_settings

RIGA_TZ = ZoneInfo("Europe/Riga")


class PricePeriod(BaseModel):
    """A single 15-minute Nord Pool price period."""

    model_config = ConfigDict(frozen=True)

    start: datetime
    eur_per_kwh: float

    @property
    def eur_per_mwh(self) -> float:
        return self.eur_per_kwh * 1000.0


class ElerinAPIError(RuntimeError):
    """Raised when the Elering API returns an unexpected payload or HTTP error."""


def _local_day_bounds_utc(target_date: date) -> tuple[datetime, datetime]:
    """Return UTC start/end covering the given Riga-local calendar date."""
    local_start = datetime.combine(target_date, time(0, 0), tzinfo=RIGA_TZ)
    local_end = local_start + timedelta(days=1) - timedelta(seconds=1)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _parse_periods(raw_entries: list[dict[str, Any]]) -> list[PricePeriod]:
    periods = [
        PricePeriod(
            start=datetime.fromtimestamp(entry["timestamp"], tz=timezone.utc),
            eur_per_kwh=float(entry["price"]) / 1000.0,
        )
        for entry in raw_entries
    ]
    periods.sort(key=lambda p: p.start)
    return periods


async def fetch_day_ahead(
    target_date: date,
    *,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[PricePeriod]:
    """Fetch all 15-minute price periods for the given Riga-local date.

    Raises ElerinAPIError if the response is missing the configured region,
    contains no entries, or signals failure via `success=false` / non-2xx.
    """
    settings = settings or get_settings()
    start_utc, end_utc = _local_day_bounds_utc(target_date)
    params = {
        "start": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    async def _do(c: httpx.AsyncClient) -> httpx.Response:
        return await c.get("/nps/price", params=params)

    try:
        if client is None:
            async with httpx.AsyncClient(
                base_url=settings.elering_base_url,
                timeout=settings.elering_timeout_s,
            ) as owned:
                response = await _do(owned)
        else:
            response = await _do(client)
    except httpx.HTTPError as exc:
        raise ElerinAPIError(f"HTTP transport error: {exc}") from exc

    if response.status_code != 200:
        raise ElerinAPIError(
            f"Elering returned HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ElerinAPIError(f"Elering returned non-JSON: {exc}") from exc

    if not payload.get("success"):
        raise ElerinAPIError(f"Elering signalled failure: {payload!r}")

    region_data = payload.get("data", {}).get(settings.elering_region)
    if region_data is None:
        raise ElerinAPIError(
            f"Region {settings.elering_region!r} missing from response"
        )
    if not region_data:
        raise ElerinAPIError(
            f"No price data for {settings.elering_region!r} on {target_date.isoformat()}"
        )

    return _parse_periods(region_data)
