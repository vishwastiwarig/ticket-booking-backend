# Seat-based Ticket Booking System

Production-quality backend for booking seats at shows. The core problem is
correctness under concurrency: two users must never get the same seat, and
money must never be lost or double-charged. See [calude.md](calude.md) for
the full spec and [docs/decisions/](docs/decisions/) for architecture
decisions.

## Status

Following the build order in calude.md.

- [x] Repo skeleton, Docker Compose, schema + migrations, health endpoint, CI
- [x] Venue/show/seat CRUD + cached seat-map endpoint
- [x] Hold flow (makes the concurrency test pass) + expiry job
- [x] Payments + webhooks + idempotency + late-payment refund
- [ ] Ticket generation, email, cancellation with refund rules
- [ ] Waiting-room queue, load test, observability dashboards

Steps 2 and 3 were built in the opposite order to the spec: the hold flow is
what the whole design exists for, and tests could seed shows through the
models, so CRUD was not on its critical path.

The expiry sweep is implemented as `bookings.service.expire_stale_holds` and
tested directly; scheduling it on ARQ every 30s is still to do. The `worker`
service in `docker-compose.yml` and the Razorpay dependency are declared
because the stack is fixed up front; they become functional as the
corresponding build-order steps land.

## Stack

Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0 (async), Alembic,
PostgreSQL, Redis, ARQ, Razorpay (test mode), pytest + testcontainers, Ruff,
mypy --strict.

> Local interpreter note: 3.12 isn't installed on this machine (3.10, 3.11 and
> 3.13 are), so the local `.venv` was created with 3.13, which satisfies
> `requires-python = ">=3.12"`. Docker and CI both pin 3.12 per the fixed
> stack. Install 3.12 and recreate the venv if you want local and CI to match
> exactly.

## Local development

Postgres is installed natively here rather than via Docker (Docker setup comes
later in the project). Either source works — the app only needs a
`DATABASE_URL`.

```bash
cp .env.example .env          # then fill in real secrets; .env is gitignored
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload --app-dir src
```

Health check: `GET http://localhost:8000/health`
Swagger UI: `http://localhost:8000/docs`

## Payments

`POST /bookings/{id}/pay` creates a Razorpay order and returns the payload for
Razorpay Checkout. `POST /webhooks/razorpay` confirms the booking when the
`payment.captured` event arrives.

Set `RAZORPAY_WEBHOOK_SECRET` in `.env` (Razorpay dashboard → Settings →
Webhooks — it is **not** the same as the API key secret). Without it every
webhook is rejected with 400: signature verification fails closed, because an
unverified body is not evidence that money moved.

Signatures are HMAC-SHA256 over the **raw** request body and compared with
`hmac.compare_digest`. Confirmation is idempotent via a conditional UPDATE on
`payments.status` — a redelivered webhook returns `already_processed` and emits
no second outbox event, so the customer gets one ticket email, not two. A
payment landing after its hold lapsed is refunded automatically and logged.

## Measured behaviour

From `tests/test_load_100_users.py`, against real Postgres:

| Scenario | Result |
| --- | --- |
| 100 users → 50 seats (2 per seat) | 50 × `201`, 50 × `409`, ~1.8s, 0 double-holds |
| 100 users → 1 seat | 1 × `201`, 99 × `409` |
| 100 concurrent seat-map reads | all identical; cache never disagrees with Postgres |

## Inspecting the data

The native install ships **pgAdmin 4** — connect to `localhost:5432`, database
`booking`. Or use `psql` from the command line:

```bash
psql -h localhost -U booking -d booking
\dt                                    # list tables
select seat_id, status, held_by, hold_expires_at from seat_inventory;
select id, status, idempotency_key from bookings;
```

## Tests

Tests run against a **real Postgres** — never SQLite. The hold flow's
correctness depends on Postgres-specific behaviour (READ COMMITTED
re-evaluating an UPDATE's WHERE clause after a concurrent commit), so anything
else would prove nothing.

```bash
# against Postgres/Redis you already run — fastest loop (~5s)
TEST_DATABASE_URL=postgresql+asyncpg://booking:booking@localhost:5432/booking_test \
TEST_REDIS_URL=redis://localhost:6379/1 \
pytest

# or leave both unset and testcontainers starts them (needs Docker; this is what CI does)
pytest
```

`TEST_DATABASE_URL` must name a **separate database** from your dev one — the
suite creates and drops all tables around each test. The database name has to
contain `test` or the suite refuses to start, because pointing it at a dev
database silently wipes it.

`tests/test_concurrency_holds.py` is the non-negotiable one: it fires 10
concurrent hold requests at the same seat and asserts exactly one wins, zero
duplicates, and no orphaned bookings from the losers.

## Migrations

```bash
alembic revision --autogenerate -m "message"
alembic upgrade head
```

## Architecture

Modular monolith — one FastAPI app + one worker process, packages
`inventory/`, `bookings/`, `payments/`, `notifications/`, `core/`. No
microservices; cross-module calls go through service-layer interfaces, not
direct model imports.
