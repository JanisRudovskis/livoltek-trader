"""Tests for EV charging advice and the house-battery hold that syncs with it."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from livoltek_trader.config import Settings
from livoltek_trader.ev import (
    advise_ev,
    cheapest_block,
    cost_per_100km_eur,
)
from livoltek_trader.strategy import HourlyPrice, plan_day

RIGA = ZoneInfo("Europe/Riga")
FRI = date(2026, 11, 13)


def _at(d: date, h: int) -> datetime:
    return datetime.combine(d, time(h), tzinfo=RIGA)


def _hours(d: date, prices: list[float], first_hour: int = 0) -> list[HourlyPrice]:
    return [
        HourlyPrice(start=_at(d, first_hour + i), eur_per_kwh=p)
        for i, p in enumerate(prices)
    ]


@pytest.fixture
def settings() -> Settings:
    return Settings(
        ev_charge_hours=3,
        ev_consumption_kwh_per_100km=20.0,
        ev_charging_efficiency=0.9,
    )


# --- cheapest block ------------------------------------------------------------


def test_cheapest_block_stays_inside_its_horizon():
    hourly = _hours(FRI, [0.30, 0.01, 0.01, 0.01, 0.20, 0.20, 0.20, 0.20])
    block = cheapest_block(hourly, 3, _at(FRI, 4), _at(FRI, 8))
    assert (block.start, block.end) == (_at(FRI, 4), _at(FRI, 7))


def test_cheapest_block_needs_contiguous_hours():
    hourly = _hours(FRI, [0.10, 0.10]) + _hours(FRI, [0.10], first_hour=5)
    assert cheapest_block(hourly, 3, _at(FRI, 0), _at(FRI, 8)) is None


# --- cost per 100 km -------------------------------------------------------------


def test_cost_per_100km_includes_charging_losses_and_the_buy_margin(settings):
    # 20 kWh/100km at 90 % = 22.22 kWh from the grid, at spot 0.04 + 0.05
    assert cost_per_100km_eur(0.04, settings) == pytest.approx(22.222 * 0.09, rel=1e-3)


# --- night / day windows -----------------------------------------------------------


def _week_night_prices() -> list[HourlyPrice]:
    """Thursday evening dear, Friday night cheapest 02-05, Friday day mid,
    Friday evening peak."""
    tonight = _hours(FRI - timedelta(days=1), [0.25, 0.22, 0.20], first_hour=21)
    friday = _hours(
        FRI,
        [0.10, 0.09, 0.03, 0.02, 0.03, 0.12, 0.20, 0.30, 0.32, 0.30,
         0.25, 0.20, 0.08, 0.07, 0.08, 0.20, 0.35, 0.40, 0.42, 0.40,
         0.30, 0.20, 0.15, 0.12],
    )
    return tonight + friday


def test_advice_offers_the_cheapest_night_and_day_windows(settings):
    hourly = _week_night_prices()
    plan = plan_day([h for h in hourly if h.start.date() == FRI], FRI, settings)
    advice = advise_ev(plan, hourly, settings)
    night, day = advice.options
    assert (night.label, night.window.start, night.window.end) == (
        "nakts", _at(FRI, 2), _at(FRI, 5),
    )
    assert (day.label, day.window.start) == ("diena", _at(FRI, 12))
    assert advice.best == 0  # night is cheaper here


def test_ev_disabled_gives_no_advice(settings):
    hourly = _week_night_prices()
    plan = plan_day([h for h in hourly if h.start.date() == FRI], FRI, settings)
    advice = advise_ev(plan, hourly, settings.model_copy(update={"ev_charge_hours": 0}))
    assert advice.options == [] and advice.plan == plan


# --- syncing the house battery with the EV ---------------------------------------------
#
# In Self-use the inverter feeds ANY load from the battery, an 11 kW EV
# included. If the EV charges right after the house battery filled, the
# battery is emptied into the car at night/midday prices within the hour and
# is gone before the peak it was bought for (2026-11-06: 6.5 kWh at 16:05,
# just before a 32-37 c evening). A Charge slot keeps a full battery full
# while the house and car run from the grid, so extending the slot over the
# EV window "holds" it.


def _scenario(settings, prices_fri: list[float], tonight=(0.25, 0.22, 0.20)):
    hourly = _hours(FRI - timedelta(days=1), list(tonight), first_hour=21) + _hours(
        FRI, prices_fri
    )
    plan = plan_day([h for h in hourly if h.start.date() == FRI], FRI, settings)
    return plan, hourly


def test_house_charge_is_held_through_the_recommended_ev_window(settings):
    """House charges 11-13, the cheapest EV window is 11-14. Unheld, 13-14
    would pour the battery into the car at 9 c; held, it serves 14-21 at
    ~18.5 c."""
    prices = [0.15] * 7 + [0.128, 0.124, 0.074, 0.117, 0.091, 0.064, 0.090,
                           0.228, 0.273, 0.237, 0.276, 0.113, 0.109, 0.057,
                           0.069, 0.059]
    plan, hourly = _scenario(settings, prices)
    assert [(c.charge.start.hour, c.charge.end.hour) for c in plan.cycles] == [(11, 13)]
    advice = advise_ev(plan, hourly, settings)
    best = advice.options[advice.best]
    assert (best.window.start, best.window.end) == (_at(FRI, 11), _at(FRI, 14))
    cycle = advice.plan.cycles[0]
    assert cycle.hold_until == _at(FRI, 14)
    assert cycle.discharge.start == _at(FRI, 14)  # revalued on the later drain
    assert advice.plan.total_net_profit_eur == pytest.approx(cycle.net_profit_eur)
    assert best.drains_battery is False


def test_hold_is_skipped_when_the_later_drain_cannot_be_valued(settings):
    """Charge 15-17, EV cheapest 15-18. Holding to 18:00 would push the drain
    past midnight, where there are no prices to value it — so no hold, and the
    advice must say this EV window will empty the house battery."""
    prices = [0.20] * 15 + [0.02, 0.02, 0.03] + [0.40] * 6
    plan, hourly = _scenario(settings, prices)
    assert [(c.charge.start.hour, c.charge.end.hour) for c in plan.cycles] == [(15, 17)]
    advice = advise_ev(plan, hourly, settings)
    best = advice.options[advice.best]
    assert best.window.start == _at(FRI, 15)
    assert advice.plan.cycles[0].hold_until is None
    assert best.drains_battery is True


def test_ev_window_before_the_house_charge_needs_no_hold(settings):
    prices = [0.03] * 3 + [0.07] * 9 + [0.04, 0.04] + [0.30] * 10
    plan, hourly = _scenario(settings, prices)
    assert [(c.charge.start.hour, c.charge.end.hour) for c in plan.cycles] == [(12, 14)]
    advice = advise_ev(plan, hourly, settings)
    assert advice.options[advice.best].window.start == _at(FRI, 0)
    assert advice.plan == plan
    assert not any(o.drains_battery for o in advice.options)


def test_cheapest_block_sorts_and_dedupes_its_input():
    # Unsorted input once let [00h, 05h, 02h] pass as a "contiguous" block
    # (last - first == 2 h) and report the wrong average.
    h = {i: HourlyPrice(start=_at(FRI, i), eur_per_kwh=p)
         for i, p in enumerate([0.01, 0.50, 0.01, 0.50, 0.50, 0.01])}
    scrambled = [h[0], h[5], h[2], h[1], h[3], h[4], h[2]]
    block = cheapest_block(scrambled, 3, _at(FRI, 0), _at(FRI, 6))
    assert block.avg_eur_per_kwh == pytest.approx((0.01 + 0.50 + 0.01) / 3)
