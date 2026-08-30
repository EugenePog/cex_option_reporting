"""Environment-driven configuration (pydantic-settings). One Settings object, imported everywhere."""
from __future__ import annotations

import os
from functools import lru_cache

from dotenv import dotenv_values
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = Field(
        default="postgresql+psycopg://cex:cex@localhost:5432/cex_option_reporting",
        alias="DATABASE_URL",
    )

    # Security
    credentials_fernet_key: str = Field(default="", alias="CREDENTIALS_FERNET_KEY")
    app_secret_key: str = Field(default="", alias="APP_SECRET_KEY")

    # OKX dev account "K" — single-account convenience for local testing.
    # In production, per-account creds live encrypted in the DB (core.cex_account); these env
    # vars are just a quick way to point the OKX adapter at one real account during development.
    okx_k_api_key: str = Field(default="", alias="OKX_K_API_KEY")
    okx_k_api_secret: str = Field(default="", alias="OKX_K_API_SECRET")
    okx_k_passphrase: str = Field(default="", alias="OKX_K_PASSPHRASE")
    okx_k_flag: str = Field(default="0", alias="OKX_K_FLAG")  # "0" live, "1" demo

    def okx_k_credentials(self) -> "Credentials":
        """Build a connector Credentials object from the OKX_K_* env vars."""
        from app.connectors.base import Credentials

        return Credentials(
            api_key=self.okx_k_api_key,
            api_secret=self.okx_k_api_secret,
            passphrase=self.okx_k_passphrase,
            flag=self.okx_k_flag,
        )

    # --- Multi-account credentials ---------------------------------------- #
    # Each core.cex_account has a `label` (e.g. "OKX_K", "OKX_M_HEDGE"). Its API
    # credentials live in the environment under that label as a prefix:
    #     <LABEL>_API_KEY / <LABEL>_API_SECRET / <LABEL>_PASSPHRASE / <LABEL>_FLAG
    # This is the multi-account convention: one credential block per account.
    # (In production these move to the encrypted columns on core.cex_account.)
    def _env_map(self) -> dict[str, str]:
        """Merged view of the .env file plus the process environment (process env wins).

        pydantic-settings only loads *declared* fields from .env, so per-account
        credentials keyed by an arbitrary label are invisible to the Settings schema.
        Read them straight from the .env file and os.environ instead.
        """
        merged: dict[str, str] = {}
        env_file = self.model_config.get("env_file")
        if env_file:
            merged.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
        merged.update(os.environ)
        return merged

    def credentials_for_label(self, label: str) -> "Credentials":
        """Build a connector Credentials object from the <LABEL>_* env vars.

        For label "OKX_M_HEDGE" this reads OKX_M_HEDGE_API_KEY / _API_SECRET /
        _PASSPHRASE / _FLAG. Missing values come back empty so the caller can
        detect an unconfigured account and skip it.
        """
        from app.connectors.base import Credentials

        env = self._env_map()
        prefix = label.strip().upper()

        def _clean(value: str | None) -> str:
            return (value or "").strip().strip("'\"").strip()

        return Credentials(
            api_key=_clean(env.get(f"{prefix}_API_KEY")),
            api_secret=_clean(env.get(f"{prefix}_API_SECRET")),
            passphrase=_clean(env.get(f"{prefix}_PASSPHRASE")),
            flag=_clean(env.get(f"{prefix}_FLAG")) or "0",
        )

    # Pipeline cadence (seconds) for `pipeline --loop`
    pipeline_interval_seconds: int = Field(default=300, alias="PIPELINE_INTERVAL_SECONDS")

    # Snapshot collector: point-in-time data (balance/positions/margin/greeks), MULTIPLE runs/day.
    # Comma-separated UTC times "HH:MM,HH:MM,...".
    snapshot_times_utc: str = Field(default="00:00,06:00,12:00,18:00", alias="SNAPSHOT_TIMES_UTC")

    # History collector: fills/closed-positions/bills, ONE run/day at this UTC time (HH:MM), limited depth.
    ingest_time_utc: str = Field(default="10:00", alias="INGEST_TIME_UTC")       # history daily run time (UTC)
    ingest_daily_lookback_days: int = Field(default=1, alias="INGEST_DAILY_LOOKBACK_DAYS")  # today + N prior days of history

    okx_k_account_label: str = Field(default="OKX_K", alias="OKX_K_ACCOUNT_LABEL")  # tag stored on bronze rows

    @staticmethod
    def _parse_hhmm(token: str) -> tuple[int, int]:
        """'10:00' -> (10, 0); a bare '10' -> (10, 0). Tolerates quotes/spaces."""
        token = token.strip().strip("'\"").strip()
        if ":" in token:
            hh, mm = token.split(":")
            return int(hh), int(mm)
        return int(token), 0

    def snapshot_time_tuples(self) -> list[tuple[int, int]]:
        """Parse SNAPSHOT_TIMES_UTC into [(hour, minute), ...]. Tolerates braces/quotes/spaces."""
        cleaned = self.snapshot_times_utc.replace("{", "").replace("}", "")
        return [self._parse_hhmm(tok) for tok in cleaned.split(",")
                if tok.strip().strip("'\"").strip()]

    def ingest_time_tuple(self) -> tuple[int, int]:
        """Parse INGEST_TIME_UTC ('HH:MM') into (hour, minute)."""
        return self._parse_hhmm(self.ingest_time_utc)

    # Web
    web_host: str = Field(default="0.0.0.0", alias="WEB_HOST")
    web_port: int = Field(default=8000, alias="WEB_PORT")

    # Logging
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


@lru_cache
def get_settings() -> Settings:
    """Cached singleton accessor."""
    return Settings()
