# 1. Conditional UPDATE over SELECT FOR UPDATE or Redis locks

## Status
Accepted

## Context
Two users must never be able to hold or book the same seat. We need one
mechanism that is the single source of truth for this guarantee.

Three options were considered:
- `SELECT ... FOR UPDATE` then `UPDATE` in the same transaction.
- A distributed lock in Redis (e.g. Redlock) guarding the seat before touching Postgres.
- A single conditional `UPDATE ... WHERE status = 'AVAILABLE'` and checking the
  row count.

## Decision
Use a single conditional `UPDATE seat_inventory SET status = 'HELD', held_by = ?,
hold_expires_at = ? WHERE show_id = ? AND seat_id IN (?) AND status = 'AVAILABLE'`,
and compare the returned row count to the number of requested seats. If they
don't match, roll back and return 409 listing the seats that failed.

## Rationale
- **No SELECT-then-UPDATE**: a read followed by a write is a race window no
  matter what isolation level is used unless paired with locking, and adds a
  round trip for no benefit — the UPDATE's WHERE clause already expresses the
  precondition atomically.
- **No Redis locks for correctness**: Redis (Redlock or otherwise) is not the
  system of record for seat state and introduces a second source of truth that
  can drift from Postgres (lock acquired but process crashes before releasing,
  clock skew, network partition). Using it for correctness would mean the
  actual guarantee lives outside the database that enforces the UNIQUE
  constraint and CHECK constraints. Redis stays in this system for caching and
  rate limiting only.
- Postgres already serializes concurrent UPDATEs to the same row; a failed
  conditional UPDATE (rowcount 0 for a seat) is enough to detect and reject a
  losing request without any explicit locking statement.
- This is boring and easy to reason about: the row count from a single
  statement is the entire correctness argument, verifiable by the concurrency
  test that fires N simultaneous holds at the same seat and asserts exactly
  one winner.

## Consequences
- Every hold request is a single round trip to Postgres for the state
  transition, keeping latency low and avoiding lock-timeout tuning.
- Callers must handle the "some seats failed" case (HTTP 409) explicitly;
  there is no ambiguity or partial success on the same seat.
