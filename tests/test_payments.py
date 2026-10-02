"""Payment order creation and webhook confirmation (calude.md flows 2 and 3)."""

import json
import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bookings.models import Booking, BookingStatus
from app.core.models import OutboxEvent
from app.inventory.models import SeatInventory, SeatStatus
from app.payments.models import Payment, PaymentStatus
from app.payments.provider import FakePaymentProvider

from .conftest import SeededShow, capture_event, sign_webhook


async def make_hold(client: AsyncClient, show: SeededShow, seat_count: int = 2) -> str:
    response = await client.post(
        f"/shows/{show.show_id}/holds",
        json={
            "seat_ids": [str(s) for s in show.seat_ids[:seat_count]],
            "idempotency_key": uuid.uuid4().hex,
            "user_id": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 201
    return str(response.json()["booking_id"])


async def post_webhook(
    client: AsyncClient, order_ref: str, payment_ref: str = "pay_testpayment01"
) -> tuple[int, dict[str, str]]:
    body = json.dumps(capture_event(order_ref, payment_ref)).encode()
    response = await client.post(
        "/webhooks/razorpay",
        content=body,
        headers={
            "X-Razorpay-Signature": sign_webhook(body),
            "Content-Type": "application/json",
        },
    )
    return response.status_code, response.json()


async def test_pay_creates_order_priced_by_seat_count(
    client: AsyncClient, seeded_show: SeededShow, payment_provider: FakePaymentProvider
) -> None:
    booking_id = await make_hold(client, seeded_show, seat_count=2)

    response = await client.post(
        f"/bookings/{booking_id}/pay", json={"idempotency_key": uuid.uuid4().hex}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["order_id"].startswith("order_")
    # 2 seats at the configured 250.00 each.
    assert body["amount"] == "500.00"
    assert body["amount_minor"] == 50000
    assert body["currency"] == "INR"
    # The client needs the publishable key id to open Razorpay Checkout.
    assert body["key_id"] == "rzp_test_fake"
    assert len(payment_provider.orders) == 1


async def test_repeated_pay_reuses_the_same_order(
    client: AsyncClient, seeded_show: SeededShow, payment_provider: FakePaymentProvider
) -> None:
    """Two live orders for one booking would be two ways to pay for one seat."""
    booking_id = await make_hold(client, seeded_show, seat_count=1)

    first = await client.post(
        f"/bookings/{booking_id}/pay", json={"idempotency_key": uuid.uuid4().hex}
    )
    second = await client.post(
        f"/bookings/{booking_id}/pay", json={"idempotency_key": uuid.uuid4().hex}
    )

    assert first.json()["order_id"] == second.json()["order_id"]
    assert len(payment_provider.orders) == 1


async def test_pay_for_unknown_booking_is_404(client: AsyncClient) -> None:
    response = await client.post(
        f"/bookings/{uuid.uuid4()}/pay", json={"idempotency_key": uuid.uuid4().hex}
    )
    assert response.status_code == 404


async def test_webhook_confirms_booking_and_books_seats(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded_show: SeededShow,
) -> None:
    booking_id = await make_hold(client, seeded_show, seat_count=2)
    order = (
        await client.post(
            f"/bookings/{booking_id}/pay", json={"idempotency_key": uuid.uuid4().hex}
        )
    ).json()

    code, ack = await post_webhook(client, order["order_id"])

    assert code == 200
    assert ack["status"] == "confirmed"

    async with session_factory() as session:
        booking = (
            await session.execute(select(Booking).where(Booking.id == uuid.UUID(booking_id)))
        ).scalar_one()
        seats = (
            (
                await session.execute(
                    select(SeatInventory).where(
                        SeatInventory.seat_id.in_(seeded_show.seat_ids[:2])
                    )
                )
            )
            .scalars()
            .all()
        )
        payment = (
            await session.execute(
                select(Payment).where(Payment.provider_ref == order["order_id"])
            )
        ).scalar_one()
        events = (await session.execute(select(OutboxEvent))).scalars().all()

    assert booking.status == BookingStatus.CONFIRMED
    assert {seat.status for seat in seats} == {SeatStatus.BOOKED}
    # A booked seat has no hold deadline left to sweep.
    assert all(seat.hold_expires_at is None for seat in seats)
    assert payment.status == PaymentStatus.CAPTURED
    # The outbox row is what will drive the ticket email, in the same
    # transaction as the confirmation so the two cannot diverge.
    assert [event.event_type for event in events] == ["booking.confirmed"]


async def test_duplicate_webhook_is_idempotent(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded_show: SeededShow,
) -> None:
    """Webhooks are at-least-once; a redelivery must change nothing."""
    booking_id = await make_hold(client, seeded_show, seat_count=1)
    order = (
        await client.post(
            f"/bookings/{booking_id}/pay", json={"idempotency_key": uuid.uuid4().hex}
        )
    ).json()

    first_code, first_ack = await post_webhook(client, order["order_id"])
    second_code, second_ack = await post_webhook(client, order["order_id"])

    assert (first_code, first_ack["status"]) == (200, "confirmed")
    assert (second_code, second_ack["status"]) == (200, "already_processed")

    async with session_factory() as session:
        events = (await session.execute(select(OutboxEvent))).scalars().all()
        booking = (
            await session.execute(select(Booking).where(Booking.id == uuid.UUID(booking_id)))
        ).scalar_one()

    assert booking.status == BookingStatus.CONFIRMED
    # Critically: one event, not two — the customer gets one ticket email.
    assert len(events) == 1


async def test_webhook_with_bad_signature_is_rejected(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded_show: SeededShow,
) -> None:
    """Anyone can POST here; only a signed body may confirm a booking."""
    booking_id = await make_hold(client, seeded_show, seat_count=1)
    order = (
        await client.post(
            f"/bookings/{booking_id}/pay", json={"idempotency_key": uuid.uuid4().hex}
        )
    ).json()

    body = json.dumps(capture_event(order["order_id"])).encode()
    response = await client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"X-Razorpay-Signature": "deadbeef", "Content-Type": "application/json"},
    )

    assert response.status_code == 400

    async with session_factory() as session:
        booking = (
            await session.execute(select(Booking).where(Booking.id == uuid.UUID(booking_id)))
        ).scalar_one()

    assert booking.status == BookingStatus.PENDING


async def test_missing_signature_is_rejected(client: AsyncClient) -> None:
    body = json.dumps(capture_event("order_whatever")).encode()
    response = await client.post("/webhooks/razorpay", content=body)
    assert response.status_code == 400


async def test_unhandled_event_type_is_acknowledged(client: AsyncClient) -> None:
    """Razorpay sends many event types; we must not make it retry forever."""
    body = json.dumps({"event": "payment.failed", "payload": {}}).encode()
    response = await client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"X-Razorpay-Signature": sign_webhook(body)},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


async def test_late_payment_after_expiry_is_refunded(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seeded_show: SeededShow,
    payment_provider: FakePaymentProvider,
) -> None:
    """The edge case the spec calls out: money arrives after the seats are gone."""
    booking_id = await make_hold(client, seeded_show, seat_count=1)
    order = (
        await client.post(
            f"/bookings/{booking_id}/pay", json={"idempotency_key": uuid.uuid4().hex}
        )
    ).json()

    # The hold lapses and the sweep hands the seat back before the money lands.
    async with session_factory() as session:
        from app.bookings.service import expire_stale_holds

        seat = (
            await session.execute(
                select(SeatInventory).where(SeatInventory.seat_id == seeded_show.seat_ids[0])
            )
        ).scalar_one()
        seat.hold_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()
        await expire_stale_holds(session)

    code, ack = await post_webhook(client, order["order_id"], payment_ref="pay_late01")

    assert code == 200
    assert ack["status"] == "refunded"
    # The refund actually reached the provider, not just the database.
    assert payment_provider.refunds == ["pay_late01"]

    async with session_factory() as session:
        booking = (
            await session.execute(select(Booking).where(Booking.id == uuid.UUID(booking_id)))
        ).scalar_one()
        payment = (
            await session.execute(
                select(Payment).where(Payment.provider_ref == order["order_id"])
            )
        ).scalar_one()
        seat = (
            await session.execute(
                select(SeatInventory).where(SeatInventory.seat_id == seeded_show.seat_ids[0])
            )
        ).scalar_one()
        events = (await session.execute(select(OutboxEvent))).scalars().all()

    assert booking.status == BookingStatus.EXPIRED
    assert payment.status == PaymentStatus.REFUNDED
    # The seat stays free for whoever holds it next — it was never sold twice.
    assert seat.status == SeatStatus.AVAILABLE
    assert [event.event_type for event in events] == ["payment.refunded"]


async def test_webhook_for_unknown_order_is_ignored(client: AsyncClient) -> None:
    code, ack = await post_webhook(client, "order_nosuchorder")
    assert code == 200
    assert ack["status"] == "ignored"
