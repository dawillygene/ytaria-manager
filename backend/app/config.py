"""Application configuration, loaded from ``YTARIA_*`` environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

MIB = 1024 * 1024
GIB = 1024 * MIB

_INSECURE_SECRETS = {"", "change-me", "changeme", "dev-secret"}


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="YTARIA_", env_file=".env", extra="ignore")

    env: Literal["development", "test", "production"] = "development"
    secret_key: str = "dev-secret"
    database_url: str = "postgresql+psycopg://ytaria:dev@127.0.0.1:55432/ytaria"
    redis_url: str = "redis://127.0.0.1:56379/0"
    # ``memory://`` keeps rate-limit counters in-process (tests / single process dev only).
    ratelimit_url: str = ""
    media_root: Path = Path("./data/media")
    scratch_root: Path = Path("./data/scratch")

    # --- HTTP surface -----------------------------------------------------------------
    cors_origins: Annotated[list[str], NoDecode] = []
    cookie_secure: bool = False
    cookie_samesite: Literal["strict", "lax"] = "strict"
    max_body_bytes: int = 64 * 1024
    docs_enabled: bool = True

    # --- Auth -------------------------------------------------------------------------
    registration_mode: Literal["open", "invite", "closed"] = "open"
    registration_code: str = ""
    access_ttl_seconds: int = 15 * 60
    refresh_ttl_seconds: int = 30 * 24 * 3600
    refresh_reuse_grace_seconds: int = 10
    password_min_length: int = 10
    # rate limits: (max requests, window seconds)
    rl_login_ip: str = "20/300"
    rl_login_account: str = "8/300"
    rl_register_ip: str = "10/3600"
    rl_refresh_ip: str = "60/300"
    rl_inspect_user: str = "30/3600"
    rl_job_create_user: str = "60/3600"
    rl_ticket_user: str = "120/300"

    # --- Job limits -------------------------------------------------------------------
    max_active_jobs_per_user: int = 5
    max_concurrent_jobs_global: int = 2
    max_concurrent_jobs_per_user: int = 1
    max_pending_inspections_per_user: int = 3
    max_attempts: int = 3
    retry_backoff_base_seconds: int = 30
    retry_backoff_cap_seconds: int = 900
    job_max_runtime_seconds: int = 2 * 3600
    lease_seconds: int = 60
    heartbeat_seconds: int = 5
    kill_grace_seconds: int = 10
    max_media_duration_seconds: int = 4 * 3600
    max_file_bytes: int = 2 * GIB
    user_quota_bytes: int = 10 * GIB
    min_free_disk_bytes: int = 2 * GIB
    dispatch_stale_seconds: int = 600
    reconcile_interval_seconds: int = 15
    cleanup_interval_seconds: int = 900

    # --- Retention --------------------------------------------------------------------
    completed_retention_hours: int = 7 * 24
    failed_partial_retention_hours: int = 24
    paused_max_age_hours: int = 72
    inspection_ttl_seconds: int = 30 * 60
    download_ticket_ttl_seconds: int = 120
    transfer_active_window_seconds: int = 90

    # --- Engine / egress --------------------------------------------------------------
    yt_dlp_bin: str = "yt-dlp"
    aria2c_bin: str = "aria2c"
    ffmpeg_bin: str = "ffmpeg"
    inspect_timeout_seconds: int = 60
    # HTTP(S) forward proxy that enforces the destination policy (see docs/SECURITY.md).
    egress_proxy_url: str = ""
    # Explicit escape hatch for local development only. Never set in production.
    allow_direct_egress: bool = False
    allowed_source_hosts: Annotated[list[str], NoDecode] = [
        "youtube.com",
        "youtu.be",
        "vimeo.com",
        "dailymotion.com",
        "archive.org",
    ]
    allowed_ports: Annotated[list[int], NoDecode] = [80, 443]

    @field_validator("cors_origins", "allowed_source_hosts", mode="before")
    @classmethod
    def _csv_strings(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("allowed_ports", mode="before")
    @classmethod
    def _csv_ints(cls, value: object) -> object:
        value = _split_csv(value)
        if isinstance(value, list):
            return [int(v) for v in value]
        return value

    @model_validator(mode="after")
    def _production_guards(self) -> "Settings":
        if self.env == "production":
            problems: list[str] = []
            if self.secret_key in _INSECURE_SECRETS or len(self.secret_key) < 32:
                problems.append("YTARIA_SECRET_KEY must be set to a random value of at least 32 characters")
            if not self.cookie_secure:
                problems.append("YTARIA_COOKIE_SECURE must be true (serve over HTTPS)")
            if not self.egress_proxy_url and not self.allow_direct_egress:
                problems.append("YTARIA_EGRESS_PROXY_URL is required (workers must not have direct egress)")
            if self.allow_direct_egress:
                problems.append("YTARIA_ALLOW_DIRECT_EGRESS must not be enabled in production")
            if problems:
                raise ValueError("; ".join(problems))
        return self

    @property
    def effective_ratelimit_url(self) -> str:
        return self.ratelimit_url or self.redis_url

    @property
    def in_production(self) -> bool:
        return self.env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def parse_rate(spec: str) -> tuple[int, int]:
    """``"20/300"`` -> (20 requests, 300 seconds)."""
    count, _, window = spec.partition("/")
    return int(count), int(window)
