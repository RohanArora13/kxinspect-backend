"""Inspection wire schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.common import EntityId, IsoUtcInstant, NonEmptyText, StrictModel


class GeneralNote(StrictModel):
    id: EntityId
    recordedAt: IsoUtcInstant
    text: NonEmptyText


class ItemAction(StrictModel):
    id: EntityId
    itemName: NonEmptyText
    notes: str
    thumbnailUrl: str | None
    chargeId: EntityId | None
    amountMinor: int | None = Field(default=None, ge=0)


class ItemUpdate(StrictModel):
    id: EntityId
    itemName: NonEmptyText
    conditionNote: str


class Inspection(StrictModel):
    id: EntityId
    bookingId: EntityId
    type: Literal["preArrival", "postArrival", "midStay", "checkout"]
    status: Literal["pending", "completed"]
    location: NonEmptyText
    roomName: NonEmptyText
    date: IsoUtcInstant
    generalNotes: list[GeneralNote]
    itemActions: list[ItemAction]
    itemUpdates: list[ItemUpdate]
