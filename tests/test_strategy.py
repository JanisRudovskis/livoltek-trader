"""Tests for the strategy module."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from livoltek_trader.config import Settings
from livoltek_trader.elering import PricePeriod
from livoltek_trader.solar import PvForecast
from livoltek_trader.strategy import (
    CyclePair,
    DailyPlan,
    HourlyPrice,
    TradingWindow,
    _build_blocks,
    _build_cycle,
    aggregate_hourly,
    plan_day,
)

UTC = timezone.utc


@pytest.fixture
def settings() -> Settings:
    return Settings(
        battery_capacity_kwh=5.0,
        round_trip_efficiency=1.0,
        battery_price_eur=3000.0,
        battery_cycle_life=6000,
        max_cycles_per_day=2,
        hours_per_cycle=2,
        min_net_profit_per_cycle_eur=0.10,
    )


def _hour(day: int, h: int) -> datetime:
    return datetime(2026, 5, day, h, 0, tzinfo=UTC)


def _hourly_series(day: int, prices: list[float]) -> list[HourlyPrice]:
    return [HourlyPrice(start=_hour(day, i), eur_per_kwh=p) for i, p in enumerate(prices)]


def _quarters(day: int, hours: dict[int, list[float]]) -> list[PricePeriod]:
    out: list[PricePeriod] = []
    for h, qs in hours.items():
        for i, price in enumerate(qs):
            out.append(
                PricePeriod(
                    start=_hour(day, h) + timedelta(minutes=15 * i),
                    eur_per_kwh=price,
                )
            )
    return out


# --- aggregate_hourly ---------------------------------------------------------


def test_aggregate_hourly_means_quarters():
    periods = _quarters(9, {10: [0.10, 0.20, 0.30, 0.40], 11: [0.50, 0.50, 0.50, 0.50]})
    out = aggregate_hourly(periods)
    assert len(out) == 2
    assert out[0].start == _hour(9, 10)
    assert out[0].eur_per_kwh == pytest.approx(0.25)
    assert out[1].eur_per_kwh == pytest.approx(0.50)


def test_aggregate_hourly_handles_partial_hours():
    periods = _quarters(9, {10: [0.10, 0.20]})
    out = aggregate_hourly(periods)
    assert len(out) == 1
    assert out[0].eur_per_kwh == pytest.approx(0.15)


def test_aggregate_hourly_full_day_96_to_24():
    flat = {h: [0.10] * 4 for h in range(24)}
    periods = _quarters(9, flat)
    out = aggregate_hourly(periods)
    assert len(out) == 24
    assert all(p.eur_per_kwh == pytest.approx(0.10) for p in out)


def test_aggregate_hourly_unsorted_input_sorted_output():
    periods = list(reversed(_quarters(9, {10: [0.1] * 4, 9: [0.2] * 4})))
    out = aggregate_hourly(periods)
    assert [h.start.hour for h in out] == [9, 10]


# --- _build_blocks ------------------------------------------------------------


def test_build_blocks_size_two_skips_non_consecutive():
    hourly = [
        HourlyPrice(start=_hour(9, 0), eur_per_kwh=0.10),
        HourlyPrice(start=_hour(9, 1), eur_per_kwh=0.20),
        # skip hour 2
        HourlyPrice(start=_hour(9, 3), eur_per_kwh=0.30),
        HourlyPrice(start=_hour(9, 4), eur_per_kwh=0.40),
    ]
    blocks = _build_blocks(hourly, block_size=2)
    starts = [b.start for b in blocks]
    assert _hour(9, 0) in starts
    assert _hour(9, 3) in starts
    assert _hour(9, 1) not in starts


def test_build_blocks_avg_is_arithmetic_mean():
    hourly = _hourly_series(9, [0.10, 0.30])
    blocks = _build_blocks(hourly, block_size=2)
    assert len(blocks) == 1
    assert blocks[0].avg_eur_per_kwh == pytest.approx(0.20)
    assert blocks[0].start == _hour(9, 0)
    assert blocks[0].end == _hour(9, 2)


# --- _build_cycle -------------------------------------------------------------


def test_build_cycle_profit_math(settings):
    charge = TradingWindow(start=_hour(9, 2), end=_hour(9, 4), avg_eur_per_kwh=0.05)
    discharge = TradingWindow(start=_hour(9, 18), end=_hour(9, 20), avg_eur_per_kwh=0.30)
    cycle = _build_cycle(charge, discharge, settings)
    # spread 0.25 * usable 5 kWh * eff 1.0 = 1.25 €; wear 3000/6000 = 0.50; net = 0.75
    assert cycle.gross_revenue_eur == pytest.approx(1.25)
    assert cycle.wear_cost_eur == pytest.approx(0.50)
    assert cycle.net_profit_eur == pytest.approx(0.75)


# --- plan_day -----------------------------------------------------------------


def test_plan_day_skips_when_too_few_hours(settings):
    hourly = _hourly_series(9, [0.10, 0.50, 0.10])  # 3 hours; needs ≥ 4 for 2-cycle of size 2
    plan = plan_day(hourly, date(2026, 5, 9), settings)
    assert plan.cycles == []
    assert plan.skipped_reason == "not enough hourly data to form a cycle"


def test_plan_day_skips_when_no_cycle_meets_threshold(settings):
    hourly = _hourly_series(9, [0.20] * 24)
    plan = plan_day(hourly, date(2026, 5, 9), settings)
    assert plan.cycles == []
    assert plan.skipped_reason and plan.skipped_reason.startswith("no cycle nets")
    assert plan.total_net_profit_eur == 0.0


def test_plan_day_picks_cheapest_charge_and_dearest_discharge(settings):
    settings = settings.model_copy(update={"max_cycles_per_day": 1})
    prices = [0.50] * 24
    prices[2] = 0.05
    prices[3] = 0.05
    prices[18] = 0.80
    prices[19] = 0.80
    hourly = _hourly_series(9, prices)
    plan = plan_day(hourly, date(2026, 5, 9), settings)
    assert len(plan.cycles) == 1
    cycle = plan.cycles[0]
    assert cycle.charge.start == _hour(9, 2)
    assert cycle.charge.end == _hour(9, 4)
    assert cycle.discharge.start == _hour(9, 18)
    assert cycle.discharge.end == _hour(9, 20)
    assert cycle.charge.avg_eur_per_kwh == pytest.approx(0.05)
    assert cycle.discharge.avg_eur_per_kwh == pytest.approx(0.80)


def test_plan_day_finds_two_cycles_when_profitable(settings):
    prices = [0.50] * 24
    prices[2:4] = [0.05, 0.05]
    prices[8:10] = [0.80, 0.80]
    prices[14:16] = [0.05, 0.05]
    prices[20:22] = [0.80, 0.80]
    hourly = _hourly_series(9, prices)
    plan = plan_day(hourly, date(2026, 5, 9), settings)
    assert len(plan.cycles) == 2
    c1, c2 = plan.cycles
    assert c1.discharge.end <= c2.charge.start
    assert plan.total_net_profit_eur == pytest.approx(c1.net_profit_eur + c2.net_profit_eur)


def test_plan_day_falls_back_to_one_cycle_if_only_one_profitable(settings):
    prices = [0.50] * 24
    prices[2:4] = [0.05, 0.05]
    prices[20:22] = [0.80, 0.80]
    hourly = _hourly_series(9, prices)
    plan = plan_day(hourly, date(2026, 5, 9), settings)
    assert len(plan.cycles) == 1


def test_plan_day_respects_max_cycles_zero(settings):
    settings = settings.model_copy(update={"max_cycles_per_day": 0})
    prices = [0.50] * 24
    prices[2:4] = [0.05, 0.05]
    prices[20:22] = [0.80, 0.80]
    hourly = _hourly_series(9, prices)
    plan = plan_day(hourly, date(2026, 5, 9), settings)
    assert plan.cycles == []


def test_plan_day_enforces_temporal_order(settings):
    # Cheap at midday, expensive at evening. Algorithm must charge first
    # and discharge later — the reverse would be more profitable but causal.
    settings = settings.model_copy(update={"max_cycles_per_day": 1})
    prices = [0.30] * 24
    prices[12:14] = [0.05, 0.05]
    prices[20:22] = [0.50, 0.50]
    hourly = _hourly_series(9, prices)
    plan = plan_day(hourly, date(2026, 5, 9), settings)
    assert len(plan.cycles) == 1
    cycle = plan.cycles[0]
    assert cycle.charge.start < cycle.discharge.start
    assert cycle.charge.start.hour == 12
    assert cycle.discharge.start.hour == 20


def test_plan_day_subtracts_wear_when_below_breakeven(settings):
    # Spread = 0.05; revenue = 0.05 * 5 * 1.0 = 0.25; wear = 0.50; net = -0.25
    prices = [0.10] * 24
    prices[2:4] = [0.05, 0.05]
    prices[20:22] = [0.15, 0.15]
    hourly = _hourly_series(9, prices)
    plan = plan_day(hourly, date(2026, 5, 9), settings)
    assert plan.cycles == []  # wear-negative => skipped


def test_plan_day_efficiency_reduces_revenue(settings):
    settings = settings.model_copy(update={"round_trip_efficiency": 0.5})
    # spread 0.50; usable = 5 * 0.5 = 2.5 kWh; rev = 1.25; wear = 0.50; net = 0.75
    prices = [0.30] * 24
    prices[2:4] = [0.05, 0.05]
    prices[20:22] = [0.55, 0.55]
    hourly = _hourly_series(9, prices)
    plan = plan_day(hourly, date(2026, 5, 9), settings)
    assert len(plan.cycles) == 1
    assert plan.cycles[0].gross_revenue_eur == pytest.approx(1.25)
    assert plan.cycles[0].net_profit_eur == pytest.approx(0.75)


# --- PV-aware planning -------------------------------------------------------


def _strong_spread_hourly(day: int) -> list[HourlyPrice]:
    prices = [0.50] * 24
    prices[2:4] = [0.05, 0.05]
    prices[20:22] = [0.80, 0.80]
    return _hourly_series(day, prices)


def _pv(target: date, kwh: float) -> PvForecast:
    return PvForecast(
        target_date=target,
        expected_kwh=kwh,
        shortwave_radiation_mj_m2=kwh / 2.98,
        sunshine_hours=0.0,
        cloud_cover_pct=50.0,
    )


def test_plan_day_skips_when_pv_covers_load(settings):
    # PV (30) > load (22) — gap 0, well below one cycle output (5).
    settings = settings.model_copy(update={"expected_daily_load_kwh": 22.0})
    hourly = _strong_spread_hourly(9)
    plan = plan_day(
        hourly,
        date(2026, 5, 9),
        settings,
        pv_forecast=_pv(date(2026, 5, 9), kwh=30.0),
    )
    assert plan.cycles == []
    assert plan.skipped_reason and "PV forecast" in plan.skipped_reason


def test_plan_day_proceeds_when_gap_exceeds_cycle_output(settings):
    # Gap = 22 - 8 = 14 kWh, cycle output = 5 kWh — clearly worth trading.
    settings = settings.model_copy(update={"expected_daily_load_kwh": 22.0})
    hourly = _strong_spread_hourly(9)
    plan = plan_day(
        hourly,
        date(2026, 5, 9),
        settings,
        pv_forecast=_pv(date(2026, 5, 9), kwh=8.0),
    )
    assert plan.cycles, "cloudy day should still produce a plan"
    assert plan.skipped_reason is None


def test_plan_day_skips_when_gap_below_one_cycle(settings):
    # Gap = 22 - 19 = 3 kWh, cycle output = 5 kWh — cycle would over-fill.
    settings = settings.model_copy(update={"expected_daily_load_kwh": 22.0})
    hourly = _strong_spread_hourly(9)
    plan = plan_day(
        hourly,
        date(2026, 5, 9),
        settings,
        pv_forecast=_pv(date(2026, 5, 9), kwh=19.0),
    )
    assert plan.cycles == []
    assert plan.skipped_reason and "below one cycle output" in plan.skipped_reason


def test_plan_day_without_pv_forecast_behaves_as_before(settings):
    hourly = _strong_spread_hourly(9)
    plan = plan_day(hourly, date(2026, 5, 9), settings, pv_forecast=None)
    assert plan.cycles, "with no forecast, fall back to grid-only logic"
