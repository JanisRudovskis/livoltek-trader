"""Dump the station page's Device List images and links.

`navigate_to_system_mode` clicks `img[src*="hp3_online"]` to open the inverter.
That selector encodes DEVICE STATE: if the inverter is offline the portal serves
a different asset and the click times out, taking the whole run down. This
script shows what the station page actually renders so the selector can be made
state-independent.

Read-only. Usage: uv run python scripts/livoltek_peek_device_card.py
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
            page = client.page
            await client.login()

            if client.HOME_URL_FRAGMENT not in page.url:
                await page.goto(
                    f"https://{client.EU_HOST}/#{client.HOME_URL_FRAGMENT}",
                    wait_until="domcontentloaded",
                )
                await page.wait_for_url(
                    f"**{client.HOME_URL_FRAGMENT}**", timeout=10000
                )

            await page.locator(".deviceIcon").first.click()
            await page.wait_for_url(
                f"**{client.STATION_URL_FRAGMENT}**", timeout=15000
            )
            await asyncio.sleep(3.0)
            await page.screenshot(
                path=str(OUT / "station-device-list.png"), full_page=True
            )

            info = await page.evaluate(
                """() => {
                    const imgs = Array.from(document.querySelectorAll('img'))
                        .map(i => ({
                            src: i.getAttribute('src') || '',
                            alt: i.getAttribute('alt') || '',
                            cls: i.className || '',
                            w: i.clientWidth, h: i.clientHeight,
                            visible: !!(i.offsetWidth || i.offsetHeight),
                        }))
                        .filter(i => i.src);
                    // Anything that looks like a device row / status label.
                    const text = document.body.innerText || '';
                    const statusWords = ['Online','Offline','online','offline',
                                         'Normal','Fault','Standby','Alarm'];
                    const statuses = statusWords.filter(w => text.includes(w));
                    return {
                        url: location.href,
                        imageCount: imgs.length,
                        images: imgs,
                        statusWordsOnPage: statuses,
                        bodyTextSample: text.slice(0, 1200),
                    };
                }"""
            )
            (OUT / "station-device-list.json").write_text(
                json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8"
            )

            print(f"URL: {info['url']}")
            print(f"status words present: {info['statusWordsOnPage']}")
            print(f"\n{info['imageCount']} images:")
            for i in info["images"]:
                mark = "  " if i["visible"] else " (hidden) "
                print(f"{mark}{i['src'][:110]}")
                if i["alt"] or i["cls"]:
                    print(f"        alt={i['alt']!r} class={i['cls']!r}")
            print("\n--- page text sample ---")
            print(info["bodyTextSample"][:700])

    except LivoltekError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
