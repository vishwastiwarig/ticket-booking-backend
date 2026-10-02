"""The day-one concurrency guarantee (calude.md, Non-negotiables).

N concurrent hold requests for the same seat, against real Postgres, must
produce exactly one winner and zero duplicate holds. This is the test the
whole design of the hold flow exists to satisfy — see
docs/decisions/0001-conditional-update-over-locks.md.
"""

import asyncio
import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bookings.models import Booking
from app.inventory.models import SeatInventory, SeatStatus

from .conftest import SeededShow

N_CONCURRENT_REQUESTS = 10


async def test_concurrent_holds_on_same_seat_exactly_one_wins(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded_show: SeededShow,
) -> None:
    contested = seeded_show.seat_ids[0]

    # Distinct idempotency keys and users: these are genuinely different people
    # racing for one seat, not one client retrying. Idempotent replay is a
    # separate concern, covered in test_holds.py.
    async def attempt() -> int:
        response = await client.post(
            f"/shows/{seeded_show.show_id}/holds",
            json={
                "seat_ids": [str(contested)],
                "idempotency_key": uuid.uuid4().hex,
                "user_id": str(uuid.uuid4()),
            },
        )
        return response.status_code

    # Each request runs on its own session and therefore its own pooled
    # connection, so the race happens inside Postgres rather than being
    # serialised by the test harness.
    statuses = await asyncio.gather(*(attempt() for _ in range(N_CONCURRENT_REQUESTS)))

    assert statuses.count(201) == 1, f"expected exactly one winner, got {statuses}"
    assert statuses.count(409) == N_CONCURRENT_REQUESTS - 1, f"unexpected statuses: {statuses}"

    async with session_factory() as session:
        seat_rows = (
            (
                await session.execute(
                    select(SeatInventory).where(SeatInventory.seat_id == contested)
                )
            )
            .scalars()
            .all()
        )
        bookings = (await session.execute(select(Booking))).scalars().all()

    # The seat is held exactly once, by exactly one user.
    assert len(seat_rows) == 1
    assert seat_rows[0].status == SeatStatus.HELD
    assert seat_rows[0].held_by is not None

    # And the losers left no bookings behind — their transactions rolled back
    # whole, rather than leaving PENDING bookings that own no seats.
    assert len(bookings) == 1
    assert bookings[0].user_id == seat_rows[0].held_by
