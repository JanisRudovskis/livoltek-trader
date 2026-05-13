"""Open the weekday picker for slot 0 and dump its DOM structure.

Usage: uv run python scripts/livoltek_peek_picker.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from livoltek_trader.livoltek import LivoltekClient, LivoltekError

OUT = Path("data/exploration")


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        async with LivoltekClient() as client:
            await client.login()
            await client.navigate_to_system_mode()
            page = client.page

            # Click slot 0's weekday picker tag
            picker_tag = page.locator(".weekday-picker__tags").first
            await picker_tag.click()
            await asyncio.sleep(1.0)

            await page.screenshot(path=str(OUT / "picker-open.png"), full_page=True)

            # Dump the popper / panel that contains day options
            structure = await page.evaluate(
                """() => {
                    // Find any element with text "Everyday" or "Mon."
                    const candidates = Array.from(document.querySelectorAll('*'));
                    const found = candidates.filter(el => {
                        const t = (el.textContent || '').trim();
                        return t === 'Mon.' || t === 'Everyday';
                    });
                    if (!found.length) return { error: 'no Mon./Everyday text found' };
                    // Walk up to a probable container
                    let container = found[0];
                    for (let i = 0; i < 10; i++) {
                        if (!container.parentElement) break;
                        container = container.parentElement;
                        const t = (container.textContent || '').trim();
                        if (t.includes('Mon.') && t.includes('Sun.') && t.includes('Everyday')) break;
                    }
                    return {
                        containerTag: container.tagName,
                        containerClass: container.className,
                        innerHTML: container.outerHTML.slice(0, 3000),
                    };
                }"""
            )
            print(json.dumps(structure, indent=2, ensure_ascii=False))

            await asyncio.sleep(3)
    except LivoltekError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
