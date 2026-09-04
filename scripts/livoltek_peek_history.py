"""Spike: discover the portal's historical-data surface (SOC, load, PV, grid).

The trader currently only WRITES to the portal. To calibrate
`battery_drain_hours` and the real winter load we need to READ history. The
portal is a Vue SPA backed by a JSON API, so we intercept XHR rather than
scrape rendered charts.

Answers three questions:
  1. What resolution is available (hourly / 5-minute / daily only)?
  2. How far back is history retained?
  3. Is SOC present in history at all, or only as a live value?

Read-only: never touches Params set, never clicks Save.

Usage: uv run python scripts/livoltek_peek_history.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from livoltek_trader.livoltek import LivoltekClient, LivoltekError

OUT = Path("data/exploration/history-spike")

# Field names worth flagging in a captured payload. Matched case-insensitively
# against JSON keys so we can tell at a glance which endpoint carries what.
FIELDS_OF_INTEREST = (
    "soc",
    "load",
    "pv",
    "grid",
    "battery",
    "charge",
    "discharge",
    "consum",
    "import",
    "export",
    "power",
    "energy",
)

# Anything matching these is session/auth noise we do NOT want written to disk.
REDACT_KEYS = re.compile(r"token|auth|cookie|password|secret|session", re.I)


def _redact(obj, depth: int = 0):
    """Recursively blank out auth-ish values so dumps are safe to keep."""
    if depth > 12:
        return "<max-depth>"
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if REDACT_KEYS.search(str(k)) else _redact(v, depth + 1))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v, depth + 1) for v in obj[:50]]
    return obj


def _keys_present(obj, found: set[str], depth: int = 0) -> None:
    """Collect every key name appearing anywhere in a nested payload."""
    if depth > 12:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            found.add(str(k))
            _keys_present(v, found, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:20]:
            _keys_present(v, found, depth + 1)


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    captured: list[dict] = []

    try:
        async with LivoltekClient() as client:
            page = client.page

            async def on_response(response) -> None:
                url = response.url
                ctype = (response.headers or {}).get("content-type", "")
                if "json" not in ctype.lower():
                    return
                try:
                    body = await response.json()
                except Exception:
                    return
                keys: set[str] = set()
                _keys_present(body, keys)
                hits = sorted(
                    {
                        f
                        for f in FIELDS_OF_INTEREST
                        for k in keys
                        if f in k.lower()
                    }
                )
                captured.append(
                    {
                        "url": url,
                        "status": response.status,
                        "key_count": len(keys),
                        "interesting_fields": hits,
                        "keys_sample": sorted(keys)[:60],
                        "body": _redact(body),
                    }
                )

            page.on("response", on_response)

            await client.login()

            # Walk homepage -> station -> device, stopping BEFORE Params set.
            await client.navigate_to_device()
            await page.screenshot(path=str(OUT / "02-device.png"), full_page=True)

            # Enumerate every tab on the device page so we can see what data
            # surfaces exist beyond "Params set".
            tabs = await page.evaluate(
                """() => Array.from(document.querySelectorAll('[role="tab"]'))
                    .map(t => (t.textContent || '').trim())
                    .filter(Boolean)"""
            )
            print("TABS FOUND:", json.dumps(tabs, ensure_ascii=False))
            (OUT / "tabs.json").write_text(
                json.dumps(tabs, indent=2, ensure_ascii=False), encoding="utf-8"
            )

            # Visit every tab EXCEPT Params set (that is the write surface).
            for name in tabs:
                if "param" in name.lower():
                    print(f"  skipping write surface: {name}")
                    continue
                before = len(captured)
                try:
                    await page.get_by_role("tab", name=name, exact=True).click(
                        timeout=5000
                    )
                except PlaywrightTimeoutError:
                    print(f"  tab not clickable: {name}")
                    continue
                await asyncio.sleep(2.5)
                safe = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
                await page.screenshot(
                    path=str(OUT / f"tab-{safe}.png"), full_page=True
                )
                print(f"  tab '{name}': +{len(captured) - before} JSON responses")

                # Some portals nest sub-tabs; enumerate and click those too.
                subtabs = await page.evaluate(
                    """() => Array.from(document.querySelectorAll('[role="tab"]'))
                        .map(t => (t.textContent || '').trim())
                        .filter(Boolean)"""
                )
                for sub in subtabs:
                    if sub in tabs or "param" in sub.lower():
                        continue
                    sub_before = len(captured)
                    try:
                        await page.get_by_role(
                            "tab", name=sub, exact=True
                        ).click(timeout=4000)
                    except PlaywrightTimeoutError:
                        continue
                    await asyncio.sleep(2.5)
                    sub_safe = re.sub(r"[^A-Za-z0-9]+", "-", sub).strip("-").lower()
                    await page.screenshot(
                        path=str(OUT / f"tab-{safe}--{sub_safe}.png"),
                        full_page=True,
                    )
                    print(
                        f"    subtab '{sub}': "
                        f"+{len(captured) - sub_before} JSON responses"
                    )

            await asyncio.sleep(2)

    except LivoltekError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    finally:
        # Write whatever we captured even on failure — a partial dump is still
        # informative about the API shape.
        index = [
            {k: v for k, v in c.items() if k != "body"} for c in captured
        ]
        (OUT / "xhr-index.json").write_text(
            json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        for i, c in enumerate(captured):
            (OUT / f"xhr-{i:03d}.json").write_text(
                json.dumps(c, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        print(f"\nWrote {len(captured)} JSON payloads to {OUT}")
        hot = [c for c in captured if c["interesting_fields"]]
        print(f"Of those, {len(hot)} carry fields of interest:")
        for c in hot:
            print(f"  {c['url'][:110]}")
            print(f"      -> {', '.join(c['interesting_fields'])}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
