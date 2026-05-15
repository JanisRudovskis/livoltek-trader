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
from livoltek_trader.strategy import CyclePair, DailyPlan, TradingWindow

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
        - If `plan.is_empty` (no cycles AND no Stop window), disable
          Enable ToU schedule — inverter falls back to pure Self-use mode.
        - Otherwise, enable ToU and fill slots in this order:
            slot 0: Stop window (if present)
            next:   Charge cycles (in time order)
            last:   `Without a strategy` for unused slots
          All slots fire on today's weekday only.

        With `save=False` (default), the form is filled but `Save Params.`
        is NOT clicked — values stay local to the browser session and are
        lost on page reload. Use `save=True` to commit to the inverter.
        """
        page = self.page
        if plan.is_empty:
            await self._set_tou_enabled(False)
            log.info("livoltek.apply_schedule.skip_day", reason=plan.skipped_reason)
        else:
            await self._set_tou_enabled(True)
            # Use the PLAN's weekday, not "now". The cron runs the evening
            # before the trading day, so `datetime.now().weekday()` would
            # be off by one and the slot would fire today instead of
            # tomorrow.
            target_weekday = _WEEKDAY_LABELS[plan.target_date.weekday()]

            slot_idx = 0
            if plan.stop_window is not None:
                await self._fill_stop_slot(
                    slot_idx, plan.stop_window, target_weekday
                )
                slot_idx += 1

            for cycle in plan.cycles:
                if slot_idx >= 6:
                    break
                await self._fill_charge_slot(slot_idx, cycle, target_weekday)
                slot_idx += 1

            for clear_idx in range(slot_idx, 6):
                await self._clear_slot(clear_idx)

            log.info(
                "livoltek.apply_schedule.filled",
                cycles=len(plan.cycles),
                stop_window=plan.stop_window is not None,
                weekday=target_weekday,
            )

        if save:
            await page.get_by_role("button", name="Save Params.").first.click()
            await self._verify_save_toast()
            log.info("livoltek.apply_schedule.saved")
        else:
            log.info("livoltek.apply_schedule.dry_run_form_filled")

    async def _verify_save_toast(self, timeout_ms: int = 8000) -> None:
        """Wait for the green success toast after Save Params; warn otherwise.

        The portal renders an `.el-message--success` toast saying
        'The instruction was issued successfully!' when the command reaches
        the inverter. If we don't see it, the Save click didn't actually
        commit — log a warning so it shows up in cron output.
        """
        try:
            await self.page.locator(".el-message--success").first.wait_for(
                state="visible", timeout=timeout_ms
            )
            log.info("livoltek.save.toast_confirmed")
        except PlaywrightTimeoutError:
            log.warning("livoltek.save.no_success_toast_seen")

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

    async def _fill_stop_slot(
        self, slot_idx: int, window: TradingWindow, weekday: str
    ) -> None:
        """Fill one schedule row as a morning Discharge slot (drain + export).

        Strategy is `Discharge` with SOC target from settings (default 15%):
        - During the window the battery actively discharges to the grid at
          the high morning spot price until SOC reaches the target.
        - PV continues to flow (Self-use priority during Discharge — load
          first, excess to grid) so we also pocket export revenue on PV.
        - After the window ends, normal Self-use resumes and the battery
          refills from afternoon PV for evening discharge to load.

        We keep the helper name `_fill_stop_slot` for backwards-compatibility
        with the planner's `stop_window` field — conceptually this is still
        the "block-and-export" morning window; the implementation just
        improves on the Stop-strategy version by also selling any leftover
        SOC from yesterday at the peak morning price.
        """
        start_str = window.start.astimezone(RIGA_TZ).strftime("%H:%M")
        end_str = window.end.astimezone(RIGA_TZ).strftime("%H:%M")
        soc_target = str(self._settings.morning_discharge_target_soc_pct)

        await self._fill_time_field("Start Time", slot_idx, start_str)
        await self._fill_time_field("End Time", slot_idx, end_str)
        await self._select_strategy(slot_idx, "Discharge")
        await self._set_slot_weekday(slot_idx, weekday)
        await self._fill_number_field("Power", slot_idx, "10.00")
        await self._fill_number_field("SOC", slot_idx, soc_target)
        log.info(
            "livoltek.slot.morning_discharge_filled",
            slot=slot_idx,
            start=start_str,
            end=end_str,
            weekday=weekday,
            soc_target=soc_target,
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
        """Open the slot's strategy dropdown and pick an EXACT-text option.

        Critical: the option list is Charge / Discharge / Stop / Without a
        strategy. `:has-text("Charge")` is a substring match — would also
        match "Discharge" and silently pick the wrong row. We do the exact
        match in JS to avoid Playwright selector quirks.
        """
        page = self.page
        strategy_input = page.locator(
            'input[placeholder="Please Select "]'
        ).nth(slot_idx)
        await strategy_input.click()
        await asyncio.sleep(0.3)
        clicked = await page.evaluate(
            """(label) => {
                const items = Array.from(document.querySelectorAll('.el-select-dropdown__item'));
                for (const item of items) {
                    if (window.getComputedStyle(item).display === 'none') continue;
                    const r = item.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    if ((item.textContent || '').trim() === label) {
                        item.click();
                        return true;
                    }
                }
                return false;
            }""",
            label,
        )
        if not clicked:
            raise LivoltekError(
                f"strategy option '{label}' not found in dropdown for slot {slot_idx}"
            )

    async def _set_slot_weekday(self, slot_idx: int, target_weekday: str) -> None:
        """Open the Nth slot's weekday picker and check only `target_weekday`.

        The picker is a Livoltek-custom component (NOT Element UI el-checkbox):
        each day is a `<label class="weekday-picker__checkbox-label">` wrapping
        an `<input type="checkbox" value="0..6">` where the value matches
        Python `datetime.weekday()` (Mon=0, Sun=6). The component's reactive
        state only updates on real PointerEvent clicks — JS `.click()` on the
        input or label silently toggles the DOM but doesn't propagate to Vue.
        We use Playwright's native click() which dispatches a real event.
        """
        page = self.page
        target_idx = _WEEKDAY_LABELS.index(target_weekday)
        picker_tag = page.locator(".weekday-picker__tags").nth(slot_idx)
        await picker_tag.click()
        await asyncio.sleep(0.4)

        dropdown = page.locator(".weekday-picker__dropdown").last
        labels = dropdown.locator("label.weekday-picker__checkbox-label")
        count = await labels.count()
        if count != 7:
            raise LivoltekError(
                f"weekday picker for slot {slot_idx} has {count} labels, expected 7"
            )

        clicked: list[dict] = []
        for i in range(count):
            label_el = labels.nth(i)
            input_el = label_el.locator('input[type="checkbox"]')
            val_str = await input_el.get_attribute("value")
            if val_str is None:
                continue
            idx = int(val_str)
            is_checked = await input_el.is_checked()
            want = idx == target_idx
            if is_checked != want:
                await label_el.click()
                clicked.append({"idx": idx, "wasChecked": is_checked})
                await asyncio.sleep(0.05)

        log.info(
            "livoltek.weekday_picker.attempt",
            slot=slot_idx,
            target=target_weekday,
            target_idx=target_idx,
            clicked=clicked,
        )
        await page.locator("body").click(position={"x": 10, "y": 200})
        await asyncio.sleep(0.3)
