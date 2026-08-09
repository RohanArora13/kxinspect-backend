"""Dev-only controls.

These routes bind only when ``KX_ENABLE_DEV_ROUTES=true`` and a non-empty ``KX_DEV_TOKEN``
exists, and every call still verifies the token in constant time — loopback included.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response

from app.api.deps import envelope, require_dev_access
from app.api.v1.endpoints.commands import _json_body, _model
from app.domain.charge_state import ChargeEvent
from app.schemas.charge import Charge
from app.schemas.common import Envelope
from app.schemas.event import ChaosRequest, ChaosState, RaiseChargeRequest, ResolveChargeRequest

router = APIRouter()


@router.post("/_dev/reset", status_code=204, summary="Destructive reseed with a new state epoch")
async def reset(request: Request) -> Response:
    context = require_dev_access(request)
    await context.broker.close_all()
    async with context.store.barrier.exclusive():
        await context.store.reset()
    return Response(status_code=204)


@router.post("/_dev/chaos", response_model=Envelope[ChaosState], summary="Configure fault injection")
async def chaos(request: Request) -> dict[str, Any]:
    context = require_dev_access(request)
    payload = await _json_body(request)
    command: ChaosRequest = _model(ChaosRequest, payload)
    state = context.chaos.configure(
        latency_ms=command.latencyMs,
        fail_next=command.failNext,
        error_status=command.errorStatus,
        error_code=command.errorCode,
    )
    return envelope(request, state.as_dict())


@router.post(
    "/_dev/raise-charge",
    response_model=Envelope[Charge],
    status_code=201,
    summary="Inject an operator-raised outstanding charge and its feed notification",
)
async def raise_charge(request: Request) -> Response:
    from fastapi.responses import JSONResponse

    context = require_dev_access(request)
    payload = await _json_body(request)
    command: RaiseChargeRequest = _model(RaiseChargeRequest, payload)
    result = await context.service.dev_raise_charge(
        {
            "id": command.id,
            "bookingId": command.bookingId,
            "inspectionId": command.inspectionId,
            "itemName": command.itemName,
            "type": command.type,
            "notes": command.notes,
            "location": command.location,
            "amountMinor": command.amountMinor,
            "currency": command.currency,
            "raisedAt": command.raisedAt,
            "gracePeriodDays": command.gracePeriodDays,
            "photos": [],
        }
    )
    return JSONResponse(status_code=201, content=envelope(request, result.value))


@router.post(
    "/_dev/resolve-charge",
    response_model=Envelope[Charge],
    summary="Operator decision on a contested charge",
)
async def resolve_charge(request: Request) -> dict[str, Any]:
    context = require_dev_access(request)
    payload = await _json_body(request)
    command: ResolveChargeRequest = _model(ResolveChargeRequest, payload)
    result = await context.service.dev_resolve_charge(
        charge_id=command.chargeId,
        event=ChargeEvent(command.event),
    )
    return envelope(request, result.value)
