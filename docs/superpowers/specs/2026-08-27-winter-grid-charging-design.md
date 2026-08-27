# Winter grid-charging: honest cycle valuation and drain-anchored charge placement

**Date:** 2026-08-27
**Status:** Approved for implementation
**Scope:** `strategy.py`, `config.py`, `tests/test_strategy.py`, plus a new local-only
telemetry command (`collect`) gated behind a portal spike.

---

## 1. Problem

Autumn and winter bring low PV yield, so the battery no longer fills itself from
the sun. The stated goal was to build a mechanism that charges the battery from
the grid ahead of expensive hours.

Investigation showed that mechanism **already exists and already activates in
winter** — but it is valued dishonestly, and the dishonesty makes it place its
charge windows in the wrong place.

### 1.1 The winter branch already turns on

`plan_day` skips grid-charge cycles only when expected grid imports fall below
one cycle's output:

```
expected_grid_imports = expected_daily_load_kwh - pv_forecast.expected_kwh
skip cycles if expected_grid_imports < cycle_output_kwh    # 22 - PV < 9.216
```

A November day forecasts 1–3 kWh of PV, so the gap is ~20 kWh and the cycle
planner runs. The sunny-day morning Discharge branch requires
`PV >= expected_daily_load_kwh * 1.5` = 33 kWh, which never happens between
November and February. The season switch is therefore already correct and
already automatic. No mode flag, no date window, and none will be added
(see §6).

### 1.2 Bug A — the discharge window is fiction

`_build_cycle` values a cycle as:

```
spread = discharge.avg_eur_per_kwh - charge.avg_eur_per_kwh
gross  = spread * cycle_output_kwh
```

The planner searches freely over `(charge_block, discharge_block)` pairs with the
only constraint being `discharge.start >= charge.end`. It will happily pair a
03:00–05:00 charge with an 18:00–20:00 "discharge" and book the full spread.

In reality the inverter is in Self-use once the Charge slot ends, so the battery
starts feeding the house **immediately** and keeps going until it is empty. The
measured drain time under a cold-winter heat-pump load is **2–5 hours**. Energy
bought at 03:00 is gone long before the 18:00 peak. The arbitrage actually
realised is against the mean price of the hours *directly following the charge*,
not against the dearest block of the day.

### 1.3 Bug B — phantom double night charges

Cycle-to-cycle exclusion is checked by shared clock hours only
(`_hours_of_cycle`). On a winter day where both the morning and the evening peak
clear the profit gate, the greedy selector picks:

- charge 00:00–02:00 → "discharge" 07:00–09:00
- charge 03:00–05:00 → "discharge" 18:00–20:00

The two are hour-disjoint, so both are accepted. But the second charge starts on
a battery the first charge just filled — it is a no-op slot, and the plan books
its profit twice.

`tests/test_strategy.py::test_plan_day_caps_at_max_cycles_per_day_six` (line 323)
encodes this behaviour as correct and must be rewritten.

### 1.4 Combined effect

Bugs A and B compound. On the worked example in §4.4, the current code claims
**~€2.80** for a day that is actually worth **€0.73** — roughly a 4×
overstatement. Every ntfy summary sent so far has reported the inflated number.

---

## 2. Measured facts this design rests on

| Fact | Value | Source |
|---|---|---|
| Battery runtime from full, cold winter day | 2–5 h | User observation |
| Winter daily household load | 30–60 kWh, temperature-dependent | User observation (Livoltek `Load Consumption`) |
| Implied continuous load | 1.25–2.5 kW | Derived |
| Implied drain window | 3.7–7.4 h | Derived |

The two observations do not perfectly agree: 30–60 kWh/day implies a 3.7–7.4 h
drain, while the runtime was reported as 2–5 h. The 2 h end would require ~4.6 kW
continuous, above the stated load range — most likely a recollection of the
coldest days, or of a battery that was not actually at 100 %. The chosen constant
of 4 h sits at the conservative end of the derived range and inside the observed
range, so both readings support it. §5.4 replaces the estimate with a
measurement.
| Charge rate | ~5 kW, 10→100 % in ~1.5 h | Hardware |
| Wear cost | €0.50 / full cycle | `battery_price_eur / battery_cycle_life` |
| Buy price | spot + €0.05/kWh | Tariff |
| Sell price | spot − €0.02/kWh | Tariff |

### 2.1 Why a single conservative constant beats a temperature model

The drain window varies 2× across the winter (3.7 h when it is −15 °C, 7.4 h at
+2 °C). Open-Meteo already returns temperature, so a load model is technically
available. It is deliberately **not** built, because erring short is safe and
erring long is not:

- **Assume 4 h, reality 7 h** → the charge sits immediately before the expensive
  block. The battery covers that block *and* some cheaper hours after it. The
  peak is captured; the bonus is simply not counted. The profit gate becomes
  stricter, so fewer but better cycles are chosen. Safe.
- **Assume 10 h, reality 4 h** → the charge is placed at the globally cheapest
  night hour and the battery is empty before the peak. This is the current bug.

A temperature model would add two coefficients (base load, heat-pump slope) to
refine a number whose conservative value already produces the right decision.
Rejected on YAGNI grounds and because multi-coefficient logic has been
explicitly rejected as a design direction for this project.

---

## 3. Economics

Notation: `C` = 10.24 kWh purchased per full charge (`battery_capacity_kwh`),
`U` = 9.216 kWh delivered to the house (`cycle_output_kwh`), `m` = €0.05/kWh buy
margin, `W` = €0.50 wear, `G` = €0.25 gate (`min_net_profit_per_cycle_eur`).

### 3.1 Self-consumption (the only leg being built)

We buy `C` kWh at `p_charge + m` and avoid buying `U` kWh at `p_drain + m`:

```
gross = U * p_drain + U * m - C * p_charge - C * m
      = U * p_drain - C * p_charge - m * (C - U)
net   = gross - W
```

`m * (C - U)` = 0.05 × 1.024 = **€0.0512** — the margin paid on round-trip
losses. The current code omits it, and also charges `U` rather than `C` for the
purchase; together those two omissions overstate each cycle by about €0.11.

Break-even spread at `p_charge` = €0.06: **€0.067/kWh**. Clearing the €0.25
gate: **€0.094/kWh**.

### 3.2 Export at peak (explicitly not built)

```
gross = U * (p_peak - 0.02) - C * (p_charge + m)
net   = gross - W
```

where `p_peak` is the spot price during the export window. At the same hour and
price `p`, exporting earns `p − 0.02` while avoided import
earns `p + 0.05`. **Self-consumption beats export by €0.07/kWh — €0.64 per
cycle** on any volume the house can absorb. In winter the house always absorbs
it. Break-even for export requires a peak **spot** price of ~€0.197/kWh and
gate-clearing ~€0.224/kWh, so on a typical winter morning peak of €0.20 it nets
~€0.03 and below €0.197 it loses money outright.

Decision: **no export leg in the winter branch.** The user's original sketch
item 4 ("export at the morning peak") is economically wrong for winter and is
dropped. The summer sunny-day Discharge slot already covers the case where
export does pay — when PV, not the grid, filled the battery for free.

### 3.3 Expected value

Volume is capped by household absorption, and each cycle's footprint is 6 hours
(§4.2), so at most 4 cycles/day are structurally possible and 1–2 typically
clear the gate. On the §4.4 reference day the honest total is €0.73/day, or
roughly €20/month across a Latvian winter. Modest, but real, and correctly
reported for the first time.

---

## 4. Design part 1 — the cycle planner

### 4.1 Configuration changes

Two additions to `Settings`:

```python
battery_drain_hours: int = Field(default=4, ge=1)
"""Hours a full battery feeds the house on a cold winter day.

Sets the window a Charge slot is valued against, and the spacing between
consecutive charges. Chosen conservatively short: erring short places the
charge immediately before the expensive block (safe), erring long places it
at the cheapest night hour and the battery is empty by the peak (the bug this
replaces). Measured range is 3.7-7.4 h depending on outside temperature; 4 is
the cold-day figure. This is the primary tuning knob for the winter branch.
"""

buy_margin_eur_per_kwh: float = Field(default=0.05, ge=0.0)
"""Supplier margin added to spot on every imported kWh.

A tariff fact, not a tuning knob. Needed so a cycle is charged the margin on
its round-trip losses (m * (capacity - output) = EUR 0.0512/cycle).
"""
```

Explicitly **unchanged**: `expected_daily_load_kwh` (22.0),
`min_net_profit_per_cycle_eur` (0.25), `sunny_day_pv_load_multiplier` (1.5),
`morning_peak_end_multiplier` (2.0), `hours_per_cycle` (2),
`max_cycles_per_day` (6), `stop_*` fields, `morning_discharge_target_soc_pct`.

`expected_daily_load_kwh` stays at 22.0 on purpose. It feeds the sunny gate,
where the summer figure is the correct one, and the PV-skip gate, where any
value from 22 to 60 yields the same winter answer (the gap always exceeds
9.216 kWh). Overloading it with a winter number would break summer.

### 4.2 Algorithm

The free `(charge, discharge)` pair search is replaced. The drain window is no
longer a choice — it is derived from physics.

```
charge_blocks = _build_blocks(hourly, settings.hours_per_cycle)   # unchanged, 2 h

for b in charge_blocks:
    drain = the settings.battery_drain_hours hourly prices starting at b.end,
            required to be contiguous clock hours with no gaps
    if fewer than battery_drain_hours contiguous hours are available:
        drop b                                  # see 4.3
    drain_avg = mean(drain)
    gross = cycle_output_kwh * drain_avg
          - battery_capacity_kwh * b.avg_eur_per_kwh
          - buy_margin_eur_per_kwh * (battery_capacity_kwh - cycle_output_kwh)
    net = gross - wear_cost_per_cycle_eur
    if net >= min_net_profit_per_cycle_eur:
        candidate with discharge = TradingWindow(
            start = b.end,
            end   = b.end + battery_drain_hours,
            avg   = drain_avg,
        )

sort candidates by (-net, charge.start)
greedy select while len(chosen) < cycle_cap:
    accept c if footprint(c) is disjoint from footprint of every chosen cycle
```

**Footprint** of a cycle is the half-open interval
`[charge.start, charge.end + battery_drain_hours)` — the charge window plus the
time the battery needs to empty again. Requiring footprints to be disjoint is
symmetric (order of selection does not matter) and is what kills bug B: a second
charge cannot begin while the first fill is still being consumed.

`_hours_of_cycle` becomes dead and is removed.

With `hours_per_cycle` = 2 and `battery_drain_hours` = 4, a footprint is 6 hours,
so at most 4 cycles fit in a day. `max_cycles_per_day` is retained as a
belt-and-braces cap, as is the reduction to 5 when a sunny-day Discharge slot may
also be written.

### 4.3 Why blocks near end of day are dropped rather than partially valued

`hourly` covers the target day only. A charge at 21:00–23:00 has one hour of
price data after it. Valuing the full 9.216 kWh output against that single hour
would overstate the cycle badly, since only ~2.6 kWh can physically be delivered
in an hour. Requiring the full `battery_drain_hours` of data is simple,
conservative, and costs nothing real: a late-evening charge draining into the
next day's cheap night hours is a poor trade anyway.

### 4.4 Reference day (becomes a golden test)

Prices: 00–05 €0.05 · 06 €0.12 · 07–09 €0.22 · 10–15 €0.13 · 16 €0.18 ·
17–20 €0.26 · 21–23 €0.10

| Charge | Drain window | Drain avg | Net | Footprint | Outcome |
|---|---|---|---|---|---|
| 05–07 | 07–11 | €0.1975 | €0.399 | [05, 11) | **chosen** |
| 03–05 | 05–09 | €0.1525 | €0.342 | [03, 09) | footprint overlaps [05, 11) |
| 14–16 | 16–20 | €0.2400 | €0.329 | [14, 20) | **chosen** |
| 12–14 | 14–18 | €0.1750 | −€0.270 | — | below €0.25 gate |

Greedy order is by net descending: accept 05–07, reject 03–05 on footprint
overlap, accept 14–16.

**Total €0.728.** Each charge sits immediately before the expensive block it
pays for.

Same day under the current code: cheapest 03–05 paired with dearest 17–19 gives
€1.44, plus a phantom second night charge of similar magnitude — approximately
**€2.80 claimed against €0.73 real**.

### 4.5 Blast radius

Confined to `strategy.py`, `config.py`, and `tests/test_strategy.py`.

`CyclePair.discharge` keeps its type and position; it now holds the *derived*
drain window instead of a *chosen* block. Because `livoltek.py`
(`_fill_charge_slot`) and `notify.py` (`format_plan_message`) read only
`cycle.charge`, **neither file changes**. The portal write path, slot ordering,
weekday picker, SOC targets, and ntfy formatting are untouched.

`_plan_stop_window` and the sunny-day gate are untouched. The two branches are
mutually exclusive by arithmetic: sunny needs PV ≥ 33 kWh, the cycle branch runs
when PV ≤ 12.8 kWh.

---

## 5. Design part 2 — telemetry

### 5.1 No second cron, no persisted plan log

A daily plan is deterministic given prices, PV forecast, and settings. All three
are recoverable after the fact:

- **Prices** — Elering API serves history.
- **PV forecast** — Open-Meteo archive API.
- **Plan** — recompute with `plan_day`.

So nothing needs to be written nightly. The only thing not reconstructible from
public APIs is **reality**: actual SOC, actual load, actual grid import. That
lives in the Livoltek portal.

Therefore telemetry is a **local, on-demand command**, not a Railway service.
No second cron, no Railway Volume, no database, no git-committing cron, and — most
importantly — no second Playwright *write* path. Production failure surface is
unchanged.

Caveat accepted: recomputed plans use *current* settings, not the settings in
force on the day. The question we actually want answered is "what would today's
algorithm have done", so this is the desired behaviour.

### 5.2 The command

```
livoltek-trader collect --days 30
```

Navigates to the portal's statistics/history view, reads hourly history, appends
JSONL to `data/telemetry.jsonl`. Read-only: it never touches System mode, never
clicks Save, and a failure exits non-zero without having contacted the schedule
form.

Target fields per hour:

| Field | Validates |
|---|---|
| `soc_pct` | `battery_drain_hours` — the one guessed knob |
| `load_kwh` | `expected_daily_load_kwh` and load shape |
| `grid_import_kwh` | Realised saving vs the recomputed plan |
| `pv_kwh` | `pv_kwh_per_mj_m2` calibration |
| `battery_charge_kwh` | Whether the Charge slot actually fired — the failure class of the 2026-05-12 silent-write incident |

### 5.3 Required spike before implementing 5.2

`LivoltekClient` currently reads nothing; it only logs in, navigates to System
mode, and writes. The portal's data surface is unknown. Before writing the
collector, run a spike:

1. Launch Playwright with `livoltek_headless=False` and walk to the
   statistics/history view.
2. **Intercept XHR via `page.on("response")` rather than scraping DOM charts.**
   The portal is a Vue SPA backed by a JSON API; those numbers are clean and far
   more stable than rendered graphs.
3. Answer three questions: what **resolution** is available (hourly or 5-minute),
   how far **back** history is retained, and whether **SOC appears in history at
   all** or only as a live value.

If SOC is not in history, or retention is only days, then — and only then — a
periodic collector is justified, and that decision is revisited with real
findings rather than assumptions.

### 5.4 First analysis once data exists

Measure `battery_drain_hours`. This needs SOC history only — no prices, no
plans. Take every episode where SOC fell from above 90 % to below 20 %, record
how many hours it took, and group by outside temperature. That converts the one
guessed constant into a measured one.

---

## 6. Explicitly not built

- **A winter/summer mode flag or date window.** The PV gates already partition
  the year, and they partition it better than a calendar: an October
  Dunkelflaute and a bright February week are both handled. A flag would be a
  second, disagreeing source of truth.
- **A peak-proximity detector.** Valuing a charge against the mean of its drain
  window makes the block before the peak win automatically. "Peak approaching"
  as a separate factor is redundant.
- **Morning export to grid** (original sketch item 4). Loses €0.64/cycle against
  self-consumption in winter; see §3.2.
- **"Switch the house to grid overnight."** In winter the battery is empty at
  night and Self-use already routes the house to grid. Only battery charging is
  schedulable, and that is the Charge slot.
- **A temperature-driven load model.** See §2.1.
- **A second cron that rewrites the schedule.** Doubles the flakiest component in
  the system to recover an estimated ~€2/month, and introduces multi-run state
  ("did the midday run already rewrite slot 3?").
- **Changing `expected_daily_load_kwh`.** See §4.1.

---

## 7. Testing

New and rewritten tests in `tests/test_strategy.py`:

1. **Golden reference day** (§4.4) — asserts the chosen charges are 05–07 and
   14–16 and the total is €0.728 (compared with `pytest.approx`; the figures in
   §4.4 are rounded to three decimals). Any valuation drift breaks this.
2. **Drain window derivation** — a charge block with fewer than
   `battery_drain_hours` of following price data is dropped (charge 22–24 yields
   no candidate).
3. **Footprint spacing** — replaces
   `test_plan_day_caps_at_max_cycles_per_day_six`, which encodes bug B. Asserts
   that two night charges 3 hours apart cannot both be chosen.
4. **Placement, not price** — on a day where the globally cheapest block is far
   from any expensive block, the chosen charge is the one adjacent to the
   expensive block, not the cheapest one.
5. **Margin accounting** — a cycle whose naive spread clears €0.25 but whose
   honest net does not is rejected.

Must continue to pass unchanged: the PV-skip gate tests, all
`_plan_stop_window` / sunny-day Discharge tests, `aggregate_hourly` tests, and
the `max_cycles_per_day == 0` short-circuit.

For the `collect` command: the spike is exploratory and not test-driven. The
parser that turns intercepted JSON into telemetry rows is pure and gets unit
tests against a captured fixture payload.

---

## 8. Sequencing

1. Part 1 — planner fix, config, tests. Independently shippable to Railway.
2. Spike — portal history surface (§5.3).
3. Part 2 — `collect` command, informed by the spike.
4. Measure `battery_drain_hours` and retune the one knob (§5.4).
