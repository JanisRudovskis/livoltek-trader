"""Minimal unit tests for LivoltekClient.

Playwright integration testing happens via `scripts/livoltek_smoke.py`
against the real portal — too costly and brittle to mock at the page level.
These tests just cover what we can without launching a browser.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from livoltek_trader.config import Settings
from livoltek_trader.livoltek import LivoltekClient, LivoltekError, _needs_save
from livoltek_trader.strategy import DailyPlan, TradingWindow


def test_url_fragments_match_real_portal_paths():
    assert LivoltekClient.HOME_URL_FRAGMENT == "/customer/homePage"
    assert LivoltekClient.STATION_URL_FRAGMENT == "/customer/tocStation/"
    assert LivoltekClient.DEVICE_URL_FRAGMENT == "/customer/tocDevice/"
    assert LivoltekClient.EU_HOST == "evs.livoltek-portal.com"


def test_client_constructs_with_explicit_settings():
    settings = Settings(
        livoltek_username="user", livoltek_password="pass", livoltek_headless=True
    )
    client = LivoltekClient(settings)
    assert client._settings.livoltek_username == "user"


async def test_login_raises_when_credentials_missing():
    settings = Settings(livoltek_username="", livoltek_password="")
    client = LivoltekClient(settings)
    # We don't enter the context manager — Playwright isn't launched — but
    # login() must validate credentials before touching the page.
    client._page = object()  # sentinel so the .page property doesn't raise
    with pytest.raises(LivoltekError, match="LIVOLTEK_USERNAME"):
        await client.login()


def _plan(*, empty: bool) -> DailyPlan:
    if empty:
        return DailyPlan(
            target_date=date(2026, 9, 3),
            cycles=[],
            skipped_reason="PV covers the day",
            total_net_profit_eur=0.0,
        )
    window = TradingWindow(
        start=datetime(2026, 9, 3, 4, tzinfo=timezone.utc),
        end=datetime(2026, 9, 3, 6, tzinfo=timezone.utc),
        avg_eur_per_kwh=0.05,
    )
    return DailyPlan(
        target_date=date(2026, 9, 3),
        cycles=[],
        skipped_reason=None,
        total_net_profit_eur=0.0,
        stop_window=window,
    )


def test_needs_save_when_slots_were_written():
    assert _needs_save(_plan(empty=False), tou_changed=False) is True


def test_needs_save_when_the_tou_toggle_was_flipped():
    assert _needs_save(_plan(empty=True), tou_changed=True) is True


def test_no_save_when_an_empty_plan_changed_nothing():
    """An empty plan on a day ToU is already off leaves the form untouched.

    Clicking Save then produces no success toast, because the portal has
    nothing to issue — which fired `livoltek.save.no_success_toast_seen` every
    such night. A warning that cries wolf daily trains everyone to ignore it,
    so it must not fire when there was genuinely nothing to save.
    """
    assert _needs_save(_plan(empty=True), tou_changed=False) is False


def test_page_raises_before_context_entry():
    client = LivoltekClient(Settings())
    with pytest.raises(LivoltekError, match="not initialised"):
        _ = client.page
