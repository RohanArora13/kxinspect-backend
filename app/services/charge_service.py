"""Business operations over the JSON store.

Every mutation is expressed as a single mutator function handed to
:meth:`JsonStore.commit`, so replay lookup, epoch check, deadline reconciliation,
transition validation, version bump, event append and ledger write either all land in one
``os.replace`` or none of them do.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from app.core.clock import Clock, format_instant, parse_instant
from app.core.config import (
    CONTEST_REASON_MAX_GRAPHEMES,
    CONTEST_REASON_MIN_GRAPHEMES,
    Settings,
)
from app.core.errors import (
    BookingNotFoundError,
    ChargeDuplicateIdError,
    ChargeNotFoundError,
    ContestReasonInvalidError,
    EpochMismatchError,
    IdempotencyPayloadMismatchError,
    InspectionNotFoundError,
    InvalidTransitionError,
    NotificationNotFoundError,
    ReportNotFoundError,
    TaskDuplicateIdError,
    VersionConflictError,
)
from app.core.graphemes import grapheme_length
from app.core.ids import IdGenerator
from app.db.models import (
    ChangeType,
    EventType,
    IdempotencyRecord,
    JsonDict,
    ResourceType,
    StoreSnapshot,
)
from app.db.store import CommitResult, DraftEvent, JsonStore, MutationOutcome, entity_list, find_entity
from app.domain.charge_state import (
    ChargeEvent,
    ChargeStatus,
    InvalidTransition,
    TransitionAccepted,
    transition,
)
from app.domain.cost_breakdown import default_cost_breakdown
from app.domain.deadline import deadline_for, is_expired

TASK_CATEGORIES: Final[tuple[str, ...]] = (
    "electrical",
    "plumbing",
    "heating",
    "appliance",
    "furniture",
    "cleaning",
    "other",
)
TASK_NOTES_MIN_GRAPHEMES: Final = 10
TASK_NOTES_MAX_GRAPHEMES: Final = 1000


@dataclass(frozen=True, slots=True)
class CommandContext:
    """Everything a mutating endpoint knows before it takes the store lock."""

    idempotency_key: str
    request_digest: str
    scope: str


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of a command: either a fresh commit or a replayed committed response."""

    body: JsonDict
    status_code: int
    replayed: bool


def _charge_summary(charge: JsonDict) -> JsonDict:
    return {"chargeId": charge["id"], "status": charge["status"], "version": charge["version"]}


class ChargeService:
    """Reads and commands over bookings, charges, tasks and notifications."""

    def __init__(
        self,
        *,
        store: JsonStore,
        clock: Clock,
        settings: Settings,
        id_generator: IdGenerator,
    ) -> None:
        self._store = store
        self._clock = clock
        self._settings = settings
        self._ids = id_generator

    # ------------------------------------------------------------------- helpers

    def now(self) -> datetime:
        return self._clock.now()

    def _reconcile_deadlines(self, snapshot: StoreSnapshot, now: datetime) -> list[DraftEvent]:
        """Materialise every elapsed deadline as an Accepted charge.

        Runs before every charge/Hub read and before every mutation, and from the deadline
        scheduler. Each charge crosses at most once: the second pass finds it Accepted.
        """
        events: list[DraftEvent] = []
        for charge in entity_list(snapshot, "charges"):
            if charge["status"] != ChargeStatus.OUTSTANDING.value:
                continue
            deadline_at = parse_instant(charge["deadlineAt"])
            if not is_expired(now, deadline_at):
                continue
            result = transition(ChargeStatus.OUTSTANDING, ChargeEvent.DEADLINE_ELAPSED)
            assert isinstance(result, TransitionAccepted)
            charge["status"] = result.to_status.value
            charge["acceptedAt"] = charge["deadlineAt"]
            charge["acceptanceOrigin"] = result.acceptance_origin.value if result.acceptance_origin else None
            charge["updatedAt"] = format_instant(now)
            charge["version"] = int(charge["version"]) + 1
            events.append(
                DraftEvent(
                    type=EventType.CHARGE_UPDATED,
                    entity_id=charge["id"],
                    resource_type=ResourceType.CHARGE,
                    change_type=ChangeType.UPDATED,
                    booking_id=charge["bookingId"],
                    entity_version=charge["version"],
                )
            )
        return events

    @staticmethod
    def _require_epoch(snapshot: StoreSnapshot, expected: str) -> None:
        if expected != snapshot["stateEpoch"]:
            raise EpochMismatchError(
                "expectedStateEpoch does not match the persisted state epoch; "
                "take a fresh /sync-snapshot instead of replaying stale intent",
                details={
                    "expectedStateEpoch": expected,
                    "stateEpoch": snapshot["stateEpoch"],
                    "storeRevision": snapshot["storeRevision"],
                },
            )

    @staticmethod
    def _replay(snapshot: StoreSnapshot, context: CommandContext) -> MutationOutcome[CommandResult] | None:
        record = JsonStore.lookup_idempotency(snapshot, context.idempotency_key)
        if record is None:
            return None
        if record["scope"] != context.scope:
            return None
        if record["requestDigest"] != context.request_digest:
            raise IdempotencyPayloadMismatchError(
                "Idempotency-Key was reused with a different request payload",
                details={"idempotencyKey": context.idempotency_key},
            )
        return MutationOutcome(
            value=CommandResult(body=record["responseBody"], status_code=record["statusCode"], replayed=True),
            persist=False,
        )

    def _record_success(
        self,
        snapshot: StoreSnapshot,
        context: CommandContext,
        *,
        body: JsonDict,
        status_code: int,
        aggregate_version: int | None,
        event_count: int,
    ) -> None:
        JsonStore.record_idempotency(
            snapshot,
            IdempotencyRecord(
                key=context.idempotency_key,
                scope=context.scope,
                requestDigest=context.request_digest,
                statusCode=status_code,
                responseBody=body,
                aggregateVersion=aggregate_version,
                # Ids are assigned by the store after this mutator returns; the count is
                # what the replay assertion needs (no duplicate events on replay).
                eventIds=[f"pending:{index}" for index in range(1, event_count + 1)],
                createdAt=format_instant(self._clock.now()),
            ),
        )

    def _fail_after_reconciliation(
        self,
        reconciliation_events: list[DraftEvent],
        error: Exception,
    ) -> MutationOutcome[CommandResult]:
        """Commit reconciliation, then fail the caller's command.

        The contract is explicit: when a deadline elapses first, the Accepted state is
        authoritative and must be durable even though the Accept/Contest that raced it
        returns 409.
        """
        if reconciliation_events:
            return MutationOutcome(
                value=CommandResult(body={}, status_code=0, replayed=False),
                events=reconciliation_events,
                persist=True,
                deferred_error=error,
            )
        raise error

    # --------------------------------------------------------------------- reads

    async def reconcile_now(self) -> CommitResult[int]:
        """Reconciliation pass used by reads and by the deadline scheduler."""
        now = self.now()

        def mutator(snapshot: StoreSnapshot) -> MutationOutcome[int]:
            events = self._reconcile_deadlines(snapshot, now)
            return MutationOutcome(value=len(events), events=events, persist=bool(events))

        return await self._store.commit(mutator)

    async def bookings(self) -> list[JsonDict]:
        snapshot = await self._store.snapshot()
        return list(entity_list(snapshot, "bookings"))

    async def hub(self, booking_id: str) -> JsonDict:
        await self.reconcile_now()
        snapshot = await self._store.snapshot()
        booking = find_entity(snapshot, "bookings", booking_id)
        if booking is None:
            raise BookingNotFoundError(
                "no booking exists for the supplied identifier", details={"bookingId": booking_id}
            )
        charges = [c for c in entity_list(snapshot, "charges") if c["bookingId"] == booking_id]
        charge_ids = {charge["id"] for charge in charges}
        return {
            "booking": booking,
            "inventoryReports": [
                r for r in entity_list(snapshot, "inventoryReports") if r["bookingId"] == booking_id
            ],
            "inspections": [i for i in entity_list(snapshot, "inspections") if i["bookingId"] == booking_id],
            "tasks": [t for t in entity_list(snapshot, "tasks") if t["bookingId"] == booking_id],
            "charges": charges,
            "notifications": [
                n for n in entity_list(snapshot, "notifications") if n["chargeId"] in charge_ids
            ],
            "generatedAt": format_instant(self.now()),
        }

    async def sync_snapshot(self) -> tuple[JsonDict, JsonDict]:
        """All server-owned rows plus stream metadata, read from one revision."""
        await self.reconcile_now()
        async with self._store.locked_snapshot() as snapshot:
            data = {
                "bookings": list(entity_list(snapshot, "bookings")),
                "inventoryReports": list(entity_list(snapshot, "inventoryReports")),
                "inspections": list(entity_list(snapshot, "inspections")),
                "tasks": list(entity_list(snapshot, "tasks")),
                "charges": list(entity_list(snapshot, "charges")),
                "notifications": list(entity_list(snapshot, "notifications")),
            }
            stream_meta = {
                "streamEpoch": snapshot["streamEpoch"],
                "streamCursor": snapshot["streamCursor"],
            }
        return data, stream_meta

    async def report(self, report_id: str) -> JsonDict:
        snapshot = await self._store.snapshot()
        report = find_entity(snapshot, "inventoryReports", report_id)
        if report is None:
            raise ReportNotFoundError(
                "no inventory report exists for the supplied identifier",
                details={"reportId": report_id},
            )
        return report

    async def inspection(self, inspection_id: str) -> JsonDict:
        snapshot = await self._store.snapshot()
        inspection = find_entity(snapshot, "inspections", inspection_id)
        if inspection is None:
            raise InspectionNotFoundError(
                "no inspection exists for the supplied identifier",
                details={"inspectionId": inspection_id},
            )
        return inspection

    async def charges(self, *, booking_id: str | None, statuses: tuple[str, ...]) -> list[JsonDict]:
        await self.reconcile_now()
        snapshot = await self._store.snapshot()
        rows = list(entity_list(snapshot, "charges"))
        if booking_id is not None:
            rows = [row for row in rows if row["bookingId"] == booking_id]
        if statuses:
            rows = [row for row in rows if row["status"] in statuses]
        return rows

    async def charge(self, charge_id: str) -> JsonDict:
        await self.reconcile_now()
        snapshot = await self._store.snapshot()
        charge = find_entity(snapshot, "charges", charge_id)
        if charge is None:
            raise ChargeNotFoundError(
                "no charge exists for the supplied identifier", details={"chargeId": charge_id}
            )
        return charge

    async def notifications(self, *, after: str | None, unread_only: bool) -> list[JsonDict]:
        snapshot = await self._store.snapshot()
        rows = list(entity_list(snapshot, "notifications"))
        if after is not None:
            boundary = parse_instant(after)
            rows = [row for row in rows if parse_instant(row["createdAt"]) > boundary]
        if unread_only:
            rows = [row for row in rows if not row["read"]]
        return rows

    # ------------------------------------------------------------------ commands

    async def apply_charge_command(
        self,
        *,
        charge_id: str,
        event: ChargeEvent,
        expected_state_epoch: str,
        expected_version: int,
        context: CommandContext,
        contest_reason: str | None = None,
        finalize_attachments: Callable[[], list[JsonDict]] | None = None,
    ) -> CommitResult[CommandResult]:
        """Accept, Contest or Pay, under the full commit protocol.

        ``finalize_attachments`` is a callback rather than a value so staged files are
        only moved into ``uploads/`` once the transition has been validated: a replay or a
        409 must not leave an orphaned attachment on disk.
        """
        now = self.now()

        def mutator(snapshot: StoreSnapshot) -> MutationOutcome[CommandResult]:
            replay = self._replay(snapshot, context)
            if replay is not None:
                return replay

            self._require_epoch(snapshot, expected_state_epoch)
            reconciliation = self._reconcile_deadlines(snapshot, now)

            charge = find_entity(snapshot, "charges", charge_id)
            if charge is None:
                return self._fail_after_reconciliation(
                    reconciliation,
                    ChargeNotFoundError(
                        "no charge exists for the supplied identifier",
                        details={"chargeId": charge_id},
                    ),
                )

            if int(charge["version"]) != expected_version:
                return self._fail_after_reconciliation(
                    reconciliation,
                    VersionConflictError(
                        "expectedVersion does not match the committed charge version",
                        details={
                            "chargeId": charge_id,
                            "expectedVersion": expected_version,
                            "currentCharge": charge,
                        },
                    ),
                )

            outcome = transition(ChargeStatus(charge["status"]), event)
            if isinstance(outcome, InvalidTransition):
                return self._fail_after_reconciliation(
                    reconciliation,
                    InvalidTransitionError(
                        f"charge cannot receive '{event.value}' from state '{outcome.from_status.value}'",
                        details={
                            "chargeId": charge_id,
                            "status": outcome.from_status.value,
                            "event": event.value,
                            "currentCharge": charge,
                        },
                    ),
                )

            charge["status"] = outcome.to_status.value
            charge["version"] = int(charge["version"]) + 1
            charge["updatedAt"] = format_instant(now)
            if event is ChargeEvent.ACCEPT:
                charge["acceptedAt"] = format_instant(now)
                charge["acceptanceOrigin"] = (
                    outcome.acceptance_origin.value if outcome.acceptance_origin else None
                )
            elif event is ChargeEvent.CONTEST:
                charge["contestedAt"] = format_instant(now)
                charge["contestReason"] = contest_reason
                charge["contestAttachments"] = (
                    finalize_attachments() if finalize_attachments is not None else []
                )
            elif event is ChargeEvent.PAY:
                charge["paidAt"] = format_instant(now)

            events = [
                *reconciliation,
                DraftEvent(
                    type=EventType.CHARGE_UPDATED,
                    entity_id=charge_id,
                    resource_type=ResourceType.CHARGE,
                    change_type=ChangeType.UPDATED,
                    booking_id=charge["bookingId"],
                    entity_version=charge["version"],
                ),
            ]
            body: JsonDict = {"charge": charge}
            self._record_success(
                snapshot,
                context,
                body=body,
                status_code=200,
                aggregate_version=int(charge["version"]),
                event_count=len(events),
            )
            return MutationOutcome(
                value=CommandResult(body=body, status_code=200, replayed=False),
                events=events,
                persist=True,
            )

        return await self._store.commit(mutator)

    def validate_contest_reason(self, reason: str) -> str:
        """10-2000 grapheme clusters after trimming; the client enforces the same bound."""
        trimmed = reason.strip()
        length = grapheme_length(trimmed)
        if length < CONTEST_REASON_MIN_GRAPHEMES or length > CONTEST_REASON_MAX_GRAPHEMES:
            raise ContestReasonInvalidError(
                "contest reason must contain between "
                f"{CONTEST_REASON_MIN_GRAPHEMES} and {CONTEST_REASON_MAX_GRAPHEMES} characters",
                details={
                    "graphemeLength": length,
                    "min": CONTEST_REASON_MIN_GRAPHEMES,
                    "max": CONTEST_REASON_MAX_GRAPHEMES,
                },
            )
        return trimmed

    async def create_task(
        self,
        *,
        task: JsonDict,
        expected_state_epoch: str,
        context: CommandContext,
    ) -> CommitResult[CommandResult]:
        now = self.now()

        def mutator(snapshot: StoreSnapshot) -> MutationOutcome[CommandResult]:
            replay = self._replay(snapshot, context)
            if replay is not None:
                return replay

            self._require_epoch(snapshot, expected_state_epoch)

            if find_entity(snapshot, "bookings", task["bookingId"]) is None:
                raise BookingNotFoundError(
                    "no booking exists for the supplied identifier",
                    details={"bookingId": task["bookingId"]},
                )
            if find_entity(snapshot, "tasks", task["id"]) is not None:
                raise TaskDuplicateIdError(
                    "a task already exists with the supplied identifier",
                    details={"taskId": task["id"]},
                )

            stored = {**task, "status": "new"}
            entity_list(snapshot, "tasks").append(stored)
            events = [
                DraftEvent(
                    type=EventType.TASK_CREATED,
                    entity_id=stored["id"],
                    resource_type=ResourceType.TASK,
                    change_type=ChangeType.CREATED,
                    booking_id=stored["bookingId"],
                )
            ]
            body: JsonDict = {"task": stored}
            self._record_success(
                snapshot,
                context,
                body=body,
                status_code=201,
                aggregate_version=None,
                event_count=len(events),
            )
            _ = now
            return MutationOutcome(
                value=CommandResult(body=body, status_code=201, replayed=False),
                events=events,
                persist=True,
            )

        return await self._store.commit(mutator)

    async def mark_notification_read(
        self, *, notification_id: str, expected_state_epoch: str
    ) -> CommitResult[JsonDict]:
        """Naturally idempotent: no Idempotency-Key, but the epoch is still checked."""

        def mutator(snapshot: StoreSnapshot) -> MutationOutcome[JsonDict]:
            self._require_epoch(snapshot, expected_state_epoch)
            notification = find_entity(snapshot, "notifications", notification_id)
            if notification is None:
                raise NotificationNotFoundError(
                    "no notification exists for the supplied identifier",
                    details={"notificationId": notification_id},
                )
            if notification["read"]:
                return MutationOutcome(value=notification, persist=False)
            notification["read"] = True
            return MutationOutcome(
                value=notification,
                events=[
                    DraftEvent(
                        type=EventType.NOTIFICATION_UPDATED,
                        entity_id=notification_id,
                        resource_type=ResourceType.NOTIFICATION,
                        change_type=ChangeType.UPDATED,
                    )
                ],
                persist=True,
            )

        return await self._store.commit(mutator)

    # ----------------------------------------------------------------- dev-only

    async def dev_raise_charge(self, charge: JsonDict) -> CommitResult[JsonDict]:
        """Insert an operator-raised charge plus its feed notification."""
        now = self.now()

        def mutator(snapshot: StoreSnapshot) -> MutationOutcome[JsonDict]:
            if find_entity(snapshot, "bookings", charge["bookingId"]) is None:
                raise BookingNotFoundError(
                    "no booking exists for the supplied identifier",
                    details={"bookingId": charge["bookingId"]},
                )
            if find_entity(snapshot, "charges", charge["id"]) is not None:
                raise ChargeDuplicateIdError(
                    "a charge already exists with the supplied identifier",
                    details={"chargeId": charge["id"]},
                )
            raised_at = parse_instant(charge["raisedAt"])
            grace = int(charge.get("gracePeriodDays", self._settings.grace_period_days))
            stored: JsonDict = {
                **charge,
                "costBreakdown": charge.get(
                    "costBreakdown", default_cost_breakdown(int(charge["amountMinor"]))
                ),
                "gracePeriodDays": grace,
                "deadlineAt": format_instant(deadline_for(raised_at, grace)),
                "status": ChargeStatus.OUTSTANDING.value,
                "contestReason": None,
                "contestAttachments": [],
                "acceptedAt": None,
                "contestedAt": None,
                "resolvedAt": None,
                "paidAt": None,
                "acceptanceOrigin": None,
                "version": 1,
                "updatedAt": format_instant(now),
            }
            entity_list(snapshot, "charges").append(stored)
            notification = {
                "id": f"NTF-{self._ids.next_uuid()[:8].upper()}",
                "type": "chargeRaised",
                "titleKey": "notifications.chargeRaised.title",
                "bodyKey": "notifications.chargeRaised.body",
                "chargeId": stored["id"],
                "createdAt": format_instant(now),
                "read": False,
            }
            entity_list(snapshot, "notifications").append(notification)
            return MutationOutcome(
                value=stored,
                events=[
                    DraftEvent(
                        type=EventType.CHARGE_CREATED,
                        entity_id=stored["id"],
                        resource_type=ResourceType.CHARGE,
                        change_type=ChangeType.CREATED,
                        booking_id=stored["bookingId"],
                        entity_version=1,
                    ),
                    DraftEvent(
                        type=EventType.NOTIFICATION_UPDATED,
                        entity_id=notification["id"],
                        resource_type=ResourceType.NOTIFICATION,
                        change_type=ChangeType.CREATED,
                    ),
                ],
                persist=True,
            )

        return await self._store.commit(mutator)

    async def dev_resolve_charge(self, *, charge_id: str, event: ChargeEvent) -> CommitResult[JsonDict]:
        """Operator decision on a contested charge: uphold (accepted) or dismiss (resolved)."""
        now = self.now()

        def mutator(snapshot: StoreSnapshot) -> MutationOutcome[JsonDict]:
            charge = find_entity(snapshot, "charges", charge_id)
            if charge is None:
                raise ChargeNotFoundError(
                    "no charge exists for the supplied identifier", details={"chargeId": charge_id}
                )
            outcome = transition(ChargeStatus(charge["status"]), event)
            if isinstance(outcome, InvalidTransition):
                raise InvalidTransitionError(
                    f"charge cannot receive '{event.value}' from state '{outcome.from_status.value}'",
                    details={
                        "chargeId": charge_id,
                        "status": outcome.from_status.value,
                        "event": event.value,
                        "currentCharge": charge,
                    },
                )
            charge["status"] = outcome.to_status.value
            charge["version"] = int(charge["version"]) + 1
            charge["updatedAt"] = format_instant(now)
            if outcome.to_status is ChargeStatus.ACCEPTED:
                charge["acceptedAt"] = format_instant(now)
                charge["acceptanceOrigin"] = (
                    outcome.acceptance_origin.value if outcome.acceptance_origin else None
                )
            else:
                charge["resolvedAt"] = format_instant(now)
            notification_id = f"NTF-{self._ids.next_uuid()[:8].upper()}"
            notification: JsonDict = {
                "id": notification_id,
                "type": (
                    "chargeAccepted" if outcome.to_status is ChargeStatus.ACCEPTED else "chargeResolved"
                ),
                "titleKey": (
                    "notifications.chargeAccepted.title"
                    if outcome.to_status is ChargeStatus.ACCEPTED
                    else "notifications.chargeResolved.title"
                ),
                "bodyKey": (
                    "notifications.chargeAccepted.body"
                    if outcome.to_status is ChargeStatus.ACCEPTED
                    else "notifications.chargeResolved.body"
                ),
                "chargeId": charge_id,
                "createdAt": format_instant(now),
                "read": False,
            }
            entity_list(snapshot, "notifications").append(notification)
            return MutationOutcome(
                value=charge,
                events=[
                    DraftEvent(
                        type=EventType.CHARGE_UPDATED,
                        entity_id=charge_id,
                        resource_type=ResourceType.CHARGE,
                        change_type=ChangeType.UPDATED,
                        booking_id=charge["bookingId"],
                        entity_version=charge["version"],
                    ),
                    DraftEvent(
                        type=EventType.NOTIFICATION_UPDATED,
                        entity_id=notification_id,
                        resource_type=ResourceType.NOTIFICATION,
                        change_type=ChangeType.CREATED,
                    ),
                ],
                persist=True,
            )

        return await self._store.commit(mutator)

    # ---------------------------------------------------------------- scheduling

    async def next_deadline_seconds(self) -> float | None:
        """Seconds until the nearest unexpired deadline, for the backend scheduler."""
        snapshot = await self._store.snapshot()
        now = self.now()
        candidates = [
            parse_instant(charge["deadlineAt"])
            for charge in entity_list(snapshot, "charges")
            if charge["status"] == ChargeStatus.OUTSTANDING.value
        ]
        future = [(deadline - now).total_seconds() for deadline in candidates if deadline > now]
        return min(future) if future else None


def validate_task_payload(task: dict[str, Any]) -> None:
    """Field rules shared by the endpoint schema and the contract tests."""
    if task["category"] not in TASK_CATEGORIES:
        raise ContestReasonInvalidError(  # pragma: no cover - schema rejects first
            "unknown task category", details={"category": task["category"]}
        )
    notes_length = grapheme_length(task["notes"].strip())
    if notes_length < TASK_NOTES_MIN_GRAPHEMES or notes_length > TASK_NOTES_MAX_GRAPHEMES:
        raise ContestReasonInvalidError(
            f"task notes must contain between {TASK_NOTES_MIN_GRAPHEMES} and "
            f"{TASK_NOTES_MAX_GRAPHEMES} characters",
            details={"graphemeLength": notes_length},
        )


__all__ = [
    "TASK_CATEGORIES",
    "ChargeService",
    "CommandContext",
    "CommandResult",
    "_charge_summary",
    "validate_task_payload",
]
