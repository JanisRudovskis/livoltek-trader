"""EV charging advice, and holding the house battery so the EV cannot drain it.

The household charges a Cupra Tavascan at ~11 kW, usually at night. Two
things follow from the inverter's Self-use behaviour:

1. The cheapest window for the car is worth stating every evening — the
   nightly run already has tomorrow's prices.
2. In Self-use the battery feeds ANY load, the car included, at up to 10 kW.
   If the car charges while the house battery is full, the battery is emptied
   into the car within the hour, at the cheap price the car was meant to pay,
   and is gone before the peak it was bought for. On 2026-11-06 a 16:05
   session took 6.5 kWh just before a 32-37 c evening.

A Charge slot keeps a full battery full while house and car run from the
grid, so extending the house's Charge slot across the car's window protects
it. That is only done when it pays: the hours the battery then serves must
be dearer than the car hours it would otherwise have been poured into.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from livoltek_trader.config import Settings
from livoltek_trader.strategy import (
    CyclePair,
    DailyPlan,
    HourlyPrice,
    TradingWindow,
    _build_blocks,
    _build_cycle,
    _derive_drain_window,
)

RIGA_TZ = ZoneInfo("Europe/Riga")

NIGHT_FROM_HOUR = 21
"""The night window starts the evening before the target day — the nightly
run lands around 21:00, so tonight's remaining hours are already usable."""
DAY_FROM_HOUR = 7
DAY_UNTIL_HOUR = 21
"""After 21:00 on the target day the next run gives fresher advice."""


class EvOption(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str  # "nakts" / "diena", shown to the user
    window: TradingWindow
    cost_100km_eur: float
    drains_battery: bool = False


class EvAdvice(BaseModel):
    model_config = ConfigDict(frozen=True)

    options: list[EvOption]
    best: int | None
    plan: DailyPlan  # the input plan, with a battery hold applied if it pays


def _at(d: date, hour: int) -> datetime:
    return datetime.combine(d, time(hour), tzinfo=RIGA_TZ)


def cheapest_block(
    hourly: list[HourlyPrice], hours: int, start: datetime, end: datetime
) -> TradingWindow | None:
    """Cheapest run of `hours` consecutive priced hours inside [start, end)."""
    # `_build_blocks` checks contiguity by first and last hour only, so it
    # needs sorted, distinct hours — callers concatenate two days' prices.
    by_start = {
        h.start: h
        for h in hourly
        if start <= h.start and h.start + timedelta(hours=1) <= end
    }
    blocks = _build_blocks([by_start[k] for k in sorted(by_start)], hours)
    if not blocks:
        return None
    return min(blocks, key=lambda b: (b.avg_eur_per_kwh, b.start))


def cost_per_100km_eur(spot_eur_per_kwh: float, settings: Settings) -> float:
    """What 100 km costs when charged at this spot price, losses included."""
    grid_kwh = settings.ev_consumption_kwh_per_100km / settings.ev_charging_efficiency
    return grid_kwh * (spot_eur_per_kwh + settings.buy_margin_eur_per_kwh)


def _overlaps(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _full_span(cycle: CyclePair) -> tuple[datetime, datetime]:
    """When the battery is full and in Self-use, i.e. exposed to the car."""
    return (cycle.hold_until or cycle.charge.end, cycle.discharge.end)


def _hold(
    plan: DailyPlan,
    ev: TradingWindow,
    price: dict[datetime, float],
    settings: Settings,
    displaceable_kwh: float | None,
) -> DailyPlan:
    cycles = list(plan.cycles)
    changed = False
    for i, c in enumerate(cycles):
        exposed_from = max(ev.start, c.charge.end)
        if not exposed_from < min(ev.end, c.discharge.end):
            continue  # the car does not charge while this battery is full
        held = TradingWindow(
            start=c.charge.start, end=ev.end, avg_eur_per_kwh=c.charge.avg_eur_per_kwh
        )
        drain = _derive_drain_window(price, held, settings.battery_drain_hours)
        if drain is None:
            continue  # cannot value the later drain, so cannot show it pays
        car_hours = []
        h = exposed_from
        while h < ev.end:
            car_hours.append(price[h])
            h += timedelta(hours=1)
        if drain.avg_eur_per_kwh <= sum(car_hours) / len(car_hours):
            continue
        new = _build_cycle(c.charge, drain, settings, displaceable_kwh).model_copy(
            update={"hold_until": ev.end}
        )
        span = (new.charge.start, new.discharge.end)
        others = [(o.charge.start, o.discharge.end) for j, o in enumerate(cycles) if j != i]
        if plan.stop_window is not None:
            others.append((plan.stop_window.start, plan.stop_window.end))
        if any(_overlaps(span, o) for o in others):
            continue
        cycles[i] = new
        changed = True
    if not changed:
        return plan
    return plan.model_copy(
        update={
            "cycles": cycles,
            "total_net_profit_eur": sum(c.net_profit_eur for c in cycles),
        }
    )


def advise_ev(
    plan: DailyPlan,
    hourly: list[HourlyPrice],
    settings: Settings,
    displaceable_kwh: float | None = None,
) -> EvAdvice:
    """Cheapest night and day car windows, and the plan with the battery
    protected across the recommended one: the house Charge slot held over it
    when that pays, and Stop over its remaining sunless hours.

    `hourly` should cover the evening before the target day as well as the
    day itself, so tonight's hours can be offered.
    """
    n = settings.ev_charge_hours
    if n <= 0:
        return EvAdvice(options=[], best=None, plan=plan)
    d = plan.target_date
    candidates = (
        ("nakts", cheapest_block(hourly, n, _at(d - timedelta(days=1), NIGHT_FROM_HOUR), _at(d, DAY_FROM_HOUR))),
        ("diena", cheapest_block(hourly, n, _at(d, DAY_FROM_HOUR), _at(d, DAY_UNTIL_HOUR))),
    )
    options = [
        EvOption(
            label=label,
            window=w,
            cost_100km_eur=cost_per_100km_eur(w.avg_eur_per_kwh, settings),
        )
        for label, w in candidates
        if w is not None
    ]
    if not options:
        return EvAdvice(options=[], best=None, plan=plan)
    best = min(range(len(options)), key=lambda i: options[i].window.avg_eur_per_kwh)

    price = {h.start: h.eur_per_kwh for h in hourly}
    plan = _hold(plan, options[best].window, price, settings, displaceable_kwh)
    if settings.ev_protect_battery:
        idle = _idle_windows(plan, options[best].window, settings)
        if idle:
            plan = plan.model_copy(update={"idle_windows": idle})
    options = [
        o.model_copy(update={"drains_battery": _exposed(plan, o.window)})
        for o in options
    ]
    return EvAdvice(options=options, best=best, plan=plan)


def _sunless(hour_start: datetime, settings: Settings) -> bool:
    h = hour_start.astimezone(RIGA_TZ).hour
    return (
        h < settings.stop_pv_window_start_hour_riga
        or h >= settings.stop_pv_window_end_hour_riga
    )


def _busy_spans(plan: DailyPlan) -> list[tuple[datetime, datetime]]:
    """Spans already owned by another slot; a Stop must not overlap them."""
    spans = [(c.charge.start, c.hold_until or c.charge.end) for c in plan.cycles]
    if plan.stop_window is not None:
        spans.append((plan.stop_window.start, plan.stop_window.end))
    return spans


def _idle_windows(
    plan: DailyPlan, ev: TradingWindow, settings: Settings
) -> list[TradingWindow]:
    """The recommended EV window's sunless hours that no other slot covers,
    merged into runs and split at midnight (a slot lives on one weekday)."""
    busy = _busy_spans(plan)
    hours = []
    h = ev.start
    while h < ev.end:
        span = (h, h + timedelta(hours=1))
        if _sunless(h, settings) and not any(_overlaps(span, b) for b in busy):
            hours.append(h)
        h += timedelta(hours=1)
    runs: list[list[datetime]] = []
    for h in hours:
        same_run = (
            runs
            and runs[-1][-1] + timedelta(hours=1) == h
            and h.astimezone(RIGA_TZ).hour != 0  # midnight starts a new slot
        )
        if same_run:
            runs[-1].append(h)
        else:
            runs.append([h])
    return [
        TradingWindow(
            start=r[0],
            end=r[-1] + timedelta(hours=1),
            avg_eur_per_kwh=ev.avg_eur_per_kwh,
        )
        for r in runs
    ]


def _exposed(plan: DailyPlan, window: TradingWindow) -> bool:
    """Would a car charging in `window` draw on a battery the plan filled?"""
    for c in plan.cycles:
        start, end = _full_span(c)
        h = max(start, window.start)
        while h < min(end, window.end):
            hour = (h, h + timedelta(hours=1))
            if not any(_overlaps(hour, (w.start, w.end)) for w in plan.idle_windows):
                return True
            h += timedelta(hours=1)
    return False
