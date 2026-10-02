"""100 concurrent users against real Postgres.

The scenario calude.md names for load testing: a crowd converging on the same
small block of seats the instant a sale opens. These assert correctness under
that load — throughput numbers come from the Locust run in build-order step 6.
"""

import asyncio
import time
import uuid
from collections import Counter

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bookings.models import Booking, BookingStatus
from app.inventory.models import Event, Seat, SeatInventory, SeatStatus, Section, Show, Venue

from .conftest import SeededShow

CONCURRENT_USERS = 100
SEAT_BLOCK = 50


@pytest_asyncio.fixture
async def big_show(session_factory: async_sessionmaker[AsyncSession]) -> SeededShow:
    from datetime import UTC, datetime, timedelta

    async with session_factory() as session:
        venue = Venue(name="Arena")
        section = Section(venue=venue, name="Floor")
        seats = [
            Seat(section=section, row_label="A", seat_number=n)
            for n in range(1, SEAT_BLOCK + 1)
        ]
        event = Event(name="Sold Out Show")
        show = Show(event=event, venue=venue, start_time=datetime.now(UTC) + timedelta(days=7))

        session.add_all([venue, section, *seats, event, show])
        await session.flush()
        session.add_all([SeatInventory(show_id=show.id, seat_id=seat.id) for seat in seats])
        await session.commit()

        return SeededShow(show_id=show.id, seat_ids=[seat.id for seat in seats])


async def test_100_users_racing_for_50_seats(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    big_show: SeededShow,
) -> None:
    """Exactly two users per seat, so exactly half must win and half must lose."""

    async def attempt(index: int) -> int:
        seat_id = big_show.seat_ids[index % SEAT_BLOCK]
        response = await client.post(
            f"/shows/{big_show.show_id}/holds",
            json={
                "seat_ids": [str(seat_id)],
                "idempotency_key": uuid.uuid4().hex,
                "user_id": str(uuid.uuid4()),
            },
        )
        return response.status_code

    started = time.perf_counter()
    statuses = await asyncio.gather(*(attempt(i) for i in range(CONCURRENT_USERS)))
    elapsed = time.perf_counter() - started

    counts = Counter(statuses)
    print(
        f"\n{CONCURRENT_USERS} users / {SEAT_BLOCK} seats in {elapsed:.2f}s "
        f"({CONCURRENT_USERS / elapsed:.0f} req/s) -> {dict(counts)}"
    )

    # No request may fail for any reason other than losing a seat race.
    assert set(counts) <= {201, 409}, f"unexpected statuses: {dict(counts)}"
    assert counts[201] == SEAT_BLOCK
    assert counts[409] == CONCURRENT_USERS - SEAT_BLOCK

    async with session_factory() as session:
        held = (
            await session.execute(
                select(func.count(SeatInventory.id)).where(
                    SeatInventory.show_id == big_show.show_id,
                    SeatInventory.status == SeatStatus.HELD,
                )
            )
        ).scalar_one()
        distinct_holders = (
            await session.execute(
                select(func.count(func.distinct(SeatInventory.held_by))).where(
                    SeatInventory.show_id == big_show.show_id,
                    SeatInventory.status == SeatStatus.HELD,
                )
            )
        ).scalar_one()
        bookings = (
            (await session.execute(select(Booking))).scalars().all()
        )

    assert held == SEAT_BLOCK
    # One distinct winner per seat: nobody's hold overwrote anybody else's.
    assert distinct_holders == SEAT_BLOCK
    # Every winner has a booking and every loser has none.
    assert len(bookings) == SEAT_BLOCK
    assert {b.status for b in bookings} == {BookingStatus.PENDING}


async def test_100_users_racing_for_one_seat(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    big_show: SeededShow,
) -> None:
    """Maximum contention: one seat, a hundred claimants, one winner."""
    contested = big_show.seat_ids[0]

    async def attempt() -> int:
        response = await client.post(
            f"/shows/{big_show.show_id}/holds",
            json={
                "seat_ids": [str(contested)],
                "idempotency_key": uuid.uuid4().hex,
                "user_id": str(uuid.uuid4()),
            },
        )
        return response.status_code

    statuses = await asyncio.gather(*(attempt() for _ in range(CONCURRENT_USERS)))
    counts = Counter(statuses)

    assert counts[201] == 1, f"expected a single winner, got {dict(counts)}"
    assert counts[409] == CONCURRENT_USERS - 1

    async with session_factory() as session:
        seat = (
            await session.execute(
                select(SeatInventory).where(SeatInventory.seat_id == contested)
            )
        ).scalar_one()
        booking_count = (await session.execute(select(func.count(Booking.id)))).scalar_one()

    assert seat.status == SeatStatus.HELD
    assert booking_count == 1


async def test_100_concurrent_seat_map_reads_stay_consistent(
    client: AsyncClient, big_show: SeededShow
) -> None:
    """The cache must not hand different users contradictory seat maps."""
    await client.post(
        f"/shows/{big_show.show_id}/holds",
        json={
            "seat_ids": [str(big_show.seat_ids[0])],
            "idempotency_key": uuid.uuid4().hex,
            "user_id": str(uuid.uuid4()),
        },
    )

    async def read() -> tuple[str, ...]:
        response = await client.get(f"/shows/{big_show.show_id}/seatmap")
        assert response.status_code == 200
        return tuple(
            seat["status"]
            for section in response.json()["sections"]
            for seat in section["seats"]
        )

    results = await asyncio.gather(*(read() for _ in range(CONCURRENT_USERS)))

    # Cached and uncached responses alike must agree on what is held.
    assert len(set(results)) == 1
    assert results[0].count("HELD") == 1
