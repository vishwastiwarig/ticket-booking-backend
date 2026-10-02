import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.models import Base, TimestampMixin


class SeatStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    HELD = "HELD"
    BOOKED = "BOOKED"


class Venue(Base, TimestampMixin):
    __tablename__ = "venues"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255))

    sections: Mapped[list["Section"]] = relationship(back_populates="venue")


class Section(Base, TimestampMixin):
    __tablename__ = "sections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    venue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("venues.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(128))

    venue: Mapped[Venue] = relationship(back_populates="sections")
    seats: Mapped[list["Seat"]] = relationship(back_populates="section")


class Seat(Base, TimestampMixin):
    __tablename__ = "seats"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    section_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sections.id", ondelete="CASCADE"))
    row_label: Mapped[str] = mapped_column(String(8))
    seat_number: Mapped[int] = mapped_column(Integer)

    section: Mapped[Section] = relationship(back_populates="seats")

    __table_args__ = (
        UniqueConstraint("section_id", "row_label", "seat_number", name="uq_seat_position"),
    )


class Event(Base, TimestampMixin):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255))

    shows: Mapped[list["Show"]] = relationship(back_populates="event")


class Show(Base, TimestampMixin):
    __tablename__ = "shows"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    venue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("venues.id", ondelete="RESTRICT"))
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    event: Mapped[Event] = relationship(back_populates="shows")
    venue: Mapped[Venue] = relationship()


class SeatInventory(Base, TimestampMixin):
    __tablename__ = "seat_inventory"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    show_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shows.id", ondelete="CASCADE"))
    seat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("seats.id", ondelete="RESTRICT"))
    status: Mapped[SeatStatus] = mapped_column(
        Enum(SeatStatus, name="seat_status"),
        default=SeatStatus.AVAILABLE,
        server_default=SeatStatus.AVAILABLE.value,
    )
    held_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    show: Mapped[Show] = relationship()
    seat: Mapped[Seat] = relationship()

    __table_args__ = (
        UniqueConstraint("show_id", "seat_id", name="uq_seat_inventory_show_seat"),
        CheckConstraint(
            "status != 'HELD' OR (held_by IS NOT NULL AND hold_expires_at IS NOT NULL)",
            name="ck_held_requires_holder_and_expiry",
        ),
    )
