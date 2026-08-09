"""Attachment download.

Opaque ids resolve through the store, never through a client-supplied path, so a
traversal attempt cannot reach outside the uploads and seed-media directories.
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse

from app.api.deps import context_of
from app.core.errors import AttachmentNotFoundError, RangeNotSatisfiableError
from app.services.attachment_service import read_range

router = APIRouter()

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")

#: Types the browser may render inline; everything else downloads as an attachment.
_INLINE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


def _media_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _disposition(media_type: str, filename: str) -> str:
    kind = "inline" if media_type in _INLINE_TYPES else "attachment"
    safe = filename.replace('"', "")
    return f'{kind}; filename="{safe}"'


@router.get(
    "/attachments/{opaque_id}",
    summary="Download seeded media or a committed contest attachment",
    responses={
        200: {"content": {"application/octet-stream": {}}, "description": "Full body"},
        206: {"description": "Partial body for a satisfiable Range request"},
        404: {"description": "Unknown opaque id"},
        416: {"description": "Range outside the resource"},
    },
)
async def download_attachment(request: Request, opaque_id: str) -> Response:
    context = context_of(request)
    path = context.store.resolve_attachment(opaque_id)
    if path is None:
        raise AttachmentNotFoundError(
            "no attachment exists for the supplied opaque identifier",
            details={"attachmentId": opaque_id},
        )
    media_type = _media_type(path)
    size = path.stat().st_size
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Accept-Ranges": "bytes",
        "Content-Disposition": _disposition(media_type, path.name),
        "Cache-Control": "no-store",
    }

    range_header = request.headers.get("range")
    if range_header is None:
        return FileResponse(path, media_type=media_type, headers=headers)

    match = _RANGE.match(range_header.strip())
    if match is None:
        raise RangeNotSatisfiableError(
            "only a single 'bytes=' range is supported", details={"range": range_header}
        )
    raw_start, raw_end = match.groups()
    if raw_start == "" and raw_end == "":
        raise RangeNotSatisfiableError("range must specify a start or a suffix length")
    if raw_start == "":
        length = int(raw_end)
        if length <= 0:
            raise RangeNotSatisfiableError("suffix length must be positive")
        start = max(size - length, 0)
        end = size - 1
    else:
        start = int(raw_start)
        end = int(raw_end) if raw_end else size - 1
    if start >= size or end < start:
        raise RangeNotSatisfiableError(
            "requested range lies outside the attachment",
            details={"attachmentId": opaque_id, "sizeBytes": size},
        )
    end = min(end, size - 1)
    body = read_range(path, start, end)
    return Response(
        content=body,
        status_code=206,
        media_type=media_type,
        headers={**headers, "Content-Range": f"bytes {start}-{end}/{size}"},
    )
