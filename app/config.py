"""Configuração por variáveis de ambiente (nenhum segredo no código ou no repositório)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Mapping

from .content.engine import ContentLimits


class ConfigError(RuntimeError):
    pass


def _int(env: Mapping[str, str], key: str, default: int, lo: int = 0, hi: int = 10**9) -> int:
    raw = env.get(key)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} deve ser um inteiro (valor recebido: {raw!r}).") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{key} deve estar entre {lo} e {hi}.")
    return value


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "sim")


@dataclass(frozen=True)
class Settings:
    env: str = "development"
    enrollment_key: str = ""
    ephemeral_key: bool = False
    database_url: str = ""
    require_https: bool = False
    trust_proxy: bool = False
    max_request_bytes: int = 4_000_000
    max_text_chars: int = 20_000
    max_html_bytes: int = 200_000
    max_image_bytes: int = 2_000_000
    max_image_pixels: int = 8_000_000
    rate_limit_per_minute: int = 60
    control_rate_limit_per_minute: int = 600
    ip_rate_limit_per_minute: int = 20
    max_devices: int = 50
    queue_ttl_seconds: int = 3600
    queue_max_per_device: int = 100
    status_retention_seconds: int = 86400
    redeliver_after_seconds: int = 20
    max_delivery_attempts: int = 5
    longpoll_max_wait: int = 25
    sse_max_seconds: int = 120
    sse_keepalive_seconds: int = 15
    token_ttl_days: int = 90
    log_level: str = "INFO"
    log_json: bool = False

    def content_limits(self) -> ContentLimits:
        return ContentLimits(self.max_text_chars, self.max_html_bytes, self.max_image_bytes, self.max_image_pixels)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        mode = env.get("OMNILINK_ENV", "development").strip().lower()
        if mode not in ("development", "production", "test"):
            raise ConfigError("OMNILINK_ENV deve ser 'development', 'production' ou 'test'.")
        production = mode == "production"
        key, ephemeral = env.get("OMNILINK_ENROLLMENT_KEY", ""), False
        if production and len(key) < 16:
            raise ConfigError("Em produção, defina OMNILINK_ENROLLMENT_KEY com pelo menos 16 caracteres.")
        if not key:
            key, ephemeral = secrets.token_urlsafe(18), True
        elif len(key) < 8:
            raise ConfigError("OMNILINK_ENROLLMENT_KEY deve ter pelo menos 8 caracteres.")
        return cls(
            env=mode, enrollment_key=key, ephemeral_key=ephemeral,
            database_url=env.get("OMNILINK_DATABASE_URL") or env.get("DATABASE_URL") or "",
            require_https=_bool(env, "OMNILINK_REQUIRE_HTTPS", production),
            trust_proxy=_bool(env, "OMNILINK_TRUST_PROXY", env.get("RENDER") == "true"),
            max_request_bytes=_int(env, "OMNILINK_MAX_REQUEST_BYTES", 4_000_000, 1_000, 50_000_000),
            max_text_chars=_int(env, "OMNILINK_MAX_TEXT_CHARS", 20_000, 1, 1_000_000),
            max_html_bytes=_int(env, "OMNILINK_MAX_HTML_BYTES", 200_000, 1, 5_000_000),
            max_image_bytes=_int(env, "OMNILINK_MAX_IMAGE_BYTES", 2_000_000, 1, 20_000_000),
            max_image_pixels=_int(env, "OMNILINK_MAX_IMAGE_PIXELS", 8_000_000, 1, 100_000_000),
            rate_limit_per_minute=_int(env, "OMNILINK_RATE_LIMIT_PER_MINUTE", 60, 0, 100_000),
            control_rate_limit_per_minute=_int(env, "OMNILINK_CONTROL_RATE_LIMIT_PER_MINUTE", 600, 0, 1_000_000),
            ip_rate_limit_per_minute=_int(env, "OMNILINK_IP_RATE_LIMIT_PER_MINUTE", 20, 0, 100_000),
            max_devices=_int(env, "OMNILINK_MAX_DEVICES", 50, 1, 10_000),
            queue_ttl_seconds=_int(env, "OMNILINK_QUEUE_TTL_SECONDS", 3600, 5, 7 * 86400),
            queue_max_per_device=_int(env, "OMNILINK_QUEUE_MAX_PER_DEVICE", 100, 1, 10_000),
            status_retention_seconds=_int(env, "OMNILINK_STATUS_RETENTION_SECONDS", 86400, 60, 30 * 86400),
            longpoll_max_wait=_int(env, "OMNILINK_LONGPOLL_MAX_WAIT", 25, 1, 55),
            sse_max_seconds=_int(env, "OMNILINK_SSE_MAX_SECONDS", 120, 5, 600),
            token_ttl_days=_int(env, "OMNILINK_TOKEN_TTL_DAYS", 90, 0, 3650),
            log_level=env.get("OMNILINK_LOG_LEVEL", "INFO").upper(),
            log_json=_bool(env, "OMNILINK_LOG_JSON", production),
        )
