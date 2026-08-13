"""Demo-state reset shared by permanent and developer routes."""

from __future__ import annotations

from app.db.store import JsonStore
from app.services.event_bus import EventBroker


async def reset_demo_state(*, store: JsonStore, broker: EventBroker) -> None:
    """Close live streams, then restore canonical seed data under one exclusive lease."""
    await broker.close_all()
    async with store.barrier.exclusive():
        await store.reset()
