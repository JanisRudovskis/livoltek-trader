"""Minimal unit tests for LivoltekClient.

Playwright integration testing happens via `scripts/livoltek_smoke.py`
against the real portal — too costly and brittle to mock at the page level.
These tests just cover what we can without launching a browser.
"""

from __future__ import annotations

import pytest

from livoltek_trader.config import Settings
from livoltek_trader.livoltek import (
    LivoltekClient,
    DeviceListEmptyError,
    LivoltekError,
    PortalBusyError,
    RetriesExhausted,
    apply_register_steps,
    points_by_address,
    with_retries,
)


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


class _FakeInverter:
    """Behaves like the real device as measured on 2026-11-12: a write is
    accepted at once, but its registers land ONE PER POLL, in order. Optionally
    drops some registers entirely, as the 2026-11-05 write did."""

    def __init__(
        self, values: dict[int, str], drop: set[int] = frozenset(), busy: int = 0
    ) -> None:
        self.values = dict(values)
        self.queue: list[tuple[int, object]] = []
        self.drop = set(drop)
        self.busy = busy  # this many writes are refused as "too soon"
        self.writes: list[dict[int, object]] = []

    def _definitions(self) -> dict[int, dict]:
        return {
            a: {"address": str(a), "pointName": f"reg {a}", "value": v}
            for a, v in self.values.items()
        }

    async def read(self) -> dict[int, dict]:
        if self.queue:
            addr, value = self.queue.pop(0)
            if addr not in self.drop:
                self.values[addr] = str(value)
        return self._definitions()

    async def write(self, points: list[dict]) -> None:
        if self.busy:
            self.busy -= 1
            raise PortalBusyError("POINT_REPEAT_CONTROL_IN_SHORT_TIME")
        self.writes.append({int(p["address"]): p["value"] for p in points})
        self.queue.extend((int(p["address"]), p["value"]) for p in points)


async def _no_sleep(_s: float) -> None:
    return None


async def test_each_step_is_confirmed_on_the_device_before_the_next():
    dev = _FakeInverter({45093: "170", 45009: "170", 45016: "8"})
    steps = [{45093: 85}, {45009: 204, 45016: 0}, {45093: 170}]
    await apply_register_steps(dev, steps, sleep=_no_sleep)
    assert dev.writes == steps
    assert dev.values == {45093: "170", 45009: "204", 45016: "0"}


async def test_a_register_that_never_lands_raises_and_stops_the_sequence():
    """A dropped slot register must not be followed by ToU ON: the device is
    left with ToU off — plain Self-use, the safe state — and the error names
    the register so the ntfy alarm says what failed."""
    dev = _FakeInverter({45093: "170", 45009: "170", 45016: "8"}, drop={45016})
    steps = [{45093: 85}, {45009: 204, 45016: 0}, {45093: 170}]
    with pytest.raises(LivoltekError, match="45016"):
        await apply_register_steps(dev, steps, sleep=_no_sleep, max_polls=6)
    assert dev.writes == steps[:2]
    assert dev.values[45093] == "85"


async def test_written_points_reuse_the_device_definitions_with_new_values():
    # remote/control takes the full point objects from point/info.
    dev = _FakeInverter({45093: "85"})
    await apply_register_steps(dev, [{45093: 170}], sleep=_no_sleep)
    assert dev.writes == [{45093: 170}]


async def test_a_write_refused_as_too_soon_is_retried_after_a_wait():
    """The portal refuses a command that follows the previous one within
    ~10 s (POINT_REPEAT_CONTROL_IN_SHORT_TIME, measured 2026-11-12). A fast
    confirmation makes ToU-off -> slots -> ToU-on hit that, so wait and retry."""
    dev = _FakeInverter({45093: "85", 45009: "204"}, busy=2)
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    await apply_register_steps(dev, [{45009: 170}, {45093: 170}], sleep=sleep)
    assert dev.writes == [{45009: 170}, {45093: 170}]
    assert dev.values == {45093: "170", 45009: "170"}
    assert slept.count(15.0) >= 2


async def test_a_portal_that_stays_busy_fails_without_turning_tou_on():
    dev = _FakeInverter({45093: "85", 45009: "204"}, busy=10_000)
    with pytest.raises(LivoltekError, match="busy"):
        await apply_register_steps(dev, [{45009: 170}, {45093: 170}], sleep=_no_sleep)
    assert dev.writes == [] and dev.values[45093] == "85"


def test_points_by_address_finds_points_nested_anywhere_in_point_info():
    payload = {
        "data": [
            {"groupName": "ToU", "children": [
                {"address": "45093", "pointName": "TOU enable", "value": "85"},
                {"address": None, "pointName": "computed", "value": "x"},
            ]},
            {"list": [{"address": "45009", "pointName": "Time 1 cmd", "value": "170"}]},
        ]
    }
    found = points_by_address(payload)
    assert sorted(found) == [45009, 45093]
    assert found[45093]["value"] == "85"


async def test_writing_an_unknown_register_is_refused_before_anything_is_sent():
    dev = _FakeInverter({45093: "85"})
    with pytest.raises(LivoltekError, match="99999"):
        await apply_register_steps(dev, [{99999: 1}], sleep=_no_sleep)
    assert dev.writes == []


def test_page_raises_before_context_entry():
    client = LivoltekClient(Settings())
    with pytest.raises(LivoltekError, match="not initialised"):
        _ = client.page


# --- telling a transient portal hiccup from a real breakage ---------------------


class _PageWithLabels:
    def __init__(self, labels) -> None:
        self.labels = labels

    async def evaluate(self, _js):
        return self.labels


async def test_an_empty_device_list_is_its_own_transient_error():
    """2026-11-12: the station page sometimes renders no Device List at all
    within 15 s ("Device List shows: []"). Reloading fixes it, so it gets its
    own type — retryable, and countable in the logs."""
    client = LivoltekClient(Settings())
    client._page = _PageWithLabels([])
    err = await client._device_card_error("HP3-10KD2", None)
    assert isinstance(err, DeviceListEmptyError)


async def test_a_device_list_without_our_model_is_not_called_transient():
    # Labels present but no match = markup or model changed. Not a hiccup.
    client = LivoltekClient(Settings())
    client._page = _PageWithLabels(["SN123(DATALOGGER)"])
    err = await client._device_card_error("HP3-10KD2", None)
    assert not isinstance(err, DeviceListEmptyError)
    assert isinstance(err, LivoltekError)


async def test_with_retries_returns_once_an_attempt_succeeds():
    calls = []
    slept: list[float] = []

    async def op():
        calls.append(1)
        if len(calls) < 3:
            raise DeviceListEmptyError("Device List shows: []")
        return "ok"

    async def sleep(s):
        slept.append(s)

    assert await with_retries(op, attempts=3, pause_s=60, what="t", sleep=sleep) == "ok"
    assert len(calls) == 3 and slept == [60, 60]


async def test_with_retries_reports_every_failed_attempt():
    async def op():
        raise DeviceListEmptyError("Device List shows: []")

    with pytest.raises(RetriesExhausted) as info:
        await with_retries(op, attempts=3, pause_s=0, what="t", sleep=_no_sleep)
    msg = str(info.value)
    assert "3" in msg and msg.count("DeviceListEmptyError") == 3
