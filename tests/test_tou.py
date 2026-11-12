"""Tests for the ToU register model — the inverter's schedule as Modbus registers."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from livoltek_trader.strategy import CyclePair, DailyPlan, TradingWindow
from livoltek_trader.tou import (
    CMD_CHARGE,
    CMD_DISCHARGE,
    INERT_SLOT,
    SLOT_COUNT,
    TOU_ADDR,
    TOU_OFF,
    TOU_ON,
    Slot,
    day_bit,
    pending,
    slot_registers,
    slots_for_plan,
    write_steps,
)

RIGA = ZoneInfo("Europe/Riga")
FRI = date(2026, 11, 13)


def _w(d: date, h0: int, h1: int) -> TradingWindow:
    s = datetime.combine(d, time(h0), tzinfo=RIGA)
    return TradingWindow(start=s, end=s + timedelta(hours=h1 - h0), avg_eur_per_kwh=0.1)


def _cycle(d: date, h0: int, h1: int, hold_until: datetime | None = None) -> CyclePair:
    return CyclePair(
        charge=_w(d, h0, h1),
        discharge=_w(d, h1, h1 + 7),
        gross_revenue_eur=1.0,
        wear_cost_eur=0.05,
        net_profit_eur=0.95,
        hold_until=hold_until,
    )


def _plan(cycles=(), stop_window=None, d: date = FRI) -> DailyPlan:
    return DailyPlan(
        target_date=d,
        cycles=list(cycles),
        skipped_reason=None if cycles or stop_window else "nothing",
        total_net_profit_eur=0.0,
        stop_window=stop_window,
    )


# --- weekday bitmask --------------------------------------------------------


def test_day_bit_is_monday_one_through_sunday_sixty_four():
    monday = date(2026, 11, 9)
    assert [day_bit(monday + timedelta(days=i)) for i in range(7)] == [
        1, 2, 4, 8, 16, 32, 64,
    ]


# --- plan -> slots ------------------------------------------------------------


def test_empty_plan_makes_every_slot_inert():
    assert slots_for_plan(_plan(), discharge_soc_pct=15) == [INERT_SLOT] * SLOT_COUNT


def test_inert_slot_selects_no_weekday_so_it_can_never_fire():
    # A cleared slot used to keep its old strategy and weekday on the device
    # (2026-11-05). An explicit zero-day slot cannot fire on any day.
    assert INERT_SLOT.days == 0


def test_charge_cycle_becomes_a_full_charge_slot_on_the_target_weekday():
    slots = slots_for_plan(_plan([_cycle(FRI, 0, 2)]), discharge_soc_pct=15)
    assert slots[0] == Slot(
        command=CMD_CHARGE, power_kw=10.0, soc_pct=100,
        start=time(0, 0), end=time(2, 0), days=16,
    )
    assert slots[1:] == [INERT_SLOT] * (SLOT_COUNT - 1)


def test_charge_slot_runs_until_hold_until_when_set():
    hold = datetime.combine(FRI, time(5), tzinfo=RIGA)
    slots = slots_for_plan(_plan([_cycle(FRI, 0, 2, hold)]), discharge_soc_pct=15)
    assert (slots[0].start, slots[0].end) == (time(0, 0), time(5, 0))


def test_morning_discharge_goes_first_then_charges_in_time_order():
    plan = _plan(
        [_cycle(FRI, 13, 15), _cycle(FRI, 2, 4)], stop_window=_w(FRI, 7, 9)
    )
    slots = slots_for_plan(plan, discharge_soc_pct=15)
    assert slots[0].command == CMD_DISCHARGE and slots[0].soc_pct == 15
    assert [s.start for s in slots[1:3]] == [time(2), time(13)]


def test_slot_ending_at_midnight_is_rejected():
    # 00:00 as an END would read as a zero-length or wrapped slot on the device.
    with pytest.raises(ValueError):
        slots_for_plan(_plan([_cycle(FRI, 22, 24)]), discharge_soc_pct=15)


# --- slots -> registers -----------------------------------------------------


def test_slot_registers_follow_the_device_address_map():
    slots = slots_for_plan(_plan([_cycle(FRI, 5, 7)]), discharge_soc_pct=15)
    regs = slot_registers(slots)
    assert len(regs) == 8 * SLOT_COUNT
    # slot 1 at 45009..45016: command, power, SOC, start h/m, end h/m, days
    assert [regs[a] for a in range(45009, 45017)] == [
        170, "10.00", 100, 5, 0, 7, 0, 16,
    ]
    # slot 6 starts at 45009 + 5*8 = 45049 and is inert
    assert regs[45049 + 7] == 0
    assert TOU_ADDR not in regs


# --- comparing with what the device reports ---------------------------------


def test_pending_ignores_formatting_differences_the_portal_introduces():
    # point/info reports every value as a string, power with two decimals.
    intended = {45009: 170, 45010: "10.00", 45011: 100, 45016: 16}
    actual = {45009: "170", 45010: "10.0", 45011: "100", 45016: "16"}
    assert pending(intended, actual) == {}


def test_pending_lists_only_registers_that_differ():
    intended = {45009: 170, 45016: 16}
    actual = {45009: "170", 45016: "8"}
    assert pending(intended, actual) == {45016: 16}


def test_pending_treats_a_missing_register_as_different():
    assert pending({45009: 170}, {}) == {45009: 170}


# --- write ordering ----------------------------------------------------------


def _device(tou: int, slots: list[Slot]) -> dict[int, str]:
    regs = {a: str(v) for a, v in slot_registers(slots).items()}
    regs[TOU_ADDR] = str(tou)
    return regs


def test_slots_are_rewritten_only_while_tou_is_off():
    """The 2026-11-05 incident: registers land one at a time, seconds apart,
    so with ToU on the device spends minutes in half-written states — one of
    which started a grid charge the evening before. Turning ToU off first
    makes every intermediate state inert."""
    old = slots_for_plan(_plan([_cycle(FRI - timedelta(days=1), 12, 14)]), discharge_soc_pct=15)
    new = slots_for_plan(_plan([_cycle(FRI, 0, 2)]), discharge_soc_pct=15)
    steps = write_steps(new, tou_on=True, device=_device(TOU_ON, old))
    assert steps[0] == {TOU_ADDR: TOU_OFF}
    assert TOU_ADDR not in steps[1] and steps[1]
    assert steps[-1] == {TOU_ADDR: TOU_ON}


def test_unchanged_slots_with_tou_already_on_need_no_writes():
    slots = slots_for_plan(_plan([_cycle(FRI, 0, 2)]), discharge_soc_pct=15)
    assert write_steps(slots, tou_on=True, device=_device(TOU_ON, slots)) == []


def test_empty_plan_turns_tou_off_before_clearing_slots():
    old = slots_for_plan(_plan([_cycle(FRI, 0, 2)]), discharge_soc_pct=15)
    steps = write_steps(
        [INERT_SLOT] * SLOT_COUNT, tou_on=False, device=_device(TOU_ON, old)
    )
    assert steps[0] == {TOU_ADDR: TOU_OFF}
    assert len(steps) == 2 and TOU_ADDR not in steps[1]


def test_slots_already_correct_but_tou_off_only_switches_tou_on():
    slots = slots_for_plan(_plan([_cycle(FRI, 0, 2)]), discharge_soc_pct=15)
    assert write_steps(slots, tou_on=True, device=_device(TOU_OFF, slots)) == [
        {TOU_ADDR: TOU_ON}
    ]
