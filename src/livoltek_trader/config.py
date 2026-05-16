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

    battery_capacity_kwh: float = Field(default=10.24, gt=0.0)
    round_trip_efficiency: float = Field(default=0.90, gt=0.0, le=1.0)
    battery_price_eur: float = Field(default=3000.0, ge=0.0)
    battery_cycle_life: int = Field(default=6000, gt=0)
    max_cycles_per_day: int = Field(default=6, ge=0, le=6)
    hours_per_cycle: int = Field(default=2, ge=1)
    min_net_profit_per_cycle_eur: float = Field(default=0.25)
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
