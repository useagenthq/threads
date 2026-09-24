"""Teams (semantic rules 31 and 33-45) are in the schema before either runtime
reduces them. Until the Teams build implements those rules, a reader refuses a log with any of
their events, or any team form of an existing event, as it refuses a critical event it doesn't
know: it never reduces one as ordinary work."""

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    Event,
    ParkedEvent,
    ParkEscalatedEvent,
    ParseError,
    ResumedEvent,
    ThreadStartedEvent,
    TurnCompletedEvent,
    UserInputEvent,
)
from threads.reduce.fold import reject

_TEAM_EVENTS = frozenset(
    {
        "team_opened",
        "member_started",
        "member_idle",
        "member_ended",
        "member_observed",
        "monitor_set",
        "wait_started",
        "wait_finished",
        "message_sent",
        "message_received",
        "mail_refused",
        "ask_closed",
        "operator_request",
        "operator_refused",
        "message_policy_decided",
    }
)
_TEAM_PARKS = frozenset({"ask", "wait", "member"})
_PIN_CODES = frozenset({"pin_unavailable", "pin_mismatch"})


def _team_form(event: Event) -> str | None:
    """The team form this event takes, if any."""
    form: str | None = None
    if event.type in _TEAM_EVENTS:
        form = event.type
    elif isinstance(event, ThreadStartedEvent):
        form = _started_form(event)
    elif isinstance(event, UserInputEvent) and event.data.source == "team_task":
        form = "user_input{team_task}"
    elif isinstance(event, ParkedEvent | ResumedEvent | ParkEscalatedEvent):
        kind = event.data.address.kind
        form = f"{event.type}{{{kind}}}" if kind in _TEAM_PARKS else None
    elif isinstance(event, TurnCompletedEvent) and event.data.code in _PIN_CODES:
        form = f"turn_completed{{{event.data.code}}}"
    return form


def _started_form(event: ThreadStartedEvent) -> str | None:
    if event.data.team is not MISSING:
        return "thread_started{team}"
    parent = event.data.parent
    if parent is not MISSING and parent.relation == "team_member":
        return "thread_started{parent: team_member}"
    return None


def not_yet(event: Event) -> ParseError | None:
    """A refusal for a team event or team form, until the Teams build."""
    form = _team_form(event)
    if form is None:
        return None
    message = (
        f"{form} is not reduced by this version: the Teams build implements rules 31 and 33-45"
    )
    return reject(event, message, "unsupported_critical_event")
