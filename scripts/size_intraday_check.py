"""Size the value of a late-afternoon SOC check, from measured history.

The idea: run ~2 h before the evening peak, read the ACTUAL battery SOC, and
if it is low while the evening is expensive, write a Charge slot. It is
attractive because it replaces forecasts with a measurement — and every
planning failure so far has come from a forecast being wrong (the PV forecast
has been badly off twice, and daily load has ranged 6.4-43.9 kWh).

It was measured before being built, and in the seasons we had data for it
almost never fires:

    Apr-May + Jul-Sep 2026, 118 days measured
    days with SOC < 60 % at 17:00 AND > 1.5 kWh evening grid import:  6
    total addressable evening import:                              ~23 kWh
    value at a ~EUR 0.08/kWh spread:                     ~EUR 0.45 / month

On the other 112 days the battery sat at 94-100 % at 17:00 because PV had
already filled it, so the check would have had nothing to do. That does not
justify a second Playwright WRITE path, which is the component that has
produced the most production incidents.

Winter is expected to be different — the battery will not be full at 17:00 —
but no winter data exists (the inverter reports from 2026-04-01, so 2026/27 is
its first). Hence: re-run this in December and decide on numbers.

    uv run python scripts/livoltek_harvest_history.py 2026-11-01 2026-12-31
    uv run python scripts/size_intraday_check.py

Reads only `data/telemetry/five_min.jsonl`; no portal access.
"""

from __future__ import annotations

import collections
import json
import statistics as st
import sys
from pathlib import Path

IN = Path("data/telemetry/five_min.jsonl")
STEP_H = 5 / 60

CHECK_AT = "17:00"
"""Clock time the hypothetical check would run — about 2 h before the usual
evening peak (19:00-21:00 Riga)."""

LOW_SOC_PCT = 60.0
"""Below this at check time, the battery cannot carry a normal evening."""

MEANINGFUL_IMPORT_KWH = 1.5
"""Below this there is nothing worth a grid charge and a wear cycle."""


def _num(value):
    return value if isinstance(value, (int, float)) else None


def main() -> int:
    if not IN.exists():
        print(f"{IN} missing — run livoltek_harvest_history.py first", file=sys.stderr)
        return 1

    by_day: dict[str, dict[str, dict]] = collections.defaultdict(dict)
    for line in IN.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        stamp = str(row.get("updateDate", ""))
        if len(stamp) < 16:
            continue
        by_day[stamp[:10]][stamp[11:16]] = row

    results = []
    for day in sorted(by_day):
        samples = by_day[day]
        soc = _num((samples.get(CHECK_AT) or {}).get("Battery SOC"))
        if soc is None:
            continue
        # Grid draw = load + battery charging - PV. `exportpwr_real` reports
        # the same quantity, but deriving it keeps this independent of that
        # confusingly named field.
        evening_import = 0.0
        for hhmm, row in samples.items():
            if not 18 <= int(hhmm[:2]) <= 23:
                continue
            grid = (
                (_num(row.get("Load Power")) or 0.0)
                + (_num(row.get("Battery power")) or 0.0)
                - (_num(row.get("PV Power")) or 0.0)
            )
            if grid > 0:
                evening_import += grid * STEP_H
        results.append((day, soc, evening_import))

    if not results:
        print("no usable days in the harvested range")
        return 1

    print(f"day         SOC@{CHECK_AT}   evening grid import 18-24h (kWh)")
    for day, soc, imp in results:
        fires = soc < LOW_SOC_PCT and imp > MEANINGFUL_IMPORT_KWH
        print(f"{day}     {soc:5.0f}        {imp:6.2f}{'   <-- would fire' if fires else ''}")

    firing = [
        (d, s, i)
        for d, s, i in results
        if s < LOW_SOC_PCT and i > MEANINGFUL_IMPORT_KWH
    ]
    print(f"\n  days measured: {len(results)}")
    print(
        f"  would fire (SOC < {LOW_SOC_PCT:.0f}% and import > "
        f"{MEANINGFUL_IMPORT_KWH} kWh): {len(firing)}"
    )
    if firing:
        total = sum(i for _, _, i in firing)
        print(f"  addressable evening import: {total:.1f} kWh")
        print(f"  median on a firing day: {st.median([i for _, _, i in firing]):.2f} kWh")
        print(f"  rough value at EUR 0.08/kWh: EUR {total * 0.08:.2f} over "
              f"{len(results)} days = EUR {total * 0.08 / len(results) * 30:.2f}/month")
    print(
        "\n  Decision rule: build the check if it fires on a large share of "
        "winter days.\n  At ~EUR 0.45/month (the Apr-Sep figure) it does not pay "
        "for a second\n  Playwright write path."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
