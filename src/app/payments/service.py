"""Payment orders and webhook confirmation.

Everything here follows the same rule as the hold flow: state transitions are
conditional UPDATEs whose WHERE clause carries the precondition, so concurrent
webhooks, retries and the expiry sweep resolve in the database rather than in
application logic.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum, auto
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bookings.models import Booking, BookingSeat, BookingStatus
from app.core.models import OutboxEvent
from app.inventory.models import SeatInventory, SeatStatus
from app.payments.models import Payment, PaymentStatus
from app.payments.provider import PaymentProvider, ProviderOrder, to_minor_units


class BookingNotFoundError(Exception):
    def __init__(self, booking_id: uuid.UUID) -> None:
        self.booking_id = booking_id
        super().__init__(f"booking not found: {booking_id}")


class BookingNotPayableError(Exception):
    def __init__(self, status: BookingStatus) -> None:
        self.status = status
        super().__init__(f"booking is {status}, only PENDING bookings can be paid")


class ConfirmOutcome(StrEnum):
    CONFIRMED = auto()
    # The webhook was delivered more than once; the first delivery already did
    # the work. Not an error — webhooks are at-least-once by contract.
    ALREADY_PROCESSED = auto()
    # Payment arrived after the hold lapsed (or against an unknown order). The
    # money must go back; the seats are already someone else's.
    REFUNDED = auto()
    IGNORED = auto()


@dataclass(frozen=True)
class ConfirmResult:
    outcome: ConfirmOutcome
    booking_id: uuid.UUID | None = None
    # Shows whose seat map changed, for cache invalidation at the router edge.
    affected_show_id: uuid.UUID | None = None


@dataclass(frozen=True)
class PaymentOrder:
    booking_id: uuid.UUID
    order: ProviderOrder
    amount: Decimal
    status: PaymentStatus


async def create_payment_order(
    session: AsyncSession,
    provider: PaymentProvider,
    *,
    booking_id: uuid.UUID,
    seat_price: Decimal,
    currency: str,
) -> PaymentOrder:
    """Create (or re-return) the provider order a client needs to pay."""
    booking = await session.get(Booking, booking_id)
    if booking is None:
        raise BookingNotFoundError(booking_id)

    if booking.status is not BookingStatus.PENDING:
        # Paying for a confirmed booking would take money for seats the user
        # already owns; paying for an expired one would buy seats that have
        # been released to somebody else.
        raise BookingNotPayableError(booking.status)

    existing = (
        await session.execute(
            select(Payment).where(
                Payment.booking_id == booking_id, Payment.status == PaymentStatus.CREATED
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        # A retried /pay call must not open a second order against the same
        # booking — two live orders means two ways to pay for one set of seats.
        return PaymentOrder(
            booking_id=booking_id,
            order=ProviderOrder(
                order_id=existing.provider_ref,
                amount_minor=to_minor_units(existing.amount),
                currency=currency,
            ),
            amount=existing.amount,
            status=existing.status,
        )

    seat_count = (
        await session.execute(
            select(BookingSeat.id).where(BookingSeat.booking_id == booking_id)
        )
    ).scalars().all()
    amount = seat_price * len(seat_count)

    order = await provider.create_order(
        amount=amount, currency=currency, receipt=str(booking_id)
    )

    session.add(
        Payment(
            booking_id=booking_id,
            provider_ref=order.order_id,
            amount=amount,
            status=PaymentStatus.CREATED,
        )
    )
    await session.commit()

    return PaymentOrder(
        booking_id=booking_id, order=order, amount=amount, status=PaymentStatus.CREATED
    )


async def handle_payment_captured(
    session: AsyncSession,
    provider: PaymentProvider,
    *,
    order_ref: str,
    payment_ref: str,
) -> ConfirmResult:
    """Confirm a booking from a ``payment.captured`` webhook.

    Ordering matters here. Seats are updated *before* the booking row, which is
    the same order the expiry sweep uses (seats, then bookings). Taking the two
    locks in a consistent order across both paths is what stops a webhook and
    an expiry running head-on into a deadlock.
    """
    payment = (
        await session.execute(select(Payment).where(Payment.provider_ref == order_ref))
    ).scalar_one_or_none()

    if payment is None:
        # An order this system never created. Acknowledge so the provider stops
        # redelivering, but change nothing.
        return ConfirmResult(outcome=ConfirmOutcome.IGNORED)

    booking_id = payment.booking_id

    # Claims this webhook delivery. A duplicate delivery finds the row already
    # CAPTURED, matches zero rows, and returns without touching seats — this is
    # the idempotency guard the at-least-once contract requires.
    claimed = (
        await session.execute(
            update(Payment)
            .where(Payment.provider_ref == order_ref, Payment.status == PaymentStatus.CREATED)
            .values(status=PaymentStatus.CAPTURED)
            .returning(Payment.id)
            .execution_options(synchronize_session=False)
        )
    ).all()

    if not claimed:
        await session.rollback()
        return ConfirmResult(
            outcome=ConfirmOutcome.ALREADY_PROCESSED, booking_id=booking_id
        )

    inventory_ids = list(
        (
            await session.execute(
                select(BookingSeat.seat_inventory_id).where(
                    BookingSeat.booking_id == booking_id
                )
            )
        )
        .scalars()
        .all()
    )

    booked = (
        await session.execute(
            update(SeatInventory)
            .where(
                SeatInventory.id.in_(inventory_ids),
                SeatInventory.status == SeatStatus.HELD,
            )
            .values(status=SeatStatus.BOOKED, hold_expires_at=None)
            .returning(SeatInventory.id, SeatInventory.show_id)
            .execution_options(synchronize_session=False)
        )
    ).all()

    if len(booked) != len(inventory_ids):
        # The hold lapsed and the sweep already handed these seats back. The
        # customer paid for seats that are no longer theirs.
        await session.rollback()
        return await _refund(session, provider, order_ref=order_ref, payment_ref=payment_ref)

    confirmed = (
        await session.execute(
            update(Booking)
            .where(Booking.id == booking_id, Booking.status == BookingStatus.PENDING)
            .values(status=BookingStatus.CONFIRMED)
            .returning(Booking.id)
            .execution_options(synchronize_session=False)
        )
    ).all()

    if not confirmed:
        # Seats were still HELD but the booking is no longer PENDING — a second
        # payment against an already-settled booking. Give this one back.
        await session.rollback()
        return await _refund(session, provider, order_ref=order_ref, payment_ref=payment_ref)

    show_id = booked[0][1]

    session.add(
        OutboxEvent(
            aggregate_type="booking",
            aggregate_id=booking_id,
            event_type="booking.confirmed",
            payload={
                "booking_id": str(booking_id),
                "show_id": str(show_id),
                "payment_ref": payment_ref,
                "seat_count": len(inventory_ids),
            },
        )
    )

    await session.commit()

    return ConfirmResult(
        outcome=ConfirmOutcome.CONFIRMED, booking_id=booking_id, affected_show_id=show_id
    )


async def _refund(
    session: AsyncSession,
    provider: PaymentProvider,
    *,
    order_ref: str,
    payment_ref: str,
) -> ConfirmResult:
    """Record and issue a refund for money we cannot honour with seats."""
    payment = (
        await session.execute(select(Payment).where(Payment.provider_ref == order_ref))
    ).scalar_one()

    payment.status = PaymentStatus.REFUNDED
    session.add(
        OutboxEvent(
            aggregate_type="payment",
            aggregate_id=payment.id,
            event_type="payment.refunded",
            payload={
                "booking_id": str(payment.booking_id),
                "order_ref": order_ref,
                "payment_ref": payment_ref,
                "reason": "seats_no_longer_held",
            },
        )
    )
    # Committed before calling the provider so the refund is durably recorded
    # even if the outbound call fails; the outbox row is the retry handle.
    await session.commit()

    await provider.refund(payment_ref=payment_ref)

    return ConfirmResult(outcome=ConfirmOutcome.REFUNDED, booking_id=payment.booking_id)


def extract_capture(event: dict[str, Any]) -> tuple[str, str] | None:
    """Pull (order_ref, payment_ref) out of a Razorpay payment.captured event."""
    if event.get("event") != "payment.captured":
        return None

    entity = event.get("payload", {}).get("payment", {}).get("entity", {})
    order_ref = entity.get("order_id")
    payment_ref = entity.get("id")

    if not order_ref or not payment_ref:
        return None

    return str(order_ref), str(payment_ref)
