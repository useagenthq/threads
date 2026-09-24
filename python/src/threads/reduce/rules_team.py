"""Semantic rules 31 and 33-46 on one log (spec/schema/README.md, "Semantic rules"; reference:
spec/tools/fixtures/ref_rules.py). Rule 43 is cross-log and 32 is woken's (rules_wake)."""

from collections.abc import Mapping

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    AskClosedEvent,
    Event,
    ForkEvent,
    MailRefusedEvent,
    MemberEndedEvent,
    MemberIdleEvent,
    MemberObservedEvent,
    MemberStartedEvent,
    MessagePolicyDecidedEvent,
    MessageReceivedEvent,
    MessageSentEvent,
    OperatorRefusedEvent,
    OperatorRequestEvent,
    OperatorSender,
    ParkedEvent,
    ParseError,
    TeamOpenedEvent,
    UserInputEvent,
    WaitFinishedEvent,
    WaitStartedEvent,
    WokenEvent,
)
from threads.reduce.fold import Fold, reject
from threads.reduce.handlers import Handler, on
from threads.reduce.team_fold import (
    joins_turn,
    mail_opens_turn,
    mail_renders,
    mail_run,
    principal_key,
)
from threads.team.dynamic import KEPT, label_ok, refused_text

TEAM_LOG = frozenset(
    {
        "operator_request",
        "operator_refused",
        "message_policy_decided",
        "member_started",
        "message_sent",
        "message_received",
        "mail_refused",
        "ask_closed",
        "wait_started",
        "member_observed",
        "wait_finished",
    }
)
_ENDED = "an ended member's log opens a turn"


def check(fold: Fold, event: Event) -> ParseError | None:
    """Rule 33 for every event, then the team rules of its type."""
    if isinstance(event, TeamOpenedEvent) and fold.event_ids:
        return reject(event, "team_opened after the resolved chain's first event")
    repair = isinstance(event, ForkEvent) and event.data.reason == "repair"
    if fold.team.team_log and event.type not in TEAM_LOG and not repair:
        return reject(event, f"a team log takes no {event.type}")
    handler = _CHECKS.get(type(event))
    return None if handler is None else handler(fold, event)


def _received(fold: Fold, event: MessageReceivedEvent) -> ParseError | None:
    """Rules 31, 34, 37 and 45 for a receipt."""
    env = event.data.envelope
    why: str | None = None
    if event.data.mail_id != env.mail_id:
        why = "a receipt's mail_id is not its envelope's"
    elif env.mail_id in fold.team.mail_done:
        why = f"mail {env.mail_id} is received twice"
    elif env.kind == "task":
        why = "a task arrives as user_input{team_task}, not as mail"
    elif principal_key(event.actor.principal) != principal_key(env.provenance.principal):
        why = "a receipt's actor is not its provenance principal"
    else:
        why = _joins(fold, event)
    return None if why is None else reject(event, why)


def _joins(fold: Fold, event: MessageReceivedEvent) -> str | None:
    env, team = event.data.envelope, fold.team
    joins = joins_turn(team.turn, mail_run(env)) or not mail_renders(env, team.settle)
    if fold.in_turn and not joins:
        return "mail of another request joins the open turn"
    return _ENDED if fold.team.ended and mail_opens_turn(fold, env) else None


def _refused(fold: Fold, event: MailRefusedEvent) -> ParseError | None:
    """Rule 31 for a refusal."""
    if event.data.mail_id in fold.team.mail_done:
        return reject(event, f"mail {event.data.mail_id} is refused after it was taken")
    return None


def _sent(fold: Fold, event: MessageSentEvent) -> ParseError | None:
    """Rules 35, 42 and 44 for mail this log sends."""
    env, team = event.data.envelope, fold.team
    if env.kind == "ask" and env.ask_id != env.mail_id:
        return reject(event, "an ask's ask_id is not its own mail_id")
    if env.kind == "reply" and (env.ask_id is MISSING or env.ask_id not in team.asks_in):
        return reject(event, "a reply names no ask this log received and left unreplied")
    sender = env.from_
    if (
        team.team_log
        and isinstance(sender, OperatorSender)
        and sender.operator not in team.requests
    ):
        return reject(event, "operator mail before its operator_request")
    return None


def _ask_closed(fold: Fold, event: AskClosedEvent) -> ParseError | None:
    """Rule 36: an ask closes once, and an answer is a reply to it received here."""
    ask, outcome = event.data.ask_id, event.data.outcome
    if ask not in fold.team.asks_out:
        return reject(event, f"ask_closed closes no open ask {ask} of this log")
    if outcome.status == "answered" and fold.team.replies_in.get(outcome.reply) != ask:
        return reject(event, "answered names no received reply to this ask")
    return None


def _member_end(fold: Fold, event: MemberEndedEvent | MemberIdleEvent) -> ParseError | None:
    """Rules 37 and 38: a member ends once, and idles only after its turn ended well."""
    if fold.team.ended:
        return reject(event, f"{event.type} after member_ended")
    idle_ok = not fold.in_turn and fold.team.last_end == "end_turn"
    if isinstance(event, MemberIdleEvent) and not idle_ok:
        return reject(event, "member_idle without its turn's turn_completed{end_turn}")
    return None


def _wait_started(fold: Fold, event: WaitStartedEvent) -> ParseError | None:
    """Rules 39 and 42: a wait names each member once, and an operator wait follows its
    request."""
    names = [m.name for m in event.data.members]
    if len(set(names)) != len(names):
        return reject(event, "a wait lists a member twice")
    prefix, _, request = event.data.wait_id.partition(":")
    ours = prefix == event.branch_id and request in fold.team.requests
    if fold.team.team_log and not ours:
        return reject(event, "an operator wait before its operator_request")
    return None


def _wait_finished(fold: Fold, event: WaitFinishedEvent) -> ParseError | None:
    if event.data.wait_id in fold.team.waits:
        return None
    return reject(event, f"wait_finished names no open wait {event.data.wait_id}")


def _observed(fold: Fold, event: MemberObservedEvent) -> ParseError | None:
    if event.data.monitor_id in fold.team.monitors:
        return None
    return reject(event, f"member_observed names no live monitor {event.data.monitor_id}")


def _parked(fold: Fold, event: ParkedEvent) -> ParseError | None:
    """Rule 40: a team park names an open ask, an unfinished wait or a start of this log."""
    team, address = fold.team, event.data.address
    known = {"ask": team.asks_out, "wait": team.waits, "member": team.task_monitors}
    names = known.get(address.kind)
    if names is None or address.id in names:
        return None
    return reject(event, f"a park on {address.kind} {address.id} names nothing of this log")


def _input(fold: Fold, event: UserInputEvent) -> ParseError | None:
    """Rules 31, 37 and 41: a member's task is its first input and only it; an ended log opens
    no turn."""
    team, mail = fold.team, event.data.mail_id
    task = event.data.source == "team_task"
    why: str | None = None
    if team.ended:
        why = _ENDED
    elif mail is not MISSING and mail in team.mail_done:
        why = f"task {mail} is taken twice"
    elif not team.member:
        why = "team_task input outside a member's log" if task else None
    elif task == team.had_input:
        why = "a member's task is its first input, and only it"
    return None if why is None else reject(event, why)


def _woken(fold: Fold, event: WokenEvent) -> ParseError | None:
    """Rule 37: an ended log is never woken."""
    return reject(event, _ENDED) if fold.team.ended else None


def _request(_fold: Fold, event: OperatorRequestEvent) -> ParseError | None:
    """Rule 45: one principal, and the request is its own root request."""
    prov = event.data.provenance
    who = {principal_key(event.actor.principal), principal_key(event.data.principal)}
    if who != {principal_key(prov.principal)}:
        return reject(event, "an operator_request's principals differ")
    if (prov.root_request.thread_id, prov.root_request.event_id) != (
        event.thread_id,
        event.event_id,
    ):
        return reject(event, "an operator_request's root request is not itself")
    return None


def _operator_refused(fold: Fold, event: OperatorRefusedEvent) -> ParseError | None:
    if event.data.request_id in fold.team.requests:
        return None
    return reject(event, "operator_refused before its operator_request")


def _decided(fold: Fold, event: MessagePolicyDecidedEvent) -> ParseError | None:
    """Rule 42: a decision follows its operator_request, or names a pending tool_call."""
    request, call = event.data.request_id, event.data.call_id
    if request is not MISSING:
        if request in fold.team.requests:
            return None
        return reject(event, "message_policy_decided before its operator_request")
    if call is not MISSING and call in fold.pending:
        return None
    return reject(event, "message_policy_decided names no pending tool_call")


def _started(fold: Fold, event: MemberStartedEvent) -> ParseError | None:
    """Rules 42, 45 and 46: a lead's member_started is its member's parent; a team log's names the
    lead and follows its operator_request; a label and a define are well formed."""
    why = _defined(event)
    if why is not None:
        return reject(event, why)
    parent, team = event.data.parent, fold.team
    if team.team_log:
        if parent.thread_id != team.lead_thread:
            return reject(event, "an operator start's parent is not the lead's thread")
        root = event.data.provenance.root_request
        if root.thread_id == event.thread_id and root.event_id in team.request_events:
            return None
        return reject(event, "an operator start before its operator_request")
    itself = (parent.thread_id, parent.branch_id, parent.event_id) == (
        event.thread_id,
        event.branch_id,
        event.event_id,
    )
    return None if itself else reject(event, "a lead's member_started does not name itself")


def _defined(event: MemberStartedEvent) -> str | None:
    """Rule 46 on one log: the label has no control or format character, and a define names each
    tool once, none of F, and instructions that pass the block check."""
    d = event.data
    if d.label is not MISSING and not label_ok(d.label):
        return "a member_started label has a control or format character, or is too long"
    if d.define is MISSING:
        return None
    tools = d.define.tools
    if len(set(tools)) != len(tools) or any(t in KEPT for t in tools):
        return "define.tools repeats a tool or chooses a framework tool"
    written = d.define.instructions
    if written is not MISSING and refused_text(written):
        return "define.instructions holds the delimiter, the precedence sentence or a control"
    return None


_CHECKS: Mapping[type, Handler] = dict(
    [
        on(MessageReceivedEvent, _received),
        on(MailRefusedEvent, _refused),
        on(MessageSentEvent, _sent),
        on(AskClosedEvent, _ask_closed),
        on(MemberEndedEvent, _member_end),
        on(MemberIdleEvent, _member_end),
        on(WaitStartedEvent, _wait_started),
        on(WaitFinishedEvent, _wait_finished),
        on(MemberObservedEvent, _observed),
        on(ParkedEvent, _parked),
        on(UserInputEvent, _input),
        on(WokenEvent, _woken),
        on(OperatorRequestEvent, _request),
        on(OperatorRefusedEvent, _operator_refused),
        on(MessagePolicyDecidedEvent, _decided),
        on(MemberStartedEvent, _started),
    ]
)
