"""Open the Livoltek login page, screenshot, snapshot — exit. No interaction.

Helps diagnose unexpected dialogs blocking the login flow.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from livoltek_trader.config import get_settings

OUT = Path("data/exploration")


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context(viewport={"width": 1366, "height": 900})
        page = await ctx.new_page()
        page.set_default_timeout(15000)

        await page.goto(settings.livoltek_portal_url, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        await page.screenshot(path=str(OUT / "peek-01-initial.png"), full_page=True)
        print("Screenshot 1: initial page load")

        # Try to read any visible el-dialog__wrapper text
        dialogs = await page.evaluate(
            """() => {
                const wrappers = Array.from(document.querySelectorAll('.el-dialog__wrapper'));
                return wrappers.map(w => ({
                    visible: window.getComputedStyle(w).display !== 'none',
                    ariaLabel: w.getAttribute('aria-label') || '',
                    text: (w.innerText || '').slice(0, 500),
                    classes: w.className,
                }));
            }"""
        )
        print("el-dialog__wrappers found:")
        for d in dialogs:
            print(d)

        msg_boxes = await page.evaluate(
            """() => {
                const wrappers = Array.from(document.querySelectorAll('.el-message-box__wrapper'));
                return wrappers.map(w => ({
                    visible: window.getComputedStyle(w).display !== 'none',
                    ariaLabel: w.getAttribute('aria-label') || '',
                    text: (w.innerText || '').slice(0, 500),
                }));
            }"""
        )
        print("el-message-box__wrappers found:")
        for d in msg_boxes:
            print(d)

        await asyncio.sleep(5)
        await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
