"""Tests for ntfy client and message formatting."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import httpx
import pytest
import respx

from livoltek_trader.config import Settings
from livoltek_trader.notify import (
    NtfyClient,
    NtfyError,
    format_error_message,
    format_plan_message,
)
from livoltek_trader.solar import PvForecast
from livoltek_trader.strategy import CyclePair, DailyPlan, HourlyPrice, TradingWindow

UTC = timezone.utc


@pytest.fixture
def settings() -> Settings:
    return Settings(
        ntfy_base_url="https://ntfy.sh",
        ntfy_topic="livoltek-trader-test-abc123",
        ntfy_token="",
        ntfy_timeout_s=5.0,
    )


def _window(h_start: int, h_end: int, price: float) -> TradingWindow:
    return TradingWindow(
        start=datetime(2026, 5, 9, h_start, 0, tzinfo=UTC),
        end=datetime(2026, 5, 9, h_end, 0, tzinfo=UTC),
        avg_eur_per_kwh=price,
    )


def _cycle(charge_h, disch_h, charge_price, disch_price, net=0.13) -> CyclePair:
    return CyclePair(
        charge=_window(charge_h, charge_h + 2, charge_price),
        discharge=_window(disch_h, disch_h + 2, disch_price),
        gross_revenue_eur=0.63,
        wear_cost_eur=0.50,
        net_profit_eur=net,
    )


# --- NtfyClient.send ---------------------------------------------------------


@respx.mock
async def test_send_posts_json_with_topic_and_message(settings):
    route = respx.post("https://ntfy.sh").mock(return_value=httpx.Response(200))
    client = NtfyClient(settings)
    await client.send("hello world")

    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "topic": "livoltek-trader-test-abc123",
        "message": "hello world",
    }


@respx.mock
async def test_send_includes_title_priority_tags(settings):
    route = respx.post("https://ntfy.sh").mock(return_value=httpx.Response(200))
    client = NtfyClient(settings)
    await client.send("body", title="Hi", priority=5, tags=["battery", "warning"])

    body = json.loads(route.calls.last.request.content)
    assert body["title"] == "Hi"
    assert body["priority"] == 5
    assert body["tags"] == ["battery", "warning"]


@respx.mock
async def test_send_handles_unicode_title(settings):
    route = respx.post("https://ntfy.sh").mock(return_value=httpx.Response(200))
    await NtfyClient(settings).send("body", title="Plāns izlaiž — žēl")
    body = json.loads(route.calls.last.request.content)
    assert body["title"] == "Plāns izlaiž — žēl"


@respx.mock
async def test_send_includes_bearer_token_when_configured(settings):
    settings = settings.model_copy(update={"ntfy_token": "secret-xyz"})
    route = respx.post("https://ntfy.sh").mock(return_value=httpx.Response(200))
    await NtfyClient(settings).send("hi")
    assert route.calls.last.request.headers["Authorization"] == "Bearer secret-xyz"


@respx.mock
async def test_send_omits_priority_field_when_default(settings):
    route = respx.post("https://ntfy.sh").mock(return_value=httpx.Response(200))
    await NtfyClient(settings).send("hi")
    body = json.loads(route.calls.last.request.content)
    assert "priority" not in body


async def test_send_raises_when_topic_empty():
    settings = Settings(ntfy_topic="")
    with pytest.raises(NtfyError, match="NTFY_TOPIC is not configured"):
        await NtfyClient(settings).send("hi")


@respx.mock
async def test_send_raises_on_non_2xx(settings):
    respx.post("https://ntfy.sh").mock(
        return_value=httpx.Response(403, text="forbidden")
    )
    with pytest.raises(NtfyError, match="HTTP 403"):
        await NtfyClient(settings).send("hi")


@respx.mock
async def test_send_raises_on_transport_error(settings):
    respx.post("https://ntfy.sh").mock(
        side_effect=httpx.ConnectError("no route")
    )
    with pytest.raises(NtfyError, match="transport error"):
        await NtfyClient(settings).send("hi")


# --- format_plan_message (minimal slot listing) ---------------------------


def _pv_forecast(kwh: float, cloud_pct: float = 50.0) -> PvForecast:
    return PvForecast(
        target_date=date(2026, 5, 9),
        expected_kwh=kwh,
        shortwave_radiation_mj_m2=kwh / 2.98,
        sunshine_hours=4.0,
        cloud_cover_pct=cloud_pct,
    )


def test_format_plan_message_empty_plan_says_tou_off():
    plan = DailyPlan(
        target_date=date(2026, 5, 10),
        cycles=[],
        skipped_reason="PV forecast 30 kWh covers expected load 22 kWh",
        total_net_profit_eur=0.0,
    )
    title, body, tags = format_plan_message(plan, pv_forecast=_pv_forecast(30.0))
    assert "2026-05-10" in title
    assert "ToU izslēgts" in title
    assert "ToU izslēgts" in body
    assert "PV: 30.0 kWh" in body
    assert "sunny" in tags


def test_format_plan_message_single_cycle_listed_with_riga_times():
    # UTC 11:00 -> Riga 14:00 (EEST in May)
    plan = DailyPlan(
        target_date=date(2026, 5, 9),
        cycles=[_cycle(charge_h=11, disch_h=18, charge_price=0.006, disch_price=0.144, net=0.13)],
        skipped_reason=None,
        total_net_profit_eur=0.13,
    )
    title, body, tags = format_plan_message(plan, pv_forecast=_pv_forecast(8.0))
    assert "1 cikls" in title
    assert "PV: 8.0 kWh" in body
    assert "14:00-16:00 Charge" in body
    # We no longer show discharge times — only the charge slot is written
    # to the portal; discharge is implicit via Self-use mode.
    assert "21:00" not in body
    assert "battery" in tags


def test_format_plan_message_two_cycles_listed_in_order():
    plan = DailyPlan(
        target_date=date(2026, 5, 9),
        cycles=[
            _cycle(charge_h=2, disch_h=8, charge_price=0.05, disch_price=0.30, net=0.20),
            _cycle(charge_h=12, disch_h=18, charge_price=0.06, disch_price=0.28, net=0.15),
        ],
        skipped_reason=None,
        total_net_profit_eur=0.35,
    )
    title, body, _ = format_plan_message(plan, pv_forecast=_pv_forecast(8.0))
    assert "2 cikli" in title
    # Both charge windows listed, in UTC→Riga order
    assert "05:00-07:00 Charge" in body
    assert "15:00-17:00 Charge" in body


def test_format_plan_message_stop_window_only():
    window = TradingWindow(
        start=datetime(2026, 5, 14, 3, 0, tzinfo=UTC),   # Riga 06:00
        end=datetime(2026, 5, 14, 7, 0, tzinfo=UTC),     # Riga 10:00
        avg_eur_per_kwh=0.08,
    )
    plan = DailyPlan(
        target_date=date(2026, 5, 14),
        cycles=[],
        skipped_reason=None,
        total_net_profit_eur=0.0,
        stop_window=window,
    )
    title, body, tags = format_plan_message(plan, pv_forecast=_pv_forecast(71.0))
    assert "Stop" in title
    assert "cikls" not in title and "cikli" not in title
    assert "06:00-10:00 Stop" in body
    assert "PV: 71.0 kWh" in body
    assert "sunny" in tags


def test_format_plan_message_stop_plus_cycle():
    window = TradingWindow(
        start=datetime(2026, 5, 14, 3, 0, tzinfo=UTC),   # Riga 06:00
        end=datetime(2026, 5, 14, 7, 0, tzinfo=UTC),     # Riga 10:00
        avg_eur_per_kwh=0.08,
    )
    plan = DailyPlan(
        target_date=date(2026, 5, 14),
        cycles=[_cycle(charge_h=11, disch_h=18, charge_price=0.01, disch_price=0.14, net=0.50)],
        skipped_reason=None,
        total_net_profit_eur=0.50,
        stop_window=window,
    )
    title, body, tags = format_plan_message(plan, pv_forecast=_pv_forecast(25.0))
    assert "Stop + 1 cikls" in title
    # Stop slot first, then the Charge slot
    assert body.index("06:00-10:00 Stop") < body.index("14:00-16:00 Charge")
    assert "battery" in tags


def test_format_plan_message_omits_pv_line_when_forecast_not_supplied():
    plan = DailyPlan(
        target_date=date(2026, 5, 9),
        cycles=[_cycle(charge_h=2, disch_h=18, charge_price=0.05, disch_price=0.30, net=0.62)],
        skipped_reason=None,
        total_net_profit_eur=0.62,
    )
    _, body, _ = format_plan_message(plan)
    assert "PV:" not in body
    assert "05:00-07:00 Charge" in body


# --- format_error_message ----------------------------------------------------


def test_format_error_message_has_high_priority_and_includes_type():
    title, body, priority = format_error_message(
        "Elering fetch", RuntimeError("upstream timeout")
    )
    assert "Elering fetch" in title
    assert "RuntimeError" in body
    assert "upstream timeout" in body
    assert priority == 4
