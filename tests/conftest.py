import hashlib
import hmac
import os
import uuid
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.bookings import models as bookings_models  # noqa: F401
from app.core.cache import get_redis
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.core.models import Base
from app.inventory.models import Event, Seat, SeatInventory, Section, Show, Venue
from app.main import create_app
from app.payments import models as payments_models  # noqa: F401
from app.payments.provider import FakePaymentProvider
from app.payments.router import get_payment_provider

SEATS_PER_TEST_SHOW = 3


@pytest.fixture(scope="session")
def database_url() -> Generator[str]:
    """A real Postgres to test against — never SQLite.

    The hold flow's correctness depends on Postgres-specific behaviour (READ
    COMMITTED re-evaluating an UPDATE's WHERE clause after a concurrent commit),
    so testing against anything else would prove nothing.

    Two ways to supply one: set TEST_DATABASE_URL to point at a Postgres you
    already run, or leave it unset and let testcontainers start one, which
    needs Docker.
    """
    configured = os.getenv("TEST_DATABASE_URL")
    if configured:
        # The fixtures below drop every table after each test. Pointing this at
        # a development database wipes it, so refuse anything not named like a
        # throwaway. (Learned the hard way.)
        database_name = urlsplit(configured).path.lstrip("/")
        if "test" not in database_name:
            raise RuntimeError(
                f"TEST_DATABASE_URL points at database {database_name!r}, which is not "
                "named like a test database. The suite drops all tables — refusing to run."
            )
        yield configured
        return

    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as container:
        yield container.get_connection_url(driver="asyncpg")


@pytest_asyncio.fixture
async def engine(database_url: str) -> AsyncGenerator[AsyncEngine]:
    # Pool sized to match app config, so concurrency tests exercise real
    # database contention rather than queueing on a 5-connection default.
    test_engine = create_async_engine(database_url, pool_size=20, max_overflow=20)
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield test_engine
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await test_engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(scope="session")
def redis_url() -> Generator[str]:
    """Real Redis, same reasoning as Postgres: TEST_REDIS_URL if you run one,
    otherwise testcontainers starts one."""
    configured = os.getenv("TEST_REDIS_URL")
    if configured:
        yield configured
        return

    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest_asyncio.fixture
async def redis_client(redis_url: str) -> AsyncGenerator[Redis]:
    client: Redis = Redis.from_url(redis_url, decode_responses=True)
    # Each test starts from an empty cache so one test's cached seat map can
    # never satisfy another test's read.
    await client.flushall()
    yield client
    await client.aclose()


WEBHOOK_SECRET = "test-webhook-secret"


@pytest.fixture
def payment_provider() -> FakePaymentProvider:
    return FakePaymentProvider()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        razorpay_key_id="rzp_test_fake",
        razorpay_key_secret="fake-secret",
        razorpay_webhook_secret=WEBHOOK_SECRET,
    )


@pytest_asyncio.fixture
async def app(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Redis,
    payment_provider: FakePaymentProvider,
    settings: Settings,
) -> FastAPI:
    application = create_app()

    async def _get_session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = _get_session
    application.dependency_overrides[get_redis] = lambda: redis_client
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_payment_provider] = lambda: payment_provider
    return application


def sign_webhook(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def capture_event(order_ref: str, payment_ref: str = "pay_testpayment01") -> dict[str, Any]:
    """A Razorpay payment.captured event, shaped like the real thing."""
    return {
        "event": "payment.captured",
        "payload": {"payment": {"entity": {"id": payment_ref, "order_id": order_ref}}},
    }


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@dataclass(frozen=True)
class SeededShow:
    show_id: uuid.UUID
    seat_ids: list[uuid.UUID]


@pytest_asyncio.fixture
async def seeded_show(session_factory: async_sessionmaker[AsyncSession]) -> SeededShow:
    """A show with AVAILABLE seat inventory, written directly to the database.

    Seeded through the models rather than an API because the CRUD endpoints
    (build order step 2) don't exist yet, and the hold flow shouldn't have to
    wait on them.
    """
    async with session_factory() as session:
        venue = Venue(name="Test Venue")
        section = Section(venue=venue, name="Stalls")
        seats = [
            Seat(section=section, row_label="A", seat_number=n)
            for n in range(1, SEATS_PER_TEST_SHOW + 1)
        ]
        event = Event(name="Test Event")
        show = Show(event=event, venue=venue, start_time=datetime.now(UTC) + timedelta(days=1))

        session.add_all([venue, section, *seats, event, show])
        # Populates the generated primary keys that the inventory rows below
        # reference, without ending the transaction.
        await session.flush()

        session.add_all([SeatInventory(show_id=show.id, seat_id=seat.id) for seat in seats])
        await session.commit()

        return SeededShow(show_id=show.id, seat_ids=[seat.id for seat in seats])
