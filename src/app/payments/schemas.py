import uuid
from decimal import Decimal

from pydantic import BaseModel, Field

from app.payments.models import PaymentStatus


class PayRequest(BaseModel):
    # Accepted on every mutating endpoint per the non-negotiables. Order
    # creation is made idempotent by reusing the booking's existing unpaid
    # order (see payments.service.create_payment_order), so this key is
    # recorded for tracing rather than being the mechanism.
    idempotency_key: str = Field(min_length=1, max_length=128)


class PayResponse(BaseModel):
    """Everything Razorpay Checkout needs on the client side."""

    booking_id: uuid.UUID
    order_id: str
    amount: Decimal
    amount_minor: int
    currency: str
    key_id: str
    status: PaymentStatus


class WebhookAck(BaseModel):
    status: str
