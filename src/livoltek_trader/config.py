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

    battery_capacity_kwh: float = Field(default=5.12, gt=0.0)
    round_trip_efficiency: float = Field(default=0.90, gt=0.0, le=1.0)
    battery_price_eur: float = Field(default=3000.0, ge=0.0)
    battery_cycle_life: int = Field(default=6000, gt=0)
    max_cycles_per_day: int = Field(default=2, ge=0)
    hours_per_cycle: int = Field(default=2, ge=1)
    min_net_profit_per_cycle_eur: float = Field(default=0.10)

    open_meteo_base_url: str = Field(default="https://api.open-meteo.com/v1")
    pv_lat: float = Field(default=56.918)
    pv_lon: float = Field(default=24.043)
    pv_kwh_per_mj_m2: float = Field(default=2.98, gt=0.0)
    expected_daily_load_kwh: float = Field(default=22.0, gt=0.0)
    open_meteo_timeout_s: float = Field(default=15.0)

    @property
    def wear_cost_per_cycle_eur(self) -> float:
        return self.battery_price_eur / self.battery_cycle_life

    @property
    def cycle_output_kwh(self) -> float:
        return self.battery_capacity_kwh * self.round_trip_efficiency


def get_settings() -> Settings:
    return Settings()
