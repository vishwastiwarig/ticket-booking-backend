import json
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import SeatMapCache, get_redis
from app.core.config import Settings, get_settings
from app.core.db import get_session
from app.payments.provider import PaymentProvider, verify_webhook_signature
from app.payments.schemas import PayRequest, PayResponse, WebhookAck
from app.payments.service import (
    BookingNotFoundError,
    BookingNotPayableError,
    ConfirmOutcome,
    create_payment_order,
    extract_capture,
    handle_payment_captured,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["payments"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
RedisDep = Annotated[Redis, Depends(get_redis)]


def get_payment_provider(settings: SettingsDep) -> PaymentProvider:
    """Overridden in tests with a fake so no test touches the network."""
    from app.payments.provider import RazorpayProvider

    return RazorpayProvider(settings.razorpay_key_id, settings.razorpay_key_secret)


ProviderDep = Annotated[PaymentProvider, Depends(get_payment_provider)]


@router.post("/bookings/{booking_id}/pay", response_model=PayResponse)
async def pay_endpoint(
    booking_id: uuid.UUID,
    payload: PayRequest,
    session: SessionDep,
    settings: SettingsDep,
    provider: ProviderDep,
) -> PayResponse:
    try:
        result = await create_payment_order(
            session,
            provider,
            booking_id=booking_id,
            seat_price=settings.seat_price,
            currency=settings.currency,
        )
    except BookingNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except BookingNotPayableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return PayResponse(
        booking_id=result.booking_id,
        order_id=result.order.order_id,
        amount=result.amount,
        amount_minor=result.order.amount_minor,
        currency=result.order.currency,
        key_id=settings.razorpay_key_id,
        status=result.status,
    )


@router.post("/webhooks/razorpay", response_model=WebhookAck)
async def razorpay_webhook(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    provider: ProviderDep,
    redis: RedisDep,
    x_razorpay_signature: Annotated[str | None, Header()] = None,
) -> WebhookAck:
    # Read the raw bytes: the signature covers exactly what was sent, so the
    # parsed-and-reserialised body would not verify.
    body = await request.body()

    if not verify_webhook_signature(
        body=body,
        signature=x_razorpay_signature or "",
        secret=settings.razorpay_webhook_secret,
    ):
        # Anyone can POST to this URL; an unverified body is not evidence that
        # a payment happened.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid webhook signature"
        )

    try:
        event = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="malformed webhook body"
        ) from exc

    capture = extract_capture(event)
    if capture is None:
        # Razorpay sends many event types; acknowledge the ones we don't act on
        # so it stops redelivering them.
        return WebhookAck(status="ignored")

    order_ref, payment_ref = capture

    result = await handle_payment_captured(
        session, provider, order_ref=order_ref, payment_ref=payment_ref
    )

    if result.affected_show_id is not None:
        await SeatMapCache(redis, settings.seatmap_cache_ttl_seconds).invalidate(
            result.affected_show_id
        )

    if result.outcome is ConfirmOutcome.REFUNDED:
        logger.warning(
            "refunded late payment",
            extra={"booking_id": str(result.booking_id), "order_ref": order_ref},
        )

    return WebhookAck(status=result.outcome.value)
