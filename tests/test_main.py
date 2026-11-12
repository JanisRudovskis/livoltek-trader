"""Tests for the daily entry point's failure handling."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone

import pytest

from livoltek_trader import main as main_mod
from livoltek_trader.config import Settings
from livoltek_trader.elering import ElerinAPIError, PricePeriod
from livoltek_trader.solar import OpenMeteoAPIError, PvForecast
from livoltek_trader.telemetry import DailyTotals

UTC = timezone.utc


@pytest.fixture
def settings() -> Settings:
    return Settings(ntfy_topic="test-topic")


@pytest.fixture
def args() -> argparse.Namespace:
    return argparse.Namespace(date="2026-12-15", execute=False, riga_hour=None)


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


class _StubPortal:
    """Stands in for LivoltekClient so no unit test can reach the portal.

    `_run` now opens a read-only portal session to fetch measured load. Without
    this fixture these tests launched a real browser and logged into the live
    portal — 36 s per suite run and a genuine side effect from a unit test.
    """

    def __init__(self, totals=None, fail: bool = False) -> None:
        self.totals = totals if totals is not None else []
        self.fail = fail
        self.write_fail: Exception | None = None
        self.write_failures: list[Exception] = []  # raised once each, in order
        self.write_calls = 0
        self.written: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def login(self) -> None:
        if self.fail:
            raise RuntimeError("portal unreachable")

    async def read_daily_totals(self, start, end):
        if self.fail:
            raise RuntimeError("portal unreachable")
        return list(self.totals)

    async def write_schedule(self, plan) -> None:
        self.write_calls += 1
        if self.write_failures:
            raise self.write_failures.pop(0)
        if self.write_fail is not None:
            raise self.write_fail
        self.written.append(plan)


@pytest.fixture(autouse=True)
def no_retry_pause(monkeypatch) -> None:
    monkeypatch.setattr(main_mod, "PORTAL_RETRY_PAUSE_S", 0)


@pytest.fixture(autouse=True)
def no_real_portal(monkeypatch) -> _StubPortal:
    """Applied to every test in this module: never touch the real portal."""
    stub = _StubPortal()
    monkeypatch.setattr(main_mod, "LivoltekClient", lambda *a, **kw: stub)
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

    def spy_plan_day(hourly, target_date, settings=None, **kwargs):
        seen.update(kwargs)
        return real_plan_day(hourly, target_date, settings=settings, **kwargs)

    monkeypatch.setattr(main_mod, "fetch_pv_forecast", boom)
    monkeypatch.setattr(main_mod, "fetch_day_ahead", prices)
    monkeypatch.setattr(main_mod, "plan_day", spy_plan_day)

    rc = await main_mod._run(args, settings)

    assert rc == 0, "a PV failure must not fail the run"
    assert seen["pv_forecast"] is None
    assert seen["pv_forecast_failed"] is True, (
        "the planner must be told PV is unavailable so it suppresses cycles "
        "rather than paying for grid energy the sun would have supplied"
    )
    assert stub_ntfy.sent, "the plan summary must still be sent"


async def test_run_logs_the_effective_hardware_constants(
    monkeypatch, args, settings, stub_ntfy
):
    """Every run must print the constants that actually took effect.

    These are measured values living as defaults in config.py, but any of them
    can be silently overridden by a stale environment variable on the host —
    which is exactly what happened on 2026-09-01, when Railway still carried
    BATTERY_CAPACITY_KWH=10.24 from the old .env.example and pinned the
    planner to numbers that overstate every cycle. Logging them makes that
    drift visible in the first line of the run instead of invisible.
    """
    import structlog

    async def pv(*_a, **_kw):
        raise OpenMeteoAPIError("skip pv")

    async def prices(*_a, **_kw):
        return _periods()

    monkeypatch.setattr(main_mod, "fetch_pv_forecast", pv)
    monkeypatch.setattr(main_mod, "fetch_day_ahead", prices)

    with structlog.testing.capture_logs() as logs:
        await main_mod._run(args, settings)

    start = next(e for e in logs if e["event"] == "main.start")
    assert start["cycle_output_kwh"] == pytest.approx(
        settings.cycle_output_kwh, abs=0.01
    )
    assert start["battery_capacity_kwh"] == pytest.approx(
        settings.battery_capacity_kwh, abs=0.01
    )
    assert start["battery_drain_hours"] == settings.battery_drain_hours


def _totals(day: str, load: float, imp: float) -> DailyTotals:
    return DailyTotals(
        day=date.fromisoformat(day),
        pv_yield_kwh=10.0,
        grid_import_kwh=imp,
        grid_export_kwh=0.0,
        battery_charged_kwh=5.0,
        battery_discharged_kwh=load - 10.0 + imp + 5.0,
        load_kwh=load,
    )


async def test_run_passes_measured_load_to_the_planner(
    monkeypatch, args, settings, stub_ntfy, no_real_portal
):
    """Measured load must reach the PV gate, and the actuals must reach ntfy."""

    async def pv(*_a, **_kw):
        return PvForecast(
            target_date=date(2026, 12, 15),
            expected_kwh=2.0,
            shortwave_radiation_mj_m2=0.7,
            sunshine_hours=1.0,
            cloud_cover_pct=90.0,
        )

    async def prices(*_a, **_kw):
        return _periods()

    no_real_portal.totals = [
        _totals("2026-12-12", 38.0, 20.0),
        _totals("2026-12-13", 42.0, 24.0),
        _totals("2026-12-14", 40.0, 22.0),
    ]
    seen: dict = {}
    real_plan_day = main_mod.plan_day

    def spy(hourly, target_date, settings=None, **kwargs):
        seen.update(kwargs)
        return real_plan_day(hourly, target_date, settings=settings, **kwargs)

    monkeypatch.setattr(main_mod, "fetch_pv_forecast", pv)
    monkeypatch.setattr(main_mod, "fetch_day_ahead", prices)
    monkeypatch.setattr(main_mod, "plan_day", spy)

    rc = await main_mod._run(args, settings)

    assert rc == 0
    assert seen["measured_daily_load_kwh"] == pytest.approx(40.0)
    # The configured constant is 22.0; a winter house pulling 40 must not be
    # planned against 22, or the gate under-states what there is to displace.
    assert seen["measured_daily_load_kwh"] > settings.expected_daily_load_kwh
    body = stub_ntfy.sent[0][1]
    assert "22.0" in body, "the latest day's grid import belongs in the message"


async def test_run_degrades_when_the_portal_read_fails(
    monkeypatch, args, settings, stub_ntfy, no_real_portal
):
    """A portal read failure must cost nothing but the measured load."""

    async def pv(*_a, **_kw):
        return PvForecast(
            target_date=date(2026, 12, 15),
            expected_kwh=2.0,
            shortwave_radiation_mj_m2=0.7,
            sunshine_hours=1.0,
            cloud_cover_pct=90.0,
        )

    async def prices(*_a, **_kw):
        return _periods()

    no_real_portal.fail = True
    seen: dict = {}
    real_plan_day = main_mod.plan_day

    def spy(hourly, target_date, settings=None, **kwargs):
        seen.update(kwargs)
        return real_plan_day(hourly, target_date, settings=settings, **kwargs)

    monkeypatch.setattr(main_mod, "fetch_pv_forecast", pv)
    monkeypatch.setattr(main_mod, "fetch_day_ahead", prices)
    monkeypatch.setattr(main_mod, "plan_day", spy)

    rc = await main_mod._run(args, settings)

    assert rc == 0, "a read failure must not fail the run"
    assert seen["measured_daily_load_kwh"] is None
    assert stub_ntfy.sent, "the plan must still be notified"
    assert "Vakar" not in stub_ntfy.sent[0][1]
    assert "Šodien" not in stub_ntfy.sent[0][1]


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


# --- 21:00 Riga, all year -------------------------------------------------------


def _pv_ok():
    async def pv(*_a, **_kw):
        return PvForecast(
            target_date=date(2026, 12, 15),
            expected_kwh=2.0,
            shortwave_radiation_mj_m2=0.7,
            sunshine_hours=1.0,
            cloud_cover_pct=90.0,
        )

    return pv


async def test_scheduled_run_outside_its_riga_hour_does_nothing(
    monkeypatch, args, settings, stub_ntfy
):
    """Railway cron is UTC-only, so the job fires at 18:00 AND 19:00 UTC and
    only the one that lands at 21:xx Riga — summer or winter — does work."""
    fetched = []

    async def prices(d, **_kw):
        fetched.append(d)
        return _periods()

    monkeypatch.setattr(main_mod, "fetch_day_ahead", prices)
    monkeypatch.setattr(
        main_mod, "_now_riga", lambda: datetime(2026, 12, 14, 20, 0, tzinfo=main_mod.RIGA_TZ)
    )
    args.riga_hour = 21
    rc = await main_mod._run(args, settings)
    assert rc == 0
    assert fetched == [] and stub_ntfy.sent == []


async def test_scheduled_run_inside_its_riga_hour_plans(
    monkeypatch, args, settings, stub_ntfy
):
    async def prices(*_a, **_kw):
        return _periods()

    monkeypatch.setattr(main_mod, "fetch_pv_forecast", _pv_ok())
    monkeypatch.setattr(main_mod, "fetch_day_ahead", prices)
    monkeypatch.setattr(
        main_mod, "_now_riga", lambda: datetime(2026, 12, 14, 21, 3, tzinfo=main_mod.RIGA_TZ)
    )
    args.riga_hour = 21
    assert await main_mod._run(args, settings) == 0
    assert stub_ntfy.sent


# --- writing the schedule and EV advice ----------------------------------------------


async def test_execute_writes_the_plan_through_the_register_api(
    monkeypatch, args, settings, stub_ntfy, no_real_portal
):
    async def prices(*_a, **_kw):
        return _periods()

    monkeypatch.setattr(main_mod, "fetch_pv_forecast", _pv_ok())
    monkeypatch.setattr(main_mod, "fetch_day_ahead", prices)
    args.execute = True
    assert await main_mod._run(args, settings) == 0
    assert len(no_real_portal.written) == 1
    assert no_real_portal.written[0].target_date == date(2026, 12, 15)


async def test_unconfirmed_write_sends_an_alarm(
    monkeypatch, args, settings, stub_ntfy, no_real_portal
):
    from livoltek_trader.livoltek import LivoltekError

    async def prices(*_a, **_kw):
        return _periods()

    monkeypatch.setattr(main_mod, "fetch_pv_forecast", _pv_ok())
    monkeypatch.setattr(main_mod, "fetch_day_ahead", prices)
    no_real_portal.write_fail = LivoltekError("inverter did not confirm registers [45016]")
    args.execute = True
    assert await main_mod._run(args, settings) == 1
    title, body, prio = stub_ntfy.sent[-1]
    assert prio == 4 and "45016" in body


async def test_message_carries_ev_advice_using_tonights_prices_too(
    monkeypatch, args, settings, stub_ntfy
):
    fetched = []

    async def prices(d, **_kw):
        fetched.append(d)
        return _periods()

    monkeypatch.setattr(main_mod, "fetch_pv_forecast", _pv_ok())
    monkeypatch.setattr(main_mod, "fetch_day_ahead", prices)
    assert await main_mod._run(args, settings) == 0
    assert date(2026, 12, 14) in fetched, "tonight's prices feed the night window"
    assert "EV uzlāde" in stub_ntfy.sent[0][1]


async def test_tonights_prices_are_optional(monkeypatch, args, settings, stub_ntfy):
    async def prices(d, **_kw):
        if d == date(2026, 12, 14):
            raise ElerinAPIError("today unavailable")
        return _periods()

    monkeypatch.setattr(main_mod, "fetch_pv_forecast", _pv_ok())
    monkeypatch.setattr(main_mod, "fetch_day_ahead", prices)
    assert await main_mod._run(args, settings) == 0
    assert stub_ntfy.sent


# --- transient portal failures are retried ---------------------------------------


async def test_portal_write_is_retried_after_a_transient_failure(
    monkeypatch, args, settings, stub_ntfy, no_real_portal
):
    import structlog
    from livoltek_trader.livoltek import DeviceListEmptyError

    async def prices(*_a, **_kw):
        return _periods()

    monkeypatch.setattr(main_mod, "fetch_pv_forecast", _pv_ok())
    monkeypatch.setattr(main_mod, "fetch_day_ahead", prices)
    no_real_portal.write_failures = [DeviceListEmptyError("Device List shows: []")]
    args.execute = True
    with structlog.testing.capture_logs() as logs:
        rc = await main_mod._run(args, settings)
    assert rc == 0
    assert no_real_portal.write_calls == 2 and len(no_real_portal.written) == 1
    failed = [e for e in logs if e["event"].endswith("attempt_failed")]
    assert failed and failed[0]["error_type"] == "DeviceListEmptyError"
    assert all(prio != 4 for _t, _b, prio in stub_ntfy.sent), "no alarm on recovery"


async def test_when_every_attempt_fails_one_alarm_lists_them(
    monkeypatch, args, settings, stub_ntfy, no_real_portal
):
    from livoltek_trader.livoltek import DeviceListEmptyError

    async def prices(*_a, **_kw):
        return _periods()

    monkeypatch.setattr(main_mod, "fetch_pv_forecast", _pv_ok())
    monkeypatch.setattr(main_mod, "fetch_day_ahead", prices)
    no_real_portal.write_fail = DeviceListEmptyError("Device List shows: []")
    args.execute = True
    assert await main_mod._run(args, settings) == 1
    assert no_real_portal.write_calls == main_mod.PORTAL_ATTEMPTS
    alarms = [m for m in stub_ntfy.sent if m[2] == 4]
    assert len(alarms) == 1
    assert alarms[0][1].count("DeviceListEmptyError") == main_mod.PORTAL_ATTEMPTS
