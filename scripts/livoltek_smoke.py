"""Smoke test: log into the Livoltek portal and navigate to System mode.

Usage: uv run python scripts/livoltek_smoke.py

Opens a visible Chromium window (set LIVOLTEK_HEADLESS=true in .env to hide),
walks through Homepage -> Site -> Device -> Params set -> System mode -> Read Params,
then pauses briefly so you can confirm the form is populated.
"""

from __future__ import annotations

import asyncio
import sys

from livoltek_trader.livoltek import LivoltekClient, LivoltekError


async def main() -> int:
    try:
        async with LivoltekClient() as client:
            print("Logging in...")
            await client.login()
            print("Logged in. Navigating to System mode...")
            await client.navigate_to_system_mode()
            print("OK — reached Params set -> System mode and read params.")
            print("Holding browser open for 8s so you can verify the form.")
            await asyncio.sleep(8)
    except LivoltekError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
