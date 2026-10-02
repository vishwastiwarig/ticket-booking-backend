"""Booking orchestration for the hold flow.

Owns the Booking/BookingSeat tables and drives the seat state transition
through ``app.inventory.service`` rather than touching seat rows itself.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bookings.models import Booking, BookingSeat, BookingStatus
from app.inventory.service import describe_inventory, hold_seats, release_expired_holds


class SeatsUnavailableError(Exception):
    """Raised when at least one requested seat was not AVAILABLE.

    Carries the specific losing seats so the API can tell the client which of
    their selections to change, rather than a bare "conflict".
    """

    def __init__(self, unavailable_seat_ids: list[uuid.UUID]) -> None:
        self.unavailable_seat_ids = unavailable_seat_ids
        super().__init__(f"seats unavailable: {unavailable_seat_ids}")


@dataclass(frozen=True)
class HoldResult:
    booking: Booking
    seat_ids: list[uuid.UUID]
    hold_expires_at: datetime | None
    # True when this request replayed an existing booking rather than creating
    # one, so the API can answer 200 instead of 201.
    replayed: bool


async def create_hold(
    session: AsyncSession,
    *,
    show_id: uuid.UUID,
    seat_ids: list[uuid.UUID],
    user_id: uuid.UUID,
    idempotency_key: str,
    ttl_seconds: int,
) -> HoldResult:
    """Hold seats and open a PENDING booking for them, in one transaction.

    Either every requested seat is held and a booking exists, or nothing
    changed — a partial hold would strand seats that no booking will ever pay
    for or release.
    """
    # Fast path for a client retrying a request that already succeeded (its
    # response was lost, the connection dropped). Cheap SELECT that avoids
    # doing the write work just to have the unique constraint reject it.
    replay = await _find_by_idempotency_key(session, idempotency_key)
    if replay is not None:
        return await _describe_booking(session, replay, replayed=True)

    outcome = await hold_seats(
        session,
        show_id=show_id,
        seat_ids=seat_ids,
        held_by=user_id,
        ttl_seconds=ttl_seconds,
    )

    if not outcome.ok:
        # Discards any seats this statement *did* manage to hold. Rolling back
        # is what makes the all-or-nothing promise above true.
        await session.rollback()
        raise SeatsUnavailableError(outcome.unavailable_seat_ids)

    booking = Booking(
        user_id=user_id,
        show_id=show_id,
        status=BookingStatus.PENDING,
        idempotency_key=idempotency_key,
    )
    session.add(booking)
    # Assigns booking.id without committing, so the BookingSeat rows below can
    # reference it inside the same transaction.
    await session.flush()

    session.add_all(
        [
            BookingSeat(booking_id=booking.id, seat_inventory_id=inventory_id)
            for inventory_id in outcome.held_inventory_ids
        ]
    )

    try:
        await session.commit()
    except IntegrityError:
        # Two concurrent requests carrying the same idempotency key: the UNIQUE
        # constraint on bookings.idempotency_key let exactly one through. The
        # loser's seat holds roll back with its transaction, so the winner's
        # booking is the only one holding seats, and both callers see the same
        # booking. The constraint — not a pre-check — is what makes this safe.
        await session.rollback()
        existing = await _find_by_idempotency_key(session, idempotency_key)
        if existing is None:
            raise
        return await _describe_booking(session, existing, replayed=True)

    return HoldResult(
        booking=booking,
        seat_ids=[sid for sid in dict.fromkeys(seat_ids)],
        hold_expires_at=outcome.hold_expires_at,
        replayed=False,
    )


@dataclass(frozen=True)
class ExpiryReport:
    released_seat_count: int
    expired_booking_ids: list[uuid.UUID]
    # Shows whose seat map changed, so the caller can drop their cache entries.
    affected_show_ids: list[uuid.UUID]


async def expire_stale_holds(session: AsyncSession) -> ExpiryReport:
    """Release lapsed holds and expire the bookings that owned them.

    Run on a schedule (every 30s per calude.md). Safe to run concurrently with
    itself and with payment webhooks: both steps are conditional updates, so a
    second runner finds nothing left matching rather than double-processing.
    """
    released = await release_expired_holds(session)

    if not released:
        await session.commit()
        return ExpiryReport(
            released_seat_count=0, expired_booking_ids=[], affected_show_ids=[]
        )

    released_inventory_ids = [seat.seat_inventory_id for seat in released]
    affected_show_ids = list(dict.fromkeys(seat.show_id for seat in released))

    booking_id_stmt = select(BookingSeat.booking_id).where(
        BookingSeat.seat_inventory_id.in_(released_inventory_ids)
    )
    candidate_ids = list((await session.execute(booking_id_stmt)).scalars().all())

    expire_stmt = (
        update(Booking)
        .where(
            Booking.id.in_(candidate_ids),
            # Only PENDING bookings expire. A booking a webhook confirmed in
            # the meantime must never be flipped to EXPIRED — that would
            # cancel a booking the customer has already paid for.
            Booking.status == BookingStatus.PENDING,
        )
        .values(status=BookingStatus.EXPIRED)
        .returning(Booking.id)
        .execution_options(synchronize_session=False)
    )
    expired_ids = [row[0] for row in (await session.execute(expire_stmt)).all()]

    await session.commit()

    return ExpiryReport(
        released_seat_count=len(released_inventory_ids),
        expired_booking_ids=expired_ids,
        affected_show_ids=affected_show_ids,
    )


async def _find_by_idempotency_key(session: AsyncSession, key: str) -> Booking | None:
    stmt = select(Booking).where(Booking.idempotency_key == key)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _describe_booking(
    session: AsyncSession, booking: Booking, *, replayed: bool
) -> HoldResult:
    """Rebuild the response for an existing booking.

    Reads this package's own BookingSeat rows, then asks the inventory service
    to describe those seats — the seat ids and hold deadline live on
    seat_inventory, which bookings does not read directly.
    """
    stmt = select(BookingSeat.seat_inventory_id).where(BookingSeat.booking_id == booking.id)
    inventory_ids = list((await session.execute(stmt)).scalars().all())

    seats = await describe_inventory(session, inventory_ids)

    return HoldResult(
        booking=booking,
        seat_ids=[seat.seat_id for seat in seats],
        hold_expires_at=seats[0].hold_expires_at if seats else None,
        replayed=replayed,
    )
