"""Application configuration via pydantic-settings."""

from __future__ import annotations

import functools

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven configuration for the pickme bot."""

    telegram_bot_token: str = Field(min_length=1)
    hetzner_api_key: str = ""
    hetzner_base_url: str = "https://inference.hetzner.com/api/v1"
    llm_primary_model: str = "Qwen/Qwen3.6-35B-A3B-FP8"
    llm_fallback_model: str = "Qwen3.8-27B"
    opencode_api_key: str = ""
    zen_base_url: str = "https://opencode.ai/zen/v1"
    zen_free_model: str = "mimo-v2.5-free"
    go_base_url: str = "https://opencode.ai/zen/go/v1"
    go_model: str = "mimo-v2.5"
    go_enabled: bool = True
    memory_batch_size: int = 50
    memory_ttl_hours: int = 6
    summarize_default: int = 100
    summarize_cap: int = 500
    router_fastpath: bool = True
    rate_limit_per_60s: int = 8
    llm_concurrency: int = 2
    log_level: str = "INFO"
    data_dir: str = "data"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@functools.lru_cache
def get_settings() -> Settings:
    """Return cached application settings loaded from environment / .env."""
    return Settings()  # type: ignore[call-arg]
