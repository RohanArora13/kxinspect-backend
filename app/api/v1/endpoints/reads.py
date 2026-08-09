"""Read endpoints. Thin: they project store state and never mutate business data."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.deps import context_of, envelope, meta_for, stream_meta_for
from app.core.clock import format_instant, parse_instant
from app.core.config import CONTRACT_VERSION, SCHEMA_VERSION, SEED_VERSION
from app.core.errors import ValidationError
from app.domain.charge_state import ChargeStatus
from app.schemas.booking import Booking, InventoryReport
from app.schemas.charge import AppNotification, Charge, HubSnapshot, SyncSnapshotData
from app.schemas.common import Envelope, HealthData, StreamEnvelope
from app.schemas.inspection import Inspection

router = APIRouter()


@router.get("/health", response_model=Envelope[HealthData], summary="Liveness and version report")
async def health(request: Request) -> dict[str, object]:
    context = context_of(request)
    return envelope(
        request,
        {
            "status": "ok",
            "schemaVersion": SCHEMA_VERSION,
            "contractVersion": CONTRACT_VERSION,
            "seedVersion": SEED_VERSION,
            "serverTime": format_instant(context.clock.now()),
            "devRoutesEnabled": context.settings.enable_dev_routes,
        },
    )


@router.get("/bookings", response_model=Envelope[list[Booking]], summary="List bookings")
async def list_bookings(request: Request) -> dict[str, object]:
    return envelope(request, await context_of(request).service.bookings())


@router.get(
    "/bookings/{booking_id}/hub",
    response_model=Envelope[HubSnapshot],
    summary="Aggregate Maintenance Hub snapshot for one booking",
)
async def booking_hub(request: Request, booking_id: str) -> dict[str, object]:
    return envelope(request, await context_of(request).service.hub(booking_id))


@router.get(
    "/sync-snapshot",
    response_model=StreamEnvelope[SyncSnapshotData],
    summary="All server-owned rows plus the stream cursor to resume from",
)
async def sync_snapshot(request: Request) -> dict[str, object]:
    data, stream = await context_of(request).service.sync_snapshot()
    return {
        "data": data,
        "meta": stream_meta_for(
            request, stream_epoch=stream["streamEpoch"], stream_cursor=stream["streamCursor"]
        ),
    }


@router.get(
    "/inventory-reports/{report_id}",
    response_model=Envelope[InventoryReport],
    summary="Inventory report detail",
)
async def inventory_report(request: Request, report_id: str) -> dict[str, object]:
    return envelope(request, await context_of(request).service.report(report_id))


@router.get(
    "/inspections/{inspection_id}",
    response_model=Envelope[Inspection],
    summary="Inspection detail",
)
async def inspection(request: Request, inspection_id: str) -> dict[str, object]:
    return envelope(request, await context_of(request).service.inspection(inspection_id))


@router.get("/charges", response_model=Envelope[list[Charge]], summary="Filtered charge list")
async def list_charges(
    request: Request,
    bookingId: Annotated[str | None, Query(description="Restrict to one booking")] = None,
    status: Annotated[list[str] | None, Query(description="Repeatable status filter")] = None,
) -> dict[str, object]:
    statuses = tuple(status or ())
    known = {item.value for item in ChargeStatus}
    unknown = sorted(set(statuses) - known)
    if unknown:
        raise ValidationError(
            "unknown charge status filter", details={"status": unknown, "allowed": sorted(known)}
        )
    return envelope(
        request,
        await context_of(request).service.charges(booking_id=bookingId, statuses=statuses),
    )


@router.get("/charges/{charge_id}", response_model=Envelope[Charge], summary="Charge detail")
async def charge_detail(request: Request, charge_id: str) -> dict[str, object]:
    return envelope(request, await context_of(request).service.charge(charge_id))


@router.get(
    "/notifications",
    response_model=Envelope[list[AppNotification]],
    summary="Notification feed",
)
async def list_notifications(
    request: Request,
    after: Annotated[str | None, Query(description="ISO-8601 UTC lower bound, exclusive")] = None,
    unreadOnly: Annotated[bool, Query(description="Return only unread rows")] = False,
) -> dict[str, object]:
    if after is not None:
        try:
            parse_instant(after)
        except ValueError as exc:
            raise ValidationError(
                "'after' must be an ISO-8601 UTC instant ending in Z", details={"after": after}
            ) from exc
    rows = await context_of(request).service.notifications(after=after, unread_only=unreadOnly)
    return envelope(request, rows)


__all__ = ["meta_for", "router"]
