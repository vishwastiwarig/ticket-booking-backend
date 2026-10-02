"""Hold flow behaviour (calude.md critical flow 1).

Concurrency is covered separately in test_concurrency_holds.py; these are the
single-threaded guarantees: all-or-nothing, useful conflict reporting, and
idempotent replay.
"""

import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bookings.models import Booking, BookingStatus
from app.inventory.models import SeatInventory, SeatStatus

from .conftest import SeededShow


def hold_payload(seat_ids: list[uuid.UUID], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "seat_ids": [str(seat_id) for seat_id in seat_ids],
        "idempotency_key": uuid.uuid4().hex,
        "user_id": str(uuid.uuid4()),
    }
    payload.update(overrides)
    return payload


async def test_hold_marks_seats_held_and_opens_pending_booking(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded_show: SeededShow,
) -> None:
    requested = seeded_show.seat_ids[:2]

    response = await client.post(
        f"/shows/{seeded_show.show_id}/holds", json=hold_payload(requested)
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == BookingStatus.PENDING
    assert set(body["seat_ids"]) == {str(seat_id) for seat_id in requested}
    # The client needs the deadline to show a countdown and to know when its
    # seats are forfeit.
    assert body["hold_expires_at"] is not None

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(SeatInventory).where(SeatInventory.seat_id.in_(requested))
                )
            )
            .scalars()
            .all()
        )

    assert {row.status for row in rows} == {SeatStatus.HELD}
    assert all(row.held_by is not None and row.hold_expires_at is not None for row in rows)


async def test_holding_a_held_seat_conflicts_and_names_the_seat(
    client: AsyncClient, seeded_show: SeededShow
) -> None:
    contested = seeded_show.seat_ids[0]

    url = f"/shows/{seeded_show.show_id}/holds"
    first = await client.post(url, json=hold_payload([contested]))
    second = await client.post(url, json=hold_payload([contested]))

    assert first.status_code == 201
    assert second.status_code == 409
    # The client can only fix its selection if the error says which seat went.
    assert second.json()["detail"]["unavailable_seat_ids"] == [str(contested)]


async def test_partial_availability_holds_nothing(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded_show: SeededShow,
) -> None:
    """A request for 2 seats where only 1 is free must hold neither.

    Holding the available one would strand it: no booking covers it, so nobody
    pays for it and nothing releases it until the hold expires.
    """
    taken, still_free = seeded_show.seat_ids[0], seeded_show.seat_ids[1]
    await client.post(f"/shows/{seeded_show.show_id}/holds", json=hold_payload([taken]))

    response = await client.post(
        f"/shows/{seeded_show.show_id}/holds", json=hold_payload([taken, still_free])
    )

    assert response.status_code == 409
    assert response.json()["detail"]["unavailable_seat_ids"] == [str(taken)]

    async with session_factory() as session:
        row = (
            await session.execute(
                select(SeatInventory).where(SeatInventory.seat_id == still_free)
            )
        ).scalar_one()
        booking_count = (await session.execute(select(func.count(Booking.id)))).scalar_one()

    assert row.status == SeatStatus.AVAILABLE
    assert row.held_by is None
    # Only the first request's booking exists; the rejected one left no trace.
    assert booking_count == 1


async def test_replayed_idempotency_key_returns_the_same_booking(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded_show: SeededShow,
) -> None:
    """A retried request must not hold a second set of seats."""
    payload = hold_payload(seeded_show.seat_ids[:1])

    first = await client.post(f"/shows/{seeded_show.show_id}/holds", json=payload)
    replay = await client.post(f"/shows/{seeded_show.show_id}/holds", json=payload)

    assert first.status_code == 201
    # 200 rather than 201: nothing was created the second time.
    assert replay.status_code == 200
    assert replay.json()["booking_id"] == first.json()["booking_id"]
    assert replay.json()["seat_ids"] == first.json()["seat_ids"]

    async with session_factory() as session:
        booking_count = (await session.execute(select(func.count(Booking.id)))).scalar_one()

    assert booking_count == 1
