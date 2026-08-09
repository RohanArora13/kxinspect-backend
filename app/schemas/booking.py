"""Booking and inventory-report wire schemas."""

from __future__ import annotations

from typing import Literal

from app.schemas.common import EntityId, IsoUtcInstant, NonEmptyText, StrictModel


class Booking(StrictModel):
    id: EntityId
    propertyCode: NonEmptyText
    roomName: NonEmptyText
    displayLocation: NonEmptyText
    startDate: IsoUtcInstant
    endDate: IsoUtcInstant


class InventoryReport(StrictModel):
    id: EntityId
    bookingId: EntityId
    name: NonEmptyText
    summary: str
    status: Literal["pending", "completed"]
    location: NonEmptyText
    completedOn: IsoUtcInstant | None
    reportUrl: NonEmptyText
    downloadMediaType: NonEmptyText
    downloadFileName: NonEmptyText
