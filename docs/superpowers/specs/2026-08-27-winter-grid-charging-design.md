# Winter grid-charging: honest cycle valuation and drain-anchored charge placement

**Date:** 2026-08-27
**Status:** Approved for implementation
**Revision:** rev2 — corrected after adversarial review. See §9 for what changed and why.
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
November and February. The season switch is therefore already automatic and no
mode flag will be added (§6). It is **not** fully correct, however — see the
shoulder-season dead zone in §4.1.

### 1.2 Bug A — the discharge window is fiction

`_build_cycle` values a cycle as:

```
spread = discharge.avg_eur_per_kwh - charge.avg_eur_per_kwh
gross  = spread * cycle_output_kwh
```

The planner searches freely over `(charge_block, discharge_block)` pairs with the
only constraint being `discharge.start >= charge.end`. It will happily pair a
00:00–02:00 charge with a 17:00–19:00 "discharge" and book the full spread.

In reality the inverter is in Self-use once the Charge slot ends, so the battery
starts feeding the house **immediately** and keeps going until it is empty. The
drain time under a winter heat-pump load is a few hours. Energy bought at 00:00
is gone long before the 17:00 peak. The value actually realised is against the
mean price of the hours *directly following the charge*, not against the dearest
block of the day.

### 1.3 Bug B — phantom extra night charges

Cycle-to-cycle exclusion is checked by shared clock hours only
(`_hours_of_cycle`). On the §4.4 reference day the current greedy picks **three**
cycles:

| Charge | "Discharge" | Booked net |
|---|---|---|
| 00–02 | 17–19 | €1.435 |
| 02–04 | 19–21 | €1.435 |
| 04–06 | 07–09 | €1.067 |

All three are hour-disjoint, so all three are accepted — **€3.937 booked**. But
the second and third charges begin on a battery the previous charge just filled.
With a real multi-hour drain they do partially charge, so the sin is not physical
no-op-ness; the sin is booking two or three *full* cycles of profit for one
battery's worth of energy.

`tests/test_strategy.py::test_plan_day_caps_at_max_cycles_per_day_six` (line 323)
encodes this behaviour as correct and must be rewritten.

### 1.4 Combined effect

Bugs A and B compound. On the §4.4 reference day the current code books
**€3.937** for a day honestly worth **€0.477** — a **3.7× overstatement**.

The inflated figure reaches the `main.plan_ready` structlog line
([main.py:108](../../../src/livoltek_trader/main.py)) in Railway logs only.
`format_plan_message` renders PV and slot times and no EUR figure at all, so no
ntfy notification has ever shown the wrong number. The accounting bug is real;
its user-visible blast radius is zero.

---

## 2. Measured facts this design rests on

| Fact | Value | Source |
|---|---|---|
| Battery runtime from full, winter | 2–5 h | User observation |
| Winter daily household load | 30–60 kWh, temperature-dependent | User observation (Livoltek `Load Consumption`) |
| Implied continuous load | 1.25–2.5 kW | Derived |
| Implied drain window | 3.7–7.4 h | Derived |
| Charge rate | ~5 kW, 10→100 % in ~1.5 h | Hardware |
| Wear cost | €0.50 / full cycle | `battery_price_eur / battery_cycle_life` |
| Buy price | spot + €0.05/kWh | Tariff |
| Sell price | spot − €0.02/kWh | Tariff |

The two observations do not perfectly agree: 30–60 kWh/day implies a 3.7–7.4 h
drain, while runtime was reported as 2–5 h. The 2 h end would require ~4.6 kW
continuous, above the stated load range — most likely a recollection of the
coldest days, or of a battery that was not actually at 100 %. Taking the union,
the plausible range is roughly **2–7.4 h**.

### 2.1 The drain constant must be the LONG end of the range, not the short end

An earlier revision of this spec argued the opposite and was wrong. The
asymmetry runs the other way:

- **Assume too short.** All `cycle_output_kwh` is attributed to the first few
  hours at their high prices, when in reality part of it is delivered in later,
  cheaper hours. The cycle is **over-valued**, and cycles that lose money get
  booked as profitable. No gate protects against this, because the booked figure
  is inflated before the gate sees it.
- **Assume too long.** The window is diluted by hours in which the battery is
  already empty and those prices never apply. The cycle is **under-valued**, so
  some genuinely profitable cycles are skipped. Profit is forgone; money is not
  lost.

Worked counter-example — a peak-then-crash day (calm morning, windy midday), a
shape the Baltic market produces regularly: 00–03 €0.13, 04–05 €0.09,
06–08 €0.30, 09–13 €0.02, 14–23 €0.12. Charge 04–06:

| Assumption | Drain mean | Booked net |
|---|---|---|
| 4 h drain | €0.230 | **+€0.647** — accepted |
| 7 h drain (truth) | €0.140 | **−€0.183** — correctly rejected |

A 4 h assumption books €0.647 of profit on a cycle that loses €0.183 and burns a
wear cycle. The same effect appears on the §4.4 reference day: a 4 h planner
books €1.063 for a day worth €0.464 at a true 7 h drain.

**Therefore `battery_drain_hours = 7`.**

This has a real cost. If the true drain is nearer 4 h, valuing at 7 h forgoes
roughly half the available profit — on the reference day, €0.477 captured
against €1.063 available. That cost is accepted deliberately, because the
alternative is a planner that occasionally pays to lose money, and because
§5.4 replaces the estimate with a measurement. **Measuring the real drain time
is the highest-value follow-up in this spec, not a nice-to-have.**

A robust-optimisation variant was considered — accept a cycle only if it clears
the gate under both a 4 h and a 7 h assumption. On both the reference day and the
counter-example it produces exactly the same selection as using 7 h alone, so it
was dropped as needless machinery.

### 2.2 Why no temperature-driven load model

Open-Meteo already returns temperature, so a load model is technically
available. It is not built: it would add two coefficients (base load, heat-pump
slope) to refine a number that §5.4 is about to measure directly from SOC
history. Multi-coefficient logic has also been explicitly rejected as a design
direction for this project.

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
purchase. Together those two omissions overstate a cycle by
`(C - U) * (p_charge + m)` — €0.113 at `p_charge` = €0.06, but €0.154 at
`p_charge` = €0.10, so it grows with the charge price rather than being constant.

Break-even spread at `p_charge` = €0.06: **€0.067/kWh**. Clearing the €0.25
gate: **€0.094/kWh**. (All figures in this section independently recomputed.)

### 3.2 Export at peak (explicitly not built)

```
gross = U * (p_peak - 0.02) - C * (p_charge + m)
net   = gross - W
```

where `p_peak` is the spot price during the export window. At the same hour and
price `p`, exporting earns `p − 0.02` while avoided import earns `p + 0.05`.
**Self-consumption beats export by €0.07/kWh — €0.645 per cycle** on any volume
the house can absorb. In winter the house always absorbs it.

Break-even for export requires a peak **spot** price of ~€0.197/kWh and
gate-clearing ~€0.224/kWh, so on a typical winter morning peak of €0.20 it nets
~€0.03 and below €0.197 it loses money outright.

Decision: **no export leg in the winter branch.** The user's original sketch
item 4 ("export at the morning peak") is economically wrong for winter and is
dropped. The summer sunny-day Discharge slot already covers the case where
export does pay — when PV, not the grid, filled the battery for free.

### 3.3 Expected value — order of magnitude only

The reference day yields €0.477 at `battery_drain_hours` = 7. Multiplying one
hand-built favourable day by 30 is not a forecast, so no monthly figure is
claimed with precision. A real Latvian winter contains windy near-flat weeks
where nothing clears the gate and cold snaps where spreads exceed €0.20/kWh.
Best available estimate: **€10–25/month, with roughly ±2× uncertainty**, rising
if §5.4 shows the true drain is shorter than 7 h and the constant can be
lowered.

This is modest. It is stated plainly here because the previous €20/month figure
was derived from an arithmetically wrong reference day and should not be trusted
as a business case.

---

## 4. Design part 1 — the cycle planner

### 4.1 Configuration changes

Two additions to `Settings`:

```python
battery_drain_hours: int = Field(default=7, ge=1, le=12)
"""Hours a full battery feeds the house before it is empty.

Sets the window a Charge slot is valued against, and the spacing between
consecutive charges. Deliberately the LONG end of the plausible 2-7.4 h range:
too long under-values a cycle (profit forgone), too short over-values it and
books cycles that lose money. See spec section 2.1 for the worked counter-
example. Replace this estimate with a measurement from portal SOC history --
this is the primary tuning knob for the winter branch and it is currently a
guess.

Upper bound 12 because a larger value silently drops every candidate block and
the planner goes dark behind a misleading "no cycle nets ..." skip reason.
"""

buy_margin_eur_per_kwh: float = Field(default=0.05, ge=0.0)
"""Supplier margin added to spot on every imported kWh.

A tariff fact, not a tuning knob. Needed so a cycle is charged the margin on
its round-trip losses (m * (capacity - output) = EUR 0.0512/cycle).
"""
```

`min_net_profit_per_cycle_eur` **stays at €0.25, with a new rationale.** Its
original justification was a ±€0.18 allowance for price-forecast error, which
was always shaky — Elering day-ahead prices for the target day are exact, not
forecast. Its real job now is to absorb **drain-window model error**, which is
the dominant uncertainty (a 2× range on the one constant that scales the whole
valuation). Lowering it to ~€0.14 would restore the old *effective* strictness
now that the formula is honest, but that trade is only worth making once §5.4
has measured the drain window. Verified on the reference day: lowering the gate
to €0.10 changes the selection not at all, because the 9-hour footprint already
excludes every additional candidate.

Also unchanged: `expected_daily_load_kwh` (22.0),
`sunny_day_pv_load_multiplier` (1.5), `morning_peak_end_multiplier` (2.0),
`hours_per_cycle` (2), `max_cycles_per_day` (6), the `stop_*` fields, and
`morning_discharge_target_soc_pct`.

#### Known gap: the shoulder-season dead zone

`expected_daily_load_kwh` stays at 22.0 because it feeds the sunny gate, where
the spring figure is the correct one. Raising it would push the sunny gate to
`load × 1.5` = 45–90 kWh, which a 10 kWp array never reaches, killing the summer
morning Discharge slot entirely.

The cost of that choice is a real dead zone, and it must not be papered over:

- The cycle branch dies when PV > 12.78 kWh (`22 − PV < 9.216`).
- The sunny branch needs PV ≥ 33 kWh.
- **PV ∈ (12.8, 33) kWh → neither branch runs, ToU is disabled, pure Self-use.**

A clear late-February day on this array forecasts 15–25 kWh while the heat pump
still pulls 40 kWh/day. Real grid imports would be ~20 kWh and a cycle would be
genuinely profitable, but the planner goes dark. Cost is roughly €0.3–0.5 of
forgone profit per affected day, on bright shoulder-season days only.

This is **accepted for now and explicitly scheduled**: no fix is available that
does not either break the summer branch or introduce a second load constant, and
the user's design constraint is to avoid new knobs. §5.4's load measurement is
the trigger to revisit it with data instead of a guess.

### 4.2 Algorithm

The free `(charge, discharge)` pair search is replaced. The drain window is no
longer a choice — it is derived from physics.

```
charge_blocks = _build_blocks(hourly, settings.hours_per_cycle)   # unchanged
# NOTE: _build_blocks yields EVERY rolling window, not aligned ones.
#       An earlier revision of this spec got its reference day wrong by
#       forgetting that. Any hand-worked example must enumerate all of them.

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
symmetric (selection order does not matter) and is what kills bug B: a second
charge cannot begin while the first fill is still being consumed. The half-open
form has no off-by-one — back-to-back footprints touching at the boundary are
correctly allowed, since the battery empties exactly as the next charge begins.

The rule does exclude physically-fine *partial top-ups* (charge 00–02, house
eats half by 04:00, top up 04–06 before the peak). Booking those honestly needs
SOC simulation, which is incompatible with whole-cycle accounting and with the
single-threshold philosophy. Accepted trade.

Greedy-by-net is a **choice, not a given**: disjoint-footprint selection is
weighted interval scheduling, where greedy can be suboptimal (one €0.60 cycle
blocking two €0.40 ones). With ≤19 candidates an exact DP is about ten lines.
Not built, because with a 9-hour footprint at most two cycles fit a day and the
conflict case is rare — but the door is left open.

`_hours_of_cycle` becomes dead and is removed.

Two consequences to handle while in the file:

- The guard `len(hourly) < 2 * settings.hours_per_cycle`
  ([strategy.py:270](../../../src/livoltek_trader/strategy.py)) is now the wrong
  minimum. It must become
  `hours_per_cycle + battery_drain_hours`; otherwise the skip reason is
  misleading even though the outcome is the same.
- `cycle_cap = min(max_cycles_per_day, 5)` on a sunny day is **dead by
  arithmetic** — the cycle branch requires PV ≤ 12.78 and the sunny branch
  requires PV ≥ 33, so they can never co-fire. Keep it as belt-and-braces but
  document it as unreachable rather than implying it can fire.

### 4.3 Why blocks near end of day are dropped rather than partially valued

`hourly` covers the target day only. A charge at 21:00–23:00 has one hour of
price data after it; valuing all 9.216 kWh against that single hour would
overstate the cycle badly.

Fetching the following day's prices is **not an option**: the cron runs at 22:30
on day D and plans D+1, and Nord Pool publishes D+2 prices only around 14:00 CET
on D+1. They do not exist at planning time.

Nothing real is lost. The next evening's run can place a 00:00 charge on D+2
covering nearly the same drain hours, typically at cheaper night prices.

### 4.4 Reference day (becomes a golden test)

Prices: 00–05 €0.05 · 06 €0.12 · 07–09 €0.22 · 10–15 €0.13 · 16 €0.18 ·
17–20 €0.26 · 21–23 €0.10

All 24 rolling 2-hour charge blocks were enumerated programmatically. At
`battery_drain_hours` = 7, three clear the €0.25 gate:

| Charge | Drain window | Drain avg | Net | Footprint | Outcome |
|---|---|---|---|---|---|
| 04–06 | 06–13 | €0.1671 | €0.477 | [04, 13) | **chosen** |
| 03–05 | 05–12 | €0.1557 | €0.372 | [03, 12) | overlaps [04, 13) |
| 02–04 | 04–11 | €0.1443 | €0.267 | [02, 11) | overlaps [04, 13) |

**Total €0.477**, one cycle, charge 04–06.

No evening cycle survives: the best is 14–16 at €0.066, because a 7-hour drain
starting at 16:00 runs past the €0.26 peak into the €0.10 late-evening hours.

For contrast, the same day at `battery_drain_hours` = 4 would select 04–06
(€0.734) and 14–16 (€0.329) for €1.063 — of which only €0.464 is real if the
true drain is 7 h, with the 14–16 cycle actually returning −€0.013. This pair of
numbers is the concrete argument for §2.1 and is worth keeping as a second test.

Same day under the current code: **€3.937** across three phantom cycles (§1.3),
a 3.7× overstatement.

### 4.5 Blast radius

Confined to `strategy.py`, `config.py`, and `tests/test_strategy.py`.

`CyclePair.discharge` keeps its type and position; it now holds the *derived*
drain window instead of a *chosen* block. Grep-verified: `livoltek.py`
(`_fill_charge_slot`, lines 367–368) and `notify.py` (`format_plan_message`,
line 151) read only `cycle.charge`, so **neither production file changes**. The
portal write path, slot ordering, weekday picker, SOC targets, and ntfy
formatting are untouched.

Two non-production follow-ups:

- `scripts/livoltek_apply_smoke.py:62` prints `c.discharge` times labelled
  "disch". It will now print a derived drain window, which is misleading dev
  output. Relabel.
- `gross_revenue_eur` becomes a misnomer — the new formula nets out the purchase
  cost, so the field is operating profit before wear, not gross revenue. Nothing
  user-facing reads it; re-document or rename while in the model.

`_plan_stop_window` and the sunny-day gate are untouched.

---

## 5. Design part 2 — telemetry

### 5.1 No second cron; one extra log field rather than a plan store

A daily plan is *mostly* deterministic given prices, PV forecast, and settings:

- **Prices** — Elering API serves history faithfully.
- **PV forecast** — only approximately recoverable. The plain Open-Meteo
  **archive** endpoint is ERA5 reanalysis, i.e. *actuals*; it answers "what was
  the weather", not "what did the 22:30 forecast say". Open-Meteo does offer a
  Historical Forecast API with the same parameter shape as `solar.py`'s call, but
  it stitches forecasts at the shortest available lead time rather than the ~24 h
  lead the cron consumed.
- **Plan** — recompute with `plan_day`.

The earlier claim that nothing needs persisting was therefore too strong. The
mitigation is cheap, because in deep winter the PV number is
**decision-irrelevant**: any forecast below 12.78 kWh yields an identical plan.
Recompute-from-prices alone is faithful for November–January, and degrades
precisely on shoulder-season days near the gates — the same days as the §4.1
dead zone.

Minimum durable addition: extend the existing `main.plan_ready` structlog line
with `pv_expected_kwh` and the chosen charge-window times. One log line per
night, no new storage system, no new write path.

Telemetry proper is a **local, on-demand command**, not a Railway service. No
second cron, no Railway Volume, no database, no git-committing cron, and — most
importantly — **no second Playwright write path**. Production failure surface is
unchanged.

Caveat accepted: recomputed plans use *current* settings, not the settings in
force on the day. The question we want answered is "what would today's algorithm
have done", so this is desired.

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
| `soc_pct` | **`battery_drain_hours`** — the guessed constant that scales the whole valuation |
| `load_kwh` | `expected_daily_load_kwh` and the §4.1 dead zone |
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
3. Answer three questions: what **resolution** is available (hourly or
   5-minute), how far **back** history is retained, and whether **SOC appears in
   history at all** or only as a live value.

If SOC is not in history, or retention is only days, then — and only then — a
periodic read-only collector is justified, and that decision is revisited with
real findings rather than assumptions.

### 5.4 First analysis once data exists — a prerequisite, not a follow-up

Measure `battery_drain_hours`. This needs SOC history only. Take every episode
where SOC fell from above 90 % to below 20 %, record how many hours it took, and
group by outside temperature.

This is the highest-value item in the spec. `battery_drain_hours` scales the
entire valuation, is currently a guess, and is deliberately set pessimistic
(§2.1) at a cost of roughly half the available profit. Measuring it is what lets
the constant come down, and is also the precondition for revisiting both the
€0.25 gate (§4.1) and the shoulder-season dead zone.

---

## 6. Explicitly not built

- **A winter/summer mode flag or date window.** The PV gates partition the year
  automatically and handle an October Dunkelflaute or a bright February week
  better than a calendar would. A flag would be a second, disagreeing source of
  truth. (This does not excuse the §4.1 dead zone, which is a separate defect in
  where the gates sit, not in using PV as the sensor.)
- **A peak-proximity detector.** Valuing a charge against the mean of its drain
  window makes the block before the peak win automatically. "Peak approaching"
  as a separate factor is redundant.
- **Morning export to grid** (original sketch item 4). Loses €0.645/cycle
  against self-consumption in winter; see §3.2.
- **"Switch the house to grid overnight."** In winter the battery is empty at
  night and Self-use already routes the house to grid. Only battery charging is
  schedulable, and that is the Charge slot.
- **A temperature-driven load model.** See §2.2.
- **A robust min-over-drain-range valuation.** See §2.1 — same output as the
  single long constant on every case tested.
- **An exact DP for cycle selection.** See §4.2 — greedy is documented as a
  choice.
- **A second cron that rewrites the schedule.** Doubles the flakiest component
  in the system and introduces multi-run state.
- **Changing `expected_daily_load_kwh`.** See §4.1, including its cost.

---

## 7. Testing

New and rewritten tests in `tests/test_strategy.py`:

1. **Golden reference day** (§4.4) — asserts a single chosen charge at 04–06 and
   total ≈ €0.477 (`pytest.approx`). The enumeration must cover every rolling
   block, which is what the previous revision of this spec got wrong.
2. **Drain-assumption sensitivity** — the same day at `battery_drain_hours` = 4
   selects 04–06 and 14–16 for ≈ €1.063. Locks in the §2.1 argument and makes
   any future change to the constant visible.
3. **Drain window derivation** — a charge block with fewer than
   `battery_drain_hours` of contiguous following price data is dropped.
4. **Footprint spacing** — replaces
   `test_plan_day_caps_at_max_cycles_per_day_six`, which encodes bug B. Asserts
   that overlapping-footprint charges cannot both be chosen, and that
   footprints touching exactly at the boundary both can.
5. **Placement, not price** — on a day where the globally cheapest block is far
   from any expensive block, the chosen charge is the one adjacent to the
   expensive block.
6. **Margin accounting** — a cycle whose naive spread clears €0.25 but whose
   honest net does not is rejected.
7. **Peak-then-crash rejection** (§2.1 counter-example day) — asserts the cycle
   is rejected at `battery_drain_hours` = 7. Regression guard against anyone
   lowering the constant without measuring.
8. **Negative prices** — charging at a negative spot is handled and profitable
   (verified: charge at −€0.10 against a €0.05 drain window nets ≈ €0.93). No
   code change needed; the test pins the behaviour.

Must continue to pass unchanged: the PV-skip gate tests, all
`_plan_stop_window` / sunny-day Discharge tests, `aggregate_hourly` tests, and
the `max_cycles_per_day == 0` short-circuit.

For the `collect` command: the spike is exploratory and not test-driven. The
parser turning intercepted JSON into telemetry rows is pure and gets unit tests
against a captured fixture payload.

### 7.1 Unrelated one-line resilience win

`main.py` lines 94–101 abort the whole run if Open-Meteo fails, even though
`plan_day` already accepts `pv_forecast=None` and a deep-winter plan barely needs
PV. Let a PV fetch failure degrade to `pv_forecast=None` instead of killing the
night's schedule. Small, in scope while in the file, and it removes a
single-point failure for the season this spec is about.

---

## 8. Sequencing

1. **Spike** — portal history surface (§5.3). Moved first: `battery_drain_hours`
   is a guess that scales everything, and the spike is what makes measuring it
   possible.
2. Part 1 — planner fix, config, tests (§4). Independently shippable to Railway
   with `battery_drain_hours` = 7.
3. Part 2 — `collect` command, informed by the spike.
4. **Measure `battery_drain_hours`** (§5.4), lower it if the data supports it,
   then revisit the €0.25 gate and the §4.1 dead zone.

---

## 9. Revision history

**rev2 (2026-08-27)** — corrected after an adversarial review that recomputed
every figure independently. Changes:

- **`battery_drain_hours` default 4 → 7, and §2.1's safety argument inverted.**
  rev1 claimed erring short was safe. It is the dangerous direction: too short
  over-values a cycle and books losses. Demonstrated with a peak-then-crash
  counter-example.
- **§4.4 reference day regenerated.** rev1 enumerated only hour-aligned charge
  blocks and missed 04–06, the day's best cycle, so its golden test asserted a
  wrong answer that a correct implementation would have failed.
- **§1.3/§1.4 corrected.** The current code books €3.937 across *three* phantom
  cycles, not €2.80 across two — a 3.7× overstatement. rev1's claim that ntfy
  messages showed the inflated figure was false; no EUR value appears in any
  notification.
- **§4.1 dead zone named.** rev1 asserted any load value from 22 to 60 gives the
  same winter answer. True only when PV ≤ 12.78 kWh. PV ∈ (12.8, 33) is a
  shoulder-season gap where no branch runs.
- **§5.1 downgraded.** The Open-Meteo archive endpoint returns actuals, not the
  forecast issued at the time, so "nothing needs persisting" was too strong.
- **§4.1 gate rationale rewritten.** €0.25 kept, but for a different and honest
  reason: it now absorbs drain-window model error, not price-forecast error.
  Lowering it was considered and rejected as premature; verified it would change
  nothing on the reference day.
- Added: §4.2 bounds and dead-code notes, §7 tests 2/7/8, §7.1 Open-Meteo
  resilience, §4.5 non-production follow-ups, §3.3 honest uncertainty.
