"""The inverter's ToU schedule as Modbus registers — pure, no Playwright.

The trader used to fill the portal's System-mode form and click Save. That
turned out to be unreliable in two ways, both measured on 2026-11-12 by
intercepting the portal's own requests:

1. **The portal sends a diff.** Save issues one register write per field that
   differs from what the form loaded, and the inverter applies them one at a
   time, roughly 5-20 s apart. With ToU enabled the device spends minutes in
   half-written states. One of those started a grid charge at 21:40 on
   2026-11-05 instead of 00:00 the next day, and the same write's slot-2
   registers never landed at all.
2. **"Without a strategy" sends nothing.** Clearing a row only greys it out in
   the UI; the inverter keeps the slot's old strategy, times and weekday.

So the schedule is now written as registers directly: every slot explicitly
(unused ones made inert with an empty weekday mask), with ToU switched OFF
while slots change so no intermediate state can fire, and each step
confirmed by reading the device back.

Register map, device-confirmed: slot i (0..5) occupies 8 registers from
`45009 + 8*i` — command, power, SOC, start hour, start minute, end hour,
end minute, weekday mask (Mon=1 ... Sun=64). ToU enable is 45093.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from livoltek_trader.strategy import DailyPlan, TradingWindow

RIGA_TZ = ZoneInfo("Europe/Riga")

SLOT_COUNT = 6
SLOT_BASE = 45009
SLOT_STRIDE = 8
TOU_ADDR = 45093
TOU_ON = 170  # 0xAA
TOU_OFF = 85  # 0x55

CMD_CHARGE = 170  # 0xAA
CMD_DISCHARGE = 187  # 0xBB
CMD_STOP = 204  # 0xCC

FULL_POWER_KW = 10.0


@dataclass(frozen=True)
class Slot:
    """One ToU row as the inverter stores it."""

    command: int
    power_kw: float
    soc_pct: int
    start: time
    end: time
    days: int  # weekday bitmask, Mon=1 ... Sun=64; 0 = never


INERT_SLOT = Slot(
    command=CMD_STOP,
    power_kw=FULL_POWER_KW,
    soc_pct=100,
    start=time(0, 0),
    end=time(0, 0),
    days=0,
)
"""What every unused slot is set to. No weekday selected, so it cannot fire
whatever the firmware makes of a 00:00-00:00 period. Device-confirmed to be
accepted (2026-11-12)."""


def day_bit(d: date) -> int:
    return 1 << d.weekday()


def _local_time(dt: datetime) -> time:
    local = dt.astimezone(RIGA_TZ)
    return time(local.hour, local.minute)


def _slot(command: int, soc_pct: int, start: datetime, end: datetime, days: int) -> Slot:
    s = _local_time(start)
    e = _local_time(end)
    if e == time(0, 0) or e <= s:
        # A slot ending at midnight cannot be expressed: 00:00 as an end reads
        # as zero-length or wrapping on the device. The planner never produces
        # one; refuse rather than write something ambiguous.
        raise ValueError(f"slot {s}-{e} does not fit inside one day")
    return Slot(
        command=command, power_kw=FULL_POWER_KW, soc_pct=soc_pct,
        start=s, end=e, days=days,
    )


def slots_for_plan(plan: DailyPlan, *, discharge_soc_pct: int) -> list[Slot]:
    """All six slots for the plan: morning Discharge first, then Charge cycles
    in time order, then inert padding."""
    days = day_bit(plan.target_date)
    slots: list[Slot] = []
    if plan.stop_window is not None:
        sw: TradingWindow = plan.stop_window
        slots.append(_slot(CMD_DISCHARGE, discharge_soc_pct, sw.start, sw.end, days))
    for c in sorted(plan.cycles, key=lambda c: c.charge.start):
        end = c.hold_until or c.charge.end
        slots.append(_slot(CMD_CHARGE, 100, c.charge.start, end, days))
    if len(slots) > SLOT_COUNT:
        raise ValueError(f"{len(slots)} slots planned, the inverter has {SLOT_COUNT}")
    return slots + [INERT_SLOT] * (SLOT_COUNT - len(slots))


def slot_registers(slots: list[Slot]) -> dict[int, int | str]:
    """Register address -> value, in the types the portal itself sends
    (integers, except power which travels as a two-decimal string)."""
    regs: dict[int, int | str] = {}
    for i, s in enumerate(slots):
        base = SLOT_BASE + SLOT_STRIDE * i
        values = (
            s.command, f"{s.power_kw:.2f}", s.soc_pct,
            s.start.hour, s.start.minute, s.end.hour, s.end.minute, s.days,
        )
        for offset, value in enumerate(values):
            regs[base + offset] = value
    return regs


def _same(a, b) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def pending(intended: dict[int, int | str], actual: dict[int, str]) -> dict[int, int | str]:
    """The intended registers the device does not yet hold."""
    return {
        addr: value
        for addr, value in intended.items()
        if addr not in actual or not _same(value, actual[addr])
    }


def write_steps(
    slots: list[Slot], *, tou_on: bool, device: dict[int, str]
) -> list[dict[int, int | str]]:
    """The ordered register writes that take `device` to the intended state.

    Slots only ever change while ToU is off, so no half-written slot can
    fire. Each step is confirmed on the device before the next is sent.
    """
    tou_is_on = _same(device.get(TOU_ADDR), TOU_ON)
    slot_changes = pending(slot_registers(slots), device)
    steps: list[dict[int, int | str]] = []
    if slot_changes:
        if tou_is_on:
            steps.append({TOU_ADDR: TOU_OFF})
            tou_is_on = False
        steps.append(slot_changes)
    if tou_on != tou_is_on:
        steps.append({TOU_ADDR: TOU_ON if tou_on else TOU_OFF})
    return steps
