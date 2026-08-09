"""Frozen error catalogue.

Every failure that can leave the API carries a stable machine ``code``. Codes are part
of contract v1: the Flutter client maps them onto its ``Failure`` hierarchy, so renaming
one is a contract change, not a refactor.
"""

from __future__ import annotations

from typing import Any, Final


class ApiError(Exception):
    """Base class for every error that maps onto the stable HTTP envelope."""

    code: str = "server.internal"
    status_code: int = 500

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code


class MalformedRequestError(ApiError):
    code = "request.malformed"
    status_code = 400


class IdempotencyKeyMissingError(ApiError):
    code = "request.idempotency_key_missing"
    status_code = 400


class IdempotencyKeyInvalidError(ApiError):
    code = "request.idempotency_key_invalid"
    status_code = 400


class ValidationError(ApiError):
    code = "validation.failed"
    status_code = 422


class NotFoundError(ApiError):
    code = "resource.not_found"
    status_code = 404


class BookingNotFoundError(NotFoundError):
    code = "booking.not_found"


class ChargeNotFoundError(NotFoundError):
    code = "charge.not_found"


class InspectionNotFoundError(NotFoundError):
    code = "inspection.not_found"


class ReportNotFoundError(NotFoundError):
    code = "report.not_found"


class NotificationNotFoundError(NotFoundError):
    code = "notification.not_found"


class AttachmentNotFoundError(NotFoundError):
    code = "attachment.not_found"


class RangeNotSatisfiableError(ApiError):
    code = "attachment.range_not_satisfiable"
    status_code = 416


class InvalidTransitionError(ApiError):
    code = "charge.invalid_transition"
    status_code = 409


class VersionConflictError(ApiError):
    code = "charge.version_conflict"
    status_code = 409


class EpochMismatchError(ApiError):
    code = "store.epoch_mismatch"
    status_code = 409


class IdempotencyPayloadMismatchError(ApiError):
    code = "idempotency.payload_mismatch"
    status_code = 409


class TaskDuplicateIdError(ApiError):
    code = "task.duplicate_id"
    status_code = 409


class ChargeDuplicateIdError(ApiError):
    code = "charge.duplicate_id"
    status_code = 409


class ContestReasonInvalidError(ApiError):
    code = "contest.reason_invalid"
    status_code = 422


class AttachmentTooManyError(ApiError):
    code = "attachment.too_many"
    status_code = 422


class AttachmentEmptyError(ApiError):
    code = "attachment.empty"
    status_code = 422


class AttachmentNameInvalidError(ApiError):
    code = "attachment.invalid_name"
    status_code = 422


class AttachmentTooLargeError(ApiError):
    code = "attachment.too_large"
    status_code = 413


class AttachmentTotalTooLargeError(ApiError):
    code = "attachment.total_too_large"
    status_code = 413


class AttachmentUnsupportedMediaTypeError(ApiError):
    code = "attachment.unsupported_media_type"
    status_code = 415


class DevRouteForbiddenError(ApiError):
    code = "dev.forbidden"
    status_code = 403


class StoreUnavailableError(ApiError):
    code = "store.unavailable"
    status_code = 503


class StreamUnavailableError(ApiError):
    code = "stream.unavailable"
    status_code = 503


class ChaosInjectedError(ApiError):
    code = "chaos.injected"
    status_code = 500


#: Complete catalogue, exported into ``docs/contract-v1.md`` and asserted by contract tests.
ERROR_CATALOG: Final[tuple[tuple[str, int, str], ...]] = (
    ("request.malformed", 400, "Request body or header could not be parsed."),
    ("request.idempotency_key_missing", 400, "A required Idempotency-Key header was absent."),
    ("request.idempotency_key_invalid", 400, "Idempotency-Key was not a UUID."),
    ("dev.forbidden", 403, "Dev-only route rejected: routes disabled or dev token invalid."),
    ("booking.not_found", 404, "No booking exists for the supplied identifier."),
    ("charge.not_found", 404, "No charge exists for the supplied identifier."),
    ("inspection.not_found", 404, "No inspection exists for the supplied identifier."),
    ("report.not_found", 404, "No inventory report exists for the supplied identifier."),
    ("notification.not_found", 404, "No notification exists for the supplied identifier."),
    ("attachment.not_found", 404, "No attachment exists for the supplied opaque identifier."),
    ("resource.not_found", 404, "Generic not-found fallback for unmapped resources."),
    ("charge.invalid_transition", 409, "The requested event is illegal for the current status."),
    ("charge.version_conflict", 409, "expectedVersion did not match the committed version."),
    ("store.epoch_mismatch", 409, "expectedStateEpoch did not match the persisted state epoch."),
    ("idempotency.payload_mismatch", 409, "Idempotency-Key reused with a different request digest."),
    ("task.duplicate_id", 409, "A task already exists with the supplied client-generated id."),
    ("charge.duplicate_id", 409, "A charge already exists with the supplied identifier."),
    ("attachment.too_large", 413, "A single attachment exceeded the per-file byte limit."),
    ("attachment.total_too_large", 413, "Attachments exceeded the combined byte limit."),
    ("attachment.unsupported_media_type", 415, "Attachment media type is not on the allow list."),
    ("attachment.range_not_satisfiable", 416, "Requested byte range lies outside the attachment."),
    ("validation.failed", 422, "Request failed field validation."),
    ("contest.reason_invalid", 422, "Contest reason was outside the 10-2000 grapheme range."),
    ("attachment.too_many", 422, "More than five attachments were supplied."),
    ("attachment.empty", 422, "An attachment part contained zero bytes."),
    ("attachment.invalid_name", 422, "Attachment display name was unsafe or unusable."),
    ("server.internal", 500, "Unhandled server fault."),
    ("chaos.injected", 500, "Deliberate dev-only fault injected by the chaos controls."),
    ("store.unavailable", 503, "Persistent store could not be read or written."),
    ("stream.unavailable", 503, "Event stream is not currently available."),
)
