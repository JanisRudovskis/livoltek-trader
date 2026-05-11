"""Tests for the Elering Nord Pool client."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from livoltek_trader.config import Settings
from livoltek_trader.elering import (
    ElerinAPIError,
    PricePeriod,
    _local_day_bounds_utc,
    _parse_periods,
    fetch_day_ahead,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "elering_full_day.json"


@pytest.fixture
def fixture_payload() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def settings() -> Settings:
    return Settings(
        elering_base_url="https://dashboard.elering.ee/api",
        elering_region="lv",
        elering_timeout_s=5.0,
    )


def test_local_day_bounds_summer_eest():
    start, end = _local_day_bounds_utc(date(2026, 5, 9))
    assert start == datetime(2026, 5, 8, 21, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 5, 9, 20, 59, 59, tzinfo=timezone.utc)


def test_local_day_bounds_winter_eet():
    start, end = _local_day_bounds_utc(date(2026, 1, 15))
    assert start == datetime(2026, 1, 14, 22, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 1, 15, 21, 59, 59, tzinfo=timezone.utc)


def test_parse_periods_sorts_and_converts():
    earlier = datetime(2026, 5, 9, 0, 0, tzinfo=timezone.utc)
    later = datetime(2026, 5, 9, 0, 15, tzinfo=timezone.utc)
    raw = [
        {"timestamp": int(later.timestamp()), "price": 200.0},
        {"timestamp": int(earlier.timestamp()), "price": 100.0},
    ]
    periods = _parse_periods(raw)
    assert [p.start for p in periods] == [earlier, later]
    assert periods[0].eur_per_kwh == pytest.approx(0.1)
    assert periods[1].eur_per_kwh == pytest.approx(0.2)


def test_price_period_eur_per_mwh_round_trip():
    p = PricePeriod(start=datetime(2026, 5, 9, tzinfo=timezone.utc), eur_per_kwh=0.123)
    assert p.eur_per_mwh == pytest.approx(123.0)


@respx.mock
async def test_fetch_day_ahead_happy_path(settings, fixture_payload):
    route = respx.get("https://dashboard.elering.ee/api/nps/price").mock(
        return_value=httpx.Response(200, json=fixture_payload)
    )
    periods = await fetch_day_ahead(date(2026, 5, 9), settings=settings)

    assert route.called
    request = route.calls.last.request
    assert request.url.params["start"] == "2026-05-08T21:00:00Z"
    assert request.url.params["end"] == "2026-05-09T20:59:59Z"

    assert len(periods) == 96
    assert all(isinstance(p, PricePeriod) for p in periods)
    deltas = {(periods[i + 1].start - periods[i].start).total_seconds() for i in range(len(periods) - 1)}
    assert deltas == {900.0}
    assert periods[0].start == datetime(2026, 5, 8, 21, 0, tzinfo=timezone.utc)
    assert periods[0].eur_per_kwh == pytest.approx(0.11775)


@respx.mock
async def test_fetch_day_ahead_empty_region(settings):
    respx.get("https://dashboard.elering.ee/api/nps/price").mock(
        return_value=httpx.Response(200, json={"success": True, "data": {"lv": []}})
    )
    with pytest.raises(ElerinAPIError, match="No price data"):
        await fetch_day_ahead(date(2026, 5, 9), settings=settings)


@respx.mock
async def test_fetch_day_ahead_missing_region(settings):
    respx.get("https://dashboard.elering.ee/api/nps/price").mock(
        return_value=httpx.Response(200, json={"success": True, "data": {"ee": []}})
    )
    with pytest.raises(ElerinAPIError, match="missing from response"):
        await fetch_day_ahead(date(2026, 5, 9), settings=settings)


@respx.mock
async def test_fetch_day_ahead_success_false(settings):
    respx.get("https://dashboard.elering.ee/api/nps/price").mock(
        return_value=httpx.Response(200, json={"success": False, "data": {}})
    )
    with pytest.raises(ElerinAPIError, match="signalled failure"):
        await fetch_day_ahead(date(2026, 5, 9), settings=settings)


@respx.mock
async def test_fetch_day_ahead_http_500(settings):
    respx.get("https://dashboard.elering.ee/api/nps/price").mock(
        return_value=httpx.Response(500, text="upstream is sad")
    )
    with pytest.raises(ElerinAPIError, match="HTTP 500"):
        await fetch_day_ahead(date(2026, 5, 9), settings=settings)


@respx.mock
async def test_fetch_day_ahead_network_error(settings):
    respx.get("https://dashboard.elering.ee/api/nps/price").mock(
        side_effect=httpx.ConnectError("no route to host")
    )
    with pytest.raises(ElerinAPIError, match="HTTP transport error"):
        await fetch_day_ahead(date(2026, 5, 9), settings=settings)


@respx.mock
async def test_fetch_day_ahead_malformed_json(settings):
    respx.get("https://dashboard.elering.ee/api/nps/price").mock(
        return_value=httpx.Response(200, text="not json at all")
    )
    with pytest.raises(ElerinAPIError, match="non-JSON"):
        await fetch_day_ahead(date(2026, 5, 9), settings=settings)
