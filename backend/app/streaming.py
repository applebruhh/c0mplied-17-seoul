import asyncio
import json
import time

from fastapi import Request

from app.services import POSService


def encode(kind: str, event_id: int, data: dict) -> str:
    return f"id: {event_id}\nevent: {kind}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def stream_events(request: Request, service: POSService, snapshot: dict,
                        checkout_id: str | None, after: int | None, follow: bool):
    """Replay to a consistent snapshot cursor, then tail the durable event table."""
    cutoff = snapshot["latest_event_id"]
    if after is not None and after > cutoff:
        yield encode("resync", cutoff, {"reason": "cursor_ahead_of_database", "latest_event_id": cutoff})
    elif after is not None:
        replay_cursor = after
        while True:
            batch = await asyncio.to_thread(service.events, replay_cursor, checkout_id, cutoff)
            if not batch:
                break
            for event in batch:
                yield encode(event.event_type, event.event_id, event.model_dump(mode="json"))
                replay_cursor = event.event_id
    yield encode("snapshot", cutoff, snapshot)
    if not follow:
        return
    last_id = cutoff
    heartbeat = time.monotonic()
    while not await request.is_disconnected():
        batch = await asyncio.to_thread(service.events, last_id, checkout_id)
        for event in batch:
            yield encode(event.event_type, event.event_id, event.model_dump(mode="json"))
            last_id = event.event_id
        if time.monotonic() - heartbeat >= 15:
            yield ": keepalive\n\n"
            heartbeat = time.monotonic()
        # Fetch the next batch immediately if the first was full.
        if len(batch) < 100:
            await asyncio.sleep(0.25)
