import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import SeatMapCache, get_redis
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.inventory.schemas import (
    EventCreate,
    EventRead,
    SeatMapResponse,
    SeatRead,
    SeatsCreate,
    SectionCreate,
    SectionRead,
    ShowCreate,
    ShowRead,
    VenueCreate,
    VenueRead,
)
from app.inventory.service import (
    ShowNotFoundError,
    VenueNotFoundError,
    create_event,
    create_seats,
    create_section,
    create_show,
    create_venue,
    get_seat_map,
)

router = APIRouter(tags=["inventory"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
RedisDep = Annotated[Redis, Depends(get_redis)]


@router.post("/venues", response_model=VenueRead, status_code=status.HTTP_201_CREATED)
async def create_venue_endpoint(payload: VenueCreate, session: SessionDep) -> VenueRead:
    venue = await create_venue(session, name=payload.name)
    return VenueRead(id=venue.id, name=venue.name)


@router.post(
    "/venues/{venue_id}/sections",
    response_model=SectionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_section_endpoint(
    venue_id: uuid.UUID, payload: SectionCreate, session: SessionDep
) -> SectionRead:
    try:
        section = await create_section(session, venue_id=venue_id, name=payload.name)
    except VenueNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return SectionRead(id=section.id, venue_id=section.venue_id, name=section.name)


@router.post(
    "/sections/{section_id}/seats",
    response_model=list[SeatRead],
    status_code=status.HTTP_201_CREATED,
)
async def create_seats_endpoint(
    section_id: uuid.UUID, payload: SeatsCreate, session: SessionDep
) -> list[SeatRead]:
    seats = await create_seats(
        session,
        section_id=section_id,
        seats=[(seat.row_label, seat.seat_number) for seat in payload.seats],
    )
    return [
        SeatRead(
            id=seat.id,
            section_id=seat.section_id,
            row_label=seat.row_label,
            seat_number=seat.seat_number,
        )
        for seat in seats
    ]


@router.post("/events", response_model=EventRead, status_code=status.HTTP_201_CREATED)
async def create_event_endpoint(payload: EventCreate, session: SessionDep) -> EventRead:
    event = await create_event(session, name=payload.name)
    return EventRead(id=event.id, name=event.name)


@router.post(
    "/events/{event_id}/shows",
    response_model=ShowRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_show_endpoint(
    event_id: uuid.UUID, payload: ShowCreate, session: SessionDep
) -> ShowRead:
    try:
        show, seats_total = await create_show(
            session,
            event_id=event_id,
            venue_id=payload.venue_id,
            start_time=payload.start_time,
        )
    except VenueNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return ShowRead(
        id=show.id,
        event_id=show.event_id,
        venue_id=show.venue_id,
        start_time=show.start_time,
        seats_total=seats_total,
    )


@router.get("/shows/{show_id}/seatmap", response_model=SeatMapResponse)
async def get_seat_map_endpoint(
    show_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    redis: RedisDep,
) -> SeatMapResponse:
    cache = SeatMapCache(redis, settings.seatmap_cache_ttl_seconds)

    cached = await cache.get(show_id)
    if cached is not None:
        return SeatMapResponse.model_validate({**cached, "cached": True})

    try:
        seat_map = await get_seat_map(session, show_id)
    except ShowNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    await cache.set(show_id, seat_map.model_dump(mode="json"))
    return seat_map
