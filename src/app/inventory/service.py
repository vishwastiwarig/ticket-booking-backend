"""Seat inventory state transitions.

This module owns every write to ``seat_inventory.status``. Other packages call
these functions instead of updating seat rows themselves, so the entire
correctness argument for "two users never get the same seat" lives in one file
and can be reviewed as a unit.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.inventory.models import Event, Seat, SeatInventory, SeatStatus, Section, Show, Venue
from app.inventory.schemas import SeatMapResponse, SeatMapSeat, SeatMapSection


@dataclass(frozen=True)
class HoldOutcome:
    """Result of attempting to hold a set of seats.

    ``unavailable_seat_ids`` is empty on success. On failure it names exactly
    which seats lost the race, which is what the 409 response body reports back
    to the client.
    """

    held_inventory_ids: list[uuid.UUID]
    unavailable_seat_ids: list[uuid.UUID]
    hold_expires_at: datetime | None

    @property
    def ok(self) -> bool:
        return not self.unavailable_seat_ids


@dataclass(frozen=True)
class SeatHoldInfo:
    seat_inventory_id: uuid.UUID
    seat_id: uuid.UUID
    status: SeatStatus
    hold_expires_at: datetime | None


@dataclass(frozen=True)
class ReleasedSeat:
    seat_inventory_id: uuid.UUID
    show_id: uuid.UUID


class ShowNotFoundError(Exception):
    def __init__(self, show_id: uuid.UUID) -> None:
        self.show_id = show_id
        super().__init__(f"show not found: {show_id}")


class VenueNotFoundError(Exception):
    def __init__(self, venue_id: uuid.UUID) -> None:
        self.venue_id = venue_id
        super().__init__(f"venue not found: {venue_id}")


async def create_venue(session: AsyncSession, *, name: str) -> Venue:
    venue = Venue(name=name)
    session.add(venue)
    await session.commit()
    return venue


async def create_section(session: AsyncSession, *, venue_id: uuid.UUID, name: str) -> Section:
    if await session.get(Venue, venue_id) is None:
        raise VenueNotFoundError(venue_id)

    section = Section(venue_id=venue_id, name=name)
    session.add(section)
    await session.commit()
    return section


async def create_seats(
    session: AsyncSession, *, section_id: uuid.UUID, seats: list[tuple[str, int]]
) -> list[Seat]:
    rows = [
        Seat(section_id=section_id, row_label=row_label, seat_number=seat_number)
        for row_label, seat_number in seats
    ]
    session.add_all(rows)
    await session.commit()
    return rows


async def create_event(session: AsyncSession, *, name: str) -> Event:
    event = Event(name=name)
    session.add(event)
    await session.commit()
    return event


async def create_show(
    session: AsyncSession,
    *,
    event_id: uuid.UUID,
    venue_id: uuid.UUID,
    start_time: datetime,
) -> tuple[Show, int]:
    """Create a show and materialise a seat_inventory row per seat in the venue.

    Inventory is written up front rather than lazily on first hold, because the
    hold flow's conditional UPDATE can only compete over rows that already
    exist — a seat with no inventory row would have nothing to update, and
    creating it on demand would reintroduce exactly the insert race that
    UNIQUE(show_id, seat_id) exists to prevent.
    """
    if await session.get(Venue, venue_id) is None:
        raise VenueNotFoundError(venue_id)

    show = Show(event_id=event_id, venue_id=venue_id, start_time=start_time)
    session.add(show)
    await session.flush()

    seat_id_stmt = (
        select(Seat.id)
        .join(Section, Seat.section_id == Section.id)
        .where(Section.venue_id == venue_id)
    )
    seat_ids = list((await session.execute(seat_id_stmt)).scalars().all())

    session.add_all(
        [SeatInventory(show_id=show.id, seat_id=seat_id) for seat_id in seat_ids]
    )
    await session.commit()

    return show, len(seat_ids)


async def get_seat_map(session: AsyncSession, show_id: uuid.UUID) -> SeatMapResponse:
    """Read the current seat map for a show, grouped by section."""
    if await session.get(Show, show_id) is None:
        raise ShowNotFoundError(show_id)

    stmt = (
        select(
            Section.id,
            Section.name,
            Seat.id,
            Seat.row_label,
            Seat.seat_number,
            SeatInventory.status,
        )
        .join(Seat, SeatInventory.seat_id == Seat.id)
        .join(Section, Seat.section_id == Section.id)
        .where(SeatInventory.show_id == show_id)
        .order_by(Section.name, Seat.row_label, Seat.seat_number)
    )
    rows = (await session.execute(stmt)).all()

    sections: dict[uuid.UUID, SeatMapSection] = {}
    for section_id, section_name, seat_id, row_label, seat_number, status in rows:
        section = sections.get(section_id)
        if section is None:
            section = SeatMapSection(section_id=section_id, name=section_name, seats=[])
            sections[section_id] = section
        section.seats.append(
            SeatMapSeat(
                seat_id=seat_id,
                row_label=row_label,
                seat_number=seat_number,
                status=status,
            )
        )

    return SeatMapResponse(show_id=show_id, sections=list(sections.values()))


async def hold_seats(
    session: AsyncSession,
    *,
    show_id: uuid.UUID,
    seat_ids: list[uuid.UUID],
    held_by: uuid.UUID,
    ttl_seconds: int,
) -> HoldOutcome:
    """Atomically move the requested seats from AVAILABLE to HELD.

    The entire concurrency guarantee rests on this single statement. Two
    properties make it safe without any explicit locking:

    1. The precondition (``status == AVAILABLE``) is part of the UPDATE's WHERE
       clause rather than a preceding SELECT, so there is no window between
       checking and writing for a competing transaction to slip through.

    2. Under Postgres READ COMMITTED, when two transactions target the same row
       concurrently the second one blocks until the first commits, then
       **re-evaluates its WHERE clause against the newly committed row version**
       (EvalPlanQual). So the loser sees ``status = 'HELD'``, no longer matches,
       and simply does not update the row. It fails by not appearing in
       RETURNING — no deadlock, no retry loop, no lock timeout to tune.

    That is why a seat can never be held twice: the database, not application
    code, arbitrates the race. See docs/decisions/0001-conditional-update-over-locks.md.
    """
    # Duplicate ids in the request would break the "did every seat succeed?"
    # comparison below, since RETURNING yields each row at most once. This is
    # untrusted client input, so normalise it here. dict.fromkeys dedupes while
    # preserving the caller's ordering.
    requested = list(dict.fromkeys(seat_ids))

    stmt = (
        update(SeatInventory)
        .where(
            SeatInventory.show_id == show_id,
            SeatInventory.seat_id.in_(requested),
            SeatInventory.status == SeatStatus.AVAILABLE,
        )
        .values(
            status=SeatStatus.HELD,
            held_by=held_by,
            # Computed by Postgres rather than in Python so that the hold
            # deadline and the expiry job's `hold_expires_at < now()` sweep are
            # measured against the same clock. App-server clock skew would
            # otherwise expire holds early or late.
            hold_expires_at=func.now() + timedelta(seconds=ttl_seconds),
        )
        # RETURNING gives us the row count *and* the seat_inventory ids needed
        # to write BookingSeat rows, so the caller needs no follow-up SELECT.
        .returning(
            SeatInventory.id,
            SeatInventory.seat_id,
            SeatInventory.hold_expires_at,
        )
        # This is a bulk UPDATE, not a flush of tracked ORM objects; nothing in
        # this session's identity map needs reconciling with it.
        .execution_options(synchronize_session=False)
    )

    rows = (await session.execute(stmt)).all()

    held_inventory_ids = [row[0] for row in rows]
    held_seat_ids = {row[1] for row in rows}
    expires_at: datetime | None = rows[0][2] if rows else None

    # Any requested seat missing from RETURNING was not AVAILABLE at the moment
    # this statement ran — it is held or booked by someone else. Note this
    # deliberately does not treat an expired-but-not-yet-swept hold as
    # available: returning those seats to AVAILABLE is the expiry job's job
    # alone, which keeps one writer per state transition.
    unavailable_seat_ids = [sid for sid in requested if sid not in held_seat_ids]

    return HoldOutcome(
        held_inventory_ids=held_inventory_ids,
        unavailable_seat_ids=unavailable_seat_ids,
        hold_expires_at=expires_at,
    )


async def release_expired_holds(session: AsyncSession) -> list[ReleasedSeat]:
    """Return seats whose hold deadline has passed to AVAILABLE.

    This is the only writer that moves a seat out of HELD without a payment,
    which is why ``hold_seats`` refuses to treat an expired-but-unswept seat as
    available: one transition, one owner, no two code paths disagreeing about
    who owns a lapsed hold.

    ``status == HELD`` in the WHERE clause is what makes this safe against a
    payment webhook landing at the same moment. If the webhook commits first
    the seat is already BOOKED, this statement no longer matches it, and the
    paid seat is left alone. If this commits first the webhook finds the seat
    AVAILABLE and takes the late-payment refund path instead.

    Returns the seats it released, so the caller can expire the bookings that
    owned them and drop the affected shows' cached seat maps.
    """
    stmt = (
        update(SeatInventory)
        .where(
            SeatInventory.status == SeatStatus.HELD,
            SeatInventory.hold_expires_at < func.now(),
        )
        .values(status=SeatStatus.AVAILABLE, held_by=None, hold_expires_at=None)
        .returning(SeatInventory.id, SeatInventory.show_id)
        .execution_options(synchronize_session=False)
    )

    rows = (await session.execute(stmt)).all()
    return [ReleasedSeat(seat_inventory_id=row[0], show_id=row[1]) for row in rows]


async def describe_inventory(
    session: AsyncSession, inventory_ids: list[uuid.UUID]
) -> list[SeatHoldInfo]:
    """Look up the current state of specific seat_inventory rows.

    Exists so that other packages (bookings) can render a response describing
    seats without importing the SeatInventory model directly — cross-module
    access goes through this service layer.
    """
    if not inventory_ids:
        return []

    stmt = select(
        SeatInventory.id,
        SeatInventory.seat_id,
        SeatInventory.status,
        SeatInventory.hold_expires_at,
    ).where(SeatInventory.id.in_(inventory_ids))

    rows = (await session.execute(stmt)).all()

    return [
        SeatHoldInfo(
            seat_inventory_id=row[0],
            seat_id=row[1],
            status=row[2],
            hold_expires_at=row[3],
        )
        for row in rows
    ]
