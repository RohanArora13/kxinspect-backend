"""Version 1 route table."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import attachments, commands, dev, events, reads


def build_router(*, include_dev_routes: bool) -> APIRouter:
    """Assemble ``/api/v1``. Dev routes are not merely guarded — they are not bound."""
    router = APIRouter(prefix="/api/v1")
    router.include_router(reads.router, tags=["reads"])
    router.include_router(commands.router, tags=["commands"])
    router.include_router(attachments.router, tags=["attachments"])
    router.include_router(events.router, tags=["events"])
    if include_dev_routes:
        router.include_router(dev.router, tags=["dev"])
    return router
