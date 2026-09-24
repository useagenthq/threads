"""Where a human input stands in the log: an approval challenge open, decided or expired; an
ask_user question open, answered or closed. The UI routes read it to make a repeated answer a
no-op and to name what the log recorded when an answer arrives too late."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from threads.log import (
    ApprovalDeniedEvent,
    ApprovalGrantedEvent,
    ApprovalRequestedEvent,
    Event,
    ParkAddress,
    ParkedEvent,
    ToolResultEvent,
)

type Recorded = Literal["granted", "denied", "expired", "answered", "cancelled"]
type State = Literal["open"] | Recorded


@dataclass(frozen=True, slots=True)
class Approval:
    state: State
    parked: bool
    """Whether the branch is still parked on it (an expired challenge stays parked)."""


@dataclass(frozen=True, slots=True)
class Question:
    state: State
    answer: str | None = None


@dataclass(frozen=True, slots=True)
class Other:
    """A park only an operator settles."""


@dataclass(frozen=True, slots=True)
class Unknown:
    """No interrupt of the thread has this id."""


type Interrupt = Approval | Question | Other | Unknown


def decision_of(events: Sequence[Event], challenge_id: str) -> Literal["granted", "denied"] | None:
    """The logged decision on a challenge, if any."""
    for e in reversed(events):
        if isinstance(e, ApprovalGrantedEvent | ApprovalDeniedEvent) and (
            e.data.challenge_id == challenge_id
        ):
            return "granted" if isinstance(e, ApprovalGrantedEvent) else "denied"
    return None


def interrupt_of(
    events: Sequence[Event], parked: Sequence[ParkAddress], interrupt_id: str, now: int
) -> Interrupt:
    """What `interrupt_id` names in the thread, and where it stands at `now`."""
    requested = next(
        (
            e
            for e in reversed(events)
            if isinstance(e, ApprovalRequestedEvent) and e.data.challenge_id == interrupt_id
        ),
        None,
    )
    if requested is not None:
        held = any(a.kind == "approval" and a.id == interrupt_id for a in parked)
        decided = decision_of(events, interrupt_id)
        if decided is not None:
            return Approval(decided, held)
        return Approval("expired" if requested.data.expires_at <= now else "open", held)
    if any(a.kind == "input" and a.id == interrupt_id for a in parked):
        return Question("open")
    asked = any(
        isinstance(e, ParkedEvent)
        and e.data.address.kind == "input"
        and e.data.address.id == interrupt_id
        for e in events
    )
    if asked:
        result = next(
            (
                e
                for e in reversed(events)
                if isinstance(e, ToolResultEvent) and e.data.call_id == interrupt_id
            ),
            None,
        )
        if result is not None and result.data.origin == "answered":
            return Question("answered", result.data.preview)
        return Question("cancelled")
    return Other() if any(a.id == interrupt_id for a in parked) else Unknown()
