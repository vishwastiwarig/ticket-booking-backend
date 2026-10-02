import uuid
from enum import StrEnum

from sqlalchemy import Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.models import Base, TimestampMixin


class BookingStatus(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class Booking(Base, TimestampMixin):
    __tablename__ = "bookings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    show_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shows.id", ondelete="RESTRICT"))
    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus, name="booking_status"),
        default=BookingStatus.PENDING,
        server_default=BookingStatus.PENDING.value,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)

    seats: Mapped[list["BookingSeat"]] = relationship(back_populates="booking")


class BookingSeat(Base, TimestampMixin):
    __tablename__ = "booking_seats"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"))
    seat_inventory_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("seat_inventory.id", ondelete="RESTRICT")
    )

    booking: Mapped[Booking] = relationship(back_populates="seats")

    __table_args__ = (
        UniqueConstraint("seat_inventory_id", name="uq_booking_seat_inventory"),
    )
