from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "local"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://booking:booking@localhost:5432/booking"
    redis_url: str = "redis://localhost:6379/0"

    hold_ttl_seconds: int = 600
    # Deliberately short: the seat map is the most contended read in the system
    # and a stale one costs a user a failed hold attempt, so entries expire fast
    # even if an invalidation is missed.
    seatmap_cache_ttl_seconds: int = 10

    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # The domain model carries no pricing, so a flat per-seat price stands in.
    # Per-section/tier pricing would live on Section when it's needed.
    seat_price: Decimal = Decimal("250.00")
    currency: str = "INR"

    # Sized for the contended case: many users racing for the same seats means
    # many short-lived connections at once, and the default pool of 5 would
    # queue them behind each other.
    db_pool_size: int = 20
    db_max_overflow: int = 20

    otel_exporter_otlp_endpoint: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
