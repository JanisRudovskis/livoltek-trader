"""Parsing and summarising the Livoltek portal's daily energy table.

Pure functions over the JSON the portal already serves to its own UI, kept free
of Playwright so it can be unit tested. `livoltek.py` fetches the rows and
hands them here; `notify.py` and `main.py` consume the results.

The portal endpoint is:

    POST /ctrller-manager/powerstation/reportForm
    {"id": <station id>, "timeType": 1, "startTime": ..., "endTime": ...}

Its first `data` row is a HEADER row (`updateDate == "Datetime"`) mapping each
key to its display label. Those labels are the only authority on what the keys
mean, and one of them is a trap — see `FIELD_LABELS`.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict

# Verbatim from the portal's own header row. Recorded here because reading
# `ETotal toGrid` as "export to grid" — which is what the name suggests —
# produced a badly wrong diagnosis on 2026-09-04: a day with 0.0 kWh exported
# looked like 10.1 kWh exported, which made a battery being steadily run down
# look like a battery doing fine.
FIELD_LABELS = {
    "ETotal toGrid": "PV Yield(kWh)",  # NOT export
    "SM_PositiveE": "Energy import from Grid(kWh)",
    "SM_NegativeE": "Export-to-grid(kWh）",  # the real export
    "ETotal Charge": "Battery Charged(kWh)",
    "ETotal Discharge": "Battery Discharged(kWh)",
    "Load": "Load Consumption(kWh)",
}


class DailyTotals(BaseModel):
    """One day of measured site energy, in kWh."""

    model_config = ConfigDict(frozen=True)

    day: date
    pv_yield_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    battery_charged_kwh: float
    battery_discharged_kwh: float
    load_kwh: float

    @property
    def balances(self) -> bool:
        """PV + import + discharge ≈ load + export + charge.

        A cheap sanity check that the column mapping is still right: if the
        portal renames or reorders fields, this stops holding.
        """
        into = self.pv_yield_kwh + self.grid_import_kwh + self.battery_discharged_kwh
        out_of = self.load_kwh + self.grid_export_kwh + self.battery_charged_kwh
        return abs(into - out_of) <= max(0.5, 0.05 * max(into, out_of))


def _num(value) -> float | None:
    """Portal cells are numbers, or '-' for days it has no data for."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def parse_daily_rows(rows) -> list[DailyTotals]:
    """Turn raw `reportForm` rows into totals, newest last.

    Drops the header row and any day whose figures are missing or dashed out
    (the portal lists future days in the requested month with '-').
    """
    out: list[DailyTotals] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        stamp = row.get("updateDate")
        if not isinstance(stamp, str) or stamp == "Datetime":
            continue
        try:
            day = date.fromisoformat(stamp[:10])
        except ValueError:
            continue
        values = {
            "pv_yield_kwh": _num(row.get("ETotal toGrid")),
            "grid_import_kwh": _num(row.get("SM_PositiveE")),
            "grid_export_kwh": _num(row.get("SM_NegativeE")),
            "battery_charged_kwh": _num(row.get("ETotal Charge")),
            "battery_discharged_kwh": _num(row.get("ETotal Discharge")),
            "load_kwh": _num(row.get("Load")),
        }
        if any(v is None for v in values.values()):
            continue
        out.append(DailyTotals(day=day, **values))
    out.sort(key=lambda t: t.day)
    return out


def trailing_mean_load_kwh(
    totals: list[DailyTotals], *, days: int = 7
) -> float | None:
    """Mean household load over the most recent `days` days with real data.

    This replaces a hard-coded `expected_daily_load_kwh` in the PV gate. The
    constant cannot be right in both seasons — measured load runs ~20 kWh in
    summer and 30–60 kWh in winter — and a stale value makes the gate skip
    cycles on exactly the cold days that pay best.

    Days reporting zero load are dropped: that means the inverter sent nothing,
    not that the house used nothing.
    """
    usable = [t.load_kwh for t in totals if t.load_kwh > 0]
    if not usable:
        return None
    window = usable[-days:] if days > 0 else usable
    return sum(window) / len(window)


def format_daily_totals_line(
    totals: DailyTotals,
    *,
    today: date,
    buy_price_eur_per_kwh: float | None = None,
) -> str:
    """One line of measured actuals for the nightly notification.

    Leads with grid import because that is the number that answers "is this
    working?". The figure the user can see in the app is battery SOC, and SOC
    is misleading on its own: 30 % against a 0.4 kW load is perfectly fine,
    while 30 % against a 2.5 kW heat pump is not. Imported kWh is unambiguous.

    The label is derived from the date rather than fixed, because the cron runs
    at 22:30 — the freshest near-complete day is the CURRENT one, not
    yesterday, and mislabelling it would undermine the point of the line.
    """
    delta = (today - totals.day).days
    label = {0: "Šodien", 1: "Vakar"}.get(delta, totals.day.isoformat())
    cost = ""
    if buy_price_eur_per_kwh is not None:
        cost = f" (€{totals.grid_import_kwh * buy_price_eur_per_kwh:.2f})"
    return (
        f"{label}: pirkts {totals.grid_import_kwh:.1f} kWh{cost}"
        f" · patēriņš {totals.load_kwh:.1f} · PV {totals.pv_yield_kwh:.1f}"
    )
