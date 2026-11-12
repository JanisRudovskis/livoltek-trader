"""Tests for the nights-ahead ranking (which coming night is cheapest for the EV)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from livoltek_trader import nights as nights_mod
from livoltek_trader.config import Settings
from livoltek_trader.nights import (
    TEMP_POINTS,
    WIND_POINTS,
    NightModel,
    cheapest_block,
    feature_row,
    fetch_weather,
    forecast_nights,
    outlook,
    power_proxy,
)
from livoltek_trader.solar import OpenMeteoAPIError

UTC = timezone.utc
RIGA = ZoneInfo("Europe/Riga")
D0 = date(2026, 11, 12)  # a Thursday; the run's evening


def _local(d: date, hour: int) -> datetime:
    return (datetime(d.year, d.month, d.day, tzinfo=RIGA) + timedelta(hours=hour)).astimezone(UTC)


def _flat(first: date, last: date, value: float) -> dict[datetime, float]:
    out, t, end = {}, _local(first, 0), _local(last + timedelta(days=1), 0)
    while t < end:
        out[t] = value
        t += timedelta(hours=1)
    return out


def _weather(first: date, last: date, wind_by_lead: dict[int, float], temp_by_lead: dict[int, float]):
    hours, t = [], datetime(first.year, first.month, first.day, tzinfo=UTC)
    end = datetime(last.year, last.month, last.day, tzinfo=UTC) + timedelta(days=1)
    while t < end:
        hours.append(t)
        t += timedelta(hours=1)
    wx = {}
    for pt in [*WIND_POINTS, *TEMP_POINTS]:
        wx[pt] = {}
        for lead in range(2, 8):
            wx[pt][f"wind_speed_100m_previous_day{lead}"] = {h: wind_by_lead.get(lead, 0.0) for h in hours}
            wx[pt][f"temperature_2m_previous_day{lead}"] = {h: temp_by_lead.get(lead, 0.0) for h in hours}
    return wx


def _zero_model(features) -> NightModel:
    n = len(features)
    return NightModel(
        features=tuple(features), mean=(0.0,) * n, scale=(1.0,) * n, coef=(0.0,) * n,
        intercept=0.0, band={k: (-0.1, 0.1) for k in range(1, 7)},
    )


# --- pure pieces --------------------------------------------------------------


def test_power_proxy_follows_a_turbine_curve():
    assert power_proxy(10.0) == 0.0, "below cut-in (3 m/s) a turbine makes nothing"
    assert 0.0 < power_proxy(30.0) < power_proxy(40.0) < 1.0
    assert power_proxy(50.0) == 1.0, "above rated (12 m/s) output is flat"
    assert power_proxy(100.0) == 0.0, "above cut-out (25 m/s) turbines stop"


def test_cheapest_block_looks_only_between_21_and_07_riga():
    prices = _flat(D0, D0 + timedelta(days=1), 100.0)
    prices[_local(D0, 12)] = 0.0  # cheap noon is not part of the night
    for h in (2, 3, 4):  # 02:00-05:00 on the next morning
        prices[_local(D0 + timedelta(days=1), h)] = 10.0
    avg, start = cheapest_block(prices, D0)
    assert avg == pytest.approx(10.0)
    assert start == _local(D0 + timedelta(days=1), 2)


def test_cheapest_block_needs_the_whole_night():
    prices = _flat(D0, D0, 100.0)  # next morning missing
    assert cheapest_block(prices, D0) is None


def test_features_use_the_forecast_issued_k_plus_1_days_before():
    """Lead k+1 keeps every hour of night k at least a day behind the 21:00 decision —
    the same alignment the model was trained on, so no future weather leaks in."""
    lv = _flat(D0 - timedelta(days=6), D0 + timedelta(days=1), 100.0)
    fi = _flat(D0 - timedelta(days=6), D0, 50.0)
    wx = _weather(D0, D0 + timedelta(days=8), wind_by_lead={4: 60.0}, temp_by_lead={2: 10.0, 4: -5.0})

    row = feature_row(D0, 3, lv, fi, wx)
    assert all(row[f"wind_{p}"] == 1.0 for p in WIND_POINTS)
    assert row["temp"] == -5.0
    assert row["tonight_wind"] == 0.0, "tonight's weather comes from the lead-2 forecast"
    assert row["d_temp"] == -15.0
    assert row["k3"] == 1.0 and row["k2"] == 0.0

    other = feature_row(D0, 2, lv, fi, wx)
    assert all(other[f"wind_{p}"] == 0.0 for p in WIND_POINTS)


def test_tomorrow_nights_published_hours_feed_only_k1():
    """In training the history holds later prices too; only D0+1's are known at 21:00."""
    lv = _flat(D0 - timedelta(days=6), D0 + timedelta(days=3), 100.0)
    for day in (1, 2):  # day-ahead for D0+1 is out by the 21:00 run, D0+2 is not
        for h in (21, 22, 23):
            lv[_local(D0 + timedelta(days=day), h)] = 40.0
    fi = _flat(D0 - timedelta(days=6), D0, 50.0)
    wx = _weather(D0, D0 + timedelta(days=8), {}, {})

    assert feature_row(D0, 1, lv, fi, wx)["known_rel"] == pytest.approx(-60.0)
    assert feature_row(D0, 2, lv, fi, wx)["known_rel"] == 0.0


def test_features_are_none_when_price_history_is_short():
    lv = _flat(D0 - timedelta(days=3), D0 + timedelta(days=1), 100.0)
    fi = _flat(D0 - timedelta(days=6), D0, 50.0)
    wx = _weather(D0, D0 + timedelta(days=8), {}, {})
    assert feature_row(D0, 1, lv, fi, wx) is None


def test_model_predicts_level_plus_standardised_dot_product():
    model = NightModel(
        features=("lvl7", "a"), mean=(0.0, 2.0), scale=(1.0, 4.0), coef=(0.0, 8.0),
        intercept=5.0, band={1: (-0.1, 0.2)},
    )
    assert model.predict({"lvl7": 100.0, "a": 6.0}) == pytest.approx(113.0)
    # the band grows with the square root of the price level (errors grow, but
    # slower than the level), and never shrinks below a 30 EUR/MWh level
    assert model.interval({"lvl7": 100.0, "a": 6.0}, 1) == pytest.approx((112.0, 115.0))
    floor = 30.0**0.5
    assert model.interval({"lvl7": 10.0, "a": 2.0}, 1) == pytest.approx(
        (15.0 - 0.1 * floor, 15.0 + 0.2 * floor)
    )


def test_outlook_is_tonight_known_then_six_forecast_nights():
    lv = _flat(D0 - timedelta(days=6), D0 + timedelta(days=1), 100.0)
    for h in (3, 4, 5):
        lv[_local(D0 + timedelta(days=1), h)] = 50.0
    fi = _flat(D0 - timedelta(days=6), D0, 50.0)
    wx = _weather(D0, D0 + timedelta(days=8), {}, {})
    model = _zero_model(feature_row(D0, 1, lv, fi, wx))

    out = outlook(D0, lv, fi, wx, model)

    assert [n.night for n in out] == [D0 + timedelta(days=i) for i in range(7)]
    assert out[0].known and out[0].eur_per_kwh == pytest.approx(0.05)
    assert out[0].block_start == _local(D0 + timedelta(days=1), 3)
    lvl7 = (6 * 100.0 + 50.0) / 7
    assert not out[3].known
    assert out[3].eur_per_kwh == pytest.approx(lvl7 / 1000)
    assert out[3].low == pytest.approx((lvl7 - 0.1 * lvl7**0.5) / 1000)
    assert out[3].high == pytest.approx((lvl7 + 0.1 * lvl7**0.5) / 1000)


def test_shipped_model_matches_the_feature_builder():
    """Guards drift between scripts/train_night_model.py and the runtime features."""
    model = NightModel.load()
    lv = _flat(D0 - timedelta(days=6), D0 + timedelta(days=1), 100.0)
    fi = _flat(D0 - timedelta(days=6), D0, 50.0)
    row = feature_row(D0, 1, lv, fi, _weather(D0, D0 + timedelta(days=8), {}, {}))
    assert set(model.features) == set(row)
    assert len(model.mean) == len(model.scale) == len(model.coef) == len(model.features)
    assert set(model.band) == set(range(1, 7))


# --- I/O ------------------------------------------------------------------------


@respx.mock
async def test_fetch_weather_maps_locations_in_order():
    points = [*WIND_POINTS, *TEMP_POINTS]
    payload = [
        {"hourly": {"time": ["2026-11-12T00:00", "2026-11-12T01:00"],
                    "wind_speed_100m_previous_day2": [float(i), float(i)],
                    "temperature_2m_previous_day2": [None, 3.0]}}
        for i, _ in enumerate(points)
    ]
    route = respx.get("https://previous-runs-api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(200, json=payload)
    )
    wx = await fetch_weather(D0, D0 + timedelta(days=7), settings=Settings())

    params = route.calls.last.request.url.params
    assert params["start_date"] == "2026-11-12" and params["end_date"] == "2026-11-19"
    assert params["timezone"] == "GMT"
    t0 = datetime(2026, 11, 12, 0, tzinfo=UTC)
    for i, pt in enumerate(points):
        assert wx[pt]["wind_speed_100m_previous_day2"][t0] == float(i)
    assert t0 not in wx[points[0]]["temperature_2m_previous_day2"], "nulls are dropped"


@respx.mock
async def test_fetch_weather_raises_on_http_error():
    respx.get("https://previous-runs-api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(502, text="Bad Gateway")
    )
    with pytest.raises(OpenMeteoAPIError):
        await fetch_weather(D0, D0 + timedelta(days=7), settings=Settings())


async def test_forecast_nights_fetches_a_week_of_history_and_the_week_ahead(monkeypatch):
    asked = {}

    async def prices(first_day, last_day, *, regions, settings=None, client=None):
        asked["prices"] = (first_day, last_day, regions)
        from livoltek_trader.elering import PricePeriod

        def periods(src):
            return [PricePeriod(start=t, eur_per_kwh=v / 1000) for t, v in src.items()]

        return {"lv": periods(_flat(first_day, last_day, 100.0)), "fi": periods(_flat(first_day, last_day, 50.0))}

    async def weather(start, end, *, settings, client=None):
        asked["weather"] = (start, end)
        return _weather(start, end, {}, {})

    monkeypatch.setattr(nights_mod, "fetch_price_range", prices)
    monkeypatch.setattr(nights_mod, "fetch_weather", weather)
    out = await forecast_nights(D0, settings=Settings())

    assert asked["prices"] == (D0 - timedelta(days=6), D0 + timedelta(days=1), ("lv", "fi"))
    assert asked["weather"] == (D0, D0 + timedelta(days=7))
    assert len(out) == 7 and out[0].known
