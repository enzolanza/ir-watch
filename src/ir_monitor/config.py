"""Configuration loading: environment variables + config/companies.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

load_dotenv(override=False)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COMPANIES_FILE = PROJECT_ROOT / "config" / "companies.yaml"

LOCAL_TZ = ZoneInfo(os.getenv("TIMEZONE", "America/Sao_Paulo"))

USER_AGENT = os.getenv(
    "USER_AGENT",
    "ir-watch/1.0 (peer disclosure monitor; contact: set CONTACT_EMAIL)",
)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class EmailSettings:
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    email_from: str | None = None
    email_to: tuple[str, ...] = ()
    use_tls: bool = True
    subject_prefix: str = "[IR Watch]"

    @property
    def configured(self) -> bool:
        return bool(self.smtp_host and self.email_from and self.email_to)


@dataclass(frozen=True)
class Settings:
    database_url: str
    email: EmailSettings
    http_timeout: int = 30
    http_retries: int = 3
    alert_on_revision: bool = False
    playwright_enabled: bool = True
    companies_file: Path = DEFAULT_COMPANIES_FILE
    log_level: str = "INFO"
    log_format: str = "text"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    recipients = tuple(
        addr.strip()
        for addr in (os.getenv("EMAIL_TO") or "").split(",")
        if addr.strip()
    )
    email = EmailSettings(
        smtp_host=os.getenv("SMTP_HOST") or None,
        smtp_port=_env_int("SMTP_PORT", 587),
        smtp_user=os.getenv("SMTP_USER") or None,
        smtp_password=os.getenv("SMTP_PASSWORD") or None,
        email_from=os.getenv("EMAIL_FROM") or None,
        email_to=recipients,
        use_tls=_env_bool("SMTP_USE_TLS", True),
        subject_prefix=os.getenv("EMAIL_SUBJECT_PREFIX", "[IR Watch]"),
    )
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///ir_watch.db"),
        email=email,
        http_timeout=_env_int("HTTP_TIMEOUT", 30),
        http_retries=_env_int("HTTP_RETRIES", 3),
        alert_on_revision=_env_bool("ALERT_ON_REVISION", False),
        playwright_enabled=_env_bool("PLAYWRIGHT_ENABLED", True),
        companies_file=Path(os.getenv("COMPANIES_FILE", str(DEFAULT_COMPANIES_FILE))),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_format=os.getenv("LOG_FORMAT", "text"),
    )


@dataclass(frozen=True)
class CompanyConfig:
    key: str
    name: str
    monitor: str
    enabled: bool = True
    primary_url: str | None = None
    options: dict[str, Any] = field(default_factory=dict)

    def option(self, name: str, default: Any = None) -> Any:
        return self.options.get(name, default)


@lru_cache(maxsize=1)
def load_companies() -> dict[str, CompanyConfig]:
    path = get_settings().companies_file
    if not path.exists():  # pragma: no cover - misconfiguration guard
        raise FileNotFoundError(f"companies file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    companies: dict[str, CompanyConfig] = {}
    for key, payload in (raw.get("companies") or {}).items():
        payload = payload or {}
        companies[key] = CompanyConfig(
            key=key,
            name=payload.get("name", key),
            monitor=payload.get("monitor", key),
            enabled=bool(payload.get("enabled", True)),
            primary_url=payload.get("primary_url"),
            options={
                k: v
                for k, v in payload.items()
                if k not in {"name", "monitor", "enabled", "primary_url"}
            },
        )
    return companies


def reset_caches() -> None:
    """Used by tests after monkeypatching environment variables."""
    get_settings.cache_clear()
    load_companies.cache_clear()
