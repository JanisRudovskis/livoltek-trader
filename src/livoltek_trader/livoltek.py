"""Livoltek cloud portal automation via Playwright.

The portal (https://www.livoltek-portal.com) is a Vue SPA with hash routing.
Deep URL navigation does not work — every entry must traverse from the
Homepage forward (Site card → Device card → tab → sub-tab). Cookie/session
state is persisted to disk so we don't burn a fresh login every day.

Log in, navigate, read history, and write the daily schedule. The schedule
is written as device registers through the portal's own parameter API and
confirmed by reading them back — not by filling the System-mode form, which
sent only a diff and left "cleared" slots live on the inverter (see `tou`).
"""

from __future__ import annotations

import asyncio
import calendar
import json
from typing import Awaitable, Callable, Protocol
from contextlib import AsyncExitStack
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from livoltek_trader.config import Settings, get_settings
from livoltek_trader.strategy import DailyPlan
from livoltek_trader.telemetry import DailyTotals, parse_daily_rows
from livoltek_trader.tou import pending, slots_for_plan, write_steps

RIGA_TZ = ZoneInfo("Europe/Riga")

log = structlog.get_logger(__name__)


class LivoltekError(RuntimeError):
    """Raised on login, navigation or schedule-write failure."""


class PortalBusyError(LivoltekError):
    """The portal refused a command because the previous one was too recent.

    It answers `POINT_REPEAT_CONTROL_IN_SHORT_TIME` to a command sent within
    roughly 10 s of the last one (measured 2026-11-12: refused ~5 s after,
    accepted 11 s after). Transient: wait and send again.
    """


class PointApi(Protocol):
    """The portal's device-parameter endpoints, as the write loop needs them."""

    async def read(self) -> dict[int, dict]:
        """Device point definitions with current values, keyed by address."""

    async def write(self, points: list[dict]) -> None:
        """Issue register writes; returns once the portal has queued them."""


REGISTER_POLL_S = 5.0
"""How often the device is re-read while a write lands. Measured 2026-11-12:
registers are applied one at a time, about 5-20 s apart."""

BUSY_RETRY_S = 15.0
BUSY_RETRIES = 20
"""A refused-as-too-soon write is retried every 15 s for up to 5 minutes."""


def points_by_address(payload) -> dict[int, dict]:
    """Every point object in a `point/info` response, keyed by register address.

    The response nests points in groups; only the objects matter, so walk the
    whole structure rather than pinning its shape.
    """
    found: dict[int, dict] = {}

    def walk(node) -> None:
        if isinstance(node, dict):
            address = node.get("address")
            if "pointName" in node and str(address or "").isdigit():
                found.setdefault(int(address), node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


async def apply_register_steps(
    api: PointApi,
    steps: list[dict[int, int | str]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_polls: int | None = None,
) -> None:
    """Write each step and wait until the DEVICE reports it, then the next.

    The portal's "issued successfully" only means it queued the command. On
    2026-11-05 part of a write never reached the inverter and nothing noticed,
    so success is now defined as reading the values back. If a step is not
    confirmed the sequence stops: later steps (ToU on) are never sent, which
    leaves the inverter in plain Self-use rather than half-programmed.
    """
    for step in steps:
        definitions = await api.read()
        unknown = sorted(a for a in step if a not in definitions)
        if unknown:
            raise LivoltekError(
                f"registers {unknown} are not in the device point list; "
                f"the portal register map changed"
            )
        points = [{**definitions[a], "value": value} for a, value in step.items()]
        for attempt in range(BUSY_RETRIES):
            try:
                await api.write(points)
                break
            except PortalBusyError:
                log.info("livoltek.registers.portal_busy_retry", attempt=attempt + 1)
                await sleep(BUSY_RETRY_S)
        else:
            raise LivoltekError(
                f"portal stayed busy (refused as too soon) for "
                f"{BUSY_RETRIES} attempts; schedule write stopped"
            )
        polls = max_polls or (12 + 3 * len(step))
        left = dict(step)
        for _ in range(polls):
            await sleep(REGISTER_POLL_S)
            current = await api.read()
            left = pending(step, {a: p.get("value") for a, p in current.items()})
            if not left:
                break
        else:
            raise LivoltekError(
                f"inverter did not confirm registers {sorted(left)} after "
                f"{polls} reads; schedule write stopped"
            )
        log.info("livoltek.registers.confirmed", count=len(step))


class LivoltekClient:
    """Async Playwright client for the Livoltek EU&MEA monitoring portal."""

    HOME_URL_FRAGMENT = "/customer/homePage"
    STATION_URL_FRAGMENT = "/customer/tocStation/"
    DEVICE_URL_FRAGMENT = "/customer/tocDevice/"
    EU_HOST = "evs.livoltek-portal.com"
    OPERATION_PATH = "/hess-ota/device/operation"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._stack: AsyncExitStack | None = None
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def __aenter__(self) -> "LivoltekClient":
        self._stack = AsyncExitStack()
        try:
            self._playwright = await self._stack.enter_async_context(async_playwright())
            self._browser = await self._playwright.chromium.launch(
                headless=self._settings.livoltek_headless
            )
            self._stack.push_async_callback(self._browser.close)

            ctx_kwargs: dict = {"viewport": {"width": 1366, "height": 900}}
            storage_path = Path(self._settings.livoltek_storage_state_path)
            if storage_path.exists():
                ctx_kwargs["storage_state"] = str(storage_path)
                log.info("livoltek.storage_state.loaded", path=str(storage_path))

            self._context = await self._browser.new_context(**ctx_kwargs)
            self._stack.push_async_callback(self._context.close)

            self._page = await self._context.new_page()
            self._page.set_default_timeout(
                self._settings.livoltek_browser_timeout_s * 1000
            )
        except Exception:
            if self._stack:
                await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._stack:
            await self._stack.aclose()

    @property
    def page(self) -> Page:
        if not self._page:
            raise LivoltekError("client not initialised — use 'async with'")
        return self._page

    async def login(self) -> None:
        """Open the portal, switch to EU&MEA, sign in, and persist storage state.

        Idempotent: if the saved storage state restored a live session, the
        portal will redirect straight to the homepage and we return early.
        """
        if not self._settings.livoltek_username or not self._settings.livoltek_password:
            raise LivoltekError(
                "LIVOLTEK_USERNAME and LIVOLTEK_PASSWORD must be set in .env"
            )

        page = self.page
        await page.goto(
            self._settings.livoltek_portal_url, wait_until="domcontentloaded"
        )

        await self._resolve_tips_popup()
        await self._dismiss_announcement_dialog()

        if await self._reached_home():
            log.info("livoltek.login.session_restored")
            return

        await self._dismiss_cookies()
        await self._dismiss_announcement_dialog()
        if self.EU_HOST not in page.url:
            await self._switch_to_eu_server_if_needed()
        await self._fill_credentials_and_submit()
        await page.wait_for_url(f"**{self.HOME_URL_FRAGMENT}**", timeout=15000)
        log.info("livoltek.login.success")
        await self._save_storage_state()

    async def _reached_home(self, timeout_ms: int = 3000) -> bool:
        try:
            await self.page.wait_for_url(
                f"**{self.HOME_URL_FRAGMENT}**", timeout=timeout_ms
            )
            return True
        except PlaywrightTimeoutError:
            return False

    async def _dismiss_cookies(self) -> None:
        try:
            await self.page.get_by_role(
                "button", name="OK,I agree"
            ).click(timeout=3000)
            log.info("livoltek.cookies.dismissed")
        except PlaywrightTimeoutError:
            pass

    async def _switch_to_eu_server_if_needed(self) -> None:
        page = self.page
        if self.EU_HOST in page.url:
            return

        server_input = page.locator('input[readonly][placeholder="Select"]').first
        await server_input.click()
        await page.locator(
            '.el-select-dropdown__item:has-text("EU&MEA Server")'
        ).first.click()

        await page.wait_for_url(f"https://{self.EU_HOST}/**", timeout=10000)
        await self._resolve_tips_popup()

    async def _resolve_tips_popup(self, timeout_ms: int = 3000) -> None:
        """Resolve the Element UI 'Tips' MessageBox so we end on the EU host.

        The dialog body distinguishes the two possible asks:
        - "...return to the EU&MEA Server..."   → click Confirm (go to EU)
        - "...return to the International Server..." → click Cancel (stay on EU)
        - anything else → click Cancel as a safe default
        """
        try:
            dialog = self.page.get_by_role("dialog", name="Tips")
            await dialog.wait_for(state="visible", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            return

        body = (
            await dialog.locator(".el-message-box__message").inner_text()
        ).strip()
        if "return to the EU&MEA" in body:
            button = "Confirm"
        else:
            button = "Cancel"
        await dialog.get_by_role("button", name=button).click()
        log.info("livoltek.tips_popup.resolved", button=button, body=body[:80])

    async def _dismiss_announcement_dialog(self, timeout_ms: int = 3000) -> None:
        """Close any visible Element UI `el-dialog__wrapper` (maintenance notices, EULA, etc.).

        The portal occasionally pops up these announcements between sessions.
        They have no stable aria-label, so we identify them by class and click
        the primary (Confirm) button via JS. Different DOM family than the Tips
        MessageBox handled by `_resolve_tips_popup`.
        """
        try:
            result = await self.page.evaluate(
                """() => {
                    const wrappers = Array.from(document.querySelectorAll('.el-dialog__wrapper'));
                    for (const w of wrappers) {
                        if (window.getComputedStyle(w).display === 'none') continue;
                        const btn = w.querySelector('.el-button--primary')
                            || w.querySelector('button[type="button"]');
                        if (btn) {
                            btn.click();
                            return { dismissed: true, text: (w.innerText || '').slice(0, 120) };
                        }
                    }
                    return { dismissed: false };
                }"""
            )
            if result.get("dismissed"):
                log.info(
                    "livoltek.announcement.dismissed",
                    preview=result.get("text", ""),
                )
                await asyncio.sleep(0.5)
        except Exception as exc:
            log.warning("livoltek.announcement.dismiss_error", error=str(exc))

    async def _fill_credentials_and_submit(self) -> None:
        page = self.page
        await self._resolve_tips_popup(timeout_ms=500)
        await self._dismiss_announcement_dialog(timeout_ms=500)
        await page.get_by_placeholder("Account or Email").fill(
            self._settings.livoltek_username
        )
        await page.get_by_placeholder("Password").fill(
            self._settings.livoltek_password
        )
        await page.get_by_role("button", name="Login", exact=True).click()

    async def _save_storage_state(self) -> None:
        if not self._context:
            return
        path = Path(self._settings.livoltek_storage_state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        await self._context.storage_state(path=str(path))
        log.info("livoltek.storage_state.saved", path=str(path))

    async def navigate_to_device(self) -> None:
        """Walk from the homepage to the inverter's device page.

        Stops before any tab is opened, so callers can pick `Params set`
        (the write path) or `Data report` (the read-only history path).

        This is the ONE place the homepage → station → device walk lives.
        It used to be copy-pasted into every script under `scripts/`, which is
        why the 2026-09-02 selector change broke the production run and then
        broke five diagnostic scripts separately, days apart.
        """
        page = self.page
        if self.HOME_URL_FRAGMENT not in page.url:
            await page.goto(
                f"https://{self.EU_HOST}/#{self.HOME_URL_FRAGMENT}",
                wait_until="domcontentloaded",
            )
            await page.wait_for_url(
                f"**{self.HOME_URL_FRAGMENT}**", timeout=10000
            )

        await page.locator(".deviceIcon").first.click()
        await page.wait_for_url(
            f"**{self.STATION_URL_FRAGMENT}**", timeout=15000
        )
        log.info("livoltek.nav.station_reached")

        await self._open_device_card()
        await page.wait_for_url(
            f"**{self.DEVICE_URL_FRAGMENT}**", timeout=15000
        )
        log.info("livoltek.nav.device_reached")

    DAILY_REPORT_PATH = "/ctrller-manager/powerstation/reportForm"

    async def read_daily_totals(
        self, start: date, end: date
    ) -> list[DailyTotals]:
        """Read the portal's daily energy table for [start, end]. Read-only.

        Walks to the device page and opens `Data report`, which is what makes
        the SPA fire the request we need — not for its response, but to capture
        its `authorization` header. Cookies alone are not enough: replaying the
        endpoint without that header returns an all-null shape that looks
        exactly like "no data retained".

        Never touches `Params set`, so this cannot write to the inverter.
        """
        page = self.page
        captured: dict = {}

        def on_request(request) -> None:
            if self.DAILY_REPORT_PATH not in request.url:
                return
            if "headers" in captured:
                return
            captured["headers"] = {
                k: v
                for k, v in (request.headers or {}).items()
                if k.lower() not in ("host", "content-length", "connection")
            }
            try:
                captured["station_id"] = json.loads(
                    request.post_data or "{}"
                ).get("id")
            except (ValueError, TypeError):
                pass

        page.on("request", on_request)
        try:
            await self.navigate_to_device()
            await page.get_by_role("tab", name="Data report", exact=True).click(
                timeout=15000
            )
            # The tab fires its requests asynchronously after the click.
            for _ in range(20):
                if "headers" in captured:
                    break
                await asyncio.sleep(0.5)
        except PlaywrightTimeoutError as exc:
            raise LivoltekError(
                "could not open the 'Data report' tab to read daily totals"
            ) from exc
        finally:
            page.remove_listener("request", on_request)

        if "headers" not in captured or captured.get("station_id") is None:
            raise LivoltekError(
                "did not observe a daily-report request, so could not capture "
                "the authorization header the endpoint requires"
            )

        rows: list = []
        # One request per calendar month — that is the granularity the endpoint
        # takes (timeType 1).
        month = date(start.year, start.month, 1)
        while month <= end:
            last = calendar.monthrange(month.year, month.month)[1]
            response = await page.request.post(
                f"https://{self.EU_HOST}{self.DAILY_REPORT_PATH}",
                headers=captured["headers"],
                data={
                    "id": captured["station_id"],
                    "timeType": 1,
                    "startTime": f"{month.isoformat()} 00:00:00",
                    "endTime": (
                        f"{month.year:04d}-{month.month:02d}-{last:02d} 23:59:59"
                    ),
                },
                timeout=30000,
            )
            payload = json.loads(await response.text())
            rows.extend((payload or {}).get("data") or [])
            month = date(
                month.year + (month.month == 12), (month.month % 12) + 1, 1
            )

        totals = [t for t in parse_daily_rows(rows) if start <= t.day <= end]
        unbalanced = [t.day.isoformat() for t in totals if not t.balances]
        if unbalanced:
            # PV + import + discharge should equal load + export + charge. If it
            # stops holding, the portal's column mapping has changed and every
            # number derived from it is suspect.
            log.warning(
                "livoltek.daily_totals.energy_balance_off", days=unbalanced[:5]
            )
        log.info("livoltek.daily_totals.read", days=len(totals))
        return totals

    async def navigate_to_system_mode(self) -> None:
        """Walk to Params set → System mode and read the inverter's params.

        After this returns, the System mode form is populated with the
        inverter's current values (Work mode, Enable ToU toggle, schedule
        rows, Grid Charging toggle).
        """
        page = self.page
        await self.navigate_to_device()

        await page.get_by_role("tab", name="Params set", exact=True).click()
        await page.get_by_role("tab", name="System mode", exact=True).click()
        log.info("livoltek.nav.system_mode_tab_opened")

        await page.get_by_role("button", name="Read Params.").first.click()
        await asyncio.sleep(2.0)
        log.info("livoltek.nav.read_params_clicked")

    async def _open_device_card(self) -> None:
        """Click the inverter's card in the station page's Device List.

        Anchored on the MODEL TEXT, not on the card image. The Device List
        renders one row per device, each an unclassed `<div>` holding exactly
        one card image plus an `el-descriptions` block whose title reads
        `<serial>(<model>)`. So: from the label, walk to the nearest ancestor
        div that contains an image, and click that image.

        Why not the image directly: the previous selector was
        `img[src*="hp3_online"]`, which encoded both the portal's asset
        filename and the device's online state. On 2026-09-02 the portal
        inlined those images as base64 data URIs; the selector stopped
        matching and every run died on a 30 s timeout before writing a
        schedule. Matching on the model survives both an asset rename and the
        device going offline.
        """
        page = self.page
        model = self._settings.livoltek_device_model
        if '"' in model:
            raise LivoltekError(
                f"device model must not contain a double quote: {model!r}"
            )

        # Scope to the Device List and to the card TITLE div. A first attempt
        # used `//*[contains(text(),"<model>")]` unscoped: it matched 24
        # elements (the model string also appears in the legend and alarm
        # tables), `.first` picked the datalogger's card image instead of the
        # inverter's, and the click silently went nowhere. It happened to work
        # the day it was written because the match set was smaller then.
        selector = (
            f'xpath=//div[contains(@class,"deviceListToc")]'
            f'//div[contains(@class,"el-descriptions__title")]'
            f'[contains(.,"{model}")]'
            f"/ancestor::div[.//img][1]//img"
        )
        cards = page.locator(selector)
        try:
            await cards.first.wait_for(state="visible", timeout=15000)
        except PlaywrightTimeoutError as exc:
            raise await self._device_card_error(model, exc)

        # Insist on exactly one match. An ambiguous selector that clicks the
        # wrong card is far worse than one that refuses: the click "succeeds",
        # navigation never happens, and the failure surfaces 15 s later as an
        # unrelated timeout.
        count = await cards.count()
        if count != 1:
            raise await self._device_card_error(
                model, None, f"selector matched {count} elements, expected 1"
            )

        try:
            await cards.click()
        except PlaywrightTimeoutError as exc:
            raise await self._device_card_error(model, exc)
        log.info("livoltek.nav.device_card_clicked", model=model)

    async def _device_card_error(
        self,
        model: str,
        cause: Exception | None,
        detail: str = "no matching card image",
    ) -> LivoltekError:
        """Build a diagnosable error naming the model and the page's own labels.

        Without this the failure reads as a bare Playwright timeout, which is
        what made the 2026-09-02 breakage take a full day to spot.
        """
        try:
            labels = await self.page.evaluate(
                """() => Array.from(
                        document.querySelectorAll('.el-descriptions__title'))
                    .map(e => (e.textContent || '').trim())
                    .filter(Boolean)"""
            )
        except Exception:  # noqa: BLE001 — diagnostics must never mask the error
            labels = ["<could not read page labels>"]
        err = LivoltekError(
            f"could not open the device card for model {model!r}: {detail}. "
            f"Device List shows: {labels}. If the model is right, the portal's "
            f"Device List markup changed — run "
            f"scripts/livoltek_peek_device_dom.py to re-derive the anchor."
        )
        if cause is not None:
            err.__cause__ = cause
        return err

    async def write_schedule(self, plan: DailyPlan) -> None:
        """Program the inverter's six ToU slots for `plan`, confirmed on the device.

        Navigates to System mode only so the SPA issues a device-parameter
        request, from which the authorization header and device id are taken.
        The schedule itself is written as registers (see `tou`), with ToU off
        while slots change, and every step read back from the device.
        """
        page = self.page
        captured: dict = {}

        def on_request(request) -> None:
            if self.OPERATION_PATH not in request.url or "headers" in captured:
                return
            try:
                device_id = json.loads(request.post_data or "{}").get("id")
            except (ValueError, TypeError, AttributeError):
                return
            if device_id is None:
                return
            captured["device_id"] = device_id
            captured["headers"] = {
                k: v
                for k, v in (request.headers or {}).items()
                if k.lower() not in ("host", "content-length", "connection")
            }

        page.on("request", on_request)
        try:
            await self.navigate_to_system_mode()
            for _ in range(20):
                if "headers" in captured:
                    break
                await asyncio.sleep(0.5)
        finally:
            page.remove_listener("request", on_request)
        if "headers" not in captured:
            raise LivoltekError(
                "System mode issued no device-parameter request, so the "
                "authorization header for the register API was not captured"
            )

        api = _PortalPointApi(
            page, self.EU_HOST, captured["headers"], captured["device_id"]
        )
        device = {a: p.get("value") for a, p in (await api.read()).items()}
        slots = slots_for_plan(
            plan, discharge_soc_pct=self._settings.morning_discharge_target_soc_pct
        )
        steps = write_steps(slots, tou_on=not plan.is_empty, device=device)
        log.info(
            "livoltek.write_schedule.start",
            steps=len(steps),
            registers=sum(len(s) for s in steps),
            tou_on=not plan.is_empty,
        )
        await apply_register_steps(api, steps)
        log.info("livoltek.write_schedule.confirmed")


class _PortalPointApi:
    """`PointApi` over the portal's own endpoints, using the SPA's auth header."""

    def __init__(self, page: Page, host: str, headers: dict, device_id) -> None:
        self._page = page
        self._base = f"https://{host}{LivoltekClient.OPERATION_PATH}"
        self._headers = headers
        self._device_id = device_id

    async def _post(self, path: str, data: dict) -> dict:
        response = await self._page.request.post(
            f"{self._base}/{path}", headers=self._headers, data=data, timeout=30000
        )
        return json.loads(await response.text()) or {}

    async def read(self) -> dict[int, dict]:
        # readCheck is what the portal's own Read Params button fires first;
        # it asks the device for fresh values before point/info is re-read.
        await self._post("point/readCheck", {"id": self._device_id})
        return points_by_address(
            await self._post("point/info", {"id": self._device_id})
        )

    async def write(self, points: list[dict]) -> None:
        body = await self._post(
            "remote/control", {"id": self._device_id, "points": points}
        )
        if body.get("msgCode") != "operate.success":
            if "REPEAT_CONTROL" in str(body.get("message") or body.get("msg_code")):
                raise PortalBusyError(str(body.get("message")))
            raise LivoltekError(f"portal rejected the register write: {body}")
