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
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from livoltek_trader.config import Settings, get_settings
from livoltek_trader.elering import PricePeriod
from livoltek_trader.solar import PvForecast

RIGA_TZ = ZoneInfo("Europe/Riga")


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
    """The chosen plan for a target day, possibly empty if not worthwhile.

    `stop_window` is an optional morning window where we explicitly block the
    battery from charging so the PV production is exported to grid at high
    spot prices instead. Active only on PV-abundant days where the morning
    spot is meaningfully higher than the cheap midday refill window. The
    battery refills naturally from later PV (or via the planned Charge
    cycles) and serves load via Self-use in the evening.
    """

    model_config = ConfigDict(frozen=True)

    target_date: date
    cycles: list[CyclePair]
    skipped_reason: str | None
    total_net_profit_eur: float
    stop_window: TradingWindow | None = None

    @property
    def is_empty(self) -> bool:
        """True when neither a cycle nor a Stop window is planned — ToU off."""
        return not self.cycles and self.stop_window is None


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


def _plan_stop_window(
    hourly: list[HourlyPrice],
    settings: Settings,
    used_hours: set[datetime],
) -> TradingWindow | None:
    """Pick a continuous morning Stop window for PV-abundant days.

    Rule: within the PV-producing daylight window, find the cheapest hour.
    Consider only hours BEFORE that cheap hour as Stop candidates. A
    candidate qualifies when spot is BOTH above the sell threshold AND
    above the cheapest hour's spot. Return the LONGEST contiguous run of
    qualifying candidates — below-threshold hours break the run and reset
    the counter, so a real morning peak can be picked even if a few weird
    near-zero quarter-hours sit between it and midday.

    Returns None if:
    - daylight window has fewer than 2 hours of price data
    - cheapest daylight hour is at the start of the window (no morning peak)
    - no qualifying hours exist before the cheap hour
    """
    if len(hourly) < 2:
        return None

    pv_start = settings.stop_pv_window_start_hour_riga
    pv_end = settings.stop_pv_window_end_hour_riga
    pv_hours = [
        h
        for h in hourly
        if pv_start <= h.start.astimezone(RIGA_TZ).hour < pv_end
    ]
    if len(pv_hours) < 2:
        return None

    cheapest_idx = min(
        range(len(pv_hours)), key=lambda i: pv_hours[i].eur_per_kwh
    )
    cheapest_price = pv_hours[cheapest_idx].eur_per_kwh

    if cheapest_idx == 0:
        return None  # cheap hour is already first daylight hour — no morning peak

    sell_threshold = settings.stop_sell_threshold_eur_per_kwh

    # Walk FORWARD through hours before the cheapest. Collect contiguous
    # qualifying runs; on disqualification, reset the current run. Keep
    # the longest run we've seen.
    best_run: list[HourlyPrice] = []
    current_run: list[HourlyPrice] = []
    for i in range(cheapest_idx):
        h = pv_hours[i]
        qualifies = (
            h.eur_per_kwh > sell_threshold
            and h.eur_per_kwh > cheapest_price
            and h.start not in used_hours
        )
        if qualifies:
            current_run.append(h)
            if len(current_run) > len(best_run):
                best_run = list(current_run)
        else:
            current_run = []

    if not best_run:
        return None

    avg = sum(h.eur_per_kwh for h in best_run) / len(best_run)
    return TradingWindow(
        start=best_run[0].start,
        end=best_run[-1].start + timedelta(hours=1),
        avg_eur_per_kwh=avg,
    )


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
            stop_window=None,
        )

    # Determine whether to plan cycles. On PV-abundant days where expected
    # grid imports fall below one cycle output, grid-charge cycles add no
    # value — but a Stop slot might still help by exporting morning PV at
    # the high spot rather than self-storing it. So we DON'T early-return
    # here anymore; we fall through to the Stop-window planner with cycles=[]
    # and let the daily-plan caller decide.
    cycle_skip_reason: str | None = None
    chosen: list[CyclePair] = []
    used_hours: set[datetime] = set()

    if pv_forecast is not None:
        expected_grid_imports = max(
            0.0, settings.expected_daily_load_kwh - pv_forecast.expected_kwh
        )
        if expected_grid_imports < settings.cycle_output_kwh:
            cycle_skip_reason = (
                f"PV forecast {pv_forecast.expected_kwh:.1f} kWh leaves only "
                f"{expected_grid_imports:.1f} kWh grid imports — below one "
                f"cycle output ({settings.cycle_output_kwh:.1f} kWh)"
            )

    if cycle_skip_reason is None:
        if len(hourly) < 2 * settings.hours_per_cycle:
            cycle_skip_reason = "not enough hourly data to form a cycle"
        else:
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
                cycle_skip_reason = (
                    f"no cycle nets at least {threshold:.2f} EUR"
                )
            else:
                candidates.sort(
                    key=lambda c: (
                        -c.net_profit_eur,
                        c.charge.start,
                        c.discharge.start,
                    )
                )

                # Cap chosen cycles at 5 if we might add a Stop slot, so the
                # 6-slot portal budget always has room. Cheap to do — drops
                # at most one marginal cycle on PV-abundant days.
                pv_abundant = (
                    pv_forecast is not None
                    and pv_forecast.expected_kwh
                    >= settings.expected_daily_load_kwh
                )
                cycle_cap = settings.max_cycles_per_day
                if pv_abundant:
                    cycle_cap = min(cycle_cap, 5)

                for cycle in candidates:
                    if len(chosen) >= cycle_cap:
                        break
                    cycle_hours = _hours_of_cycle(cycle)
                    if cycle_hours.isdisjoint(used_hours):
                        chosen.append(cycle)
                        used_hours.update(cycle_hours)

                chosen.sort(key=lambda c: c.charge.start)

    # Stop-window planning: only on PV-abundant days.
    stop_window: TradingWindow | None = None
    if (
        pv_forecast is not None
        and pv_forecast.expected_kwh >= settings.expected_daily_load_kwh
    ):
        stop_window = _plan_stop_window(hourly, settings, used_hours)

    total_profit = sum(c.net_profit_eur for c in chosen)

    if not chosen and stop_window is None:
        return DailyPlan(
            target_date=target_date,
            cycles=[],
            skipped_reason=cycle_skip_reason,
            total_net_profit_eur=0.0,
            stop_window=None,
        )

    return DailyPlan(
        target_date=target_date,
        cycles=chosen,
        skipped_reason=None,
        total_net_profit_eur=total_profit,
        stop_window=stop_window,
    )
