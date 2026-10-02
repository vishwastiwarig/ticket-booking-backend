# 4. Webhook idempotency and lock ordering

## Status
Accepted

## Context
Razorpay delivers webhooks **at least once**: the same `payment.captured` event
can arrive twice (retry after a timeout, or a redelivery days later). Handling
one twice would confirm a booking twice, emit two outbox events, and send the
customer two tickets.

Separately, the confirm path and the expiry sweep both write to
`seat_inventory` and `bookings`. Taking those two locks in opposite orders
would deadlock under load.

## Decision
1. **Idempotency comes from a conditional UPDATE on the payment row**, not from
   a pre-check:
   `UPDATE payments SET status='CAPTURED' WHERE provider_ref=? AND status='CREATED'`.
   A redelivery matches zero rows, returns `already_processed`, and touches
   nothing else.
2. **The confirm path updates seats before the booking**, matching the order
   the expiry sweep uses.
3. A payment whose seats are no longer HELD, or whose booking is no longer
   PENDING, is **refunded** rather than forced through.

## Rationale
- Checking "is this already processed?" with a SELECT and then acting on the
  answer is the same read-then-write race the hold flow avoids: two concurrent
  deliveries would both read CREATED and both proceed. Making the precondition
  part of the UPDATE closes that window, and reuses the pattern from ADR-0001
  rather than inventing a second concurrency story.
- Consistent lock ordering is the cheapest deadlock prevention available: no
  retry loops, no advisory locks, no serializable isolation. Both writers touch
  `seat_inventory` first and `bookings` second, so one simply waits for the
  other.
- Refunding is the only honest option when seats cannot be delivered. Taking
  money for seats that belong to someone else is worse than any refund fee, and
  forcing a confirmation would double-sell the seat.

## Consequences
- The refund is recorded (payment row + outbox event) and committed **before**
  the provider call, so a failed outbound call leaves a durable record to retry
  from rather than losing the obligation.
- A second payment against an already-confirmed booking is refunded rather than
  merged; this is correct but means a customer who somehow pays twice sees a
  refund rather than a credit.
- The webhook endpoint returns 200 for events it does not act on (unknown
  orders, other event types), because a non-2xx would make Razorpay redeliver
  something that will never succeed.
