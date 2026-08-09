"""Maintenance task wire schemas."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import StringConstraints

from app.schemas.common import EntityId, IsoUtcInstant, NonEmptyText, StrictModel, Uuid

TaskCategory = Literal["electrical", "plumbing", "heating", "appliance", "furniture", "cleaning", "other"]
TaskStatus = Literal["new", "inProgress", "outstanding", "accepted", "completed"]

#: Grapheme bounds are enforced by the service; the schema only rejects obvious abuse.
TaskNotes = Annotated[str, StringConstraints(min_length=1, max_length=4000)]


class MaintenanceTask(StrictModel):
    id: EntityId
    bookingId: EntityId
    category: TaskCategory
    notes: TaskNotes
    location: NonEmptyText
    date: IsoUtcInstant
    status: TaskStatus


class CreateTaskRequest(StrictModel):
    """Task id is a client-generated UUID and is authoritative on the server."""

    id: Uuid
    bookingId: EntityId
    category: TaskCategory
    notes: TaskNotes
    location: NonEmptyText
    date: IsoUtcInstant
    expectedStateEpoch: str
