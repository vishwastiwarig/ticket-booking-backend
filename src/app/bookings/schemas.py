import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.bookings.models import BookingStatus


class HoldRequest(BaseModel):
    seat_ids: list[uuid.UUID] = Field(min_length=1)
    # Required on every mutating endpoint (calude.md non-negotiables). The
    # client generates it, so a retried request after a timeout replays the
    # original booking instead of holding a second set of seats.
    idempotency_key: str = Field(min_length=1, max_length=128)
    # Placeholder for the authenticated principal until auth lands; the hold is
    # attributed to this user via seat_inventory.held_by.
    user_id: uuid.UUID


class HoldResponse(BaseModel):
    booking_id: uuid.UUID
    show_id: uuid.UUID
    status: BookingStatus
    seat_ids: list[uuid.UUID]
    hold_expires_at: datetime | None


class SeatsUnavailableResponse(BaseModel):
    message: str
    unavailable_seat_ids: list[uuid.UUID]
