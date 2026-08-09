"""Idempotency scope and request digests.

Scope is ``(method, normalized route template, resource id, UUID key)`` so the same key
replayed against a different charge is a different operation, not a cache hit. The digest
is taken over RFC 8785 canonical JSON, which makes it independent of key order,
whitespace, multipart boundaries and transport headers.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.canonical_json import canonical_sha256
from app.core.errors import IdempotencyKeyInvalidError, IdempotencyKeyMissingError


def require_idempotency_key(raw: str | None) -> str:
    """Validate the ``Idempotency-Key`` header. Missing is 400, malformed is 400."""
    if raw is None or not raw.strip():
        raise IdempotencyKeyMissingError("Idempotency-Key header is required for this command")
    candidate = raw.strip()
    try:
        parsed = uuid.UUID(candidate)
    except ValueError as exc:
        raise IdempotencyKeyInvalidError("Idempotency-Key must be a UUID") from exc
    return str(parsed)


def scope_key(*, method: str, route_template: str, resource_id: str, key: str) -> str:
    return f"{method.upper()} {route_template}#{resource_id}#{key}"


def json_digest(payload: object) -> str:
    """Digest for JSON command bodies."""
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class AttachmentDigestPart:
    """The only attachment facts that participate in the digest."""

    sha256: str
    size_bytes: int
    media_type: str
    sanitized_display_name: str

    def as_tuple(self) -> list[object]:
        return [self.sha256, self.size_bytes, self.media_type, self.sanitized_display_name]


def contest_digest(
    *,
    metadata: dict[str, object],
    attachments: Sequence[AttachmentDigestPart],
) -> str:
    """Digest for the Contest multipart command.

    Attachment order is user order and is part of the digest: re-uploading the same files
    in a different order is a different request, not a replay.
    """
    return canonical_sha256(
        {
            "metadata": metadata,
            "attachments": [part.as_tuple() for part in attachments],
        }
    )
