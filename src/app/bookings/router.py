import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.bookings.schemas import HoldRequest, HoldResponse, SeatsUnavailableResponse
from app.bookings.service import SeatsUnavailableError, create_hold
from app.core.cache import SeatMapCache, get_redis
from app.core.config import Settings, get_settings
from app.core.db import get_session

router = APIRouter(tags=["holds"])


@router.post(
    "/shows/{show_id}/holds",
    response_model=HoldResponse,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_409_CONFLICT: {"model": SeatsUnavailableResponse}},
    summary="Hold seats for a show",
)
async def create_hold_endpoint(
    show_id: uuid.UUID,
    payload: HoldRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> HoldResponse:
    try:
        result = await create_hold(
            session,
            show_id=show_id,
            seat_ids=payload.seat_ids,
            user_id=payload.user_id,
            idempotency_key=payload.idempotency_key,
            ttl_seconds=settings.hold_ttl_seconds,
        )
    except SeatsUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=SeatsUnavailableResponse(
                message="One or more seats are no longer available",
                unavailable_seat_ids=exc.unavailable_seat_ids,
            ).model_dump(mode="json"),
        ) from exc

    # Seats just changed state, so any cached seat map for this show is now
    # wrong. Dropped after the transaction committed — invalidating earlier
    # would let a concurrent reader repopulate the cache from the pre-commit
    # state. Invalidation is kept at this edge rather than inside the service
    # so the booking logic stays independent of Redis.
    if not result.replayed:
        await SeatMapCache(redis, settings.seatmap_cache_ttl_seconds).invalidate(show_id)

    # A replay of an already-created booking is not a creation, so the 201 that
    # this route declares would be misleading; answer 200 instead.
    if result.replayed:
        response.status_code = status.HTTP_200_OK

    return HoldResponse(
        booking_id=result.booking.id,
        show_id=result.booking.show_id,
        status=result.booking.status,
        seat_ids=result.seat_ids,
        hold_expires_at=result.hold_expires_at,
    )
