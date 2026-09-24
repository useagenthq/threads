"""Teams (spec/schema/README.md, "Teams"): which received mail opens a turn, and the fold step
for the team bookkeeping semantic rules 31 and 33-45 read (rules_team)."""

from collections.abc import Set as AbstractSet

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    AgentSpawnedEvent,
    AskClosedEvent,
    CancelRequestedEvent,
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
    Principal,
    TeamOpenedEvent,
    ThreadStartedEvent,
    TurnCompletedEvent,
    UserInputEvent,
    WaitFinishedEvent,
    WaitStartedEvent,
    WokenEvent,
)
from threads.reduce.fold import Fold, PrincipalKey, Team, TurnRun

NOTICES = frozenset({"member_settled", "member_ended"})


def mail_renders(env: MailEnvelope, settle: AbstractSet[str]) -> bool:
    """The mail kinds that render as a `<message>` line and would open a turn: a message, an
    ask, a bounce naming no ask, or a task or end monitor's notification (reference:
    turn_open.py). A reply, an ask's bounce and a wait's notification answer their call; a
    cancel and a park notice reach no model."""
    if env.kind in NOTICES:
        return env.monitor_id is not MISSING and env.monitor_id not in settle
    return env.kind in ("message", "ask") or (env.kind == "bounce" and env.ask_id is MISSING)


def mail_opens_turn(fold: Fold, env: MailEnvelope) -> bool:
    """In a member's log (the lead's included) with no turn open, mail that renders, once no
    park is left but the one it resolves (spec/schema/README.md, "Which events open a turn")."""
    if fold.in_turn or fold.team.team_log:
        return False
    resolved = all(p.kind == "member" and p.id == env.monitor_id for p in fold.parked)
    return mail_renders(env, fold.team.settle) and resolved


def principal_key(p: Principal) -> PrincipalKey:
    return (p.issuer, p.tenant, p.subject)


def mail_run(env: MailEnvelope) -> TurnRun:
    """The run a mail belongs to: its provenance's principal and root request."""
    root = env.provenance.root_request
    return TurnRun(principal_key(env.provenance.principal), (root.thread_id, root.event_id))


def joins_turn(turn: TurnRun | None, run: TurnRun) -> bool:
    """Whether mail of `run` shares the open turn's run (rule 34)."""
    if turn is None or turn.principal != run.principal:
        return False
    return turn.root is None or turn.root == run.root


def monitor_id(event: Event, target: str) -> str:
    """`<watcher branch_id>:<registering event_id>:<target name>`."""
    return f"{event.branch_id}:{event.event_id}:{target}"


def advance(fold: Fold, event: Event) -> None:
    """The team bookkeeping after an event passed validate_next."""
    _log(fold.team, event)
    _turn(fold, event)
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
    elif isinstance(event, MemberEndedEvent):
        team.ended = True
    elif isinstance(event, OperatorRequestEvent):
        team.requests.add(event.data.request_id)
        team.request_events.add(event.event_id)


def _turn(fold: Fold, event: Event) -> None:
    """Which run the open turn belongs to. A woken runs before rules_wake clears its causes."""
    team = fold.team
    if isinstance(event, UserInputEvent):
        task = event.data.source == "team_task"
        root = None if task else (event.thread_id, event.event_id)
        team.turn = TurnRun(principal_key(event.actor.principal), root)
    elif isinstance(event, WokenEvent):
        call = fold.wake.trailing.get(event.data.causes[0])
        team.turn = None if call is None else team.spawns.get(call)
    elif isinstance(event, TurnCompletedEvent):
        if event.data.reason != "end_turn" and team.turn is not None:
            team.ended_runs.add(team.turn)
        team.turn, team.last_end = None, event.data.reason
    elif isinstance(event, CancelRequestedEvent) and event.data.scope != "turn":
        team.barred.update(team.spawns)
    elif isinstance(event, AgentSpawnedEvent) and event.data.mode == "background" and team.turn:
        team.spawns[event.data.call_id] = team.turn


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
        run = mail_run(env)
        fold.in_turn, team.turn = True, run
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
