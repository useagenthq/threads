"""Teams Phase 2 (host teams, host members, callers, turn failures and supervision; lane 29) is in
the schema before either runtime reduces it. Until its build implements semantic rules 50-55, a
reader refuses a log with any Phase 2 event or form, as it refuses a critical event it doesn't
know: it never reduces one as ordinary work."""

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    AskClosedEvent,
    BudgetExceededEvent,
    CallerAddress,
    Event,
    MailEnvelope,
    MemberIdleEvent,
    MemberStartedEvent,
    MessageReceivedEvent,
    MessageSentEvent,
    ParseError,
    SupervisorDecidedEvent,
    TeamOpenedEvent,
    ThreadStartedEvent,
)
from threads.reduce.fold import reject


def _envelope_form(env: MailEnvelope) -> str | None:
    if isinstance(env.from_, CallerAddress):
        return "a caller's mail"
    if isinstance(env.to, CallerAddress):
        return "mail to a caller"
    return "a turn_failed bounce" if env.code == "turn_failed" else None


def _form(event: Event) -> str | None:  # noqa: PLR0911 - one return per Phase 2 form
    """The Phase 2 form this event takes, if any."""
    if isinstance(event, SupervisorDecidedEvent):
        return event.type
    if isinstance(event, TeamOpenedEvent):
        return "team_opened{kind: host}" if event.data.kind == "host" else None
    if isinstance(event, ThreadStartedEvent | MemberStartedEvent):
        host = event.data.host_member is not MISSING
        return f"{event.type}{{host_member}}" if host else None
    if isinstance(event, MemberIdleEvent):
        return None if event.data.turn_failed is MISSING else "member_idle{turn_failed}"
    if isinstance(event, AskClosedEvent):
        return "ask_closed{failed}" if event.data.outcome.status == "failed" else None
    if isinstance(event, BudgetExceededEvent):
        return "budget_exceeded{scope: hop}" if event.data.scope == "hop" else None
    if isinstance(event, MessageSentEvent | MessageReceivedEvent):
        return _envelope_form(event.data.envelope)
    return None


def not_yet(event: Event) -> ParseError | None:
    """A refusal for a Phase 2 event or form, until the Teams Phase 2 build."""
    form = _form(event)
    if form is None:
        return None
    message = (
        f"{form} is not reduced by this version:"
        " the Teams Phase 2 build (lane 29) implements semantic rules 50-55"
    )
    return reject(event, message, "unsupported_critical_event")
