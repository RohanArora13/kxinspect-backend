"""Server-sent events.

Subscribe bootstrap takes the store lock, then the broker lock, prefills the replay queue
from the retained ring, registers the subscriber and releases both — before any socket
write. The operation-barrier lease is released at the same point, so a long-lived stream
never blocks a reset.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from app.api.deps import context_of
from app.db.models import StoredEvent
from app.services.event_bus import Subscriber, replayable_events

router = APIRouter()


@router.get(
    "/events",
    summary="Event stream; resume with Last-Event-ID",
    responses={200: {"content": {"text/event-stream": {}}}, 503: {"description": "Stream closed"}},
)
async def event_stream(request: Request) -> EventSourceResponse:
    context = context_of(request)
    cursor = request.headers.get("last-event-id") or request.query_params.get("lastEventId")

    async with context.store.barrier.shared(), context.store.locked_snapshot() as snapshot:
        replay, servable = replayable_events(
            events=snapshot["events"],
            stream_epoch=snapshot["streamEpoch"],
            evicted_through=snapshot["streamEvictedThrough"],
            cursor=cursor,
        )
        if not servable:
            return EventSourceResponse(_sync_required())
        async with context.broker.lock:
            pass
        subscriber = await context.broker.register(replay)

    return EventSourceResponse(
        _stream(request, context.broker, subscriber, context.settings.sse_heartbeat_seconds),
        ping=max(1, int(context.settings.sse_heartbeat_seconds)),
    )


async def _sync_required() -> AsyncIterator[dict[str, str]]:
    """One id-less event telling the client to take a fresh snapshot, then close."""
    yield {
        "event": "sync.required",
        "data": json.dumps({"reason": "cursorUnservable"}, separators=(",", ":")),
    }


async def _stream(
    request: Request,
    broker: object,
    subscriber: Subscriber,
    heartbeat_seconds: float,
) -> AsyncIterator[dict[str, str]]:
    from app.services.event_bus import EventBroker  # local import keeps the module acyclic

    assert isinstance(broker, EventBroker)
    try:
        while True:
            if await request.is_disconnected():
                return
            event = await subscriber.next_event(timeout=heartbeat_seconds)
            if event is None:
                if subscriber.closed:
                    return
                continue  # heartbeat handled by sse-starlette's ping
            yield _render(event)
    except asyncio.CancelledError:  # pragma: no cover - client disconnects
        raise
    finally:
        with contextlib.suppress(Exception):
            await broker.unregister(subscriber)


def _render(event: StoredEvent) -> dict[str, str]:
    return {
        "id": event["id"],
        "event": event["type"],
        "data": json.dumps(event, separators=(",", ":"), sort_keys=True),
    }
