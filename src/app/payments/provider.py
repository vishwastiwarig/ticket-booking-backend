"""Payment provider boundary.

The rest of the codebase talks to ``PaymentProvider``, never to Razorpay
directly, so the booking logic can be tested without network access and a
second provider could be added without touching the confirm path.
"""

import asyncio
import hashlib
import hmac
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

import razorpay


@dataclass(frozen=True)
class ProviderOrder:
    order_id: str
    amount_minor: int
    currency: str


class PaymentProvider(Protocol):
    async def create_order(
        self, *, amount: Decimal, currency: str, receipt: str
    ) -> ProviderOrder: ...

    async def refund(self, *, payment_ref: str) -> None: ...


def to_minor_units(amount: Decimal) -> int:
    """Razorpay bills in paise, not rupees."""
    return int(amount * 100)


def verify_webhook_signature(*, body: bytes, signature: str, secret: str) -> bool:
    """Razorpay signs the webhook with HMAC-SHA256 over the raw request body.

    Must be checked against the exact bytes received: re-serialising the parsed
    JSON can reorder keys or change spacing and would fail a valid signature
    (or, worse, pass a tampered one). ``compare_digest`` avoids leaking the
    expected value through timing.
    """
    if not signature or not secret:
        return False

    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class RazorpayProvider:
    def __init__(self, key_id: str, key_secret: str) -> None:
        self._client = razorpay.Client(auth=(key_id, key_secret))

    async def create_order(
        self, *, amount: Decimal, currency: str, receipt: str
    ) -> ProviderOrder:
        # The Razorpay SDK is synchronous; off-loading it keeps one slow HTTP
        # call from stalling the whole event loop.
        created: dict[str, Any] = await asyncio.to_thread(
            self._client.order.create,
            {
                "amount": to_minor_units(amount),
                "currency": currency,
                "receipt": receipt,
                # Ask Razorpay to capture automatically so a successful payment
                # produces exactly one payment.captured webhook.
                "payment_capture": 1,
            },
        )
        return ProviderOrder(
            order_id=str(created["id"]),
            amount_minor=int(created["amount"]),
            currency=str(created["currency"]),
        )

    async def refund(self, *, payment_ref: str) -> None:
        await asyncio.to_thread(self._client.payment.refund, payment_ref, {})


@dataclass
class FakePaymentProvider:
    """In-memory provider for tests; records what it was asked to do."""

    orders: list[ProviderOrder] = field(default_factory=list)
    refunds: list[str] = field(default_factory=list)

    async def create_order(
        self, *, amount: Decimal, currency: str, receipt: str
    ) -> ProviderOrder:
        order = ProviderOrder(
            order_id=f"order_{uuid.uuid4().hex[:14]}",
            amount_minor=to_minor_units(amount),
            currency=currency,
        )
        self.orders.append(order)
        return order

    async def refund(self, *, payment_ref: str) -> None:
        self.refunds.append(payment_ref)
