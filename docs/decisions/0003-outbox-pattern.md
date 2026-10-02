# 3. Outbox pattern for event publishing

## Status
Accepted

## Context
State changes (booking confirmed, seat booked, payment captured) need to
trigger side effects outside the main transaction: emails, ticket generation,
analytics. Publishing directly to a broker/queue from inside the request
handler risks a dual-write: the DB transaction commits but the publish fails
(or vice versa), leaving state and side effects inconsistent — e.g. a
confirmed booking with no ticket email ever sent.

## Decision
Write an `OutboxEvent` row in the same transaction as the state change it
announces. A separate relay process polls for unpublished events
(`published_at IS NULL`) and publishes them (to the worker queue, or directly
performs the side effect), then marks them published.

## Rationale
- The event and the state change either both commit or both roll back —
  ordinary transactional guarantees from Postgres, no distributed transaction
  needed.
- The relay is idempotent-safe to retry: if it crashes after publishing but
  before marking `published_at`, the event is republished, and downstream
  consumers (ticket generation, email) are required to be idempotent anyway
  per the non-negotiables in calude.md.
- Keeps the webhook and booking-confirmation code paths free of direct broker
  calls, so they only need to reason about their own transaction.

## Consequences
- Adds a small amount of publish latency (poll interval) versus publishing
  inline.
- Requires the relay/poller to be a running process (part of the worker), and
  requires downstream handlers to be idempotent — already required by the
  at-least-once webhook non-negotiable.
