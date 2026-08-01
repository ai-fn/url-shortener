"""Application settings. Every value is env-overridable; secrets have no default."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, PostgresDsn, RedisDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "url-shortener"
    # No default, and a closed set: an unset ENVIRONMENT must not fall into the branch
    # that publishes /docs, and "prod" must not silently mean "not production".
    environment: Literal["local", "test", "staging", "production"]
    debug: bool = False

    # Public origin, feeding the self-domain loop guard in app/core/url_validation.py.
    # Validated, not a bare str: urlparse yields no host for "short.example.com", which
    # would disarm the guard silently.
    public_base_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8000")

    database_url: PostgresDsn
    redis_url: RedisDsn

    kafka_bootstrap_servers: str = "kafka:9092"
    kafka_clicks_topic: str = "clicks"
    # Bounded: a broker outage drops events instead of growing memory.
    click_queue_maxsize: int = 10_000

    clickhouse_host: str = "clickhouse"
    clickhouse_port: int = 8123
    clickhouse_database: str = "analytics"
    clickhouse_user: str = "default"
    clickhouse_password: str = ""

    # SecretStr, not str: pydantic embeds the offending value in ValidationError, so a
    # too-short key would be printed verbatim into logs. Read with .get_secret_value().
    secret_key: SecretStr = Field(min_length=32)
    # HMAC key for IP hashing. Rotated monthly; rotation intentionally breaks
    # unique-visitor correlation across the boundary.
    ip_hash_key: SecretStr = Field(min_length=32)

    access_token_expire_minutes: int = 60
    # Never caller-controlled: app/core/security.py always passes this as the sole
    # entry of `algorithms=[...]` to jwt.decode, closing the alg-confusion class of bug.
    jwt_algorithm: str = "HS256"

    # Long TTL plus explicit DEL on write; a short TTL does not help the one hot key.
    link_cache_ttl_seconds: int = 86_400
    link_cache_negative_ttl_seconds: int = 60

    rate_limit_create_per_minute: int = 30
    rate_limit_notfound_per_minute: int = 120
    # Fail closed like create: argon2id hashing is deliberately expensive, so an
    # unthrottled endpoint is a CPU/memory amplification DoS via the hash itself.
    rate_limit_login_per_minute: int = 10
    rate_limit_register_per_minute: int = 10

    geoip_database_path: str = "/data/GeoLite2-City.mmdb"

    @property
    def public_host(self) -> str:
        """Lowercased host, for the loop guard. Raises rather than returning "": an
        empty host matches nothing and makes us a self-referential open redirect."""
        host = self.public_base_url.host
        if not host:
            raise ValueError(f"public_base_url has no host: {self.public_base_url}")
        return host.lower()


@lru_cache
def get_settings() -> Settings:
    """Parsed once per process. Call `cache_clear()` when the env changes underneath."""
    return Settings()
