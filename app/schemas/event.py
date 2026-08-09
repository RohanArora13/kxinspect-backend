"""SSE event and dev-control wire schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.common import CurrencyCode, EntityId, IsoUtcInstant, NonEmptyText, StrictModel

EventTypeLiteral = Literal[
    "charge.created", "charge.updated", "task.created", "notification.updated", "sync.required"
]


class EventData(StrictModel):
    """Allow-listed invalidation payload. Carries no user text, filename or hash."""

    resourceType: Literal["charge", "task", "notification"]
    changeType: Literal["created", "updated"]


class StreamEvent(StrictModel):
    id: str
    stateEpoch: str
    storeRevision: int = Field(ge=0)
    type: EventTypeLiteral
    occurredAt: IsoUtcInstant
    bookingId: EntityId | None
    entityId: EntityId
    entityVersion: int | None
    data: EventData


class ChaosRequest(StrictModel):
    latencyMs: int = Field(default=0, ge=0, le=60_000)
    failNext: int = Field(default=0, ge=0, le=1_000)
    errorStatus: int = Field(default=503, ge=400, le=599)
    errorCode: NonEmptyText = "chaos.injected"


class ChaosState(StrictModel):
    latencyMs: int
    failNext: int
    errorStatus: int
    errorCode: str
    excludedPaths: list[str]


class RaiseChargeRequest(StrictModel):
    """Dev control: inject an operator-raised outstanding charge."""

    id: EntityId
    bookingId: EntityId
    inspectionId: EntityId | None = None
    itemName: NonEmptyText
    type: Literal["replace", "repair", "clean"]
    notes: str = ""
    location: NonEmptyText
    amountMinor: int = Field(ge=0)
    currency: CurrencyCode = "GBP"
    raisedAt: IsoUtcInstant
    gracePeriodDays: int = Field(default=30, ge=0)


class ResolveChargeRequest(StrictModel):
    """Dev control: the operator decision on a contested charge."""

    chargeId: EntityId
    event: Literal["operatorUphold", "operatorDismiss"]
