"""Application configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(key)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = _env(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Static and dynamic application settings."""

    llm_provider: str = field(default_factory=lambda: (_env("LLM_PROVIDER", "openai") or "openai").lower())
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "gpt-4o-mini") or "gpt-4o-mini")
    llm_api_key: Optional[str] = field(default_factory=lambda: _env("LLM_API_KEY"))
    # Accept either LLM_API_BASE (canonical) or LLM_BASE_URL (OpenAI SDK style).
    llm_api_base: Optional[str] = field(
        default_factory=lambda: _env("LLM_API_BASE") or _env("LLM_BASE_URL")
    )
    llm_timeout_seconds: float = field(default_factory=lambda: _env_float("LLM_TIMEOUT_SECONDS", 20.0))
    llm_temperature: float = field(default_factory=lambda: _env_float("LLM_TEMPERATURE", 0.0))

    solver_time_limit_seconds: float = field(default_factory=lambda: _env_float("SOLVER_TIME_LIMIT_SECONDS", 15.0))

    fallback_offline: bool = field(default_factory=lambda: (_env("OFFLINE_FALLBACK", "true") or "true").lower() == "true")

    log_level: str = field(default_factory=lambda: (_env("LOG_LEVEL", "INFO") or "INFO").upper())

    app_host: str = field(default_factory=lambda: _env("APP_HOST", "0.0.0.0") or "0.0.0.0")
    app_port: int = field(default_factory=lambda: _env_int("APP_PORT", 8000))

    tolerance: float = field(default_factory=lambda: _env_float("VALIDATOR_TOLERANCE", 0.01))


SETTINGS = Settings()
