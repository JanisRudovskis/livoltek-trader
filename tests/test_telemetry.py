"""Tests for parsing and summarising the portal's daily energy table."""

from __future__ import annotations

from datetime import date

import pytest

from livoltek_trader.telemetry import (
    DailyTotals,
    format_daily_totals_line,
    parse_daily_rows,
    trailing_mean_load_kwh,
)

# Verbatim shape of `powerstation/reportForm` rows, including the header row the
# portal puts first and the column names it actually uses.
HEADER_ROW = {
    "updateDate": "Datetime",
    "ETotal toGrid": "PV Yield(kWh)",
    "SM_PositiveE": "Energy import from Grid(kWh)",
    "SM_NegativeE": "Export-to-grid(kWh）",
    "ETotal Charge": "Battery Charged(kWh)",
    "ETotal Discharge": "Battery Discharged(kWh)",
    "Load": "Load Consumption(kWh)",
}


def _row(day: str, pv, imp, exp, chg, dis, load):
    return {
        "updateDate": day,
        "ETotal toGrid": pv,
        "SM_PositiveE": imp,
        "SM_NegativeE": exp,
        "ETotal Charge": chg,
        "ETotal Discharge": dis,
        "Load": load,
    }


def test_etotal_to_grid_is_pv_yield_not_export():
    """`ETotal toGrid` is PV YIELD. This mapping cost a wrong diagnosis once.

    Reading it as export made a day with 0.0 kWh exported look like 10.1 kWh
    exported, which made a battery being run down look like a battery doing
    fine. The column header the portal ships is the authority.
    """
    assert HEADER_ROW["ETotal toGrid"] == "PV Yield(kWh)"
    assert HEADER_ROW["SM_NegativeE"] == "Export-to-grid(kWh）"

    rows = parse_daily_rows([HEADER_ROW, _row("2026-09-04", 10.1, 0.6, 0.0, 6.0, 9.3, 14.0)])
    assert len(rows) == 1
    t = rows[0]
    assert t.pv_yield_kwh == pytest.approx(10.1)
    assert t.grid_export_kwh == pytest.approx(0.0)
    assert t.grid_import_kwh == pytest.approx(0.6)


def test_parse_daily_rows_skips_header_and_dashes():
    rows = parse_daily_rows(
        [
            HEADER_ROW,
            _row("2026-09-03", 25.2, 0.1, 12.0, 7.2, 9.8, 22.8),
            # The portal renders a future/incomplete day with dashes.
            _row("2026-09-05", "-", "-", "-", "-", "-", 0),
        ]
    )
    assert [r.day for r in rows] == [date(2026, 9, 3)]


def test_energy_balance_holds_for_a_real_day():
    """in == out is the check that the column mapping is right at all."""
    t = parse_daily_rows(
        [_row("2026-09-04", 10.1, 0.6, 0.0, 6.0, 9.3, 14.0)]
    )[0]
    into = t.pv_yield_kwh + t.grid_import_kwh + t.battery_discharged_kwh
    out_of = t.load_kwh + t.grid_export_kwh + t.battery_charged_kwh
    assert into == pytest.approx(out_of, abs=0.15)


def test_trailing_mean_load_uses_recent_complete_days():
    rows = parse_daily_rows(
        [
            _row("2026-09-01", 12.6, 0.0, 0.0, 7.2, 9.8, 15.2),
            _row("2026-09-02", 28.1, 2.4, 10.6, 10.8, 7.9, 17.3),
            _row("2026-09-03", 25.2, 0.1, 12.0, 7.2, 9.8, 22.8),
            _row("2026-09-04", 10.1, 0.6, 0.0, 6.0, 9.3, 14.0),
        ]
    )
    assert trailing_mean_load_kwh(rows, days=3) == pytest.approx(
        (17.3 + 22.8 + 14.0) / 3
    )
    assert trailing_mean_load_kwh(rows, days=99) == pytest.approx(
        (15.2 + 17.3 + 22.8 + 14.0) / 4
    )


def test_trailing_mean_load_is_none_without_usable_days():
    assert trailing_mean_load_kwh([], days=7) is None


def test_trailing_mean_load_ignores_zero_load_days():
    """A zero-load row means the inverter reported nothing, not a quiet house."""
    rows = parse_daily_rows(
        [
            _row("2026-09-01", 0, 0, 0, 0, 0, 0.0),
            _row("2026-09-02", 28.1, 2.4, 10.6, 10.8, 7.9, 17.3),
        ]
    )
    assert trailing_mean_load_kwh(rows, days=7) == pytest.approx(17.3)


def test_format_daily_line_leads_with_grid_import():
    """Grid import is the number that answers "is this working?".

    SOC percentage is what the user can see in the app and it is misleading —
    a battery at 30% with a 0.4 kW load is fine. Imported kWh is not.
    """
    t = parse_daily_rows(
        [_row("2026-09-04", 10.1, 0.6, 0.0, 6.0, 9.3, 14.0)]
    )[0]
    line = format_daily_totals_line(
        t, today=date(2026, 9, 5), buy_price_eur_per_kwh=0.174
    )
    assert line.startswith("Vakar:")
    assert "0.6" in line
    assert "0.10" in line  # 0.6 kWh * 0.174 EUR
    assert "14.0" in line
    assert "10.1" in line


def test_format_daily_line_without_a_price():
    t = parse_daily_rows(
        [_row("2026-09-04", 10.1, 0.6, 0.0, 6.0, 9.3, 14.0)]
    )[0]
    line = format_daily_totals_line(t, today=date(2026, 9, 4))
    assert line.startswith("Šodien:"), (
        "the 22:30 cron sees the CURRENT day as the freshest one"
    )
    assert "0.6" in line
    assert "EUR" not in line and "€" not in line
