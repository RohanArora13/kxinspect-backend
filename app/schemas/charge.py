"""Charge, evidence, contest and aggregate wire schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.booking import Booking, InventoryReport
from app.schemas.common import (
    CurrencyCode,
    EntityId,
    IsoUtcInstant,
    NonEmptyText,
    StrictModel,
)
from app.schemas.inspection import Inspection
from app.schemas.task import MaintenanceTask

ChargeType = Literal["replace", "repair", "clean"]
ChargeStatusLiteral = Literal["outstanding", "accepted", "contested", "resolved", "paid"]
AcceptanceOriginLiteral = Literal["student", "deadline", "operator"]


class Photo(StrictModel):
    id: EntityId
    url: NonEmptyText
    thumbnailUrl: NonEmptyText
    mediaType: NonEmptyText
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    altKey: NonEmptyText
    sortOrder: int = Field(ge=0)


class ContestAttachment(StrictModel):
    id: EntityId
    displayName: NonEmptyText
    mediaType: NonEmptyText
    sizeBytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    downloadUrl: NonEmptyText
    thumbnailUrl: str | None


class Charge(StrictModel):
    id: EntityId
    bookingId: EntityId
    inspectionId: EntityId | None
    itemName: NonEmptyText
    type: ChargeType
    notes: str
    location: NonEmptyText
    #: Integer minor units. Floats are rejected by the strict model.
    amountMinor: int = Field(ge=0)
    currency: CurrencyCode
    status: ChargeStatusLiteral
    raisedAt: IsoUtcInstant
    gracePeriodDays: int = Field(ge=0)
    deadlineAt: IsoUtcInstant
    photos: list[Photo]
    contestReason: str | None
    contestAttachments: list[ContestAttachment]
    acceptedAt: IsoUtcInstant | None
    contestedAt: IsoUtcInstant | None
    resolvedAt: IsoUtcInstant | None
    paidAt: IsoUtcInstant | None
    acceptanceOrigin: AcceptanceOriginLiteral | None
    version: int = Field(ge=1)
    updatedAt: IsoUtcInstant


class AppNotification(StrictModel):
    id: EntityId
    type: Literal[
        "chargeRaised",
        "chargeDeadlineApproaching",
        "chargeAccepted",
        "chargeResolved",
        "chargePaid",
    ]
    titleKey: NonEmptyText
    bodyKey: NonEmptyText
    chargeId: EntityId | None
    createdAt: IsoUtcInstant
    read: bool


class HubSnapshot(StrictModel):
    booking: Booking
    inventoryReports: list[InventoryReport]
    inspections: list[Inspection]
    tasks: list[MaintenanceTask]
    charges: list[Charge]
    notifications: list[AppNotification]
    generatedAt: IsoUtcInstant


class SyncSnapshotData(StrictModel):
    bookings: list[Booking]
    inventoryReports: list[InventoryReport]
    inspections: list[Inspection]
    tasks: list[MaintenanceTask]
    charges: list[Charge]
    notifications: list[AppNotification]


class ChargeCommandRequest(StrictModel):
    """Body of Accept and Pay."""

    expectedStateEpoch: str
    expectedVersion: int = Field(ge=1)


class ContestMetadata(StrictModel):
    """The required ``metadata`` part of the Contest multipart request."""

    reason: NonEmptyText
    expectedStateEpoch: str
    expectedVersion: int = Field(ge=1)


class NotificationReadRequest(StrictModel):
    expectedStateEpoch: str
