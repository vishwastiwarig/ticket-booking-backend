"""Seat map read path and its cache (calude.md critical flow 5)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient
from redis.asyncio import Redis

from .conftest import SeededShow


async def build_show(client: AsyncClient, *, rows: int = 2) -> dict[str, Any]:
    """Create a venue/section/seats/event/show through the public CRUD API."""
    venue = (await client.post("/venues", json={"name": "Odeon"})).json()
    section = (
        await client.post(f"/venues/{venue['id']}/sections", json={"name": "Stalls"})
    ).json()
    seats = (
        await client.post(
            f"/sections/{section['id']}/seats",
            json={"seats": [{"row_label": "A", "seat_number": n} for n in range(1, rows + 1)]},
        )
    ).json()
    event = (await client.post("/events", json={"name": "Dune"})).json()
    show = (
        await client.post(
            f"/events/{event['id']}/shows",
            json={
                "venue_id": venue["id"],
                "start_time": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
        )
    ).json()
    return {"venue": venue, "section": section, "seats": seats, "show": show}


async def test_creating_a_show_materialises_inventory_for_every_seat(
    client: AsyncClient,
) -> None:
    built = await build_show(client, rows=3)

    assert built["show"]["seats_total"] == 3

    seat_map = (await client.get(f"/shows/{built['show']['id']}/seatmap")).json()
    seats = [seat for section in seat_map["sections"] for seat in section["seats"]]

    assert len(seats) == 3
    assert {seat["status"] for seat in seats} == {"AVAILABLE"}


async def test_seat_map_is_served_from_cache_on_repeat_read(
    client: AsyncClient, seeded_show: SeededShow
) -> None:
    first = await client.get(f"/shows/{seeded_show.show_id}/seatmap")
    second = await client.get(f"/shows/{seeded_show.show_id}/seatmap")

    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    # Same content either way — the cache must not change what a client sees.
    assert first.json()["sections"] == second.json()["sections"]


async def test_holding_a_seat_invalidates_the_cached_seat_map(
    client: AsyncClient, seeded_show: SeededShow
) -> None:
    """A stale seat map would send users at seats that are already gone."""
    await client.get(f"/shows/{seeded_show.show_id}/seatmap")

    held_seat = seeded_show.seat_ids[0]
    hold = await client.post(
        f"/shows/{seeded_show.show_id}/holds",
        json={
            "seat_ids": [str(held_seat)],
            "idempotency_key": uuid.uuid4().hex,
            "user_id": str(uuid.uuid4()),
        },
    )
    assert hold.status_code == 201

    after = await client.get(f"/shows/{seeded_show.show_id}/seatmap")

    # Cache was dropped by the hold, so this read went back to Postgres.
    assert after.json()["cached"] is False

    statuses = {
        seat["seat_id"]: seat["status"]
        for section in after.json()["sections"]
        for seat in section["seats"]
    }
    assert statuses[str(held_seat)] == "HELD"


async def test_seat_map_for_unknown_show_is_404(client: AsyncClient) -> None:
    response = await client.get(f"/shows/{uuid.uuid4()}/seatmap")
    assert response.status_code == 404


async def test_cache_entry_carries_a_ttl(
    client: AsyncClient, redis_client: Redis, seeded_show: SeededShow
) -> None:
    """The TTL is the backstop for any invalidation this code forgets."""
    await client.get(f"/shows/{seeded_show.show_id}/seatmap")

    ttl = await redis_client.ttl(f"seatmap:{seeded_show.show_id}")

    assert 0 < ttl <= 10
