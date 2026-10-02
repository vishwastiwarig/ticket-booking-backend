import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.inventory.models import SeatStatus


class VenueCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class VenueRead(BaseModel):
    id: uuid.UUID
    name: str


class SectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class SectionRead(BaseModel):
    id: uuid.UUID
    venue_id: uuid.UUID
    name: str


class SeatCreate(BaseModel):
    row_label: str = Field(min_length=1, max_length=8)
    seat_number: int = Field(ge=1)


class SeatsCreate(BaseModel):
    seats: list[SeatCreate] = Field(min_length=1)


class SeatRead(BaseModel):
    id: uuid.UUID
    section_id: uuid.UUID
    row_label: str
    seat_number: int


class EventCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class EventRead(BaseModel):
    id: uuid.UUID
    name: str


class ShowCreate(BaseModel):
    venue_id: uuid.UUID
    start_time: datetime


class ShowRead(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    venue_id: uuid.UUID
    start_time: datetime
    seats_total: int


class SeatMapSeat(BaseModel):
    seat_id: uuid.UUID
    row_label: str
    seat_number: int
    status: SeatStatus


class SeatMapSection(BaseModel):
    section_id: uuid.UUID
    name: str
    seats: list[SeatMapSeat]


class SeatMapResponse(BaseModel):
    show_id: uuid.UUID
    sections: list[SeatMapSection]
    # Lets a client tell a cached response from a fresh one, and makes the
    # cache behaviour observable in tests without reaching into Redis.
    cached: bool = False
