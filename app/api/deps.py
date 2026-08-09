"""Request-scoped access to the objects ``build_app`` wired together.

Everything hangs off ``app.state.context``; no module-level singletons exist, so tests can
build several apps in one process with different clocks and runtime roots.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from app.core.chaos import ChaosController
from app.core.clock import Clock, Scheduler, format_instant
from app.core.config import SCHEMA_VERSION, Settings
from app.core.errors import DevRouteForbiddenError
from app.core.ids import IdGenerator
from app.db.store import JsonStore
from app.services.charge_service import ChargeService
from app.services.event_bus import EventBroker


@dataclass(slots=True)
class AppContext:
    settings: Settings
    store: JsonStore
    clock: Clock
    scheduler: Scheduler
    id_generator: IdGenerator
    broker: EventBroker
    chaos: ChaosController
    service: ChargeService


def context_of(request: Request) -> AppContext:
    context: AppContext = request.app.state.context
    return context


def request_id_of(request: Request) -> str:
    value: str = request.state.request_id
    return value


def meta_for(request: Request) -> dict[str, Any]:
    """Envelope metadata attached to every JSON success and error response."""
    context = context_of(request)
    store = context.store
    return {
        "schemaVersion": SCHEMA_VERSION,
        "requestId": request_id_of(request),
        "serverTime": format_instant(context.clock.now()),
        "stateEpoch": store.state_epoch if store.is_open else "",
        "storeRevision": store.store_revision if store.is_open else 0,
    }


def stream_meta_for(request: Request, *, stream_epoch: str, stream_cursor: str) -> dict[str, Any]:
    return {**meta_for(request), "streamEpoch": stream_epoch, "streamCursor": stream_cursor}


def envelope(request: Request, data: Any) -> dict[str, Any]:
    return {"data": data, "meta": meta_for(request)}


def require_dev_access(request: Request) -> AppContext:
    """Dev routes always require a constant-time token match, loopback included."""
    context = context_of(request)
    settings = context.settings
    if not settings.enable_dev_routes or not settings.dev_token:
        raise DevRouteForbiddenError("dev routes are disabled")
    supplied = request.headers.get("X-Dev-Token", "")
    if not hmac.compare_digest(supplied, settings.dev_token):
        raise DevRouteForbiddenError("invalid dev token")
    return context
