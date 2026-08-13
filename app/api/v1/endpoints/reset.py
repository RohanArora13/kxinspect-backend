"""Permanent reset endpoint for the intentionally disposable demo backend."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.api.deps import context_of
from app.services.demo_reset import reset_demo_state

router = APIRouter()


@router.post("/reset", status_code=204, summary="Restore original demo data")
async def reset(request: Request) -> Response:
    """Discard demo changes and reseed the backend without developer configuration."""
    context = context_of(request)
    await reset_demo_state(store=context.store, broker=context.broker)
    return Response(status_code=204)
