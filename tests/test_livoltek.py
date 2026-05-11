"""Minimal unit tests for LivoltekClient.

Playwright integration testing happens via `scripts/livoltek_smoke.py`
against the real portal — too costly and brittle to mock at the page level.
These tests just cover what we can without launching a browser.
"""

from __future__ import annotations

import pytest

from livoltek_trader.config import Settings
from livoltek_trader.livoltek import LivoltekClient, LivoltekError


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


def test_page_raises_before_context_entry():
    client = LivoltekClient(Settings())
    with pytest.raises(LivoltekError, match="not initialised"):
        _ = client.page
