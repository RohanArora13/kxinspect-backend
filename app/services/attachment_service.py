"""Contest attachment staging, validation and atomic finalisation.

Bytes are streamed into a private staging directory keyed by request id (never by
idempotency key, so two concurrent holders of the same key cannot collide), validated and
hashed *before* the store lock is taken. Only the winner renames files into
``runtime/uploads``; every other path deletes its own staging directory in ``finally``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final, Protocol

from app.core.config import (
    ALLOWED_ATTACHMENT_MEDIA_TYPES,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_TOTAL_BYTES,
    MAX_ATTACHMENTS,
    Settings,
)
from app.core.errors import (
    AttachmentEmptyError,
    AttachmentNameInvalidError,
    AttachmentTooLargeError,
    AttachmentTooManyError,
    AttachmentTotalTooLargeError,
    AttachmentUnsupportedMediaTypeError,
)
from app.core.idempotency import AttachmentDigestPart

_CHUNK_BYTES: Final = 64 * 1024
_MAX_DISPLAY_NAME_CHARS: Final = 120
_UNSAFE_NAME_CHARS: Final = re.compile(r"[\x00-\x1f\x7f/\\:*?\"<>|]")


class UploadSource(Protocol):
    """The part of Starlette's ``UploadFile`` this service depends on.

    Both attributes are read-only properties on ``UploadFile``, so the protocol declares
    them the same way; a settable variable would not be structurally compatible.
    """

    @property
    def filename(self) -> str | None: ...

    @property
    def content_type(self) -> str | None: ...

    async def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True, slots=True)
class StagedAttachment:
    staged_path: Path
    display_name: str
    media_type: str
    size_bytes: int
    sha256: str
    extension: str

    def digest_part(self) -> AttachmentDigestPart:
        return AttachmentDigestPart(
            sha256=self.sha256,
            size_bytes=self.size_bytes,
            media_type=self.media_type,
            sanitized_display_name=self.display_name,
        )


@dataclass(frozen=True, slots=True)
class FinalizedAttachment:
    """Wire shape of a committed contest attachment."""

    id: str
    display_name: str
    media_type: str
    size_bytes: int
    sha256: str
    download_url: str

    def as_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "displayName": self.display_name,
            "mediaType": self.media_type,
            "sizeBytes": self.size_bytes,
            "sha256": self.sha256,
            "downloadUrl": self.download_url,
            "thumbnailUrl": None,
        }


def sanitize_display_name(raw: str | None) -> str:
    """Reduce a client filename to a safe display string.

    Path components are dropped rather than escaped: the stored file name is always
    server-generated, so the client string is presentation only.
    """
    if raw is None:
        raise AttachmentNameInvalidError("attachment part is missing a filename")
    candidate = unicodedata.normalize("NFC", raw).strip()
    candidate = candidate.replace("\\", "/").rsplit("/", 1)[-1]
    candidate = _UNSAFE_NAME_CHARS.sub("_", candidate).strip(" .")
    if not candidate or candidate in {".", ".."}:
        raise AttachmentNameInvalidError("attachment filename is empty or unsafe")
    if len(candidate) > _MAX_DISPLAY_NAME_CHARS:
        stem, dot, suffix = candidate.rpartition(".")
        if dot and len(suffix) <= 10:
            keep = _MAX_DISPLAY_NAME_CHARS - len(suffix) - 1
            candidate = f"{stem[:keep]}.{suffix}"
        else:
            candidate = candidate[:_MAX_DISPLAY_NAME_CHARS]
    return candidate


def detect_media_type(head: bytes) -> str | None:
    """Identify the media type from magic bytes; ``None`` when unrecognised."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        return "video/mp4"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def resolve_media_type(*, declared: str | None, display_name: str, head: bytes) -> str:
    """Agree declared MIME, extension and magic bytes, or reject the file.

    Magic bytes win: a ``.png`` named MP4 is an MP4, and the contract only allows the
    five listed types.
    """
    sniffed = detect_media_type(head)
    if sniffed is None:
        raise AttachmentUnsupportedMediaTypeError(
            "attachment content did not match any supported media type",
            details={"displayName": display_name},
        )
    normalised_declared = (declared or "").split(";", 1)[0].strip().lower()
    if normalised_declared and normalised_declared != sniffed:
        raise AttachmentUnsupportedMediaTypeError(
            "declared media type does not match attachment content",
            details={"displayName": display_name, "declared": normalised_declared, "actual": sniffed},
        )
    suffix = Path(display_name).suffix.lower()
    allowed_suffixes = ALLOWED_ATTACHMENT_MEDIA_TYPES[sniffed]
    if suffix and suffix not in allowed_suffixes:
        raise AttachmentUnsupportedMediaTypeError(
            "attachment extension does not match attachment content",
            details={"displayName": display_name, "actual": sniffed},
        )
    return sniffed


class AttachmentStager:
    """Per-request staging area. Always used as an async context manager."""

    def __init__(self, settings: Settings, request_id: str) -> None:
        self._settings = settings
        self._directory = settings.staging_root / request_id
        self._staged: list[StagedAttachment] = []
        self._total_bytes = 0

    async def __aenter__(self) -> AttachmentStager:
        self._directory.mkdir(parents=True, exist_ok=True)
        self._directory.chmod(0o700)
        return self

    async def __aexit__(self, *_: object) -> None:
        self.discard()

    @property
    def staged(self) -> tuple[StagedAttachment, ...]:
        return tuple(self._staged)

    async def stage(self, upload: UploadSource) -> StagedAttachment:
        """Stream one part to disk under the per-file and total caps."""
        if len(self._staged) >= MAX_ATTACHMENTS:
            raise AttachmentTooManyError(
                f"at most {MAX_ATTACHMENTS} attachments are accepted",
                details={"limit": MAX_ATTACHMENTS},
            )
        display_name = sanitize_display_name(upload.filename)
        target = self._directory / f"{len(self._staged):02d}.part"
        digest = hashlib.sha256()
        size = 0
        head = b""
        with target.open("wb") as handle:
            while True:
                chunk = await upload.read(_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ATTACHMENT_BYTES:
                    raise AttachmentTooLargeError(
                        "attachment exceeds the 10 MiB per-file limit",
                        details={"displayName": display_name, "limitBytes": MAX_ATTACHMENT_BYTES},
                    )
                if self._total_bytes + size > MAX_ATTACHMENT_TOTAL_BYTES:
                    raise AttachmentTotalTooLargeError(
                        "attachments exceed the 25 MiB combined limit",
                        details={"limitBytes": MAX_ATTACHMENT_TOTAL_BYTES},
                    )
                if len(head) < 16:
                    head += chunk[: 16 - len(head)]
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if size == 0:
            raise AttachmentEmptyError("attachment contained no bytes", details={"displayName": display_name})
        media_type = resolve_media_type(declared=upload.content_type, display_name=display_name, head=head)
        extension = ALLOWED_ATTACHMENT_MEDIA_TYPES[media_type][0]
        staged = StagedAttachment(
            staged_path=target,
            display_name=display_name,
            media_type=media_type,
            size_bytes=size,
            sha256=digest.hexdigest(),
            extension=extension,
        )
        self._staged.append(staged)
        self._total_bytes += size
        _fsync_directory(self._directory)
        return staged

    def finalize(self) -> list[FinalizedAttachment]:
        """Move staged files into ``uploads/`` under server-generated opaque ids."""
        uploads = self._settings.uploads_root
        uploads.mkdir(parents=True, exist_ok=True)
        finalized: list[FinalizedAttachment] = []
        for staged in self._staged:
            opaque_id = f"att_{uuid.uuid4().hex}"
            destination = uploads / f"{opaque_id}{staged.extension}"
            staged.staged_path.replace(destination)
            destination.chmod(0o600)
            finalized.append(
                FinalizedAttachment(
                    id=opaque_id,
                    display_name=staged.display_name,
                    media_type=staged.media_type,
                    size_bytes=staged.size_bytes,
                    sha256=staged.sha256,
                    download_url=f"/api/v1/attachments/{opaque_id}",
                )
            )
        _fsync_directory(uploads)
        self._staged.clear()
        return finalized

    def rollback(self, finalized: list[FinalizedAttachment]) -> None:
        """Delete files finalised by a mutation that then failed to commit."""
        for attachment in finalized:
            for candidate in self._settings.uploads_root.glob(f"{attachment.id}.*"):
                candidate.unlink(missing_ok=True)

    def discard(self) -> None:
        shutil.rmtree(self._directory, ignore_errors=True)


def _fsync_directory(path: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def cleanup_staging(settings: Settings) -> None:
    """Crash backstop invoked at startup."""
    shutil.rmtree(settings.staging_root, ignore_errors=True)
    settings.staging_root.mkdir(parents=True, exist_ok=True)
    settings.staging_root.chmod(0o700)


def read_range(path: Path, start: int, end: int) -> bytes:
    """Read an inclusive byte range for MP4 Range requests."""
    with path.open("rb") as handle:
        handle.seek(start)
        return handle.read(end - start + 1)


def open_stream(path: Path) -> BinaryIO:
    return path.open("rb")
