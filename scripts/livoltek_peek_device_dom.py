"""Dump the station page's Device List DOM to find a stable device selector.

The portal moved device-card images from `./static/img/hp3_online.*.png` to
inlined base64 data URIs, which broke `img[src*="hp3_online"]`. This finds an
alternative anchor that depends on neither the asset name nor the device's
online/offline state.

Read-only. Usage: uv run python scripts/livoltek_peek_device_dom.py
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

            info = await page.evaluate(
                """() => {
                    const out = {};
                    out.fullText = document.body.innerText || '';

                    // Locate the "Device List" heading and dump its section.
                    const all = Array.from(document.querySelectorAll('*'));
                    const heading = all.find(el =>
                        (el.textContent || '').trim() === 'Device List');
                    let section = null;
                    if (heading) {
                        section = heading;
                        for (let i = 0; i < 8; i++) {
                            if (!section.parentElement) break;
                            section = section.parentElement;
                            const t = section.textContent || '';
                            if (t.includes('Comm. Status')) break;
                        }
                        out.sectionHtml = section.outerHTML.slice(0, 6000);
                    }

                    // Every element whose own text looks like a device serial.
                    const serialRe = /[A-Z0-9]{8,}\\(/;
                    out.serialCandidates = all
                        .filter(el => el.children.length === 0)
                        .map(el => ({
                            tag: el.tagName,
                            cls: el.className || '',
                            text: (el.textContent || '').trim(),
                        }))
                        .filter(e => serialRe.test(e.text))
                        .slice(0, 20);

                    // Clickable-looking elements inside the section.
                    if (section) {
                        out.clickables = Array.from(
                            section.querySelectorAll(
                              'a,button,img,[class*="cursor"],[class*="pointer"],[class*="link"]'
                            ))
                            .map(el => ({
                                tag: el.tagName,
                                cls: el.className || '',
                                src: (el.getAttribute('src') || '').slice(0, 60),
                                text: (el.textContent || '').trim().slice(0, 60),
                                w: el.clientWidth, h: el.clientHeight,
                            }))
                            .slice(0, 40);
                    }

                    // Table rows anywhere (the device list is likely a table).
                    out.tables = Array.from(document.querySelectorAll('table'))
                        .map(t => ({
                            cls: t.className || '',
                            headers: Array.from(t.querySelectorAll('th'))
                                .map(h => (h.textContent||'').trim()).slice(0,12),
                            rowCount: t.querySelectorAll('tbody tr').length,
                            firstRow: Array.from(
                                t.querySelectorAll('tbody tr')
                            ).slice(0,3).map(r => Array.from(r.querySelectorAll('td'))
                                .map(c => (c.textContent||'').trim().slice(0,40))),
                        }));
                    return out;
                }"""
            )
            (OUT / "station-device-dom.json").write_text(
                json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8"
            )

            print("=== FULL PAGE TEXT (tail) ===")
            print(info["fullText"][-1500:])
            print("\n=== SERIAL-LIKE LEAF ELEMENTS ===")
            for e in info.get("serialCandidates", []):
                print(f"  <{e['tag']}> cls={e['cls']!r} text={e['text']!r}")
            print("\n=== TABLES ===")
            for t in info.get("tables", []):
                print(f"  table cls={t['cls']!r} rows={t['rowCount']}")
                print(f"    headers: {t['headers']}")
                for r in t["firstRow"]:
                    print(f"    row: {r}")
            print(f"\nfull dump -> {OUT / 'station-device-dom.json'}")

    except LivoltekError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
