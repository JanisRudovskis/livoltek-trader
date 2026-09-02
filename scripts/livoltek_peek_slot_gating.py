"""Dump which ToU slot inputs the portal leaves editable, and when.

The portal gates each schedule row: `Start Time`, `End Time`, `Power` and `SOC`
carry `disabled` until that row's Strategy is something other than `Without a
strategy`. The Strategy dropdowns themselves are never disabled. That is why
`_fill_charge_slot` selects the strategy before filling anything else.

Run this if `_await_row_editable` starts raising — it shows the current gating
rule directly.

Read-only: enables the ToU toggle and sets slot 0's strategy in the browser
form, but never clicks `Save Params.`, so nothing reaches the inverter.

Usage: uv run python scripts/livoltek_peek_slot_gating.py
"""

from __future__ import annotations

import asyncio
import sys

from livoltek_trader.livoltek import LivoltekClient, LivoltekError

DUMP_JS = """() => {
    const grab = (ph) => Array.from(
        document.querySelectorAll(`input[placeholder="${ph}"]`)
    ).map(i => ({disabled: i.disabled, value: i.value}));
    return {
        startTime: grab('Start Time'),
        endTime: grab('End Time'),
        power: grab('Power'),
        soc: grab('SOC'),
        strategy: grab('Please Select '),
        rowCount: document.querySelectorAll('.weekday-picker__tags').length,
    };
}"""


def _render(label: str, state: dict) -> None:
    print(f"=== {label} ===")
    print(f"  rows rendered: {state.get('rowCount')}")
    for key in ("strategy", "startTime", "endTime", "power", "soc"):
        rows = state.get(key) or []
        flags = "".join("D" if r["disabled"] else "." for r in rows)
        values = [str(r["value"])[:20] for r in rows]
        print(f"  {key:<10} disabled={flags or '(none)'}  {values}")
    print("  (D = disabled, . = editable)\n")


async def main() -> int:
    try:
        async with LivoltekClient() as client:
            await client.login()
            await client.navigate_to_system_mode()
            page = client.page

            _render("after Read Params.", await page.evaluate(DUMP_JS))

            await client._set_tou_enabled(True)
            await asyncio.sleep(2.0)
            _render("after enabling ToU", await page.evaluate(DUMP_JS))

            await client._select_strategy(0, "Charge")
            await asyncio.sleep(1.5)
            _render("after slot 0 strategy = Charge", await page.evaluate(DUMP_JS))

            print(
                "Expected rule: a row's time/Power/SOC become editable only\n"
                "once that row's Strategy is not 'Without a strategy'.\n"
                "Nothing was saved — Save Params. was never clicked."
            )
    except LivoltekError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
