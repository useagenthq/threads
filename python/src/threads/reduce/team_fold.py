"""Teams (spec/schema/README.md, "Teams"): which received mail opens a turn, and the fold step
for the team bookkeeping semantic rules 31 and 33-45 read (rules_team)."""

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    AskClosedEvent,
    Event,
    MailEnvelope,
    MailRefusedEvent,
    MemberEndedEvent,
    MemberObservedEvent,
    MemberStartedEvent,
    MessageReceivedEvent,
    MessageSentEvent,
    MonitorSetEvent,
    OperatorRequestEvent,
    TeamOpenedEvent,
    ThreadStartedEvent,
    TurnCompletedEvent,
    UserInputEvent,
    WaitFinishedEvent,
    WaitStartedEvent,
)
from threads.reduce.fold import Fold, Run, Team

NOTICES = frozenset({"member_settled", "member_ended"})


def mail_opens_turn(fold: Fold, env: MailEnvelope) -> bool:
    """Only in a member's log (the lead's included) with no turn open: a message, an ask, a
    bounce naming no ask, or a task or end monitor's notification, once no park is left but the
    one it resolves. A reply, an ask's bounce and a wait's notification resume their call instead
    (spec/schema/README.md, "Which events open a turn"; reference: turn_open.py)."""
    if fold.in_turn or fold.team.team_log:
        return False
    if env.kind in NOTICES:
        opens = env.monitor_id is not MISSING and env.monitor_id not in fold.team.settle
    else:
        opens = env.kind in ("message", "ask") or (env.kind == "bounce" and env.ask_id is MISSING)
    return opens and all(p.kind == "member" and p.id == env.monitor_id for p in fold.parked)


def monitor_id(event: Event, target: str) -> str:
    """`<watcher branch_id>:<registering event_id>:<target name>`."""
    return f"{event.branch_id}:{event.event_id}:{target}"


def advance(fold: Fold, event: Event) -> None:
    """The team bookkeeping after an event passed validate_next."""
    _log(fold.team, event)
    _mail(fold, event)
    _monitors(fold.team, event)


def _log(team: Team, event: Event) -> None:
    if isinstance(event, TeamOpenedEvent):
        team.team_log, team.lead_thread = True, event.data.lead_thread_id
    elif isinstance(event, ThreadStartedEvent):
        parent = event.data.parent
        team.member = parent is not MISSING and parent.relation == "team_member"
    elif isinstance(event, UserInputEvent):
        team.had_input = True
        if event.data.mail_id is not MISSING:
            team.mail_done.add(event.data.mail_id)
    elif isinstance(event, TurnCompletedEvent):
        team.last_end = event.data.reason
    elif isinstance(event, MemberEndedEvent):
        team.ended = True
    elif isinstance(event, OperatorRequestEvent):
        team.requests.add(event.data.request_id)


def _mail(fold: Fold, event: Event) -> None:
    team = fold.team
    if isinstance(event, MessageReceivedEvent):
        _received(fold, event.data.envelope)
    elif isinstance(event, MailRefusedEvent):
        team.mail_done.add(event.data.mail_id)
    elif isinstance(event, AskClosedEvent):
        team.asks_out.discard(event.data.ask_id)
    elif isinstance(event, MessageSentEvent):
        env = event.data.envelope
        if env.ask_id is MISSING:
            return
        if env.kind == "ask":
            team.asks_out.add(env.ask_id)
        elif env.kind == "reply":
            team.asks_in.discard(env.ask_id)


def _received(fold: Fold, env: MailEnvelope) -> None:
    """A turn-opening receipt starts a turn of the mail's run: its provenance's principal and
    root request (spec/schema/README.md, "Background wakes")."""
    team = fold.team
    if mail_opens_turn(fold, env):
        p = env.provenance.principal
        fold.in_turn = True
        fold.wake.run = Run((p.issuer, p.tenant, p.subject), env.provenance.root_request.event_id)
    team.mail_done.add(env.mail_id)
    if env.kind == "ask" and env.ask_id is not MISSING:
        team.asks_in.add(env.ask_id)
    elif env.kind == "reply" and env.ask_id is not MISSING:
        team.replies_in[env.mail_id] = env.ask_id
    elif env.kind in NOTICES and env.monitor_id is not MISSING:
        team.monitors.discard(env.monitor_id)


def _monitors(team: Team, event: Event) -> None:
    if isinstance(event, MemberStartedEvent):
        team.task_monitors.add(monitor_id(event, "task"))
    elif isinstance(event, MonitorSetEvent):
        team.monitors.add(monitor_id(event, event.data.member.name))
    elif isinstance(event, WaitStartedEvent):
        team.waits.add(event.data.wait_id)
        for m in event.data.members:
            team.monitors.add(monitor_id(event, m.name))
            team.settle.add(monitor_id(event, m.name))
    elif isinstance(event, WaitFinishedEvent):
        team.waits.discard(event.data.wait_id)
    elif isinstance(event, MemberObservedEvent):
        team.monitors.discard(event.data.monitor_id)
