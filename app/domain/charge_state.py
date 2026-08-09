"""Pure charge lifecycle. No FastAPI, Pydantic, filesystem or clock imports.

The same table is exported to ``charge_state_vectors.json`` and replayed by the Dart
suite, so Python and Flutter can never disagree about a transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class ChargeStatus(StrEnum):
    OUTSTANDING = "outstanding"
    ACCEPTED = "accepted"
    CONTESTED = "contested"
    RESOLVED = "resolved"
    PAID = "paid"


class ChargeEvent(StrEnum):
    ACCEPT = "accept"
    CONTEST = "contest"
    PAY = "pay"
    DEADLINE_ELAPSED = "deadlineElapsed"
    OPERATOR_UPHOLD = "operatorUphold"
    OPERATOR_DISMISS = "operatorDismiss"


class AcceptanceOrigin(StrEnum):
    STUDENT = "student"
    DEADLINE = "deadline"
    OPERATOR = "operator"


class Actor(StrEnum):
    STUDENT = "student"
    SYSTEM = "system"
    OPERATOR = "operator"


TERMINAL_STATUSES: Final[frozenset[ChargeStatus]] = frozenset({ChargeStatus.RESOLVED, ChargeStatus.PAID})

#: Statuses shown on the Hub "Open" tab.
OPEN_STATUSES: Final[tuple[ChargeStatus, ...]] = (
    ChargeStatus.OUTSTANDING,
    ChargeStatus.ACCEPTED,
    ChargeStatus.CONTESTED,
)

#: Statuses shown on the Hub "History" tab. ``accepted`` deliberately appears on both.
HISTORY_STATUSES: Final[tuple[ChargeStatus, ...]] = (
    ChargeStatus.ACCEPTED,
    ChargeStatus.CONTESTED,
    ChargeStatus.RESOLVED,
    ChargeStatus.PAID,
)

ACTOR_EVENTS: Final[dict[Actor, tuple[ChargeEvent, ...]]] = {
    Actor.STUDENT: (ChargeEvent.ACCEPT, ChargeEvent.CONTEST, ChargeEvent.PAY),
    Actor.SYSTEM: (ChargeEvent.DEADLINE_ELAPSED,),
    Actor.OPERATOR: (ChargeEvent.OPERATOR_UPHOLD, ChargeEvent.OPERATOR_DISMISS),
}

_ACCEPTANCE_ORIGIN_BY_EVENT: Final[dict[ChargeEvent, AcceptanceOrigin]] = {
    ChargeEvent.ACCEPT: AcceptanceOrigin.STUDENT,
    ChargeEvent.DEADLINE_ELAPSED: AcceptanceOrigin.DEADLINE,
    ChargeEvent.OPERATOR_UPHOLD: AcceptanceOrigin.OPERATOR,
}

_TRANSITIONS: Final[dict[tuple[ChargeStatus, ChargeEvent], ChargeStatus]] = {
    (ChargeStatus.OUTSTANDING, ChargeEvent.ACCEPT): ChargeStatus.ACCEPTED,
    (ChargeStatus.OUTSTANDING, ChargeEvent.CONTEST): ChargeStatus.CONTESTED,
    (ChargeStatus.OUTSTANDING, ChargeEvent.DEADLINE_ELAPSED): ChargeStatus.ACCEPTED,
    (ChargeStatus.ACCEPTED, ChargeEvent.PAY): ChargeStatus.PAID,
    (ChargeStatus.CONTESTED, ChargeEvent.OPERATOR_UPHOLD): ChargeStatus.ACCEPTED,
    (ChargeStatus.CONTESTED, ChargeEvent.OPERATOR_DISMISS): ChargeStatus.RESOLVED,
}


@dataclass(frozen=True, slots=True)
class TransitionAccepted:
    """A legal transition and the acceptance provenance it implies, if any."""

    from_status: ChargeStatus
    event: ChargeEvent
    to_status: ChargeStatus
    acceptance_origin: AcceptanceOrigin | None


@dataclass(frozen=True, slots=True)
class InvalidTransition:
    """An illegal (status, event) pair. Never a silent no-op."""

    from_status: ChargeStatus
    event: ChargeEvent


TransitionResult = TransitionAccepted | InvalidTransition


def transition(status: ChargeStatus, event: ChargeEvent) -> TransitionResult:
    """Total, exhaustive transition function over the frozen lifecycle."""
    to_status = _TRANSITIONS.get((status, event))
    if to_status is None:
        return InvalidTransition(from_status=status, event=event)
    origin = _ACCEPTANCE_ORIGIN_BY_EVENT.get(event) if to_status is ChargeStatus.ACCEPTED else None
    return TransitionAccepted(
        from_status=status,
        event=event,
        to_status=to_status,
        acceptance_origin=origin,
    )


def actor_for(event: ChargeEvent) -> Actor:
    """Return the only actor permitted to raise ``event``."""
    for actor, events in ACTOR_EVENTS.items():
        if event in events:
            return actor
    raise AssertionError(f"unmapped event: {event}")  # pragma: no cover - enum is closed


def is_terminal(status: ChargeStatus) -> bool:
    return status in TERMINAL_STATUSES


def appears_in_open(status: ChargeStatus) -> bool:
    return status in OPEN_STATUSES


def appears_in_history(status: ChargeStatus) -> bool:
    return status in HISTORY_STATUSES


def state_vectors() -> list[dict[str, object]]:
    """Every ``(status, event)`` pair with its expected outcome, in deterministic order.

    This is the single source for ``charge_state_vectors.json``; the Dart lifecycle test
    and the Python unit test both replay it.
    """
    vectors: list[dict[str, object]] = []
    for status in ChargeStatus:
        for event in ChargeEvent:
            result = transition(status, event)
            if isinstance(result, TransitionAccepted):
                vectors.append(
                    {
                        "from": status.value,
                        "event": event.value,
                        "actor": actor_for(event).value,
                        "outcome": "accepted",
                        "to": result.to_status.value,
                        "acceptanceOrigin": (
                            result.acceptance_origin.value if result.acceptance_origin else None
                        ),
                    }
                )
            else:
                vectors.append(
                    {
                        "from": status.value,
                        "event": event.value,
                        "actor": actor_for(event).value,
                        "outcome": "invalidTransition",
                        "to": None,
                        "acceptanceOrigin": None,
                    }
                )
    return vectors
