# Winter grid-charging: honest cycle valuation and drain-anchored charge placement

**Date:** 2026-08-27
**Status:** Part 1 approved for implementation; business case downgraded — see §3.3
**Revision:** rev3 — rebuilt on measured telemetry. See §9 for the revision trail.
**Scope:** `strategy.py`, `config.py`, `tests/test_strategy.py`. Telemetry turned
out to need no new production code at all (§5).

---

## 1. Problem

Autumn and winter bring low PV yield, so the battery no longer fills itself from
the sun. The goal was a mechanism to charge the battery from the grid ahead of
expensive hours.

That mechanism **already exists and already activates in winter**. It is valued
dishonestly, and the dishonesty makes it place charge windows in the wrong hours.

### 1.1 The winter branch already turns on

`plan_day` skips grid-charge cycles only when expected grid imports fall below
one cycle's output (`22 − PV < cycle_output`). A November day forecasts 1–3 kWh
of PV, so the gap is ~20 kWh and the cycle planner runs. The sunny-day morning
Discharge branch needs `PV ≥ 33 kWh`, which never happens November–February. The
season switch is automatic; no mode flag will be added. It is not fully correct
though — see the shoulder-season dead zone in §4.1.

### 1.2 Bug A — the discharge window is fiction

`_build_cycle` values a cycle as `(discharge.avg − charge.avg) × cycle_output`,
and the planner searches freely over `(charge_block, discharge_block)` pairs with
only `discharge.start >= charge.end` as a constraint. It will pair a 00:00–02:00
charge with a 17:00–19:00 "discharge" and book the full spread.

In reality the inverter returns to Self-use the moment the Charge slot ends, so
the battery begins feeding the house immediately and continues until empty.
Energy bought at 00:00 is gone long before the 17:00 peak. Value is realised
against the mean price of the hours *directly following the charge*.

### 1.3 Bug B — phantom extra night charges

Cycle exclusion is checked by shared clock hours only (`_hours_of_cycle`). On the
§4.4 reference day the current greedy picks **three** cycles — charge 00–02,
02–04 and 04–06, each paired with a different evening or morning block — and
books **€3.74**. The second and third charges begin on a battery the previous
charge just filled. The sin is not physical no-op-ness (with a multi-hour drain
they do partially charge); it is booking three full cycles of profit for one
battery's worth of energy.

`tests/test_strategy.py::test_plan_day_caps_at_max_cycles_per_day_six` (line 323)
encodes this as correct and must be rewritten.

### 1.4 Combined effect

On the reference day the current code books **€3.74** for a day honestly worth
**€0.27** — a **14× overstatement** once measured hardware constants are used.

The inflated figure reaches only the `main.plan_ready` structlog line in Railway
logs. `format_plan_message` renders PV and slot times and no EUR figure, so no
ntfy notification has ever shown a wrong number.

---

## 2. Measured facts

148 days of 5-minute inverter telemetry (2026-04-01 … 2026-08-26, 42 476 usable
samples) and 149 days of daily aggregates were harvested from the portal and
analysed offline. Scripts: `scripts/livoltek_harvest_history.py`,
`scripts/analyze_telemetry.py`.

| Quantity | Config assumes | **Measured** | Basis |
|---|---|---|---|
| kWh delivered to house, SOC 100→10 | 9.216 | **8.88** | 147 PV-free discharge episodes; median 0.0987 kWh/SOC-pt (p10 0.0901, p90 0.1178) |
| kWh bought from grid, SOC 10→100 | 10.24 | **11.63** | **1 clean grid-charge episode** — see §2.3 |
| Round-trip efficiency (grid→house) | 0.900 | **≈0.76** | Derived from the two above |
| kWh into battery per SOC-pt, PV charge | — | 0.1195 | 162 episodes — DC-side, not comparable to grid charge |
| Daily household load, Apr–Aug | 22.0 | median 20.5, mean 21.1, σ 4.2, range 7.0–31.6 | 149 days |
| Grid import, Apr–Aug | — | median 1.8 kWh/day | 149 days (summer; PV covers nearly all) |
| Winter daily load | — | **unknown** | No winter data exists — see §2.2 |

`Battery power` is reported AC-side: the identity
`exportpwr_real = Load Power + Battery power` holds exactly during grid charging
(0.058 + 0.749 = 0.807; 8.852 + 1.096 = 9.948). So `exportpwr_real`, despite its
name, is grid draw, and the measured charge energy needs no further loss
adjustment.

### 2.1 The drain constant must be the LONG end of its range

An earlier revision argued the opposite and was wrong. The asymmetry:

- **Too short** → all `cycle_output` is attributed to the first few expensive
  hours when part of it is really delivered later at cheap prices. The cycle is
  **over-valued** and losing cycles get booked as profitable. No gate protects
  against this: the figure is inflated before the gate sees it.
- **Too long** → the window is diluted by hours in which the battery is already
  empty. The cycle is **under-valued**; profit is forgone but not lost.

Counter-example, a peak-then-crash day (calm morning, windy midday — a shape the
Baltic market produces regularly): 00–03 €0.13, 04–05 €0.09, 06–08 €0.30,
09–13 €0.02, 14–23 €0.12. Charge 04–06:

| Assumption | Drain mean | Net |
|---|---|---|
| 4 h | €0.230 | **+€0.358** — accepted |
| 7 h (truth) | €0.140 | **−€0.441** — correctly rejected |

Measured drain hours, from the 8.88 kWh delivery figure:

| Daily load | Continuous | Drain hours |
|---|---|---|
| 24 kWh | 1.00 kW | 8.9 h |
| 30 kWh | 1.25 kW | 7.1 h |
| 36 kWh | 1.50 kW | 5.9 h |
| 48 kWh | 2.00 kW | 4.4 h |
| 60 kWh | 2.50 kW | 3.6 h |

The user's stated winter range of 30–60 kWh/day therefore maps to **3.6–7.1 h**,
and their reported "2–5 h" runtime is consistent with the cold end plus a battery
not starting at 100 %.

**`battery_drain_hours = 7`** — the warm-winter end, i.e. the safe end.

The cost is real and concentrated where it hurts: on cold days true drain is
~3.6 h, so a 7 h assumption dilutes the peak with 3.4 empty hours and rejects
exactly the best cycles of the coldest, highest-spread days. On the reference day
the constant swings the result between €0.51 (at 4 h) and €0.27 (at 7 h) — a 1.9×
lever, the largest single uncertainty in the model. §5.4 replaces it with a
measurement in November.

A robust min-over-range valuation (require the gate to clear at both 4 h and 7 h)
was considered and dropped: on every case tested it selects identically to using
7 h alone.

### 2.2 Why winter cannot be calibrated yet

Portal history is available for any requested date, but **inverter data begins
2026-04-01** — 2026-03-20 and earlier return 288 empty rows. The system has never
seen a winter. This will be its first.

So neither winter load nor winter drain time can be measured now, at any effort.
Both must wait for real cold weather.

### 2.3 Why the purchase-side figure is weak

Only **3** grid-charge episodes exist in 148 days, because the summer PV gate
suppresses grid charging almost entirely. Two are contaminated: one ends at 98 %
SOC inside the taper region, the other is a 0.25 kW trickle over 7.2 h where
standby overhead dominates. The single clean episode (2026-08-22, SOC 25→84 at
7.6 kW, avoiding both the bottom and the taper) gives 0.1292 kWh/SOC-pt →
**11.63 kWh** for a full 10→100 charge and a 0.76 round-trip.

An alternative estimate via the PV-charge figure (0.1195 kWh/pt DC-side, implying
10.74 kWh and a 0.83 round-trip) is more optimistic but compares DC-side charging
to AC-side, so it is not like-for-like.

11.63 is used because erring high on cost is the safe direction. The uncertainty
band 0.76–0.83 swings the reference day between €0.27 and €0.35 — about 30 %.

**This resolves itself automatically.** From October the PV gate stops firing, the
trader grid-charges most nights, and within weeks there will be dozens of clean
episodes. Re-running the two existing scripts in November settles it.

### 2.4 Rejected: deriving load from recent actuals

An adaptive scheme — read yesterday's actual daily load from the portal and set
`drain_hours = 8.88 / (load/24)` — was proposed and **tested against the data**,
predicting each day's load over 146 days:

| Predictor | Mean absolute error |
|---|---|
| Fixed 22.0 kWh | **3.50 kWh** |
| Yesterday's load | 4.04 kWh |
| Mean of last 3 days | 3.49 kWh |

Yesterday is a *worse* predictor than a constant, because summer load is
stationary noise around 21 kWh with no trend to track. The 3-day mean merely
ties the constant. Winter load may well behave differently — it tracks
temperature — but that cannot be shown with the data that exists, so the idea is
not adopted. A temperature-driven model is rejected for the same reason plus the
two coefficients it would add.

---

## 3. Economics

`C` = 11.63 kWh bought per full charge, `U` = 8.88 kWh delivered,
`m` = €0.05/kWh buy margin, `W` = €0.50 wear, `G` = €0.25 gate.

### 3.1 Self-consumption — the only leg being built

Buy `C` at `p_charge + m`, avoid buying `U` at `p_drain + m`:

```
gross = U * p_drain - C * p_charge - m * (C - U)
net   = gross - W
```

`m * (C − U)` = 0.05 × 2.75 = **€0.1375** — margin paid on round-trip losses,
which the current code omits entirely.

Break-even spread at `p_charge` = €0.05: **€0.115/kWh**. Clearing the €0.25
gate: **€0.115 → p_drain ≥ €0.165**, i.e. a required spread of **€0.115**; at
`p_charge` = €0.08 the required spread rises to **€0.125**.

For comparison, the same break-evens computed with the config's optimistic
constants were €0.067 and €0.094. Measured hardware raises the bar by roughly
€0.03/kWh.

### 3.2 Export at peak — explicitly not built

```
gross = U * (p_peak - 0.02) - C * (p_charge + m)
```

At the same hour and price `p`, exporting earns `p − 0.02` while avoided import
earns `p + 0.05`. **Self-consumption beats export by €0.07/kWh — €0.62 per
cycle** on volume the house can absorb, and in winter it always can.

The user's original sketch item 4 ("export at the morning peak") is dropped. The
summer sunny-day Discharge slot already covers the case where export does pay:
when PV, not the grid, filled the battery for free.

### 3.3 Expected value — downgraded, and this is the headline

On the reference day (§4.4) — a perfectly ordinary winter shape with €0.05 nights
and a €0.26 evening peak — the honest plan is **one cycle worth €0.265**, barely
clearing the gate.

Three things eat the margin: €0.50 wear per cycle, a 0.76 round-trip, and the
€0.07/kWh tariff asymmetry. Together they demand a €0.115/kWh spread before a
cycle pays at all.

Realistic expectation: **€5–15/month across a Latvian winter**, with wide
uncertainty, and quite possibly less. The previous estimates in this document
(€20/month in rev1, €10–25 in rev2) were built on unmeasured hardware constants
and an arithmetically wrong reference day.

Two measurements could move this materially, both due in November:

- If the true round-trip is 0.83 rather than 0.76, cycle value rises ~30 %.
- If true winter drain is 4 h rather than 7 h, cycle value roughly doubles.

Both landing favourably would make the mechanism clearly worthwhile. Both landing
badly would mean grid arbitrage on this hardware is not worth the wear, and the
right answer would be to leave ToU off in winter and let Self-use run.

**Shipping Part 1 is still correct regardless**, for two reasons that do not
depend on the business case: the current code books profits that do not exist and
places charges in the wrong hours, and running the honest version through autumn
is what generates the grid-charge episodes needed to settle §2.3.

---

## 4. Design part 1 — the cycle planner

### 4.1 Configuration changes

```python
battery_capacity_kwh: float = Field(default=11.63, gt=0.0)
"""Grid energy bought to take the battery from 10% to 100% SOC.

MEASURED, provisionally: one clean grid-charge episode (2026-08-22, SOC 25->84
at 7.6 kW) gives 0.1292 kWh per SOC point. Only 3 grid charges exist in 148
days of history because the summer PV gate suppresses them; re-measure in
November when autumn grid charging produces dozens. Deliberately the pessimistic
end of the 10.74-11.63 band -- erring high on cost is the safe direction.
NOT the nameplate capacity (10.24 kWh nominal); this is grid-side energy
including charge losses.
"""

round_trip_efficiency: float = Field(default=0.764, gt=0.0, le=1.0)
"""Measured grid-to-house round trip, so cycle_output_kwh = 8.88 kWh.

Discharge side is solid: 147 PV-free episodes, median 0.0987 kWh per SOC point,
i.e. 8.88 kWh delivered over a 90-point swing. The charge side carries the
uncertainty (see battery_capacity_kwh). Config previously assumed 0.900, which
overstated every cycle.
"""

battery_drain_hours: int = Field(default=7, ge=1, le=12)
"""Hours a full battery feeds the house before it is empty.

Sets the window a Charge slot is valued against and the spacing between
consecutive charges. Deliberately the LONG end of the measured 3.6-7.1 h winter
range: too long under-values a cycle (profit forgone), too short over-values it
and books cycles that lose money. See spec section 2.1. Currently a guess
pinned to an unmeasured winter load -- the single largest uncertainty in the
model, worth a 1.9x swing in daily value.

Upper bound 12: larger values silently drop every candidate block and the
planner goes dark behind a misleading "no cycle nets ..." skip reason.
"""

buy_margin_eur_per_kwh: float = Field(default=0.05, ge=0.0)
"""Supplier margin added to spot on every imported kWh. A tariff fact, not a
tuning knob. Needed so a cycle is charged the margin on its round-trip losses
(m * (capacity - output) = EUR 0.1375/cycle at measured constants).
"""
```

`min_net_profit_per_cycle_eur` **stays at €0.25, with a new rationale.** Its
original justification was a ±€0.18 allowance for price-forecast error, which was
always shaky — Elering day-ahead prices for the target day are exact. Its real
job now is to absorb **drain-window model error**, the dominant uncertainty at a
1.9× lever. Lowering it was considered; on the reference day it changes nothing,
because the 9-hour footprint already excludes every other candidate.

Also unchanged: `sunny_day_pv_load_multiplier` (1.5),
`morning_peak_end_multiplier` (2.0), `hours_per_cycle` (2),
`max_cycles_per_day` (6), the `stop_*` fields,
`morning_discharge_target_soc_pct`, and `expected_daily_load_kwh` (22.0 —
validated by measurement at median 20.5 / mean 21.1 for April–August).

#### Known gap: the shoulder-season dead zone

`expected_daily_load_kwh` stays at 22.0 because it feeds the sunny gate, where
the summer figure is correct and measurement confirms it. Raising it would push
the sunny gate to 45–90 kWh, which a 10 kWp array never reaches, killing the
summer morning Discharge slot.

The cost is a real dead zone:

- Cycle branch dies when PV > 12.78 kWh (`22 − PV < 9.216`; with the corrected
  8.88 output, PV > 13.12).
- Sunny branch needs PV ≥ 33 kWh.
- **PV ∈ (13.1, 33) kWh → neither branch runs; ToU off, pure Self-use.**

On a 10 kWp array in Latvia that band covers much of March, April, September and
October — plausibly 60–90 days a year. A clear late-February day forecasting
20 kWh while a heat pump pulls 40 kWh/day would have ~20 kWh of real imports and
a genuinely profitable cycle, and the planner goes dark. Order of magnitude:
**€20–50/year forgone** — a meaningful fraction of the whole benefit, not a
rounding error.

No fix is available that does not either break the summer branch or introduce a
second load constant, and §2.4 showed the adaptive alternative fails on the data
we have. Accepted for now, named here so it is not forgotten, and revisited with
the November measurements.

### 4.2 Algorithm

```
charge_blocks = _build_blocks(hourly, settings.hours_per_cycle)
# NOTE: _build_blocks yields EVERY rolling window, not aligned ones. rev1 of
#       this spec got its reference day wrong by forgetting that. Any hand-
#       worked example must enumerate all of them.

for b in charge_blocks:
    drain = the settings.battery_drain_hours hourly prices starting at b.end,
            required to be contiguous clock hours with no gaps
    if fewer than battery_drain_hours contiguous hours available: drop b   # 4.3
    gross = cycle_output_kwh   * mean(drain)
          - battery_capacity_kwh * b.avg_eur_per_kwh
          - buy_margin_eur_per_kwh * (battery_capacity_kwh - cycle_output_kwh)
    net = gross - wear_cost_per_cycle_eur
    if net >= min_net_profit_per_cycle_eur:
        candidate, discharge = TradingWindow(b.end, b.end + drain_hours, mean)

sort candidates by (-net, charge.start)
greedy: accept c if footprint(c) is disjoint from every chosen footprint
```

**Footprint** = `[charge.start, charge.end + battery_drain_hours)` — the charge
window plus the time the battery needs to empty. Disjointness is symmetric, so
selection order does not matter, and it is what kills bug B: a second charge
cannot begin while the first fill is still being consumed. The half-open form has
no off-by-one — footprints touching exactly at the boundary are correctly
allowed, since the battery empties as the next charge begins.

It does exclude physically-fine *partial top-ups* (charge 00–02, house eats half
by 04:00, top up 04–06 before the peak). Booking those honestly needs SOC
simulation, incompatible with whole-cycle accounting and with the
single-threshold philosophy. Accepted trade.

Greedy-by-net is a **choice, not a given**: this is weighted interval scheduling
and greedy can be suboptimal (one €0.60 cycle blocking two €0.40 ones). With ≤19
candidates an exact DP is about ten lines. Not built — with a 9-hour footprint at
most two cycles fit a day — but the door is left open.

`_hours_of_cycle` becomes dead and is removed.

Two consequences to handle while in the file:

- The guard `len(hourly) < 2 * settings.hours_per_cycle` (strategy.py:270) is now
  the wrong minimum; it must become `hours_per_cycle + battery_drain_hours`.
  Outcome is unchanged either way, but the skip reason is currently misleading.
- `cycle_cap = min(max_cycles_per_day, 5)` on a sunny day is **dead by
  arithmetic** — the cycle branch needs PV ≤ 13.1 and the sunny branch PV ≥ 33,
  so they can never co-fire. Keep as belt-and-braces, document as unreachable.

### 4.3 Why blocks near end of day are dropped

`hourly` covers the target day only. A charge at 21:00–23:00 has one hour of
price data after it; valuing all 8.88 kWh against that hour overstates it badly.

Fetching the next day's prices is **not possible**: the cron runs 22:30 on day D
planning D+1, and Nord Pool publishes D+2 prices around 14:00 CET on D+1.

Nothing real is lost — the next evening's run can place a 00:00 charge on D+2
covering nearly the same drain hours at typically cheaper night prices.

### 4.4 Reference day (becomes a golden test)

Prices: 00–05 €0.05 · 06 €0.12 · 07–09 €0.22 · 10–15 €0.13 · 16 €0.18 ·
17–20 €0.26 · 21–23 €0.10

All 24 rolling 2-hour charge blocks enumerated programmatically at measured
constants (`C` = 11.63, `U` = 8.88, `D` = 7). Exactly **one** clears the gate:

| Charge | Drain window | Drain avg | Net | Footprint |
|---|---|---|---|---|
| 04–06 | 06–13 | €0.1671 | **€0.265** | [04, 13) |

**Total €0.265**, one cycle.

Sensitivity, same day, same prices:

| Variant | Result |
|---|---|
| `drain_hours` = 4 (measured constants) | 04–06 only, **€0.513** |
| `drain_hours` = 7, config's old optimistic constants | 04–06 only, €0.477 |
| `drain_hours` = 4, config's old constants | 04–06 + 14–16, €1.063 |
| Current code (free pairing, three phantom cycles) | **€3.739** |

The drain=4 plan re-valued at a true 7 h drain yields €0.265 — it books €0.513
for the same physical outcome, which is §2.1's argument in one line.

### 4.5 Blast radius

Confined to `strategy.py`, `config.py`, `tests/test_strategy.py`.

`CyclePair.discharge` keeps its type and position; it now holds the *derived*
drain window instead of a *chosen* block. Grep-verified: `livoltek.py`
(`_fill_charge_slot`, lines 367–368) and `notify.py` (`format_plan_message`,
line 151) read only `cycle.charge`, so **neither production file changes**.

Two non-production follow-ups:

- `scripts/livoltek_apply_smoke.py:62` prints `c.discharge` labelled "disch"; it
  will now print a derived drain window. Relabel.
- `gross_revenue_eur` becomes a misnomer — the new formula nets out purchase
  cost, so the field is operating profit before wear. Nothing user-facing reads
  it; re-document or rename while in the model.

`_plan_stop_window` and the sunny-day gate are untouched.

---

## 5. Telemetry — resolved, and it needs no production code

### 5.1 The portal is the archive

Discovered by spike (`scripts/livoltek_peek_history.py`,
`scripts/livoltek_peek_history_range.py`):

| Endpoint | Payload | Retention |
|---|---|---|
| `POST /ctrller-manager/energystorage/reportFormToCString` `{id, timeType:0, startTime, endTime}` | **5-minute** series: `Battery SOC`, `Battery power`, `Load Power`, `PV Power`, `Battery temperature`, `exportpwr_real` | any date; data from 2026-04-01 |
| `POST /ctrller-manager/powerstation/reportForm` `{id, timeType:1, startTime, endTime}` | daily aggregates: `Load`, `ETotal Charge/Discharge/toGrid`, `SM_PositiveE/NegativeE`, `Total Profit` | any month; data from 2026-04 |

Both require the SPA's `authorization` header, not just cookies — a replay with
cookies alone returns an empty shape. Requests are one per day (5-minute) and one
per month (daily).

**Consequence: no nightly collection is needed, on Railway or anywhere.**
Retention is effectively unlimited and a full 148-day harvest takes about a
minute. The earlier plan to push JSONL from the Railway cron via the GitHub
Contents API is dropped, and **no GitHub token is required**.

The plan-reconstruction gap from rev2 also closes: the portal supplies *actual*
PV at 5-minute resolution, which is better than the forecast for judging whether
a plan was right. Reproducing the exact forecast that was issued is a secondary
concern and not worth persisting anything for.

### 5.2 Working scripts, already committed

- `livoltek_peek_history.py` — enumerates device tabs, captures all JSON XHR
- `livoltek_peek_history_range.py` — captures request shapes, probes retention
- `livoltek_harvest_history.py` — pulls a date range to `data/telemetry/*.jsonl`
- `analyze_telemetry.py` — offline: capacity, efficiency, load, drain hours

All read-only; none touch Params set or click Save. `data/exploration/` is
gitignored because captured XHR carries session tokens.

The `collect` subcommand proposed in rev2 is **not built**. These scripts cover
the need, and adding it to the shipped CLI would put portal-reading code in the
same binary as the schedule writer for no benefit.

### 5.3 November re-measurement — the real prerequisite

Re-run `livoltek_harvest_history.py` then `analyze_telemetry.py` once ~3 weeks of
autumn grid charging exist, and settle three numbers:

1. **Grid-charge kWh per SOC point** → real `battery_capacity_kwh` and round
   trip. Currently n=1 (§2.3); by November there will be dozens.
2. **Winter daily load** → real `battery_drain_hours` via 8.88 / (load/24).
3. **Whether the dead zone matters** — with real winter load in hand, decide
   whether §4.1's gate needs rework.

Then retune the constants, and re-examine §3.3: if both numbers land badly, the
correct decision may be to leave ToU off in winter.

---

## 6. Explicitly not built

- **A winter/summer mode flag or date window** — the PV gates partition the year
  automatically and handle a Dunkelflaute or a bright February week better than a
  calendar. (This does not excuse the §4.1 dead zone, which is a defect in where
  the gates sit, not in using PV as the sensor.)
- **A peak-proximity detector** — valuing against the drain-window mean makes the
  block before the peak win automatically.
- **Morning export to grid** (sketch item 4) — loses €0.62/cycle; §3.2.
- **"Switch the house to grid overnight"** — in winter the battery is empty at
  night and Self-use already does this.
- **A temperature-driven load model, or deriving load from recent actuals** —
  §2.4, tested and rejected on data.
- **A robust min-over-drain-range valuation** — §2.1, identical output.
- **An exact DP for cycle selection** — §4.2, greedy documented as a choice.
- **Any nightly telemetry push (Railway Volume, GitHub API, second cron)** —
  §5.1, unnecessary given unlimited portal retention.
- **A `collect` CLI subcommand** — §5.2, the scripts cover it.

---

## 7. Testing

New and rewritten tests in `tests/test_strategy.py`:

1. **Golden reference day** (§4.4) — one chosen charge at 04–06, total ≈ €0.265
   (`pytest.approx`). The enumeration must cover every rolling block.
2. **Drain-assumption sensitivity** — same day at `battery_drain_hours` = 4 gives
   ≈ €0.513. Locks in §2.1 and makes any change to the constant visible.
3. **Drain window derivation** — a block with fewer than `battery_drain_hours` of
   contiguous following data is dropped (charge 22–24 yields no candidate).
4. **Footprint spacing** — replaces
   `test_plan_day_caps_at_max_cycles_per_day_six`. Overlapping footprints cannot
   both be chosen; footprints touching exactly at the boundary both can.
5. **Placement, not price** — where the globally cheapest block is far from any
   expensive block, the chosen charge is the one adjacent to the expensive block.
6. **Margin accounting** — a cycle whose naive spread clears €0.25 but whose
   honest net does not is rejected.
7. **Peak-then-crash rejection** (§2.1) — rejected at `drain_hours` = 7.
   Regression guard against lowering the constant without measuring.
8. **Negative prices** — charging at a negative spot is handled and profitable.
   No code change needed; the test pins behaviour.

Must continue to pass unchanged: PV-skip gate tests, all `_plan_stop_window` /
sunny-day tests, `aggregate_hourly` tests, and the `max_cycles_per_day == 0`
short-circuit.

### 7.1 Unrelated one-line resilience win

`main.py` lines 94–101 abort the whole run if Open-Meteo fails, even though
`plan_day` accepts `pv_forecast=None` and a deep-winter plan barely needs PV. Let
a PV fetch failure degrade to `None` instead of killing the night's schedule.
Independently shippable, and it removes a single-point failure for exactly the
season this spec addresses.

---

## 8. Sequencing

1. **§7.1 resilience fix** — independent, ship immediately.
2. **Part 1** — planner fix plus measured constants (§4). Ship to Railway. Its
   honest valuation will decline unprofitable cycles instead of burning wear, and
   its autumn grid charges generate the data §5.3 needs.
3. **November** — re-harvest, re-analyse, retune `battery_capacity_kwh`,
   `round_trip_efficiency`, `battery_drain_hours`.
4. **Then decide** — revisit §3.3 and the §4.1 dead zone with real numbers, and
   if the economics do not hold up, turn ToU off for winter deliberately.

---

## 9. Revision history

**rev3 (2026-08-27)** — rebuilt on 148 days of harvested telemetry.

- **Hardware constants measured, all worse than config.** Delivery 8.88 kWh not
  9.216 (147 episodes, solid). Grid purchase 11.63 kWh not 10.24 (1 clean
  episode, weak — §2.3). Round trip ≈0.76 not 0.90.
- **Reference day falls to €0.265** from rev2's €0.477. The current code's
  overstatement is therefore **14×**, not 3.7×.
- **Business case downgraded to €5–15/month** and flagged as possibly not worth
  the wear (§3.3). Previous estimates rested on unmeasured constants.
- **`expected_daily_load_kwh` = 22.0 validated** by measurement (median 20.5,
  mean 21.1 over 149 days) — kept, not guessed.
- **Adaptive load rejected on evidence** (§2.4): yesterday's load predicts today
  *worse* than a fixed 22.0 (MAE 4.04 vs 3.50).
- **No winter data exists** — inverter history begins 2026-04-01, so this is the
  system's first winter and calibration must wait (§2.2).
- **Telemetry needs no production code** (§5). Portal retention is unlimited and
  the endpoints are documented; the Railway push and GitHub token are dropped, as
  is the `collect` subcommand.

**rev2 (2026-08-27)** — corrected after adversarial review: inverted §2.1's
safety argument (`battery_drain_hours` 4 → 7); regenerated the reference day
after rev1 enumerated only hour-aligned blocks and missed 04–06; corrected the
phantom-cycle count to three; withdrew the false claim that ntfy showed inflated
figures; named the shoulder-season dead zone; downgraded the Open-Meteo
reconstruction claim.

**rev1 (2026-08-27)** — initial design.
