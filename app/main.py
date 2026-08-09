"""Application construction.

``create_app()`` is the Uvicorn factory: it reads validated environment and builds the
production services. ``build_app`` takes every collaborator explicitly, so tests inject a
:class:`ManualClock`, a manual scheduler, deterministic ids and a temporary runtime root.
There is no mutable module-level app state.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.deps import AppContext, meta_for
from app.api.v1.router import build_router
from app.core.chaos import ChaosController
from app.core.clock import AsyncioScheduler, Clock, ManualClock, Scheduler, SystemClock
from app.core.config import (
    CONTRACT_VERSION,
    MAX_ATTACHMENT_TOTAL_BYTES,
    SCHEMA_VERSION,
    SEED_VERSION,
    Settings,
)
from app.core.errors import ApiError, ValidationError
from app.core.ids import IdGenerator, UuidGenerator
from app.db.models import StoredEvent
from app.db.store import JsonStore
from app.services.attachment_service import cleanup_staging
from app.services.charge_service import ChargeService
from app.services.event_bus import EventBroker

logger = logging.getLogger("kxinspect")

#: Hard ceiling applied before Starlette's multipart parser sees a single byte.
MAX_REQUEST_BYTES = MAX_ATTACHMENT_TOTAL_BYTES + 1 * 1024 * 1024

CORS_ALLOWED_HEADERS = ("Content-Type", "Idempotency-Key", "Last-Event-ID", "X-Dev-Token")
CORS_ALLOWED_METHODS = ("GET", "POST", "OPTIONS")

#: Paths that manage their own barrier lease or must stay reachable during a reset.
_SELF_MANAGED_PATHS = frozenset({"/api/v1/events", "/api/v1/_dev/reset"})


@dataclass(frozen=True, slots=True)
class _Envelope:
    code: str
    message: str
    details: dict[str, Any]


class MaxBodySizeMiddleware:
    """Raw ASGI byte counter.

    Content-Length is only a hint, so the streamed body is counted too. Rejecting here
    means an oversize upload never reaches the multipart parser.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = {key.decode("latin-1").lower(): value for key, value in scope.get("headers", [])}
        declared = headers.get("content-length")
        if declared is not None:
            with contextlib.suppress(ValueError):
                if int(declared) > self._max_bytes:
                    await self._reject(send)
                    return

        seen = 0
        rejected = False

        async def counting_receive() -> Any:
            nonlocal seen, rejected
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self._max_bytes:
                    rejected = True
                    return {"type": "http.disconnect"}
            return message

        if rejected:  # pragma: no cover - defensive
            await self._reject(send)
            return
        await self._app(scope, counting_receive, send)

    async def _reject(self, send: Send) -> None:
        body = (
            b'{"error":{"code":"attachment.total_too_large",'
            b'"message":"request body exceeds the accepted size",'
            b'"details":{"limitBytes":' + str(self._max_bytes).encode() + b"}},"
            b'"meta":{"schemaVersion":"1","requestId":"","serverTime":"1970-01-01T00:00:00Z",'
            b'"stateEpoch":"","storeRevision":0}}'
        )
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def build_app(
    settings: Settings,
    store: JsonStore,
    clock: Clock,
    scheduler: Scheduler,
    id_generator: IdGenerator,
) -> FastAPI:
    """Assemble the application from explicit collaborators."""
    broker = EventBroker(queue_capacity=settings.sse_queue_capacity)
    chaos = ChaosController()
    service = ChargeService(store=store, clock=clock, settings=settings, id_generator=id_generator)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store.open()
        cleanup_staging(settings)

        async def publish(events: tuple[StoredEvent, ...]) -> None:
            await broker.publish(events)

        store.set_publisher(publish)
        await _schedule_next_deadline(service, scheduler)
        logger.info(
            "kxinspect backend ready schema=%s contract=%s seed=%s dev_routes=%s",
            SCHEMA_VERSION,
            CONTRACT_VERSION,
            SEED_VERSION,
            settings.enable_dev_routes,
        )
        try:
            yield
        finally:
            await broker.close_all()
            await scheduler.shutdown()
            store.close()

    app = FastAPI(
        title="KxInspections mock backend",
        version=f"{CONTRACT_VERSION}.0.0",
        description=(
            "Contract v1 mock service for the KxInspections assignment. Demo data only: "
            "there is no authentication, no payment provider and no multi-process storage. "
            "Never expose this service as a production system."
        ),
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.context = AppContext(
        settings=settings,
        store=store,
        clock=clock,
        scheduler=scheduler,
        id_generator=id_generator,
        broker=broker,
        chaos=chaos,
        service=service,
    )

    app.include_router(build_router(include_dev_routes=settings.enable_dev_routes))
    _install_middleware(app, settings)
    _install_exception_handlers(app)
    return app


def _install_middleware(app: FastAPI, settings: Settings) -> None:
    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
        context: AppContext = request.app.state.context
        request.state.request_id = context.id_generator.next_uuid()
        try:
            # Middleware sits outside Starlette's exception middleware, so an ApiError
            # raised here has to be rendered into the envelope by hand.
            await context.chaos.apply(request.url.path)
            if request.url.path in _SELF_MANAGED_PATHS:
                return await call_next(request)
            async with context.store.barrier.shared():
                return await call_next(request)
        except ApiError as error:
            return _error_response(request, error.status_code, error.code, error.message, error.details)

    app.add_middleware(MaxBodySizeMiddleware, max_bytes=MAX_REQUEST_BYTES)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=list(CORS_ALLOWED_METHODS),
        allow_headers=list(CORS_ALLOWED_HEADERS),
        expose_headers=["Content-Range", "Accept-Ranges"],
    )


def _install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ApiError)
        return _error_response(request, exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        details = {
            "errors": [
                {"field": ".".join(str(part) for part in error["loc"]), "rule": error["type"]}
                for error in exc.errors()
            ]
        }
        error = ValidationError("request failed field validation", details=details)
        return _error_response(request, error.status_code, error.code, error.message, error.details)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Responses never carry a stack trace; the correlation id links to the server log.
        logger.exception("unhandled error", exc_info=exc)
        return _error_response(request, 500, "server.internal", "an unexpected server error occurred", {})


def _error_response(
    request: Request, status_code: int, code: str, message: str, details: dict[str, Any]
) -> JSONResponse:
    if not hasattr(request.state, "request_id"):
        request.state.request_id = ""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {"code": code, "message": message, "details": details},
            "meta": meta_for(request),
        },
    )


async def _schedule_next_deadline(service: ChargeService, scheduler: Scheduler) -> None:
    """Wake exactly once at the nearest deadline, then re-arm from the new state."""
    delay = await service.next_deadline_seconds()
    if delay is None:
        return

    async def wake() -> None:
        await service.reconcile_now()
        await _schedule_next_deadline(service, scheduler)

    scheduler.schedule(delay, wake)


def create_app() -> FastAPI:
    """Uvicorn factory: ``uvicorn app.main:create_app --factory``."""
    settings = Settings()
    demo = settings.demo_instant
    clock: Clock = ManualClock(demo) if demo is not None else SystemClock()
    store = JsonStore(settings, clock)
    return build_app(settings, store, clock, AsyncioScheduler(), UuidGenerator())
