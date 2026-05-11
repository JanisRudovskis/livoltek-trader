"""Tests for the Open-Meteo PV forecast client."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from livoltek_trader.config import Settings
from livoltek_trader.solar import (
    OpenMeteoAPIError,
    PvForecast,
    _extract,
    fetch_pv_forecast,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "openmeteo_forecast_one_day.json"


@pytest.fixture
def fixture_payload() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def settings() -> Settings:
    return Settings(
        open_meteo_base_url="https://api.open-meteo.com/v1",
        pv_lat=56.918,
        pv_lon=24.043,
        pv_kwh_per_mj_m2=2.98,
        expected_daily_load_kwh=22.0,
    )


def test_extract_pulls_correct_index():
    payload = {
        "daily": {
            "time": ["2026-05-09", "2026-05-10", "2026-05-11"],
            "shortwave_radiation_sum": [4.0, 6.0, 20.0],
            "sunshine_duration": [0.0, 3600.0, 36000.0],
            "cloud_cover_mean": [100, 80, 10],
        }
    }
    result = _extract(payload, date(2026, 5, 10))
    assert result["shortwave_radiation_mj_m2"] == pytest.approx(6.0)
    assert result["sunshine_hours"] == pytest.approx(1.0)
    assert result["cloud_cover_pct"] == pytest.approx(80)


def test_extract_raises_on_missing_date():
    payload = {"daily": {"time": ["2026-05-09"], "shortwave_radiation_sum": [4.0]}}
    with pytest.raises(OpenMeteoAPIError, match="not in forecast"):
        _extract(payload, date(2026, 5, 10))


def test_extract_raises_on_missing_daily_block():
    with pytest.raises(OpenMeteoAPIError, match="missing 'daily' block"):
        _extract({"foo": "bar"}, date(2026, 5, 10))


def test_extract_handles_null_values():
    payload = {
        "daily": {
            "time": ["2026-05-10"],
            "shortwave_radiation_sum": [None],
            "sunshine_duration": [None],
            "cloud_cover_mean": [None],
        }
    }
    result = _extract(payload, date(2026, 5, 10))
    assert result["shortwave_radiation_mj_m2"] == 0.0
    assert result["sunshine_hours"] == 0.0
    assert result["cloud_cover_pct"] == 0.0


@respx.mock
async def test_fetch_pv_forecast_happy_path(settings, fixture_payload):
    fixture_date_str = fixture_payload["daily"]["time"][0]
    fixture_date = date.fromisoformat(fixture_date_str)
    rad = fixture_payload["daily"]["shortwave_radiation_sum"][0]

    route = respx.get("https://api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(200, json=fixture_payload)
    )
    forecast = await fetch_pv_forecast(fixture_date, settings=settings)

    assert route.called
    request = route.calls.last.request
    assert request.url.params["latitude"] == "56.918"
    assert request.url.params["longitude"] == "24.043"
    assert request.url.params["timezone"] == "Europe/Riga"

    assert isinstance(forecast, PvForecast)
    assert forecast.target_date == fixture_date
    assert forecast.shortwave_radiation_mj_m2 == pytest.approx(rad)
    assert forecast.expected_kwh == pytest.approx(rad * settings.pv_kwh_per_mj_m2)


@respx.mock
async def test_fetch_pv_forecast_http_500(settings):
    respx.get("https://api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(500, text="upstream sad")
    )
    with pytest.raises(OpenMeteoAPIError, match="HTTP 500"):
        await fetch_pv_forecast(date(2026, 5, 10), settings=settings)


@respx.mock
async def test_fetch_pv_forecast_network_error(settings):
    respx.get("https://api.open-meteo.com/v1/forecast").mock(
        side_effect=httpx.ConnectError("no route")
    )
    with pytest.raises(OpenMeteoAPIError, match="HTTP transport error"):
        await fetch_pv_forecast(date(2026, 5, 10), settings=settings)


@respx.mock
async def test_fetch_pv_forecast_malformed_json(settings):
    respx.get("https://api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(200, text="not json")
    )
    with pytest.raises(OpenMeteoAPIError, match="non-JSON response"):
        await fetch_pv_forecast(date(2026, 5, 10), settings=settings)


@respx.mock
async def test_fetch_pv_forecast_missing_target_date(settings):
    respx.get("https://api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(
            200,
            json={"daily": {"time": ["2026-01-01"], "shortwave_radiation_sum": [5.0]}},
        )
    )
    with pytest.raises(OpenMeteoAPIError, match="not in forecast"):
        await fetch_pv_forecast(date(2026, 5, 10), settings=settings)
