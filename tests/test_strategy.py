"""Tests for the strategy module."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from livoltek_trader.config import Settings
from livoltek_trader.elering import PricePeriod
from livoltek_trader.solar import PvForecast
from livoltek_trader.strategy import (
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
        wear_cost_per_cycle_eur=0.50,
        max_cycles_per_day=2,
        hours_per_cycle=2,
        battery_drain_hours=7,
        buy_margin_eur_per_kwh=0.05,
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
    drain = TradingWindow(start=_hour(9, 4), end=_hour(9, 11), avg_eur_per_kwh=0.30)
    cycle = _build_cycle(charge, drain, settings)
    # bought 5 @ 0.05 = 0.25; delivered 5 @ 0.30 = 1.50; losses 0 so no margin
    # term; wear pinned to 0.50 by the fixture => gross 1.25, net 0.75
    assert cycle.gross_revenue_eur == pytest.approx(1.25)
    assert cycle.wear_cost_eur == pytest.approx(0.50)
    assert cycle.net_profit_eur == pytest.approx(0.75)


def test_build_cycle_charges_full_capacity_and_margin_on_losses(settings):
    """With round-trip losses, we buy more than we deliver and pay margin on
    the difference. The old spread formula ignored both."""
    lossy = settings.model_copy(update={"round_trip_efficiency": 0.5})
    charge = TradingWindow(start=_hour(9, 2), end=_hour(9, 4), avg_eur_per_kwh=0.05)
    drain = TradingWindow(start=_hour(9, 4), end=_hour(9, 11), avg_eur_per_kwh=0.55)

    lossless_gross = _build_cycle(charge, drain, settings).gross_revenue_eur
    lossy_gross = _build_cycle(charge, drain, lossy).gross_revenue_eur

    # lossless: 5 * 0.55 - 5 * 0.05 - 0        = 2.50
    # lossy   : 2.5 * 0.55 - 5 * 0.05 - 0.05*2.5 = 1.00
    assert lossless_gross == pytest.approx(2.50)
    assert lossy_gross == pytest.approx(1.00)
    assert lossy_gross < lossless_gross


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


def test_plan_day_derives_discharge_from_charge_not_from_dearest_block(settings):
    """The dearest block of the day is NOT the discharge window.

    Previously the planner paired the cheapest charge with the dearest block
    anywhere later in the day (here 18-20 at 0.80) and booked the full spread.
    The battery cannot hold its charge that long, so the discharge window is
    now derived: the hours immediately following the charge.
    """
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
    assert cycle.charge.avg_eur_per_kwh == pytest.approx(0.05)
    # Derived: starts where the charge ends, runs battery_drain_hours, and is
    # valued at 0.50 — the actual price of those hours, not the 0.80 peak.
    assert cycle.discharge.start == _hour(9, 4)
    assert cycle.discharge.end == _hour(9, 11)
    assert cycle.discharge.avg_eur_per_kwh == pytest.approx(0.50)


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


def test_plan_day_discharge_always_begins_where_charge_ends(settings):
    # Cheap at midday, expensive at evening. Temporal order is now structural:
    # the drain window starts at charge.end by construction, so it can never
    # precede the charge.
    settings = settings.model_copy(update={"max_cycles_per_day": 1})
    prices = [0.30] * 24
    prices[12:14] = [0.05, 0.05]
    prices[20:22] = [0.50, 0.50]
    hourly = _hourly_series(9, prices)
    plan = plan_day(hourly, date(2026, 5, 9), settings)
    assert len(plan.cycles) == 1
    cycle = plan.cycles[0]
    assert cycle.charge.start.hour == 12
    assert cycle.charge.start < cycle.discharge.start
    assert cycle.discharge.start == cycle.charge.end
    assert cycle.discharge.end == cycle.charge.end + timedelta(hours=7)


def test_plan_day_subtracts_wear_when_below_breakeven(settings):
    # Spread = 0.05; revenue = 0.05 * 5 * 1.0 = 0.25; wear = 0.50; net = -0.25
    prices = [0.10] * 24
    prices[2:4] = [0.05, 0.05]
    prices[20:22] = [0.15, 0.15]
    hourly = _hourly_series(9, prices)
    plan = plan_day(hourly, date(2026, 5, 9), settings)
    assert plan.cycles == []  # wear-negative => skipped


def test_plan_day_poor_efficiency_can_kill_an_otherwise_viable_day(settings):
    """Halving round-trip efficiency turns a profitable day into no plan.

    Same prices, same gate: at full efficiency the cycle clears, at 0.5 it
    does not, because we still buy the full capacity but deliver half.
    """
    prices = [0.30] * 24
    prices[2:4] = [0.05, 0.05]
    prices[20:22] = [0.55, 0.55]
    hourly = _hourly_series(9, prices)

    assert plan_day(hourly, date(2026, 5, 9), settings).cycles

    lossy = settings.model_copy(update={"round_trip_efficiency": 0.5})
    assert plan_day(hourly, date(2026, 5, 9), lossy).cycles == []


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


def test_plan_day_skips_cycles_when_the_pv_forecast_could_not_be_fetched(settings):
    """A failed PV fetch must suppress cycles, not plan blind.

    Planning blind is not neutral: on 2026-09-02 the Open-Meteo call timed out,
    the planner charged from the grid, and the day went on to produce 28 kWh of
    PV that would have filled the battery for free. Buying ~11.6 kWh plus a
    wear cycle to displace nothing costs about EUR 1.08. Skipping instead costs
    at most one winter cycle's profit (~EUR 0.43, on 29% of winter days), so
    skipping wins by roughly 9x in expectation.

    Note this is DIFFERENT from `pv_forecast=None`, which means "no PV
    constraint requested" and still plans.
    """
    hourly = _strong_spread_hourly(9)

    blind = plan_day(hourly, date(2026, 5, 9), settings, pv_forecast=None)
    assert blind.cycles, "sanity: these prices are otherwise tradeable"

    plan = plan_day(
        hourly,
        date(2026, 5, 9),
        settings,
        pv_forecast=None,
        pv_forecast_failed=True,
    )
    assert plan.cycles == []
    assert plan.stop_window is None
    assert plan.is_empty, "empty plan disables ToU and leaves Self-use running"
    assert plan.skipped_reason and "PV forecast unavailable" in plan.skipped_reason


# --- max-six-cycle cap ------------------------------------------------------


def _alternating_prices() -> list[float]:
    """12 two-hour blocks alternating cheap/dear."""
    return [0.05 if (h // 2) % 2 == 0 else 0.80 for h in range(24)]


def test_plan_day_footprint_not_the_cap_is_what_limits_cycles(settings):
    """A 9 h footprint (2 h charge + 7 h drain) leaves room for only 2 cycles.

    This replaces the old six-cycle test, which asserted that six phantom
    back-to-back night charges were correct: they were hour-disjoint but each
    began on a battery the previous charge had just filled, so the plan booked
    six cycles of profit for one battery's worth of energy.
    """
    generous = settings.model_copy(update={"max_cycles_per_day": 6})
    hourly = _hourly_series(9, _alternating_prices())
    plan = plan_day(hourly, date(2026, 5, 9), generous)

    assert len(plan.cycles) == 2, f"expected 2, got {len(plan.cycles)}"
    starts = [c.charge.start for c in plan.cycles]
    assert starts == sorted(starts), "cycles must be returned in time order"
    assert starts == [_hour(9, 0), _hour(9, 12)]


def test_plan_day_cap_binds_below_the_structural_limit(settings):
    """max_cycles_per_day still applies when set below what would fit."""
    capped = settings.model_copy(update={"max_cycles_per_day": 1})
    hourly = _hourly_series(9, _alternating_prices())
    plan = plan_day(hourly, date(2026, 5, 9), capped)
    assert len(plan.cycles) == 1


def test_plan_day_greedy_prefers_higher_net_over_cheaper_charge(settings):
    """When footprints collide, the highest net wins — not the cheapest charge.

    00-02 is the cheapest block of the day, but its drain window catches only
    two of the expensive hours. 02-04 costs more to charge yet drains entirely
    across the dear run, so it nets more and displaces 00-02.
    """
    generous = settings.model_copy(update={"max_cycles_per_day": 6})
    prices = [0.05, 0.05, 0.06, 0.06] + [0.60] * 7 + [0.20] * 13
    assert len(prices) == 24
    hourly = _hourly_series(9, prices)
    plan = plan_day(hourly, date(2026, 5, 9), generous)

    assert len(plan.cycles) == 1
    assert plan.cycles[0].charge.start == _hour(9, 2)


# --- Stop slot (block-and-export) -----------------------------------------


def _stop_scenario_hourly(day: int) -> list[HourlyPrice]:
    """Synthetic day with morning peak, midday trough, evening peak.

    UTC hours map to Riga local (UTC+3 in May DST):
    - UTC 04-06 = Riga 07-09 (morning peak)
    - UTC 09-11 = Riga 12-14 (cheap midday trough)
    - UTC 15-17 = Riga 18-20 (evening peak — outside PV window for Riga 20)
    """
    prices = [0.10] * 24
    prices[4:7] = [0.15, 0.15, 0.15]
    prices[9:12] = [0.03, 0.03, 0.03]
    prices[15:18] = [0.25, 0.25, 0.25]
    return _hourly_series(day, prices)


def test_plan_day_adds_stop_window_on_sunny_day(settings):
    # PV (35) ≥ load (22) × 1.5. Cycle output 5 kWh > grid imports 0 — cycles
    # skipped. Discharge window should cover the morning peak.
    settings = settings.model_copy(update={"expected_daily_load_kwh": 22.0})
    hourly = _stop_scenario_hourly(9)
    plan = plan_day(
        hourly,
        date(2026, 5, 9),
        settings,
        pv_forecast=_pv(date(2026, 5, 9), kwh=35.0),
    )
    assert plan.cycles == []
    assert plan.stop_window is not None
    # Window ends just before the cheapest hour (UTC 9 = Riga 12).
    assert plan.stop_window.end == _hour(9, 9)
    assert plan.stop_window.avg_eur_per_kwh > 0.02
    assert plan.skipped_reason is None
    assert not plan.is_empty


def test_plan_day_no_stop_window_when_pv_below_multiplier(settings):
    # PV (27) > load (22), but PV/load = 1.23 < 1.5 multiplier.
    # Borderline cloudy day: forecast might be wrong, so skip Discharge.
    # This is today's scenario (2026-05-21).
    settings = settings.model_copy(update={"expected_daily_load_kwh": 22.0})
    hourly = _stop_scenario_hourly(9)
    plan = plan_day(
        hourly,
        date(2026, 5, 9),
        settings,
        pv_forecast=_pv(date(2026, 5, 9), kwh=27.0),
    )
    assert plan.stop_window is None
    # Cycles also skipped via existing gap-vs-cycle-output check.
    assert plan.cycles == []
    assert plan.is_empty


def test_plan_day_stop_window_skipped_when_pv_low(settings):
    # PV (15) < load (22) — Discharge must NOT trigger on cloudy days.
    settings = settings.model_copy(update={"expected_daily_load_kwh": 22.0})
    hourly = _stop_scenario_hourly(9)
    plan = plan_day(
        hourly,
        date(2026, 5, 9),
        settings,
        pv_forecast=_pv(date(2026, 5, 9), kwh=15.0),
    )
    assert plan.stop_window is None


def test_plan_day_stop_window_truncates_at_peak_end_threshold(settings):
    # Morning peak ends where price drops below cheapest × peak_end_multiplier.
    # cheapest = 0.05; peak_end_threshold = 0.05 × 2 = 0.10.
    # Hours above 0.10 form the run; first hour at or below ends it.
    settings = settings.model_copy(
        update={
            "expected_daily_load_kwh": 22.0,
            "morning_peak_end_multiplier": 2.0,
        }
    )
    prices = [0.05] * 24
    # PV daylight window UTC 3..16. Morning peak UTC 3-5 (Riga 6-8),
    # then a transitional cheap stretch, then cheapest at UTC 10.
    prices[3:6] = [0.20, 0.20, 0.20]  # well above 0.10 → in run
    prices[6:10] = [0.08, 0.08, 0.08, 0.08]  # below 0.10 → ends the run
    prices[10] = 0.05  # cheapest midday
    hourly = _hourly_series(9, prices)
    plan = plan_day(
        hourly,
        date(2026, 5, 9),
        settings,
        pv_forecast=_pv(date(2026, 5, 9), kwh=35.0),
    )
    assert plan.stop_window is not None
    assert plan.stop_window.start == _hour(9, 3)
    assert plan.stop_window.end == _hour(9, 6)  # stops where price falls to 0.08


def test_plan_day_stop_window_skipped_when_prices_flat(settings):
    # Flat prices: no peak before the cheapest hour. Discharge must be None.
    settings = settings.model_copy(update={"expected_daily_load_kwh": 22.0})
    prices = [0.10] * 24
    hourly = _hourly_series(9, prices)
    plan = plan_day(
        hourly,
        date(2026, 5, 9),
        settings,
        pv_forecast=_pv(date(2026, 5, 9), kwh=35.0),
    )
    assert plan.stop_window is None


def test_plan_day_stop_window_respects_sell_threshold(settings):
    # An hour below sell_threshold breaks the morning peak run even if it's
    # nominally part of "morning". Window ends at the first below-threshold hour.
    settings = settings.model_copy(
        update={
            "expected_daily_load_kwh": 22.0,
            "stop_sell_threshold_eur_per_kwh": 0.05,
            "morning_peak_end_multiplier": 2.0,
        }
    )
    prices = [0.10] * 24
    prices[3:6] = [0.20, 0.20, 0.20]  # qualifying morning peak (UTC 3-5)
    prices[6] = 0.04  # below sell_threshold → breaks the run
    prices[10] = 0.01  # cheapest midday
    hourly = _hourly_series(9, prices)
    plan = plan_day(
        hourly,
        date(2026, 5, 9),
        settings,
        pv_forecast=_pv(date(2026, 5, 9), kwh=35.0),
    )
    assert plan.stop_window is not None
    assert plan.stop_window.start == _hour(9, 3)
    assert plan.stop_window.end == _hour(9, 6)


def test_plan_day_stop_window_skipped_when_cheapest_is_first_daylight_hour(settings):
    # Cheapest hour is the very first daylight hour — no morning peak exists.
    settings = settings.model_copy(update={"expected_daily_load_kwh": 22.0})
    prices = [0.50] * 24
    prices[3] = 0.01  # UTC 3 = Riga 6 (first daylight hour)
    prices[10] = 0.20
    hourly = _hourly_series(9, prices)
    plan = plan_day(
        hourly,
        date(2026, 5, 9),
        settings,
        pv_forecast=_pv(date(2026, 5, 9), kwh=35.0),
    )
    assert plan.stop_window is None


def test_plan_day_stop_window_and_no_cycle_on_sunny_day(settings):
    # PV (35) ≥ load (22) × 1.5 → Discharge planned. Cycles skipped because
    # grid imports gap = 0 < cycle output 5. Discharge-only result.
    settings = settings.model_copy(
        update={
            "expected_daily_load_kwh": 22.0,
            "battery_capacity_kwh": 5.0,
            "round_trip_efficiency": 1.0,
            "max_cycles_per_day": 6,
        }
    )
    hourly = _stop_scenario_hourly(9)
    plan = plan_day(
        hourly,
        date(2026, 5, 9),
        settings,
        pv_forecast=_pv(date(2026, 5, 9), kwh=35.0),
    )
    assert plan.stop_window is not None
    assert plan.cycles == []


def test_plan_day_returns_empty_plan_on_flat_low_pv_day(settings):
    # PV low + flat prices: no cycles, no Discharge, empty plan with skipped_reason.
    settings = settings.model_copy(update={"expected_daily_load_kwh": 22.0})
    prices = [0.10] * 24
    hourly = _hourly_series(9, prices)
    plan = plan_day(
        hourly,
        date(2026, 5, 9),
        settings,
        pv_forecast=_pv(date(2026, 5, 9), kwh=5.0),
    )
    assert plan.cycles == []
    assert plan.stop_window is None
    assert plan.is_empty
    assert plan.skipped_reason is not None


# --- drain-anchored valuation (spec rev3 Part 1) -----------------------------
#
# Expected numbers below were computed independently of the implementation from
# the formula in spec section 4.2, using the shipping defaults:
#   capacity 11.63, RTE 0.764 -> output 8.88532, margin 0.05, wear 0.50
#   loss-margin term = 0.05 * (11.63 - 8.88532) = 0.137234


MEASURED_CAPACITY = 11.63
MEASURED_RTE = 0.764
MEASURED_OUTPUT = MEASURED_CAPACITY * MEASURED_RTE  # 8.88532


@pytest.fixture
def measured() -> Settings:
    """Shipping hardware constants, stated explicitly so .env cannot alter them."""
    return Settings(
        battery_capacity_kwh=MEASURED_CAPACITY,
        round_trip_efficiency=MEASURED_RTE,
        battery_price_eur=3000.0,
        wear_cost_per_cycle_eur=0.05,
        max_cycles_per_day=6,
        hours_per_cycle=2,
        battery_drain_hours=7,
        buy_margin_eur_per_kwh=0.05,
        min_net_profit_per_cycle_eur=0.25,
    )


def _reference_day_prices() -> list[float]:
    """Ordinary Latvian winter shape: cheap night, morning and evening peaks."""
    return (
        [0.05] * 6 + [0.12] + [0.22] * 3 + [0.13] * 6 + [0.18]
        + [0.26] * 4 + [0.10] * 3
    )


def test_wear_cost_is_marginal_not_amortised():
    """Wear must be the MARGINAL cost of one extra cycle, not price/cycles.

    price/cycles (EUR 3000 / 6000 = EUR 0.50) assumes cycles are the binding
    constraint. At ~1 cycle/day, 6000 cycles is 16 years while LFP calendar
    life is 10-15, so the battery dies of age before it runs out of cycles.
    The cycle we are pricing is therefore nearly free at the margin, and a
    EUR 0.50 charge blocks cycles that are genuinely worth taking.
    """
    default = Settings.model_fields["wear_cost_per_cycle_eur"].default
    assert default < 0.20, (
        "an amortised EUR 0.50 blocks real winter cycles; wear must reflect "
        "the marginal cost of cycling a calendar-limited battery"
    )
    assert default > 0.0, "cycling is not completely free — deep cycles do age cells"


def test_wear_cost_is_not_derived_from_price_over_cycle_life(settings):
    """The old derivation must be gone, not merely overridden.

    Leaving `battery_price_eur / battery_cycle_life` in place invited exactly
    the confusion it caused: a reader (or a future edit) treats it as the real
    wear cost again.
    """
    assert not hasattr(Settings, "wear_cost_per_cycle_eur_derived")
    cheap = settings.model_copy(update={"wear_cost_per_cycle_eur": 0.02})
    assert cheap.wear_cost_per_cycle_eur == pytest.approx(0.02)


def test_config_defaults_are_the_measured_hardware_constants():
    # Read class defaults, not an instance — an instance would absorb .env.
    f = Settings.model_fields
    assert f["battery_capacity_kwh"].default == pytest.approx(11.63)
    assert f["round_trip_efficiency"].default == pytest.approx(0.764)
    assert f["battery_drain_hours"].default == 7
    assert f["buy_margin_eur_per_kwh"].default == pytest.approx(0.05)


def test_plan_day_golden_reference_day(measured):
    hourly = _hourly_series(9, _reference_day_prices())
    plan = plan_day(hourly, date(2026, 5, 9), measured)

    assert len(plan.cycles) == 1
    cycle = plan.cycles[0]
    assert cycle.charge.start == _hour(9, 4)
    assert cycle.charge.end == _hour(9, 6)
    assert cycle.net_profit_eur == pytest.approx(0.7163837714, abs=1e-6)
    assert plan.total_net_profit_eur == pytest.approx(0.7163837714, abs=1e-6)


def test_plan_day_drain_assumption_changes_valuation(measured):
    """Same day at a 4 h assumption books ~2x the value for one physical cycle.

    This is the argument for choosing the long end of the drain range: the
    shorter assumption attributes all output to the first, dearest hours.
    """
    hourly = _hourly_series(9, _reference_day_prices())
    short = measured.model_copy(update={"battery_drain_hours": 4})
    plan = plan_day(hourly, date(2026, 5, 9), short)

    # A 4 h window also lets a SECOND cycle fit (6 h footprint instead of 9),
    # so the shorter assumption both over-values each cycle and books more of
    # them — the two errors compound.
    assert [c.charge.start for c in plan.cycles] == [_hour(9, 4), _hour(9, 14)]
    assert plan.total_net_profit_eur == pytest.approx(1.3972462, abs=1e-6)

    long_plan = plan_day(hourly, date(2026, 5, 9), measured)
    assert len(long_plan.cycles) == 1
    assert long_plan.total_net_profit_eur < plan.total_net_profit_eur


def test_derived_drain_window_is_the_hours_following_the_charge(measured):
    hourly = _hourly_series(9, _reference_day_prices())
    cycle = plan_day(hourly, date(2026, 5, 9), measured).cycles[0]

    assert cycle.discharge.start == cycle.charge.end
    assert cycle.discharge.end == cycle.charge.end + timedelta(hours=7)
    # Mean of hours 06..12 inclusive.
    assert cycle.discharge.avg_eur_per_kwh == pytest.approx(1.17 / 7)


def test_plan_day_drops_charge_block_without_a_full_drain_window(measured):
    """A late charge with only 4 following hours is dropped, not part-valued.

    Hours 18-19 are nearly free and 20-23 are dear, so valuing the block
    against a partial window would book a huge, unrealisable profit: the
    battery cannot deliver its whole output into four hours.
    """
    prices = [0.30] * 18 + [0.02] * 2 + [0.60] * 4
    assert len(prices) == 24
    hourly = _hourly_series(9, prices)

    assert plan_day(hourly, date(2026, 5, 9), measured).cycles == [], (
        "no block leaves 7 contiguous drain hours, so nothing is tradeable"
    )

    # Same day with a 4 h drain: the window now fits and the cycle is taken.
    # This proves the rejection above comes from the window length, not from
    # the prices being unattractive.
    short = measured.model_copy(update={"battery_drain_hours": 4})
    taken = plan_day(hourly, date(2026, 5, 9), short)
    assert len(taken.cycles) == 1
    assert taken.cycles[0].charge.start == _hour(9, 18)


def test_plan_day_rejects_overlapping_footprints(measured):
    """Two night charges 3 h apart cannot both be chosen.

    This replaces the old six-cycle cap test, which asserted that phantom
    back-to-back night charges were correct.
    """
    hourly = _hourly_series(9, _reference_day_prices())
    plan = plan_day(hourly, date(2026, 5, 9), measured)

    spans = [
        (c.charge.start, c.charge.end + timedelta(hours=7)) for c in plan.cycles
    ]
    for i, a in enumerate(spans):
        for b in spans[i + 1 :]:
            assert a[0] >= b[1] or a[1] <= b[0], f"footprints overlap: {a} {b}"


def test_plan_day_allows_footprints_touching_at_the_boundary(measured):
    """A charge starting exactly when the previous fill runs out is allowed."""
    prices = (
        [0.05] * 2 + [0.25] * 7      # charge 00-02, drain 02-09
        + [0.05] * 2 + [0.25] * 7    # charge 09-11, drain 11-18
        + [0.10] * 6
    )
    assert len(prices) == 24
    hourly = _hourly_series(9, prices)
    plan = plan_day(hourly, date(2026, 5, 9), measured)

    starts = [c.charge.start for c in plan.cycles]
    assert starts == [_hour(9, 0), _hour(9, 9)]
    # The derived drain windows must span the full 7 h, so the first cycle's
    # footprint ends exactly where the second charge begins.
    assert plan.cycles[0].discharge.end == _hour(9, 9)
    assert plan.cycles[1].discharge.end == _hour(9, 18)


def test_plan_day_prefers_block_adjacent_to_peak_over_globally_cheapest(measured):
    """The cheapest hours of the day lose if the battery empties before the peak."""
    prices = [0.01] * 4 + [0.11] * 16 + [0.40] * 4
    assert len(prices) == 24
    hourly = _hourly_series(9, prices)
    short = measured.model_copy(update={"battery_drain_hours": 4})
    plan = plan_day(hourly, date(2026, 5, 9), short)

    # 18-20 charges at 0.11 and drains across the 0.40 run. The 0.01 hours are
    # eleven times cheaper, yet their drain window never sees an expensive hour,
    # so the peak-adjacent block must be the most valuable cycle of the day.
    best = max(plan.cycles, key=lambda c: c.net_profit_eur)
    assert best.charge.start == _hour(9, 18)
    cheapest_charge = min(plan.cycles, key=lambda c: c.charge.avg_eur_per_kwh)
    assert cheapest_charge.net_profit_eur < best.net_profit_eur, (
        "cheapness must not outrank proximity to the expensive hours"
    )


def test_plan_day_charges_buy_margin_on_round_trip_losses(measured):
    """The margin paid on losses alone can push a cycle below the gate."""
    prices = [0.05] * 2 + [0.1048] * 7 + [0.05] * 15
    assert len(prices) == 24
    hourly = _hourly_series(9, prices)

    without_margin = measured.model_copy(
        update={"buy_margin_eur_per_kwh": 0.0}
    )
    assert plan_day(hourly, date(2026, 5, 9), without_margin).cycles, (
        "cycle should clear the gate when losses carry no margin"
    )
    assert plan_day(hourly, date(2026, 5, 9), measured).cycles == [], (
        "the 0.137 EUR margin on round-trip losses must sink this cycle"
    )


def test_plan_day_rejects_peak_then_crash_day(measured):
    """Calm morning peak followed by a windy midday crash must not be traded.

    At a 4 h assumption this books +0.36 EUR; the real 7 h drain runs into the
    0.02 EUR crash and loses money. Regression guard against shortening
    battery_drain_hours without measuring it first.
    """
    prices = [0.13] * 4 + [0.09] * 2 + [0.30] * 3 + [0.02] * 5 + [0.12] * 10
    assert len(prices) == 24
    hourly = _hourly_series(9, prices)

    honest = plan_day(hourly, date(2026, 5, 9), measured)
    charge_hours = [c.charge.start for c in honest.cycles]
    assert _hour(9, 4) not in charge_hours, (
        "the 04-06 charge drains into the midday crash and loses money"
    )
    # The genuinely good trade on this day is buying inside the crash itself
    # (0.02) and draining across the flat 0.12 evening — that one must survive.
    assert _hour(9, 12) in charge_hours

    short = measured.model_copy(update={"battery_drain_hours": 4})
    optimistic = plan_day(hourly, date(2026, 5, 9), short)
    assert _hour(9, 4) in [c.charge.start for c in optimistic.cycles], (
        "a 4 h assumption wrongly makes the pre-crash charge look profitable"
    )


def test_plan_day_handles_negative_prices(measured):
    """Being paid to charge is profitable and must not break the arithmetic."""
    prices = [-0.10] * 4 + [0.05] * 20
    hourly = _hourly_series(9, prices)
    plan = plan_day(hourly, date(2026, 5, 9), measured)

    assert len(plan.cycles) == 1
    assert plan.cycles[0].charge.start == _hour(9, 2)
    assert plan.cycles[0].net_profit_eur == pytest.approx(1.420032, abs=1e-5)


def test_daily_plan_is_empty_property():
    empty = DailyPlan(
        target_date=date(2026, 5, 9),
        cycles=[],
        skipped_reason="x",
        total_net_profit_eur=0.0,
    )
    assert empty.is_empty

    window = TradingWindow(
        start=_hour(9, 4), end=_hour(9, 7), avg_eur_per_kwh=0.15
    )
    stop_only = DailyPlan(
        target_date=date(2026, 5, 9),
        cycles=[],
        skipped_reason=None,
        total_net_profit_eur=0.0,
        stop_window=window,
    )
    assert not stop_only.is_empty
