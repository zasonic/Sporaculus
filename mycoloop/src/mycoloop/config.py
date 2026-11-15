"""Runtime configuration. Everything is env-overridable with MYCOLOOP_ prefix."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent
CONTRACTS_DIR = PACKAGE_ROOT / "contracts"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MYCOLOOP_", env_file=".env", extra="ignore", protected_namespaces=()
    )

    # --- persistence -------------------------------------------------------
    # Dev/test default is SQLite so the whole stack runs with zero services.
    # Production: postgresql+psycopg://mycoloop:mycoloop@db:5432/mycoloop
    database_url: str = "sqlite+pysqlite:///./mycoloop.dev.db"
    timescale_enabled: bool = True
    create_schema_on_boot: bool = True  # dev convenience; prod uses alembic

    # --- messaging HAL ----------------------------------------------------
    bus: Literal["loopback", "mqtt"] = "loopback"
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_client_id: str = "mycoloop-backend"
    mqtt_keepalive: int = 30

    # --- auth -------------------------------------------------------------
    operator_api_key: str = "dev-operator-key"
    admin_api_key: str = "dev-admin-key"
    device_token_secret: str = "dev-device-secret-change-me"

    # --- gateway / control timing ----------------------------------------
    heartbeat_timeout_s: int = 60
    command_ttl_s: int = 60
    command_max_attempts: int = 3
    command_retry_backoff_s: float = 5.0
    watchdog_timeout_s: int = 60
    sweep_interval_s: float = 1.0
    background_workers: bool = True

    # --- safety -----------------------------------------------------------
    envelope_path: Path = CONTRACTS_DIR / "safety-envelope.yaml"
    dry_run: bool = False

    # --- simulation -------------------------------------------------------
    sim_autostart: bool = False
    sim_device_id: str = "chamber-01"
    sim_speed: float = 4.0
    sim_enabled: bool = True  # exposes /devices/{id}/sim/* fault-injection routes

    # --- misc -------------------------------------------------------------
    log_level: str = "INFO"
    cors_origins: list[str] = ["*"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
