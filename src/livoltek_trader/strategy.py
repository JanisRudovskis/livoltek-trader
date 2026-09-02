"""Charge/discharge scheduling strategy — pure functions over hourly prices.

Self-consumption arbitrage: charge the battery from the grid during cheap
spot hours and use the stored energy in the household during expensive hours,
avoiding a buy at the higher tariff.

Two things about the accounting are easy to get wrong, and both were wrong
here until 2026-08-27:

1. **The discharge window is not a choice.** Once a Charge slot ends the
   inverter returns to Self-use, so the battery covers household load
   immediately and keeps going until empty (~7 h in winter). Value accrues
   over the hours *directly following* the charge, not at the dearest block of
   the day. Pairing a night charge with an evening peak is physically
   impossible and used to inflate every plan.
2. **The supplier margin does not fully cancel.** It cancels between the buy
   and the avoided-buy legs, but we buy `battery_capacity_kwh` and only
   deliver `cycle_output_kwh`, so the margin on round-trip losses is a real
   cost (~€0.14/cycle at measured constants).

Hardware constants in `config.py` are measured from portal telemetry, not
nameplate figures. See
`docs/superpowers/specs/2026-08-27-winter-grid-charging-design.md`.
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
    """One grid charge window plus the drain window it is valued against.

    Only `charge` is ever written to the inverter — it becomes a single
    `Charge` slot. `discharge` is DERIVED, not scheduled: it is the run of
    hours after the charge during which Self-use feeds the battery's energy
    into household load. It exists for accounting and for the ntfy summary.

    `gross_revenue_eur` is operating profit before wear — the formula nets out
    the purchase cost, so despite the name it is not a revenue figure. Kept
    for field compatibility; see `_build_cycle`.
    """

    model_config = ConfigDict(frozen=True)

    charge: TradingWindow
    discharge: TradingWindow
    gross_revenue_eur: float
    wear_cost_eur: float
    net_profit_eur: float


class DailyPlan(BaseModel):
    """The chosen plan for a target day, possibly empty if not worthwhile.

    `stop_window` is an optional morning Discharge window: the battery is
    drained to grid down to `morning_discharge_target_soc_pct` during the
    morning peak, capturing the spot premium. Active only on sunny days
    where expected PV ≥ load × `sunny_day_pv_load_multiplier` — below that
    margin, we don't risk draining a battery we can't reliably refill from
    PV. The battery refills from afternoon PV via Self-use and serves load
    via Self-use in the evening peak.
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


def _derive_drain_window(
    price_by_hour: dict[datetime, float],
    charge: TradingWindow,
    drain_hours: int,
) -> TradingWindow | None:
    """The hours the battery actually feeds the house after a charge ends.

    This is NOT a choice. The moment a Charge slot ends the inverter returns
    to Self-use, so the battery starts covering household load immediately and
    keeps going until it is empty. The value of a charge is therefore realised
    against the mean price of the hours directly following it — never against
    the dearest block of the day.

    Returns None unless `drain_hours` of *contiguous* price data exist after
    `charge.end`. Valuing the full cycle output against a partial window would
    overstate the cycle badly, since the battery cannot deliver 9 kWh into one
    or two hours.
    """
    prices: list[float] = []
    h = charge.end
    for _ in range(drain_hours):
        price = price_by_hour.get(h)
        if price is None:
            return None
        prices.append(price)
        h += timedelta(hours=1)
    return TradingWindow(
        start=charge.end,
        end=h,
        avg_eur_per_kwh=sum(prices) / len(prices),
    )


def _build_cycle(
    charge: TradingWindow, drain: TradingWindow, settings: Settings
) -> CyclePair:
    """Value one charge window against its derived drain window.

    We buy `battery_capacity_kwh` from the grid at the charge window's price
    and avoid buying `cycle_output_kwh` at the drain window's price. The
    supplier margin cancels between the two legs *except* on round-trip
    losses, which is the `margin * (bought - delivered)` term — about €0.14
    per cycle at measured constants, and omitted by the previous formula.
    """
    bought = settings.battery_capacity_kwh
    delivered = settings.cycle_output_kwh
    gross = (
        delivered * drain.avg_eur_per_kwh
        - bought * charge.avg_eur_per_kwh
        - settings.buy_margin_eur_per_kwh * (bought - delivered)
    )
    wear = settings.wear_cost_per_cycle_eur
    return CyclePair(
        charge=charge,
        discharge=drain,
        gross_revenue_eur=gross,
        wear_cost_eur=wear,
        net_profit_eur=gross - wear,
    )


def _footprint(cycle: CyclePair) -> tuple[datetime, datetime]:
    """Charge window plus the time the battery needs to empty again.

    Two cycles may coexist only if their footprints are disjoint: a second
    charge must not begin while the previous fill is still being consumed —
    otherwise the plan books two full cycles of profit for one battery's worth
    of energy. Half-open, so footprints touching exactly at the boundary are
    allowed: the battery empties as the next charge begins.
    """
    return (cycle.charge.start, cycle.discharge.end)


def _hours_in_span(span: tuple[datetime, datetime]) -> set[datetime]:
    hours: set[datetime] = set()
    h = span[0]
    while h < span[1]:
        hours.add(h)
        h += timedelta(hours=1)
    return hours


def _plan_stop_window(
    hourly: list[HourlyPrice],
    settings: Settings,
    used_hours: set[datetime],
) -> TradingWindow | None:
    """Pick the morning Discharge window for sunny days.

    Rule: within the PV-producing daylight window, find the cheapest hour.
    Compute a peak-end threshold = `cheapest_price × morning_peak_end_multiplier`.
    Walk forward from the first daylight hour and collect the FIRST contiguous
    run of hours whose spot is above this threshold. Stop at the first hour
    that falls back below — we don't want to keep selling once prices are
    only slightly above cheapest.

    Returns None if:
    - daylight window has fewer than 2 hours of price data
    - cheapest daylight hour is at the start of the window (no morning peak)
    - no hour above the peak-end threshold exists before the cheap hour
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
        return None  # cheap hour is first daylight hour — no morning peak

    peak_end_threshold = cheapest_price * settings.morning_peak_end_multiplier
    sell_threshold = settings.stop_sell_threshold_eur_per_kwh

    # Walk forward, collect the FIRST contiguous run above peak_end_threshold.
    # Once we leave the run (hour drops below threshold), stop — we won't pick
    # later peaks. A morning peak is what we want; trailing near-cheap hours
    # would only erode the avg sell price.
    run: list[HourlyPrice] = []
    for i in range(cheapest_idx):
        h = pv_hours[i]
        qualifies = (
            h.eur_per_kwh > peak_end_threshold
            and h.eur_per_kwh > sell_threshold
            and h.start not in used_hours
        )
        if qualifies:
            run.append(h)
        elif run:
            break  # peak ended — stop the run

    if not run:
        return None

    avg = sum(h.eur_per_kwh for h in run) / len(run)
    return TradingWindow(
        start=run[0].start,
        end=run[-1].start + timedelta(hours=1),
        avg_eur_per_kwh=avg,
    )


def plan_day(
    hourly: list[HourlyPrice],
    target_date: date,
    settings: Settings | None = None,
    pv_forecast: PvForecast | None = None,
    pv_forecast_failed: bool = False,
) -> DailyPlan:
    """Pick the best footprint-disjoint set of grid-charge cycles for the day.

    Every rolling `hours_per_cycle` block is a charge candidate. Its drain
    window is derived, not chosen (see `_derive_drain_window`), and the block
    is dropped unless a full `battery_drain_hours` of contiguous prices follow
    it. Net profit must clear `min_net_profit_per_cycle_eur`.

    Candidates are sorted by net profit descending and selected greedily: a
    cycle is added if its footprint — charge window plus drain time — does not
    overlap any already chosen footprint. That is what prevents a second
    charge from starting on a battery the first one just filled.

    Greedy is a choice, not a requirement: this is weighted interval
    scheduling, so one high-net cycle can block two mid-net ones. With a
    ~9 hour footprint at most two cycles fit a day, so the conflict is rare
    and an exact DP is not worth the code.

    If `pv_forecast` says expected PV meets or exceeds expected daily load
    closely enough that grid imports fall below one cycle's output, cycles are
    skipped: the battery will fill from PV surplus for free and any grid-charge
    cycle would waste wear without arbitrage value. NOTE: this gate uses
    `expected_daily_load_kwh`, a spring figure, so on shoulder-season days with
    PV between (load − cycle_output) and load × 1.5 neither this branch nor the
    sunny branch runs. Known gap, documented in the spec.

    The Livoltek portal supports at most 6 schedule slots; one cycle maps to
    one Charge slot, since Self-use handles the discharge implicitly.
    `max_cycles_per_day` is bounded at 6 to match, though the footprint rule
    binds well before the cap does.
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

    # `pv_forecast_failed` is NOT the same as `pv_forecast is None`. None means
    # "no PV constraint requested" (manual runs, tests) and still plans. This
    # flag means we asked and could not find out, and then the safe answer is
    # to do nothing: buying a full charge that the sun would have supplied for
    # free costs ~EUR 1.08, while skipping costs at most one winter cycle's
    # profit (~EUR 0.43, on 29% of winter days). Roughly 9x asymmetric, so we
    # fall back to plain Self-use. See the 2026-09-02 run, which planned blind
    # through an Open-Meteo timeout on a day that yielded 28 kWh of PV.
    if pv_forecast_failed:
        return DailyPlan(
            target_date=target_date,
            cycles=[],
            skipped_reason=(
                "PV forecast unavailable — skipping rather than buying grid "
                "energy the sun might have supplied"
            ),
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
        # A cycle needs its charge block AND a full drain window inside the
        # day's price data.
        min_hours = settings.hours_per_cycle + settings.battery_drain_hours
        if len(hourly) < min_hours:
            cycle_skip_reason = "not enough hourly data to form a cycle"
        else:
            price_by_hour = {h.start: h.eur_per_kwh for h in hourly}
            blocks = _build_blocks(hourly, settings.hours_per_cycle)
            threshold = settings.min_net_profit_per_cycle_eur

            candidates: list[CyclePair] = []
            for charge in blocks:
                drain = _derive_drain_window(
                    price_by_hour, charge, settings.battery_drain_hours
                )
                if drain is None:
                    continue  # no full drain window — see _derive_drain_window
                cycle = _build_cycle(charge, drain, settings)
                if cycle.net_profit_eur < threshold:
                    continue
                candidates.append(cycle)

            if not candidates:
                cycle_skip_reason = (
                    f"no cycle nets at least {threshold:.2f} EUR"
                )
            else:
                candidates.sort(
                    key=lambda c: (-c.net_profit_eur, c.charge.start)
                )

                # Cap chosen cycles at 5 if a sunny-day Discharge slot might be
                # added, so the 6-slot portal budget always has room. Dead by
                # arithmetic — the cycle branch needs PV below
                # (load - cycle_output) while the sunny branch needs PV above
                # load × 1.5, so they can never co-fire — but kept as
                # belt-and-braces in case either gate is retuned.
                pv_sunny = (
                    pv_forecast is not None
                    and pv_forecast.expected_kwh
                    >= settings.expected_daily_load_kwh
                    * settings.sunny_day_pv_load_multiplier
                )
                cycle_cap = settings.max_cycles_per_day
                if pv_sunny:
                    cycle_cap = min(cycle_cap, 5)

                spans: list[tuple[datetime, datetime]] = []
                for cycle in candidates:
                    if len(chosen) >= cycle_cap:
                        break
                    span = _footprint(cycle)
                    if all(
                        span[0] >= s[1] or span[1] <= s[0] for s in spans
                    ):
                        chosen.append(cycle)
                        spans.append(span)

                chosen.sort(key=lambda c: c.charge.start)
                # The Stop planner must not place a Discharge-to-grid slot
                # inside a window where the battery is charging or feeding the
                # house.
                for span in spans:
                    used_hours.update(_hours_in_span(span))

    # Morning Discharge window: only on sunny days where PV clearly exceeds
    # load by the configured safety margin. Below this gate we trust Self-use
    # to manage the battery without explicitly draining it in the morning.
    stop_window: TradingWindow | None = None
    if (
        pv_forecast is not None
        and pv_forecast.expected_kwh
        >= settings.expected_daily_load_kwh
        * settings.sunny_day_pv_load_multiplier
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
