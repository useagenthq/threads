"""Teams Phase 2 lands in sub-lanes, and a reader refuses the forms whose build has not landed as
it refuses a critical event it doesn't know: it never reduces one as ordinary work. Host teams,
host members, callers and turn failures are reduced (rules 50 and 52-55, rules_host); supervision
(rule 51, lane 29E) is still refused."""

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import Event, MemberStartedEvent, ParseError, SupervisorDecidedEvent
from threads.reduce.fold import reject


def _form(event: Event) -> str | None:
    """The unbuilt Phase 2 form this event takes, if any."""
    if isinstance(event, SupervisorDecidedEvent):
        return event.type
    if isinstance(event, MemberStartedEvent) and event.data.restart_of is not MISSING:
        return "member_started{restart_of}"
    return None


def not_yet(event: Event) -> ParseError | None:
    """A refusal for a Phase 2 form whose sub-lane has not landed."""
    form = _form(event)
    if form is None:
        return None
    message = (
        f"{form} is not reduced by this version:"
        " the Teams Phase 2 supervision build (lane 29E) implements semantic rule 51"
    )
    return reject(event, message, "unsupported_critical_event")
