"""Charge/discharge scheduling strategy — pure functions over hourly prices.

Self-consumption arbitrage: charge the battery from the grid during cheap
spot hours and use the stored energy in the household during expensive hours,
avoiding a buy at the higher tariff. Supplier margin cancels in this mode,
so net per-kWh benefit is the pure Nord Pool spread; the only cost we
internalise is battery wear.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable

from pydantic import BaseModel, ConfigDict

from livoltek_trader.config import Settings, get_settings
from livoltek_trader.elering import PricePeriod
from livoltek_trader.solar import PvForecast


class HourlyPrice(BaseModel):
    """Price for a single clock hour, averaged from contributing periods."""

    model_config = ConfigDict(frozen=True)

    start: datetime
    eur_per_kwh: float


class TradingWindow(BaseModel):
    """A consecutive run of hours on which to charge or discharge."""

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime
    avg_eur_per_kwh: float


class CyclePair(BaseModel):
    """One charge window followed by one discharge window."""

    model_config = ConfigDict(frozen=True)

    charge: TradingWindow
    discharge: TradingWindow
    gross_revenue_eur: float
    wear_cost_eur: float
    net_profit_eur: float


class DailyPlan(BaseModel):
    """The chosen plan for a target day, possibly empty if not worthwhile."""

    model_config = ConfigDict(frozen=True)

    target_date: date
    cycles: list[CyclePair]
    skipped_reason: str | None
    total_net_profit_eur: float


def aggregate_hourly(periods: Iterable[PricePeriod]) -> list[HourlyPrice]:
    """Bucket sub-hour periods into clock hours; each hour is the mean of its periods.

    Hours with no contributing periods are dropped; output is sorted by time.
    """
    buckets: dict[datetime, list[float]] = defaultdict(list)
    for p in periods:
        bucket_key = p.start.replace(minute=0, second=0, microsecond=0)
        buckets[bucket_key].append(p.eur_per_kwh)
    return [
        HourlyPrice(start=h, eur_per_kwh=sum(prices) / len(prices))
        for h, prices in sorted(buckets.items())
    ]


def _build_blocks(hourly: list[HourlyPrice], block_size: int) -> list[TradingWindow]:
    """Build every length-`block_size` rolling window of consecutive hours."""
    blocks: list[TradingWindow] = []
    for i in range(len(hourly) - block_size + 1):
        chunk = hourly[i : i + block_size]
        if (chunk[-1].start - chunk[0].start) != timedelta(hours=block_size - 1):
            continue
        avg = sum(h.eur_per_kwh for h in chunk) / block_size
        blocks.append(
            TradingWindow(
                start=chunk[0].start,
                end=chunk[-1].start + timedelta(hours=1),
                avg_eur_per_kwh=avg,
            )
        )
    return blocks


def _build_cycle(
    charge: TradingWindow, discharge: TradingWindow, settings: Settings
) -> CyclePair:
    spread = discharge.avg_eur_per_kwh - charge.avg_eur_per_kwh
    usable_kwh = settings.battery_capacity_kwh * settings.round_trip_efficiency
    gross = spread * usable_kwh
    wear = settings.wear_cost_per_cycle_eur
    return CyclePair(
        charge=charge,
        discharge=discharge,
        gross_revenue_eur=gross,
        wear_cost_eur=wear,
        net_profit_eur=gross - wear,
    )


def _hours_of_cycle(cycle: CyclePair) -> set[datetime]:
    """Return every clock hour occupied by either the charge or discharge window."""
    hours: set[datetime] = set()
    for window in (cycle.charge, cycle.discharge):
        h = window.start
        while h < window.end:
            hours.add(h)
            h += timedelta(hours=1)
    return hours


def plan_day(
    hourly: list[HourlyPrice],
    target_date: date,
    settings: Settings | None = None,
    pv_forecast: PvForecast | None = None,
) -> DailyPlan:
    """Pick the best disjoint set of up to `max_cycles_per_day` cycles for the day.

    Each cycle is a (charge_window, discharge_window) pair with discharge
    starting at or after the charge ends; net profit (gross − wear) must clear
    `min_net_profit_per_cycle_eur`. Candidates are sorted by net profit
    descending and selected greedily — the highest-profit cycle that doesn't
    overlap (hour-wise) any already chosen cycle is added, up to the cap.

    If `pv_forecast` says expected PV meets or exceeds expected daily load
    closely enough that grid imports fall below one cycle's output, the day
    is skipped: the battery will fill from PV surplus for free and any
    grid-charge cycle would waste wear without arbitrage value.

    The Livoltek portal supports at most 6 schedule slots; with the
    Charge-only approach (Self-use handles discharge implicitly), one cycle
    maps to one Charge slot. `max_cycles_per_day` is bounded at 6 to match.
    """
    settings = settings or get_settings()

    if settings.max_cycles_per_day == 0:
        return DailyPlan(
            target_date=target_date,
            cycles=[],
            skipped_reason="max_cycles_per_day is 0",
            total_net_profit_eur=0.0,
        )

    if pv_forecast is not None:
        expected_grid_imports = max(
            0.0, settings.expected_daily_load_kwh - pv_forecast.expected_kwh
        )
        if expected_grid_imports < settings.cycle_output_kwh:
            return DailyPlan(
                target_date=target_date,
                cycles=[],
                skipped_reason=(
                    f"PV forecast {pv_forecast.expected_kwh:.1f} kWh leaves only "
                    f"{expected_grid_imports:.1f} kWh grid imports — below one "
                    f"cycle output ({settings.cycle_output_kwh:.1f} kWh)"
                ),
                total_net_profit_eur=0.0,
            )

    if len(hourly) < 2 * settings.hours_per_cycle:
        return DailyPlan(
            target_date=target_date,
            cycles=[],
            skipped_reason="not enough hourly data to form a cycle",
            total_net_profit_eur=0.0,
        )

    blocks = _build_blocks(hourly, settings.hours_per_cycle)
    threshold = settings.min_net_profit_per_cycle_eur

    candidates: list[CyclePair] = []
    for c in blocks:
        for d in blocks:
            if d.start < c.end:
                continue
            cycle = _build_cycle(c, d, settings)
            if cycle.net_profit_eur < threshold:
                continue
            candidates.append(cycle)

    if not candidates:
        return DailyPlan(
            target_date=target_date,
            cycles=[],
            skipped_reason=f"no cycle nets at least {threshold:.2f} EUR",
            total_net_profit_eur=0.0,
        )

    candidates.sort(
        key=lambda c: (-c.net_profit_eur, c.charge.start, c.discharge.start)
    )

    chosen: list[CyclePair] = []
    used_hours: set[datetime] = set()
    for cycle in candidates:
        if len(chosen) >= settings.max_cycles_per_day:
            break
        cycle_hours = _hours_of_cycle(cycle)
        if cycle_hours.isdisjoint(used_hours):
            chosen.append(cycle)
            used_hours.update(cycle_hours)

    chosen.sort(key=lambda c: c.charge.start)
    total_profit = sum(c.net_profit_eur for c in chosen)

    return DailyPlan(
        target_date=target_date,
        cycles=chosen,
        skipped_reason=None,
        total_net_profit_eur=total_profit,
    )
