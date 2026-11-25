
"""Validated application configuration for HealthOps Intelligence."""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

PROJECT_ROOT = Path(__file__).resolve().parent


class SettingsError(ValueError):
    """Raised when application configuration is invalid or unsafe."""


def _parse_int(value, name, minimum, maximum):
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{name} must be an integer.") from exc

    if not minimum <= result <= maximum:
        raise SettingsError(
            f"{name} must be between {minimum} and {maximum}."
        )
    return result


def _parse_bool(value, name):
    value = str(value).strip().lower()

    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False

    raise SettingsError(f"{name} must be true or false.")


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str
    host: str
    port: int
    max_upload_mb: int
    log_level: str
    data_dir: Path
    llm_provider: str
    gemini_model: str
    llm_enabled: bool
    llm_timeout_seconds: int

    @property
    def max_upload_bytes(self):
        return self.max_upload_mb * 1024 * 1024

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        base_dir: Path | None = None,
    ):
        source = os.environ if environ is None else environ
        root = (base_dir or PROJECT_ROOT).resolve()

        environment = source.get(
            "HEALTHOPS_ENV", "development"
        ).strip().lower()

        if environment not in {"development", "test", "production"}:
            raise SettingsError("Invalid HEALTHOPS_ENV.")

        # Production is intentionally blocked until authentication,
        # authorization and workspace isolation are implemented.
        if environment == "production":
            raise SettingsError(
                "Production mode is disabled until authentication "
                "and tenant isolation are implemented."
            )

        host = source.get(
            "HEALTHOPS_HOST", "127.0.0.1"
        ).strip().lower()

        if host != "localhost":
            try:
                address = ipaddress.ip_address(host)
            except ValueError as exc:
                raise SettingsError(
                    "HEALTHOPS_HOST must be localhost or a loopback IP."
                ) from exc

            if not address.is_loopback or address.version != 4:
                raise SettingsError(
                    "Network binding is disabled. Use 127.0.0.1 "
                    "or localhost until authentication and isolation exist."
                )

        port = _parse_int(
            source.get(
                "HEALTHOPS_PORT",
                source.get("CARELENS_PORT", "8765")
            ),
            "HEALTHOPS_PORT", 1, 65535
        )

        upload = _parse_int(
            source.get("HEALTHOPS_MAX_UPLOAD_MB", "100"),
            "HEALTHOPS_MAX_UPLOAD_MB", 1, 500
        )

        log_level = source.get(
            "HEALTHOPS_LOG_LEVEL", "INFO"
        ).strip().upper()

        if log_level not in {
            "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
        }:
            raise SettingsError("Invalid HEALTHOPS_LOG_LEVEL.")

        provider = source.get(
            "LLM_PROVIDER", "gemini"
        ).strip().lower()

        if provider not in {"gemini", "disabled"}:
            raise SettingsError("Unsupported LLM_PROVIDER.")

        timeout = _parse_int(
            source.get("HEALTHOPS_LLM_TIMEOUT_SECONDS", "30"),
            "HEALTHOPS_LLM_TIMEOUT_SECONDS", 5, 180
        )

        enabled = _parse_bool(
            source.get("HEALTHOPS_LLM_ENABLED", "true"),
            "HEALTHOPS_LLM_ENABLED"
        )

        data_value = Path(
            source.get("HEALTHOPS_DATA_DIR", "instance")
        ).expanduser()

        data_dir = (
            data_value.resolve()
            if data_value.is_absolute()
            else (root / data_value).resolve()
        )

        return cls(
            environment=environment,
            host=host,
            port=port,
            max_upload_mb=upload,
            log_level=log_level,
            data_dir=data_dir,
            llm_provider=provider,
            gemini_model=source.get(
                "GEMINI_MODEL", "gemini-3.5-flash-lite"
            ).strip(),
            llm_enabled=enabled,
            llm_timeout_seconds=timeout,
        )


def load_settings():
    """Load local .env values without overriding shell configuration."""
    if os.environ.get(
        "HEALTHOPS_ENV", "development"
    ).strip().lower() != "production":
        try:
            from dotenv import load_dotenv
            load_dotenv(
                dotenv_path=PROJECT_ROOT / ".env",
                override=False
            )
        except ImportError:
            pass

    return Settings.from_env(
        os.environ,
        base_dir=PROJECT_ROOT
    )
