"""Envelope, metadata and the primitive types every schema reuses.

Timestamps stay strings on the wire. The store already holds the exact contract form, and
round-tripping through ``datetime`` would let a serialiser quietly change precision or
offset spelling — which the frontend hashes.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

#: ISO-8601 UTC with a mandatory ``Z``. Optional milliseconds, never an offset.
ISO_UTC_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$"

IsoUtcInstant = Annotated[str, StringConstraints(pattern=ISO_UTC_PATTERN)]
EntityId = Annotated[str, StringConstraints(min_length=1, max_length=64, strip_whitespace=False)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
Uuid = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    ),
]
NonEmptyText = Annotated[str, StringConstraints(min_length=1)]


class StrictModel(BaseModel):
    """Rejects unknown fields, coercion, naive timestamps and floats for minor units."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Meta(StrictModel):
    schemaVersion: Literal["1"]
    requestId: str
    serverTime: IsoUtcInstant
    stateEpoch: str
    storeRevision: int = Field(ge=0)


class StreamMeta(Meta):
    """``/sync-snapshot`` adds the stream identity the SSE client must resume from."""

    streamEpoch: str
    streamCursor: str


class Envelope[DataT](StrictModel):
    data: DataT
    meta: Meta


class StreamEnvelope[DataT](StrictModel):
    data: DataT
    meta: StreamMeta


class ErrorBody(StrictModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(StrictModel):
    error: ErrorBody
    meta: Meta


class HealthData(StrictModel):
    status: Literal["ok"]
    schemaVersion: Literal["1"]
    contractVersion: Literal["1"]
    seedVersion: str
    serverTime: IsoUtcInstant
    devRoutesEnabled: bool
