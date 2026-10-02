"""Hold expiry (calude.md critical flow 4).

Exercises the service function directly rather than through ARQ: the schedule
is just a trigger, and the transaction semantics are what matter. Wiring it to
the worker happens when Redis is available.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bookings.models import Booking, BookingStatus
from app.bookings.service import create_hold, expire_stale_holds
from app.inventory.models import SeatInventory, SeatStatus

from .conftest import SeededShow

# A hold created with a negative TTL is already past its deadline the moment it
# is written, which is how these tests produce a lapsed hold without sleeping.
ALREADY_EXPIRED = -60
STILL_LIVE = 600


async def test_expired_hold_releases_seats_and_expires_booking(
    session_factory: async_sessionmaker[AsyncSession], seeded_show: SeededShow
) -> None:
    async with session_factory() as session:
        result = await create_hold(
            session,
            show_id=seeded_show.show_id,
            seat_ids=seeded_show.seat_ids[:2],
            user_id=uuid.uuid4(),
            idempotency_key=uuid.uuid4().hex,
            ttl_seconds=ALREADY_EXPIRED,
        )
        booking_id = result.booking.id

    async with session_factory() as session:
        report = await expire_stale_holds(session)

    assert report.released_seat_count == 2
    assert report.expired_booking_ids == [booking_id]

    async with session_factory() as session:
        seats = (
            (
                await session.execute(
                    select(SeatInventory).where(
                        SeatInventory.seat_id.in_(seeded_show.seat_ids[:2])
                    )
                )
            )
            .scalars()
            .all()
        )
        booking = (
            await session.execute(select(Booking).where(Booking.id == booking_id))
        ).scalar_one()

    # Seats are fully reset, not just flagged — a stale held_by would
    # misattribute the next hold.
    assert {seat.status for seat in seats} == {SeatStatus.AVAILABLE}
    assert all(seat.held_by is None and seat.hold_expires_at is None for seat in seats)
    assert booking.status == BookingStatus.EXPIRED


async def test_live_hold_is_left_alone(
    session_factory: async_sessionmaker[AsyncSession], seeded_show: SeededShow
) -> None:
    async with session_factory() as session:
        result = await create_hold(
            session,
            show_id=seeded_show.show_id,
            seat_ids=seeded_show.seat_ids[:1],
            user_id=uuid.uuid4(),
            idempotency_key=uuid.uuid4().hex,
            ttl_seconds=STILL_LIVE,
        )
        booking_id = result.booking.id

    async with session_factory() as session:
        report = await expire_stale_holds(session)

    assert report.released_seat_count == 0
    assert report.expired_booking_ids == []

    async with session_factory() as session:
        seat = (
            await session.execute(
                select(SeatInventory).where(SeatInventory.seat_id == seeded_show.seat_ids[0])
            )
        ).scalar_one()
        booking = (
            await session.execute(select(Booking).where(Booking.id == booking_id))
        ).scalar_one()

    assert seat.status == SeatStatus.HELD
    assert booking.status == BookingStatus.PENDING


async def test_confirmed_booking_is_never_expired(
    session_factory: async_sessionmaker[AsyncSession], seeded_show: SeededShow
) -> None:
    """A payment that confirmed the booking wins over a lapsed deadline.

    Stands in for the webhook/expiry race: if the booking reached CONFIRMED,
    the expiry sweep must not cancel a seat the customer paid for.
    """
    async with session_factory() as session:
        result = await create_hold(
            session,
            show_id=seeded_show.show_id,
            seat_ids=seeded_show.seat_ids[:1],
            user_id=uuid.uuid4(),
            idempotency_key=uuid.uuid4().hex,
            ttl_seconds=ALREADY_EXPIRED,
        )
        booking_id = result.booking.id

        booking = (
            await session.execute(select(Booking).where(Booking.id == booking_id))
        ).scalar_one()
        booking.status = BookingStatus.CONFIRMED
        await session.commit()

    async with session_factory() as session:
        report = await expire_stale_holds(session)

    assert report.expired_booking_ids == []

    async with session_factory() as session:
        booking = (
            await session.execute(select(Booking).where(Booking.id == booking_id))
        ).scalar_one()

    assert booking.status == BookingStatus.CONFIRMED
