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


def plan_day(
    hourly: list[HourlyPrice],
    target_date: date,
    settings: Settings | None = None,
    pv_forecast: PvForecast | None = None,
) -> DailyPlan:
    """Find the most profitable set of up to N cycles for the day.

    Brute-forces the (charge, discharge) block pairs and picks the
    combination with the highest total net profit, subject to:
    - charge starts before discharge within a cycle
    - at most `max_cycles_per_day` cycles, in time order, non-overlapping
    - each cycle nets at least `min_net_profit_per_cycle_eur`

    If `pv_forecast` says expected PV meets or exceeds expected daily load,
    the day is skipped: the battery will be filled from PV surplus for free,
    so any grid-charge cycle would waste battery wear without arbitrage value.
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
    best_plan: list[CyclePair] = []
    best_profit = float("-inf")

    for c1 in blocks:
        for d1 in blocks:
            if d1.start < c1.end:
                continue
            cycle1 = _build_cycle(c1, d1, settings)
            if cycle1.net_profit_eur < threshold:
                continue

            if cycle1.net_profit_eur > best_profit:
                best_profit = cycle1.net_profit_eur
                best_plan = [cycle1]

            if settings.max_cycles_per_day < 2:
                continue

            for c2 in blocks:
                if c2.start < d1.end:
                    continue
                for d2 in blocks:
                    if d2.start < c2.end:
                        continue
                    cycle2 = _build_cycle(c2, d2, settings)
                    if cycle2.net_profit_eur < threshold:
                        continue
                    total = cycle1.net_profit_eur + cycle2.net_profit_eur
                    if total > best_profit:
                        best_profit = total
                        best_plan = [cycle1, cycle2]

    if not best_plan:
        return DailyPlan(
            target_date=target_date,
            cycles=[],
            skipped_reason=f"no cycle nets at least {threshold:.2f} EUR",
            total_net_profit_eur=0.0,
        )

    return DailyPlan(
        target_date=target_date,
        cycles=best_plan,
        skipped_reason=None,
        total_net_profit_eur=best_profit,
    )
