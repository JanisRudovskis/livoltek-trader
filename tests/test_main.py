"""Tests for the daily entry point's failure handling."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone

import pytest

from livoltek_trader import main as main_mod
from livoltek_trader.config import Settings
from livoltek_trader.elering import ElerinAPIError, PricePeriod
from livoltek_trader.solar import OpenMeteoAPIError, PvForecast

UTC = timezone.utc


@pytest.fixture
def settings() -> Settings:
    return Settings(ntfy_topic="test-topic")


@pytest.fixture
def args() -> argparse.Namespace:
    return argparse.Namespace(date="2026-12-15", execute=False)


def _periods() -> list[PricePeriod]:
    """A full cheap-night / dear-evening day, enough to plan a cycle."""
    base = datetime(2026, 12, 15, 0, 0, tzinfo=UTC)
    prices = (
        [0.05] * 6 + [0.12] + [0.22] * 3 + [0.13] * 6 + [0.18]
        + [0.26] * 4 + [0.10] * 3
    )
    return [
        PricePeriod(start=base + timedelta(hours=h), eur_per_kwh=p)
        for h, p in enumerate(prices)
    ]


class _StubNtfy:
    """Captures sends instead of hitting the network."""

    def __init__(self, *_a, **_kw) -> None:
        self.sent: list[tuple] = []

    async def send(self, message, *, title=None, priority=3, tags=None) -> None:
        self.sent.append((title, message, priority))


@pytest.fixture
def stub_ntfy(monkeypatch) -> _StubNtfy:
    stub = _StubNtfy()
    monkeypatch.setattr(main_mod, "NtfyClient", lambda *a, **kw: stub)
    return stub


async def test_run_survives_pv_forecast_failure(
    monkeypatch, args, settings, stub_ntfy
):
    """A PV outage must not cost the night's schedule.

    `plan_day` already accepts `pv_forecast=None`, and a deep-winter plan
    barely depends on PV, so an Open-Meteo hiccup should degrade to a
    PV-blind plan rather than abort the run.
    """

    async def boom(*_a, **_kw):
        raise OpenMeteoAPIError("open-meteo unreachable")

    async def prices(*_a, **_kw):
        return _periods()

    seen: dict = {}
    real_plan_day = main_mod.plan_day

    def spy_plan_day(hourly, target_date, settings=None, pv_forecast=None):
        seen["pv_forecast"] = pv_forecast
        return real_plan_day(
            hourly, target_date, settings=settings, pv_forecast=pv_forecast
        )

    monkeypatch.setattr(main_mod, "fetch_pv_forecast", boom)
    monkeypatch.setattr(main_mod, "fetch_day_ahead", prices)
    monkeypatch.setattr(main_mod, "plan_day", spy_plan_day)

    rc = await main_mod._run(args, settings)

    assert rc == 0, "a PV failure must not fail the run"
    assert seen["pv_forecast"] is None, "planning must proceed PV-blind"
    assert stub_ntfy.sent, "the plan summary must still be sent"


async def test_run_aborts_when_price_fetch_fails(
    monkeypatch, args, settings, stub_ntfy
):
    """Prices are not optional — without them there is nothing to plan."""

    async def pv(*_a, **_kw):
        return PvForecast(
            target_date=date(2026, 12, 15),
            expected_kwh=2.0,
            shortwave_radiation_mj_m2=0.7,
            sunshine_hours=1.0,
            cloud_cover_pct=90.0,
        )

    async def boom(*_a, **_kw):
        raise ElerinAPIError("elering unreachable")

    monkeypatch.setattr(main_mod, "fetch_pv_forecast", pv)
    monkeypatch.setattr(main_mod, "fetch_day_ahead", boom)

    rc = await main_mod._run(args, settings)

    assert rc == 1
    assert stub_ntfy.sent, "an error notification must be sent"
    assert stub_ntfy.sent[0][2] == 4, "error notifications use priority 4"
