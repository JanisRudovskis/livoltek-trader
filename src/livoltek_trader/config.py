"""Application configuration loaded from environment variables / .env."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    elering_base_url: str = Field(default="https://dashboard.elering.ee/api")
    elering_region: str = Field(default="lv")
    elering_timeout_s: float = Field(default=15.0)

    battery_capacity_kwh: float = Field(default=11.63, gt=0.0)
    """Grid energy bought to take the battery from 10% to 100% SOC.

    MEASURED, provisionally. One clean grid-charge episode (2026-08-22,
    SOC 25→84 at 7.6 kW, avoiding both the bottom and the >90% taper) gives
    0.1292 kWh per SOC point. Only 3 grid charges exist in 148 days of
    history because the summer PV gate suppresses them, so re-measure in
    November once autumn grid charging has produced dozens. Deliberately the
    pessimistic end of the 10.74–11.63 band: erring high on cost is safe.

    NOT the nameplate capacity (10.24 kWh nominal) — this is grid-side energy
    including charge losses, which is what the economics need.
    """
    round_trip_efficiency: float = Field(default=0.764, gt=0.0, le=1.0)
    """Measured grid-to-house round trip, so `cycle_output_kwh` ≈ 8.885 kWh.

    The discharge side is solid: 147 PV-free discharge episodes, median
    0.0987 kWh per SOC point, i.e. 8.88 kWh delivered over a 90-point swing.
    The charge side carries the uncertainty (see `battery_capacity_kwh`).
    Previously assumed 0.90, which overstated every cycle.
    """
    battery_price_eur: float = Field(default=3000.0, ge=0.0)
    battery_cycle_life: int = Field(default=6000, gt=0)
    max_cycles_per_day: int = Field(default=6, ge=0, le=6)
    hours_per_cycle: int = Field(default=2, ge=1)
    """Length of a Charge window in hours. Sizes the charge block only —
    the discharge side is derived from `battery_drain_hours`, not chosen."""
    battery_drain_hours: int = Field(default=7, ge=1, le=12)
    """Hours a full battery feeds the house before it is empty.

    Sets the window a Charge slot is valued against, and the spacing between
    consecutive charges. Deliberately the LONG end of the measured 3.6–7.1 h
    winter range, because the two errors are not symmetric:

    - too SHORT attributes all output to the first, dearest hours when part of
      it is really delivered later at cheap prices → over-values the cycle and
      books cycles that lose money. No gate protects against this, since the
      figure is inflated before the gate sees it.
    - too LONG dilutes with hours where the battery is already empty →
      under-values, forgoing profit but never losing money.

    Currently pinned to an unmeasured winter load — the largest single
    uncertainty in the model, worth roughly a 2x swing in daily value. The
    system has never seen a winter (inverter data begins 2026-04-01), so this
    can only be measured from real cold weather.

    Upper bound 12: larger values silently drop every candidate block and the
    planner goes dark behind a misleading "no cycle nets ..." skip reason.
    """
    min_net_profit_per_cycle_eur: float = Field(default=0.25)
    """Minimum net profit for a cycle to be worth scheduling.

    Originally justified as a ±0.18 EUR allowance for price-forecast error,
    which was always shaky — Elering day-ahead prices for the target day are
    exact, not forecast. Its real job now is to absorb `battery_drain_hours`
    model error, which dominates. Lowering it to ~0.14 would restore the old
    effective strictness now that the valuation is honest, but that trade is
    only worth making once the drain window has been measured.
    """
    buy_margin_eur_per_kwh: float = Field(default=0.05, ge=0.0)
    """Supplier margin added to spot on every imported kWh.

    A tariff fact, not a tuning knob. Needed so a cycle is charged the margin
    on its round-trip losses: `margin * (capacity - output)` ≈ 0.137 EUR per
    cycle at measured constants. The current code omits this entirely.
    """
    stop_sell_threshold_eur_per_kwh: float = Field(default=0.02, ge=0.0)
    """Minimum spot price for a Stop slot to be worth adding.

    When PV is producing and the spot price exceeds this threshold, we'd
    rather let PV export to grid (revenue = spot − supplier margin) than
    let it charge the battery. Below this threshold the export revenue is
    too low to bother — natural Self-use (PV → battery → evening) wins.
    """
    stop_pv_window_start_hour_riga: int = Field(default=6, ge=0, le=23)
    stop_pv_window_end_hour_riga: int = Field(default=20, ge=1, le=24)
    """Riga-local clock hours bracketing the PV-producing window.

    Stop slots are only considered for hours inside this window. Default
    06:00–20:00 covers Latvian sunrise/sunset across all seasons (summer
    is wider, winter narrower — we use the union).
    """
    morning_discharge_target_soc_pct: int = Field(default=15, ge=15, le=100)
    """SOC target written to the Livoltek Discharge slot used as the
    morning block-and-export window.

    The inverter drains battery to grid until this SOC, then holds. The
    floor is 15 — the BMS minimum is 10%, but we keep a 5% safety margin
    on top so any UPS-style backup headroom remains and we don't graze
    the BMS cutoff. After the window ends, PV refills the battery during
    the day for evening Self-use discharge.
    """
    sunny_day_pv_load_multiplier: float = Field(default=1.5, ge=1.0)
    """Margin above load required to commit to the morning Discharge slot.

    Discharge slot is planned only when expected PV ≥ load × this. A margin
    above 1.0 protects against forecast error on borderline cloudy days
    (e.g. cloud_cover_pct=90 still gives high diffuse radiation in May, but
    actual production can drop 30%+ vs forecast). Default 1.5 → PV must
    cover load with 50% headroom before we drain the battery.
    """
    morning_peak_end_multiplier: float = Field(default=2.0, ge=1.0)
    """Threshold for truncating the morning Discharge window.

    Window covers the FIRST contiguous run of daylight hours whose spot
    is above `cheapest_daylight_price × this`. Default 2.0 → if the
    cheapest daylight hour is €0.05, the window stops as soon as prices
    drop to €0.10. Avoids selling at near-cheap rates and overstaying.
    """

    open_meteo_base_url: str = Field(default="https://api.open-meteo.com/v1")
    pv_lat: float = Field(default=56.918)
    pv_lon: float = Field(default=24.043)
    pv_kwh_per_mj_m2: float = Field(default=2.98, gt=0.0)
    expected_daily_load_kwh: float = Field(default=22.0, gt=0.0)
    open_meteo_timeout_s: float = Field(default=15.0)

    ntfy_base_url: str = Field(default="https://ntfy.sh")
    ntfy_topic: str = Field(default="")
    ntfy_token: str = Field(default="")
    ntfy_timeout_s: float = Field(default=10.0)

    livoltek_portal_url: str = Field(default="https://evs.livoltek-portal.com/#/")
    livoltek_username: str = Field(default="")
    livoltek_password: str = Field(default="")
    livoltek_device_model: str = Field(default="HP3-10KD2")
    """Inverter model as printed on the station page's Device List card.

    The card label reads `<serial>(<model>)`, e.g. `HP310K2HAC130295(HP3-10KD2)`.
    We locate the device by this text rather than by its card image.

    History: the nav used to click `img[src*="hp3_online"]`. That selector
    encoded two things it should not have — the portal's image asset name and
    the device's online/offline state. On 2026-09-02 the portal switched device
    card images from `./static/img/hp3_online.*.png` to inlined base64 data
    URIs, the selector stopped matching, and every run died with a 30 s
    Playwright timeout before writing any schedule.
    """
    livoltek_storage_state_path: str = Field(default="browser-data/storage_state.json")
    livoltek_browser_timeout_s: float = Field(default=30.0, gt=0.0)
    livoltek_headless: bool = Field(default=False)

    @property
    def wear_cost_per_cycle_eur(self) -> float:
        return self.battery_price_eur / self.battery_cycle_life

    @property
    def cycle_output_kwh(self) -> float:
        return self.battery_capacity_kwh * self.round_trip_efficiency


def get_settings() -> Settings:
    return Settings()
