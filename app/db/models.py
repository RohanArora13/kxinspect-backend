"""Shapes persisted in ``runtime/state.json``.

Entities are held as JSON-native dictionaries: the wire contract is the source of truth
and every response is validated by a Pydantic schema on the way out, so a second
in-memory object model would only add a mapping layer that can drift.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final, TypedDict

JsonDict = dict[str, Any]


class EventType(StrEnum):
    """Frozen SSE event vocabulary."""

    CHARGE_CREATED = "charge.created"
    CHARGE_UPDATED = "charge.updated"
    TASK_CREATED = "task.created"
    NOTIFICATION_UPDATED = "notification.updated"
    #: Emitted without an id when a cursor cannot be served; never persisted.
    SYNC_REQUIRED = "sync.required"


class ResourceType(StrEnum):
    CHARGE = "charge"
    TASK = "task"
    NOTIFICATION = "notification"


class ChangeType(StrEnum):
    CREATED = "created"
    UPDATED = "updated"


class EventData(TypedDict):
    """Allow-listed invalidation payload. Never carries user text or attachment metadata."""

    resourceType: str
    changeType: str


class StoredEvent(TypedDict):
    id: str
    stateEpoch: str
    storeRevision: int
    type: str
    occurredAt: str
    bookingId: str | None
    entityId: str
    entityVersion: int | None
    data: EventData


class IdempotencyRecord(TypedDict):
    key: str
    scope: str
    requestDigest: str
    statusCode: int
    responseBody: JsonDict
    aggregateVersion: int | None
    eventIds: list[str]
    createdAt: str


class Entities(TypedDict):
    bookings: list[JsonDict]
    inventoryReports: list[JsonDict]
    inspections: list[JsonDict]
    tasks: list[JsonDict]
    charges: list[JsonDict]
    notifications: list[JsonDict]


class StoreSnapshot(TypedDict):
    schemaVersion: str
    contractVersion: str
    seedVersion: str
    stateEpoch: str
    storeRevision: int
    streamEpoch: str
    streamCursor: str
    #: Cursor of the newest event evicted from the ring; the baseline cursor while the
    #: ring is still complete. A subscriber cursor below this cannot be replayed.
    streamEvictedThrough: str
    entities: Entities
    events: list[StoredEvent]
    idempotency: dict[str, IdempotencyRecord]


ENTITY_KEYS: Final[tuple[str, ...]] = (
    "bookings",
    "inventoryReports",
    "inspections",
    "tasks",
    "charges",
    "notifications",
)

#: Seed file for each entity collection, in the order the exporter emits them.
SEED_FILES: Final[dict[str, str]] = {
    "bookings": "bookings.json",
    "inventoryReports": "inventory_reports.json",
    "inspections": "inspections.json",
    "tasks": "tasks.json",
    "charges": "charges.json",
    "notifications": "notifications.json",
}


def baseline_cursor(stream_epoch: str) -> str:
    """Cursor for an epoch that has committed no events yet."""
    return f"{stream_epoch}:0:0"


def event_id(stream_epoch: str, store_revision: int, ordinal: int) -> str:
    return f"{stream_epoch}:{store_revision}:{ordinal}"


def parse_cursor(cursor: str) -> tuple[str, int, int] | None:
    """Parse ``{streamEpoch}:{storeRevision}:{ordinal}``; ``None`` when unparseable."""
    parts = cursor.split(":")
    if len(parts) != 3:
        return None
    epoch, revision, ordinal = parts
    if not epoch:
        return None
    try:
        return epoch, int(revision), int(ordinal)
    except ValueError:
        return None
