"""Print the inverter's ToU slot table as the DEVICE holds it. Read-only.

The portal's System-mode form is not a reliable view of the inverter: a row
shown as "Without a strategy" can still hold a live Charge slot on the device
(that is how 2026-11-05 went wrong). This reads the registers themselves via
the portal's parameter API and decodes them.

Usage:
    LIVOLTEK_HEADLESS=true uv run python scripts/livoltek_tou_registers.py
"""

from __future__ import annotations

import asyncio
import json
import sys

from livoltek_trader.livoltek import (
    LivoltekClient,
    LivoltekError,
    _PortalPointApi,
)
from livoltek_trader.tou import (
    CMD_CHARGE,
    CMD_DISCHARGE,
    CMD_STOP,
    SLOT_BASE,
    SLOT_COUNT,
    SLOT_STRIDE,
    TOU_ADDR,
    TOU_ON,
)

COMMANDS = {CMD_CHARGE: "Charge", CMD_DISCHARGE: "Discharge", CMD_STOP: "Stop"}
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _days(mask: int) -> str:
    names = [d for i, d in enumerate(DAYS) if mask & (1 << i)]
    return ",".join(names) or "never"


async def main() -> int:
    try:
        async with LivoltekClient() as client:
            page = client.page
            captured: dict = {}

            def on_request(request) -> None:
                if LivoltekClient.OPERATION_PATH in request.url and "headers" not in captured:
                    body = json.loads(request.post_data or "{}")
                    if body.get("id") is not None:
                        captured["id"] = body["id"]
                        captured["headers"] = {
                            k: v for k, v in request.headers.items()
                            if k.lower() not in ("host", "content-length", "connection")
                        }

            page.on("request", on_request)
            await client.login()
            await client.navigate_to_system_mode()
            await asyncio.sleep(3)
            if "headers" not in captured:
                print("could not capture the API header", file=sys.stderr)
                return 1
            api = _PortalPointApi(page, client.EU_HOST, captured["headers"], captured["id"])
            regs = {a: p.get("value") for a, p in (await api.read()).items()}
    except LivoltekError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    tou = int(float(regs[TOU_ADDR]))
    print(f"ToU enable: {'ON' if tou == TOU_ON else 'off'} ({tou})")
    for i in range(SLOT_COUNT):
        base = SLOT_BASE + SLOT_STRIDE * i
        cmd, power, soc, sh, sm, eh, em, days = (
            regs.get(base + j) for j in range(SLOT_STRIDE)
        )
        cmd = int(float(cmd))
        print(
            f"  slot {i + 1}: {COMMANDS.get(cmd, cmd):9s} "
            f"{int(float(sh)):02d}:{int(float(sm)):02d}-"
            f"{int(float(eh)):02d}:{int(float(em)):02d}  "
            f"days={_days(int(float(days))):24s} SOC {soc}%  {power} kW"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
