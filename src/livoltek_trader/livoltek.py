"""Livoltek cloud portal automation via Playwright.

The portal (https://www.livoltek-portal.com) is a Vue SPA with hash routing.
Deep URL navigation does not work — every entry must traverse from the
Homepage forward (Site card → Device card → tab → sub-tab). Cookie/session
state is persisted to disk so we don't burn a fresh login every day.

Steps 6–7 of the build plan: log in, navigate to Params set → System mode,
and write the daily schedule (toggle ToU + fill 1-6 Charge slots, then Save).
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
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
from livoltek_trader.strategy import CyclePair, DailyPlan

RIGA_TZ = ZoneInfo("Europe/Riga")

_WEEKDAY_LABELS = ["Mon.", "Tue.", "Wed.", "Thu.", "Fri.", "Sat.", "Sun."]
"""Element UI weekday picker labels, in Python isoweekday order (Mon=0)."""

log = structlog.get_logger(__name__)


class LivoltekError(RuntimeError):
    """Raised on login or navigation failure."""


class LivoltekClient:
    """Async Playwright client for the Livoltek EU&MEA monitoring portal."""

    HOME_URL_FRAGMENT = "/customer/homePage"
    STATION_URL_FRAGMENT = "/customer/tocStation/"
    DEVICE_URL_FRAGMENT = "/customer/tocDevice/"
    EU_HOST = "evs.livoltek-portal.com"

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

        if await self._reached_home():
            log.info("livoltek.login.session_restored")
            return

        await self._dismiss_cookies()
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

    async def _fill_credentials_and_submit(self) -> None:
        page = self.page
        await self._resolve_tips_popup(timeout_ms=500)
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

    async def navigate_to_system_mode(self) -> None:
        """Walk from the homepage to Params set → System mode and read params.

        After this returns, the System mode form is populated with the
        inverter's current values (Work mode, Enable ToU toggle, schedule
        rows, Grid Charging toggle).
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

        await page.locator('img[src*="hp3_online"]').first.click()
        await page.wait_for_url(
            f"**{self.DEVICE_URL_FRAGMENT}**", timeout=15000
        )
        log.info("livoltek.nav.device_reached")

        await page.get_by_role("tab", name="Params set", exact=True).click()
        await page.get_by_role("tab", name="System mode", exact=True).click()
        log.info("livoltek.nav.system_mode_tab_opened")

        await page.get_by_role("button", name="Read Params.").first.click()
        await asyncio.sleep(2.0)
        log.info("livoltek.nav.read_params_clicked")

    async def apply_schedule(self, plan: DailyPlan, *, save: bool = False) -> None:
        """Write the day's plan to the System mode form.

        Behaviour:
        - If `plan.cycles` is empty, disable Enable ToU schedule (inverter
          falls back to pure Self-use mode — no scheduled grid charging).
        - Otherwise, enable ToU and fill slots 1..N with Charge rows for
          today's weekday only. Slots N+1..6 are set to `Without a strategy`
          so any old configuration is overwritten.

        With `save=False` (default), the form is filled but `Save Params.`
        is NOT clicked — values stay local to the browser session and are
        lost on page reload. Use `save=True` to commit to the inverter.
        """
        page = self.page
        if not plan.cycles:
            await self._set_tou_enabled(False)
            log.info("livoltek.apply_schedule.skip_day", reason=plan.skipped_reason)
        else:
            await self._set_tou_enabled(True)
            today_weekday = _WEEKDAY_LABELS[
                datetime.now(RIGA_TZ).weekday()
            ]
            for slot_idx in range(6):
                if slot_idx < len(plan.cycles):
                    await self._fill_charge_slot(
                        slot_idx, plan.cycles[slot_idx], today_weekday
                    )
                else:
                    await self._clear_slot(slot_idx)
            log.info(
                "livoltek.apply_schedule.filled",
                cycles=len(plan.cycles),
                weekday=today_weekday,
            )

        if save:
            await page.get_by_role("button", name="Save Params.").first.click()
            await asyncio.sleep(3.0)
            log.info("livoltek.apply_schedule.saved")
        else:
            log.info("livoltek.apply_schedule.dry_run_form_filled")

    async def _set_tou_enabled(self, enabled: bool) -> None:
        page = self.page
        toggle_row = page.locator('text="Enable ToU schedule"').locator(
            'xpath=following-sibling::*[contains(@class, "el-switch")][1]'
        )
        current = await toggle_row.get_attribute("aria-checked")
        is_on = current == "true"
        if is_on != enabled:
            await toggle_row.click()
            log.info("livoltek.tou_toggle.set", enabled=enabled)
        else:
            log.info("livoltek.tou_toggle.already", enabled=enabled)

    async def _fill_charge_slot(
        self, slot_idx: int, cycle: CyclePair, weekday: str
    ) -> None:
        page = self.page
        start_str = cycle.charge.start.astimezone(RIGA_TZ).strftime("%H:%M")
        end_str = cycle.charge.end.astimezone(RIGA_TZ).strftime("%H:%M")

        await self._fill_time_field("Start Time", slot_idx, start_str)
        await self._fill_time_field("End Time", slot_idx, end_str)
        await self._select_strategy(slot_idx, "Charge")
        await self._set_slot_weekday(slot_idx, weekday)
        await self._fill_number_field("Power", slot_idx, "10.00")
        await self._fill_number_field("SOC", slot_idx, "100")
        log.info(
            "livoltek.slot.filled",
            slot=slot_idx,
            start=start_str,
            end=end_str,
            weekday=weekday,
        )

    async def _clear_slot(self, slot_idx: int) -> None:
        await self._select_strategy(slot_idx, "Without a strategy")
        log.info("livoltek.slot.cleared", slot=slot_idx)

    async def _fill_time_field(
        self, placeholder: str, slot_idx: int, value: str
    ) -> None:
        field = self.page.locator(f'input[placeholder="{placeholder}"]').nth(slot_idx)
        await field.click()
        await field.fill("")
        await field.fill(value)
        await field.press("Tab")

    async def _fill_number_field(
        self, placeholder: str, slot_idx: int, value: str
    ) -> None:
        field = self.page.locator(f'input[placeholder="{placeholder}"]').nth(slot_idx)
        await field.click()
        await field.fill(value)
        await field.press("Tab")

    async def _select_strategy(self, slot_idx: int, label: str) -> None:
        strategy_input = self.page.locator(
            'input[placeholder="Please Select "]'
        ).nth(slot_idx)
        await strategy_input.click()
        await self.page.locator(
            f'.el-select-dropdown__item:has-text("{label}")'
        ).last.click()

    async def _set_slot_weekday(self, slot_idx: int, target_weekday: str) -> None:
        """Open the Nth slot's weekday picker and check only `target_weekday`."""
        page = self.page
        picker_tag = page.locator(".weekday-picker__tags").nth(slot_idx)
        await picker_tag.click()
        await asyncio.sleep(0.4)

        await page.evaluate(
            """(target) => {
                const labels = ['Mon.','Tue.','Wed.','Thu.','Fri.','Sat.','Sun.'];
                const checkboxes = Array.from(document.querySelectorAll('.el-checkbox'));
                for (const cb of checkboxes) {
                    const lblEl = cb.querySelector('.el-checkbox__label');
                    if (!lblEl) continue;
                    const lbl = lblEl.textContent.trim();
                    if (!labels.includes(lbl)) continue;
                    const r = cb.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    const isChecked = cb.classList.contains('is-checked');
                    const want = (lbl === target);
                    if (isChecked !== want) cb.click();
                }
            }""",
            target_weekday,
        )
        await page.locator("body").click(position={"x": 10, "y": 200})
        await asyncio.sleep(0.3)
