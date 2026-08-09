"""Mutating endpoints: Accept, Contest, Pay, task create and notification read.

Each one prepares everything it can outside the store lock — key validation, body
parsing, attachment streaming, hashing, digesting — and then performs exactly one
:meth:`JsonStore.commit`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from starlette.datastructures import FormData, UploadFile

from app.api.deps import context_of, envelope, meta_for, request_id_of
from app.core.config import MAX_ATTACHMENTS
from app.core.errors import (
    AttachmentTooManyError,
    MalformedRequestError,
    ValidationError,
)
from app.core.idempotency import contest_digest, json_digest, require_idempotency_key, scope_key
from app.domain.charge_state import ChargeEvent
from app.schemas.charge import (
    Charge,
    ChargeCommandRequest,
    ContestMetadata,
    NotificationReadRequest,
)
from app.schemas.common import Envelope
from app.schemas.task import CreateTaskRequest, MaintenanceTask
from app.services.attachment_service import AttachmentStager, FinalizedAttachment
from app.services.charge_service import CommandContext, validate_task_payload

router = APIRouter()

ACCEPT_ROUTE = "/api/v1/charges/{chargeId}/accept"
CONTEST_ROUTE = "/api/v1/charges/{chargeId}/contest"
PAY_ROUTE = "/api/v1/charges/{chargeId}/pay"
TASKS_ROUTE = "/api/v1/tasks"


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MalformedRequestError("request body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise MalformedRequestError("request body must be a JSON object")
    return payload


def _model(model_type: type[Any], payload: dict[str, Any]) -> Any:
    try:
        return model_type.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError(
            "request failed field validation", details={"errors": _redact_errors(exc)}
        ) from exc


def _redact_errors(exc: PydanticValidationError) -> list[dict[str, Any]]:
    """Field paths and rule names only: user text never leaves in an error body."""
    return [
        {"field": ".".join(str(part) for part in error["loc"]), "rule": error["type"]}
        for error in exc.errors()
    ]


async def _run_charge_command(
    request: Request,
    *,
    charge_id: str,
    event: ChargeEvent,
    route_template: str,
) -> dict[str, Any]:
    context = context_of(request)
    key = require_idempotency_key(request.headers.get("Idempotency-Key"))
    payload = await _json_body(request)
    command: ChargeCommandRequest = _model(ChargeCommandRequest, payload)
    result = await context.service.apply_charge_command(
        charge_id=charge_id,
        event=event,
        expected_state_epoch=command.expectedStateEpoch,
        expected_version=command.expectedVersion,
        context=CommandContext(
            idempotency_key=key,
            request_digest=json_digest(payload),
            scope=scope_key(method="POST", route_template=route_template, resource_id=charge_id, key=key),
        ),
    )
    return envelope(request, result.value.body["charge"])


@router.post(
    "/charges/{charge_id}/accept",
    response_model=Envelope[Charge],
    summary="Student accepts a charge",
)
async def accept_charge(request: Request, charge_id: str) -> dict[str, Any]:
    return await _run_charge_command(
        request, charge_id=charge_id, event=ChargeEvent.ACCEPT, route_template=ACCEPT_ROUTE
    )


@router.post(
    "/charges/{charge_id}/pay",
    response_model=Envelope[Charge],
    summary="Student pays an accepted charge",
)
async def pay_charge(request: Request, charge_id: str) -> dict[str, Any]:
    return await _run_charge_command(
        request, charge_id=charge_id, event=ChargeEvent.PAY, route_template=PAY_ROUTE
    )


@router.post(
    "/charges/{charge_id}/contest",
    response_model=Envelope[Charge],
    summary="Student contests a charge with a reason and up to five attachments",
)
async def contest_charge(request: Request, charge_id: str) -> dict[str, Any]:
    context = context_of(request)
    key = require_idempotency_key(request.headers.get("Idempotency-Key"))
    request_id = request_id_of(request)

    # The form is an async context manager: leaving it closes every spooled upload,
    # so no temporary file survives a rejected request.
    async with _contest_form(request) as form, AttachmentStager(context.settings, request_id) as stager:
        metadata, uploads = await _split_contest_parts(form)
        parsed: ContestMetadata = _model(ContestMetadata, metadata)
        reason = context.service.validate_contest_reason(parsed.reason)
        for upload in uploads:
            await stager.stage(upload)

        digest = contest_digest(
            metadata={
                "reason": reason,
                "expectedStateEpoch": parsed.expectedStateEpoch,
                "expectedVersion": parsed.expectedVersion,
            },
            attachments=[item.digest_part() for item in stager.staged],
        )
        finalized: list[FinalizedAttachment] = []

        def finalize() -> list[dict[str, object]]:
            finalized.extend(stager.finalize())
            return [item.as_json() for item in finalized]

        try:
            result = await context.service.apply_charge_command(
                charge_id=charge_id,
                event=ChargeEvent.CONTEST,
                expected_state_epoch=parsed.expectedStateEpoch,
                expected_version=parsed.expectedVersion,
                context=CommandContext(
                    idempotency_key=key,
                    request_digest=digest,
                    scope=scope_key(
                        method="POST",
                        route_template=CONTEST_ROUTE,
                        resource_id=charge_id,
                        key=key,
                    ),
                ),
                contest_reason=reason,
                finalize_attachments=finalize,
            )
        except Exception:
            # Files finalised by a mutation that did not commit must not survive it.
            stager.rollback(finalized)
            raise
        return envelope(request, result.value.body["charge"])


@asynccontextmanager
async def _contest_form(request: Request) -> AsyncIterator[FormData]:
    """Parse the multipart body, guaranteeing every spooled part is closed on exit."""
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        raise MalformedRequestError("Contest requires a multipart/form-data body")
    try:
        form = await request.form(max_files=MAX_ATTACHMENTS + 1, max_fields=8)
    except Exception as exc:  # Starlette raises several parser-specific types.
        raise MalformedRequestError("multipart body could not be parsed") from exc
    try:
        yield form
    finally:
        await form.close()


async def _split_contest_parts(form: FormData) -> tuple[dict[str, Any], list[UploadFile]]:
    """Split the parsed form into the required metadata part and the file parts."""
    raw_metadata = form.get("metadata")
    if raw_metadata is None:
        raise MalformedRequestError("multipart body is missing the required 'metadata' part")
    if isinstance(raw_metadata, UploadFile):
        declared = (raw_metadata.content_type or "").split(";", 1)[0].strip()
        if declared and declared != "application/json":
            raise MalformedRequestError("'metadata' part must declare Content-Type application/json")
        raw_text = (await raw_metadata.read()).decode("utf-8", errors="strict")
    else:
        raw_text = raw_metadata
    try:
        metadata = json.loads(raw_text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MalformedRequestError("'metadata' part is not valid JSON") from exc
    if not isinstance(metadata, dict):
        raise MalformedRequestError("'metadata' part must be a JSON object")

    uploads = [item for item in form.getlist("attachments") if isinstance(item, UploadFile)]
    if len(uploads) > MAX_ATTACHMENTS:
        raise AttachmentTooManyError(
            f"at most {MAX_ATTACHMENTS} attachments are accepted", details={"limit": MAX_ATTACHMENTS}
        )
    return metadata, uploads


@router.post(
    "/tasks",
    response_model=Envelope[MaintenanceTask],
    status_code=201,
    summary="Create a maintenance task with a client-generated id",
)
async def create_task(request: Request, response: Response) -> JSONResponse:
    context = context_of(request)
    key = require_idempotency_key(request.headers.get("Idempotency-Key"))
    payload = await _json_body(request)
    command: CreateTaskRequest = _model(CreateTaskRequest, payload)
    task = {
        "id": command.id,
        "bookingId": command.bookingId,
        "category": command.category,
        "notes": command.notes.strip(),
        "location": command.location,
        "date": command.date,
        "status": "new",
    }
    validate_task_payload(task)
    result = await context.service.create_task(
        task=task,
        expected_state_epoch=command.expectedStateEpoch,
        context=CommandContext(
            idempotency_key=key,
            request_digest=json_digest(payload),
            scope=scope_key(method="POST", route_template=TASKS_ROUTE, resource_id=command.id, key=key),
        ),
    )
    _ = response
    return JSONResponse(
        status_code=result.value.status_code,
        content=envelope(request, result.value.body["task"]),
    )


@router.post(
    "/notifications/{notification_id}/read",
    summary="Mark one notification read (naturally idempotent)",
)
async def mark_notification_read(request: Request, notification_id: str) -> dict[str, Any]:
    context = context_of(request)
    payload = await _json_body(request)
    command: NotificationReadRequest = _model(NotificationReadRequest, payload)
    result = await context.service.mark_notification_read(
        notification_id=notification_id,
        expected_state_epoch=command.expectedStateEpoch,
    )
    return envelope(request, result.value)


__all__ = ["meta_for", "router"]
