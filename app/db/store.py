"""Single-writer JSON store with atomic replace, an event ring and an idempotency ledger.

Everything a mutation must do atomically — replay lookup, epoch check, deadline
reconciliation, transition, attachment finalise, version bump, event append, ledger
write — happens inside one :meth:`JsonStore.commit` call, and one ``os.replace`` makes
it durable. There is no code path that persists half of a mutation.

This is a demo store, not a database: it supports exactly one process and one worker,
guarded by an exclusive ``flock`` on ``runtime/runtime.lock``.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import fcntl
import json
import os
import shutil
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from app.core.clock import Clock, format_instant
from app.core.config import (
    CONTRACT_VERSION,
    EVENT_RING_CAPACITY,
    RUNTIME_MARKER_UUID,
    SCHEMA_VERSION,
    SEED_VERSION,
    Settings,
)
from app.core.errors import StoreUnavailableError
from app.db.models import (
    ENTITY_KEYS,
    SEED_FILES,
    ChangeType,
    Entities,
    EventData,
    EventType,
    IdempotencyRecord,
    JsonDict,
    ResourceType,
    StoredEvent,
    StoreSnapshot,
    baseline_cursor,
    event_id,
)


class IncompatibleRuntimeStateError(RuntimeError):
    """Runtime state exists but was written by an incompatible schema, contract or seed."""


class CorruptRuntimeStateError(RuntimeError):
    """Runtime state exists but cannot be parsed or references missing files."""


class RuntimeLockedError(RuntimeError):
    """Another process already holds the exclusive runtime lock."""


@dataclass(frozen=True, slots=True)
class DraftEvent:
    """An event a mutation wants to publish; the store assigns its id after commit."""

    type: EventType
    entity_id: str
    resource_type: ResourceType
    change_type: ChangeType
    booking_id: str | None = None
    entity_version: int | None = None


@dataclass(slots=True)
class MutationOutcome[T]:
    """What a mutator asks the store to do.

    ``deferred_error`` covers the one case the contract calls out explicitly: deadline
    reconciliation wins the race, its Accepted state must be committed, and only then
    does the caller's Accept or Contest fail with 409.
    """

    value: T
    events: list[DraftEvent] = field(default_factory=list)
    persist: bool = True
    deferred_error: Exception | None = None


@dataclass(frozen=True, slots=True)
class CommitResult[T]:
    value: T
    events: tuple[StoredEvent, ...]
    state_epoch: str
    store_revision: int
    stream_epoch: str
    stream_cursor: str


class OperationBarrier:
    """Shared/exclusive lease taken by every finite operation.

    Reset needs to know that no read, upload or download is mid-flight before it deletes
    files, and a plain lock cannot express "many readers, one resetter".
    """

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._shared = 0
        self._exclusive = False

    @contextlib.asynccontextmanager
    async def shared(self) -> AsyncIterator[None]:
        async with self._condition:
            while self._exclusive:
                await self._condition.wait()
            self._shared += 1
        try:
            yield
        finally:
            async with self._condition:
                self._shared -= 1
                self._condition.notify_all()

    @contextlib.asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        async with self._condition:
            while self._exclusive or self._shared > 0:
                await self._condition.wait()
            self._exclusive = True
        try:
            yield
        finally:
            async with self._condition:
                self._exclusive = False
                self._condition.notify_all()


class _RuntimeFileLock:
    """Exclusive advisory lock so a second backend process fails fast instead of racing."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise RuntimeLockedError(f"another process holds the runtime lock at {self._path}") from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(self._fd)
        self._fd = None


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _referenced_attachment_ids(entities: Entities) -> set[str]:
    """Opaque ids the state expects to be resolvable, from photos and contest files."""
    referenced: set[str] = set()
    for charge in entities["charges"]:
        for photo in charge.get("photos", []):
            for key in ("url", "thumbnailUrl"):
                value = photo.get(key)
                if isinstance(value, str):
                    referenced.add(value.rsplit("/", 1)[-1])
        for attachment in charge.get("contestAttachments", []):
            for key in ("downloadUrl", "thumbnailUrl"):
                value = attachment.get(key)
                if isinstance(value, str):
                    referenced.add(value.rsplit("/", 1)[-1])
    for report in entities["inventoryReports"]:
        url = report.get("reportUrl")
        if isinstance(url, str):
            referenced.add(url.rsplit("/", 1)[-1])
    for inspection in entities["inspections"]:
        for action in inspection.get("itemActions", []):
            thumb = action.get("thumbnailUrl")
            if isinstance(thumb, str):
                referenced.add(thumb.rsplit("/", 1)[-1])
    return referenced


class JsonStore:
    """Locked, atomically replaced JSON snapshot with an in-snapshot event ring."""

    def __init__(self, settings: Settings, clock: Clock) -> None:
        self._settings = settings
        self._clock = clock
        self._lock = asyncio.Lock()
        self._file_lock = _RuntimeFileLock(settings.lock_path)
        self._barrier = OperationBarrier()
        self._snapshot: StoreSnapshot | None = None
        self._publisher: Callable[[tuple[StoredEvent, ...]], Awaitable[None]] | None = None

    def set_publisher(self, publisher: Callable[[tuple[StoredEvent, ...]], Awaitable[None]]) -> None:
        """Register the broker fan-out invoked after each successful atomic replace."""
        self._publisher = publisher

    # ----------------------------------------------------------------- lifecycle

    @property
    def barrier(self) -> OperationBarrier:
        return self._barrier

    def open(self) -> None:
        """Acquire the runtime lock, then load or seed state. Called from lifespan."""
        settings = self._settings
        settings.runtime_root.mkdir(parents=True, exist_ok=True)
        settings.runtime_root.chmod(0o700)
        self._file_lock.acquire()
        settings.uploads_root.mkdir(parents=True, exist_ok=True)
        settings.uploads_root.chmod(0o700)
        self._write_marker()
        self._clear_staging()
        if settings.state_path.exists():
            self._snapshot = self._load_state()
        else:
            self._snapshot = self._build_seed_snapshot()
            self._persist(self._snapshot)
        self._verify_attachments(self._snapshot)
        self._remove_unreferenced_uploads(self._snapshot)

    def close(self) -> None:
        self._file_lock.release()

    def _write_marker(self) -> None:
        marker = self._settings.marker_path
        marker.write_text(RUNTIME_MARKER_UUID + "\n", encoding="utf-8")
        marker.chmod(0o600)

    def _clear_staging(self) -> None:
        """Crash backstop: staging never survives a restart."""
        staging = self._settings.staging_root
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        staging.chmod(0o700)
        for stale in self._settings.runtime_root.glob("state.json.*.tmp"):
            stale.unlink(missing_ok=True)

    # --------------------------------------------------------------------- seed

    def _seed_entities(self) -> Entities:
        root = self._settings.seed_root
        loaded: dict[str, list[JsonDict]] = {}
        for key in ENTITY_KEYS:
            path = root / SEED_FILES[key]
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CorruptRuntimeStateError(f"seed file unreadable: {path}") from exc
            if not isinstance(payload, list):
                raise CorruptRuntimeStateError(f"seed file must contain a JSON array: {path}")
            loaded[key] = payload
        return Entities(
            bookings=loaded["bookings"],
            inventoryReports=loaded["inventoryReports"],
            inspections=loaded["inspections"],
            tasks=loaded["tasks"],
            charges=loaded["charges"],
            notifications=loaded["notifications"],
        )

    def _build_seed_snapshot(self) -> StoreSnapshot:
        stream_epoch = str(uuid.uuid4())
        return StoreSnapshot(
            schemaVersion=SCHEMA_VERSION,
            contractVersion=CONTRACT_VERSION,
            seedVersion=SEED_VERSION,
            stateEpoch=str(uuid.uuid4()),
            storeRevision=1,
            streamEpoch=stream_epoch,
            streamCursor=baseline_cursor(stream_epoch),
            streamEvictedThrough=baseline_cursor(stream_epoch),
            entities=self._seed_entities(),
            events=[],
            idempotency={},
        )

    # -------------------------------------------------------------------- load

    def _load_state(self) -> StoreSnapshot:
        path = self._settings.state_path
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CorruptRuntimeStateError(
                f"runtime state at {path} is unreadable; reset it deliberately with scripts/reset_runtime.py"
            ) from exc
        if not isinstance(raw, dict):
            raise CorruptRuntimeStateError(f"runtime state at {path} is not a JSON object")

        for key in ("schemaVersion", "contractVersion", "seedVersion", "stateEpoch", "streamEpoch"):
            if not isinstance(raw.get(key), str):
                raise CorruptRuntimeStateError(f"runtime state is missing '{key}'")
        if raw["schemaVersion"] != SCHEMA_VERSION or raw["contractVersion"] != CONTRACT_VERSION:
            raise IncompatibleRuntimeStateError(
                f"runtime state schema {raw['schemaVersion']}/contract {raw['contractVersion']} "
                f"cannot be served by schema {SCHEMA_VERSION}/contract {CONTRACT_VERSION}"
            )
        if raw["seedVersion"] != SEED_VERSION:
            raise IncompatibleRuntimeStateError(
                f"runtime state was seeded from seedVersion {raw['seedVersion']} but this build "
                f"ships seedVersion {SEED_VERSION}; existing demo state is preserved. Run "
                f"'uv run python scripts/reset_runtime.py --runtime-root <path>' to reseed."
            )
        entities = raw.get("entities")
        if not isinstance(entities, dict) or any(
            not isinstance(entities.get(key), list) for key in ENTITY_KEYS
        ):
            raise CorruptRuntimeStateError("runtime state entities are malformed")
        return StoreSnapshot(
            schemaVersion=raw["schemaVersion"],
            contractVersion=raw["contractVersion"],
            seedVersion=raw["seedVersion"],
            stateEpoch=raw["stateEpoch"],
            storeRevision=int(raw.get("storeRevision", 1)),
            streamEpoch=raw["streamEpoch"],
            streamCursor=str(raw.get("streamCursor") or baseline_cursor(raw["streamEpoch"])),
            streamEvictedThrough=str(raw.get("streamEvictedThrough") or baseline_cursor(raw["streamEpoch"])),
            entities=Entities(
                bookings=entities["bookings"],
                inventoryReports=entities["inventoryReports"],
                inspections=entities["inspections"],
                tasks=entities["tasks"],
                charges=entities["charges"],
                notifications=entities["notifications"],
            ),
            events=list(raw.get("events", [])),
            idempotency=dict(raw.get("idempotency", {})),
        )

    def _verify_attachments(self, snapshot: StoreSnapshot) -> None:
        missing = [
            opaque
            for opaque in sorted(_referenced_attachment_ids(snapshot["entities"]))
            if self._resolve_attachment(opaque) is None
        ]
        if missing:
            raise CorruptRuntimeStateError(
                "runtime state references attachments that do not exist: " + ", ".join(missing)
            )

    def _remove_unreferenced_uploads(self, snapshot: StoreSnapshot) -> None:
        referenced = _referenced_attachment_ids(snapshot["entities"])
        uploads = self._settings.uploads_root
        if not uploads.exists():
            return
        for candidate in uploads.iterdir():
            if candidate.is_dir():
                continue
            if candidate.stem not in referenced:
                candidate.unlink(missing_ok=True)

    def _resolve_attachment(self, opaque_id: str) -> Path | None:
        """Uploads win over seed media so a replaced file is never shadowed."""
        if not opaque_id or "/" in opaque_id or ".." in opaque_id:
            return None
        for root in (self._settings.uploads_root, self._settings.static_root):
            if not root.exists():
                continue
            for candidate in sorted(root.glob(f"{opaque_id}.*")):
                if candidate.is_file():
                    return candidate
        return None

    def resolve_attachment(self, opaque_id: str) -> Path | None:
        return self._resolve_attachment(opaque_id)

    # ------------------------------------------------------------------ persist

    def _persist(self, snapshot: StoreSnapshot) -> None:
        path = self._settings.state_path
        temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        try:
            with temp.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temp.chmod(0o600)
            temp.replace(path)
            _fsync_directory(path.parent)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise StoreUnavailableError("runtime state could not be written") from exc

    # -------------------------------------------------------------------- reads

    def _require_snapshot(self) -> StoreSnapshot:
        if self._snapshot is None:
            raise StoreUnavailableError("runtime store has not been opened")
        return self._snapshot

    @property
    def state_epoch(self) -> str:
        return self._require_snapshot()["stateEpoch"]

    @property
    def store_revision(self) -> int:
        return self._require_snapshot()["storeRevision"]

    @property
    def stream_epoch(self) -> str:
        return self._require_snapshot()["streamEpoch"]

    @property
    def stream_cursor(self) -> str:
        return self._require_snapshot()["streamCursor"]

    @property
    def is_open(self) -> bool:
        return self._snapshot is not None

    async def snapshot(self) -> StoreSnapshot:
        """Deep copy taken under the store lock, so callers cannot mutate live state."""
        async with self._lock:
            return copy.deepcopy(self._require_snapshot())

    @contextlib.asynccontextmanager
    async def locked_snapshot(self) -> AsyncIterator[StoreSnapshot]:
        """Read a consistent snapshot while holding the store lock (no copy)."""
        async with self._lock:
            yield self._require_snapshot()

    # ------------------------------------------------------------------ mutation

    async def commit[T](
        self,
        mutator: Callable[[StoreSnapshot], MutationOutcome[T]],
    ) -> CommitResult[T]:
        """Run ``mutator`` under the store lock and atomically replace the snapshot.

        The mutator receives a working copy: if it raises, live state is untouched.
        """
        async with self._lock:
            current = self._require_snapshot()
            working = copy.deepcopy(current)
            outcome = mutator(working)

            committed_events: tuple[StoredEvent, ...] = ()
            if outcome.persist:
                working["storeRevision"] = current["storeRevision"] + 1
                committed_events = self._append_events(working, outcome.events)
                self._persist(working)
                self._snapshot = working
                target = working
                if committed_events and self._publisher is not None:
                    # Broker lock is only ever taken while the store lock is held, never
                    # the other way around, and no socket is awaited here.
                    await self._publisher(committed_events)
            else:
                target = current

            result = CommitResult(
                value=outcome.value,
                events=committed_events,
                state_epoch=target["stateEpoch"],
                store_revision=target["storeRevision"],
                stream_epoch=target["streamEpoch"],
                stream_cursor=target["streamCursor"],
            )

        if outcome.deferred_error is not None:
            raise outcome.deferred_error
        return result

    def _append_events(self, snapshot: StoreSnapshot, drafts: list[DraftEvent]) -> tuple[StoredEvent, ...]:
        if not drafts:
            return ()
        occurred_at = format_instant(self._clock.now())
        revision = snapshot["storeRevision"]
        stream_epoch = snapshot["streamEpoch"]
        stored: list[StoredEvent] = []
        for ordinal, draft in enumerate(drafts, start=1):
            stored.append(
                StoredEvent(
                    id=event_id(stream_epoch, revision, ordinal),
                    stateEpoch=snapshot["stateEpoch"],
                    storeRevision=revision,
                    type=draft.type.value,
                    occurredAt=occurred_at,
                    bookingId=draft.booking_id,
                    entityId=draft.entity_id,
                    entityVersion=draft.entity_version,
                    data=EventData(
                        resourceType=draft.resource_type.value,
                        changeType=draft.change_type.value,
                    ),
                )
            )
        combined = [*snapshot["events"], *stored]
        evicted = combined[:-EVENT_RING_CAPACITY]
        if evicted:
            snapshot["streamEvictedThrough"] = evicted[-1]["id"]
        snapshot["events"] = combined[-EVENT_RING_CAPACITY:]
        snapshot["streamCursor"] = stored[-1]["id"]
        return tuple(stored)

    # --------------------------------------------------------------------- reset

    async def reset(self) -> StoreSnapshot:
        """Destructive reseed: new ``stateEpoch`` and ``streamEpoch``, empty ring and ledger."""
        async with self._lock:
            uploads = self._settings.uploads_root
            if uploads.exists():
                shutil.rmtree(uploads, ignore_errors=True)
            uploads.mkdir(parents=True, exist_ok=True)
            uploads.chmod(0o700)
            self._clear_staging()
            fresh = self._build_seed_snapshot()
            self._persist(fresh)
            self._snapshot = fresh
            return copy.deepcopy(fresh)

    # -------------------------------------------------------------- ledger utils

    @staticmethod
    def lookup_idempotency(snapshot: StoreSnapshot, key: str) -> IdempotencyRecord | None:
        return snapshot["idempotency"].get(key)

    @staticmethod
    def record_idempotency(snapshot: StoreSnapshot, record: IdempotencyRecord) -> None:
        snapshot["idempotency"][record["key"]] = record

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def iter_entity(snapshot: StoreSnapshot, key: str) -> Iterator[JsonDict]:
    collection: list[JsonDict] = snapshot["entities"][key]  # type: ignore[literal-required]
    yield from collection


def find_entity(snapshot: StoreSnapshot, key: str, entity_id: str) -> JsonDict | None:
    for item in iter_entity(snapshot, key):
        if item.get("id") == entity_id:
            return item
    return None


def entity_list(snapshot: StoreSnapshot, key: str) -> list[Any]:
    return snapshot["entities"][key]  # type: ignore[literal-required,no-any-return]
