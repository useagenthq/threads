"""Semantic rules 50 and 52-55 on one log (spec/schema/README.md, "Teams Phase 2"): where a host
team's and a host member's forms may appear, callers, a host member's turn failures and the failed
ask. The reference is spec/tools/fixtures/ref_host.py; rule 43's cross-log clauses are
team/cross.py's.

Rule 51 (supervision) is rules_super's: this module's member_started and supervisor_decided cases
call it, and keeps here only the clause a first start takes — generation 1, once per name, with no
provenance."""

from collections.abc import Callable, Mapping

from pydantic.experimental.missing_sentinel import MISSING

from threadsai.log import (
    AskClosedEvent,
    BudgetExceededEvent,
    CallerAddress,
    Event,
    MailEnvelope,
    MemberEndedEvent,
    MemberIdleEvent,
    MemberStartedEvent,
    MessageReceivedEvent,
    MessageSentEvent,
    OperatorSender,
    ParseError,
    SupervisorDecidedEvent,
    TeamOpenedEvent,
    ThreadStartedEvent,
    TurnCompletedEvent,
)
from threadsai.reduce import rules_super
from threadsai.reduce.fold import Fold, Host, reject
from threadsai.reduce.handlers import Handler, on
from threadsai.team.host_team import TURN_KEPT, host_team_ids, turn_failure_code

ORDINARY = frozenset({"message", "ask"})
"""The mail kinds a host rule decides, so the kinds rule 55 keys by sender class."""


def sender_class(env: MailEnvelope) -> str:
    """Who decided a mail to a host member (rule 55): a rule is keyed by the sender's agent, so
    one caller agent, one member name, or the operator."""
    sender = env.from_
    if isinstance(sender, CallerAddress):
        return f"caller:{sender.caller.agent}"
    return "operator" if isinstance(sender, OperatorSender) else f"member:{sender.name}"


def tenant_team(env: MailEnvelope) -> bool:
    """Rule 52: host team mail belongs to the team its provenance principal's tenant derives."""
    return env.team == host_team_ids(env.provenance.principal.tenant).team


def check(fold: Fold, event: Event) -> ParseError | None:
    """Rule 50's placement, then rule 53's append order, then the rules of the event's type."""
    why = _placement(fold, event) or (_while_failing(event) if fold.host.failing else None)
    if why is not None:
        return reject(event, why)
    handler = _CHECKS.get(type(event))
    return None if handler is None else handler(fold, event)


def _placement(fold: Fold, event: Event) -> str | None:
    """Rule 50: host-only events and forms stay in a host team's or host member's log, and a
    host team's log is at the ids its tenant derives."""
    host = fold.host
    if isinstance(event, TeamOpenedEvent) and event.data.kind == "host":
        ids = host_team_ids(_tenant_of(event))
        here = (event.data.team, event.thread_id, event.branch_id)
        if here != (ids.team, ids.log_thread_id, ids.log_branch_id):
            return "50: a host team's ids are not the ones its tenant derives"
    started = isinstance(event, MemberStartedEvent)
    if started and (event.data.host_member is not MISSING) != host.host_team:
        return "50: a host team log starts only host members, and only it does"
    idled = isinstance(event, MemberIdleEvent) and event.data.turn_failed is not MISSING
    if idled and not host.host_member:
        return "53: member_idle{turn_failed} outside a host member's log"
    if isinstance(event, BudgetExceededEvent) and event.data.scope == "hop":
        return None if host.host_member else "53: a hop cap outside a host member's log"
    return None


def _tenant_of(event: TeamOpenedEvent) -> str:
    tenant = event.data.tenant
    return "" if tenant is MISSING else tenant


def _while_failing(event: Event) -> str | None:
    """Rule 53: after a failed turn, its asks' bounces, then member_idle{turn_failed}; or the
    same append ends the member (its own budget, a failed rebind), and rule 53 is off."""
    if isinstance(event, MessageSentEvent) and event.data.envelope.code == "turn_failed":
        return None
    if isinstance(event, MemberIdleEvent) and event.data.turn_failed is not MISSING:
        return None
    if event.type == "member_ended":
        return None
    return f"53: {event.type} before a failed turn's bounces and member_idle{{turn_failed}}"


def _started(fold: Fold, event: MemberStartedEvent) -> ParseError | None:
    """Rule 51: in a host team's log, a first host member start is generation 1, once per name,
    with no provenance; a start with `restart_of` is a restart (rules_super)."""
    if not fold.host.host_team:
        return None
    m, d = event.data.member, event.data
    if d.restart_of is not MISSING:
        why = rules_super.restarted(fold, event, d.restart_of)
        return None if why is None else reject(event, why)
    first = m.generation == 1 and m.name not in fold.host.generations and d.provenance is MISSING
    return None if first else reject(event, "51: a first host member start is generation 1, once")


def _decided(fold: Fold, event: SupervisorDecidedEvent) -> ParseError | None:
    """Rule 51: one decision per ended generation, with the logged count and the policy."""
    why = rules_super.decided(fold, event)
    return None if why is None else reject(event, why)


def _sent(fold: Fold, event: MessageSentEvent) -> ParseError | None:
    """Rules 52 and 53: a caller's mail names its own log and its tenant's host team; a
    turn_failed bounce answers an ask its failed turn took and left unanswered."""
    env = event.data.envelope
    why: str | None = None
    if isinstance(env.from_, CallerAddress):
        why = _caller_sent(fold, event, env)
    if why is None and env.code == "turn_failed":
        why = _turn_failed(fold, env)
    if why is None and env.kind == "reply" and env.ask_id in fold.host.answered:
        why = "53: a reply to an ask already bounced"
    return None if why is None else reject(event, why)


def _caller_sent(fold: Fold, event: MessageSentEvent, env: MailEnvelope) -> str | None:
    host = fold.host
    caller = env.from_
    if not isinstance(caller, CallerAddress):
        return None
    if not tenant_team(env):
        return "52: a caller's mail is for another tenant's host team"
    here = (caller.caller.thread_id, caller.caller.branch_id) == (event.thread_id, host.branch)
    if host.host_member or not here:
        return "52: a caller's mail names its own log, which is no host member's"
    if caller.caller.agent != host.agent:
        return "52: a caller's agent is its thread's agent"
    return None


def _turn_failed(fold: Fold, env: MailEnvelope) -> str | None:
    host = fold.host
    if not host.failing or env.ask_id not in host.turn_asks:
        return "53: a turn_failed bounce names no unanswered ask of a failed turn"
    if host.error is not None and env.error != host.error:
        return "53: one failed turn's bounces carry one error"
    if env.error is MISSING or env.error.code != host.code:
        return "53: a turn_failed error's code is not its turn end's"
    return None


def _received(fold: Fold, event: MessageReceivedEvent) -> ParseError | None:
    """Rules 52 and 55: a receipt to a caller is in that caller's log, host team mail stays in
    its principal's tenant, and a host member's turn takes mail of one sender class."""
    host, env = fold.host, event.data.envelope
    to = env.to
    caller = to.caller if isinstance(to, CallerAddress) else None
    if (caller is not None or host.host_member) and not tenant_team(env):
        return reject(event, "52: mail of another tenant's principal in a host team")
    if caller is not None and (caller.thread_id, caller.branch_id) != (
        event.thread_id,
        host.branch,
    ):
        return reject(event, "52: a receipt to a caller is in that caller's log")
    klass = sender_class(env)
    mixed = host.host_member and env.kind in ORDINARY and host.turn_class not in (None, klass)
    if mixed:
        return reject(event, "55: a host member's turn takes mail of one sender class")
    return None


def _idle(fold: Fold, event: MemberIdleEvent) -> ParseError | None:
    """Rule 53: member_idle{turn_failed} closes a failed turn once every ask is bounced."""
    host, failed = fold.host, event.data.turn_failed
    if failed is MISSING:
        return None
    if not host.failing or host.turn_asks:
        return reject(event, "53: member_idle{turn_failed} before every ask of its turn bounced")
    if host.error is not None and failed != host.error:
        return reject(event, "53: its error is not the turn's bounces'")
    if failed.code != host.code:
        return reject(event, "53: its code is not its turn end's")
    return None


def _ask_closed(fold: Fold, event: AskClosedEvent) -> ParseError | None:
    """Rule 54: failed closes an ask whose turn_failed bounce this log received, and such an ask
    closes only failed."""
    outcome, ask = event.data.outcome, event.data.ask_id
    got = fold.host.bounces_in.get(ask)
    if outcome.status == "failed":
        same = got is not None and got == outcome.error
        return None if same else reject(event, "54: failed names no received turn_failed bounce")
    if got is not None:
        return reject(event, "54: a turn_failed bounce closes its ask failed")
    return None


_CHECKS: Mapping[type, Handler] = dict(
    [
        on(MemberStartedEvent, _started),
        on(MessageSentEvent, _sent),
        on(MessageReceivedEvent, _received),
        on(MemberIdleEvent, _idle),
        on(AskClosedEvent, _ask_closed),
        on(SupervisorDecidedEvent, _decided),
    ]
)


# ---------- the fold ----------


def advance(fold: Fold, event: Event) -> None:
    """The Phase 2 bookkeeping after an event passed validate_next."""
    host = fold.host
    if len(fold.events) == 1:
        _first(host, event)
    # Set before the step: when the next event is checked this holds the previous event's type,
    # which is what rule 51's "directly follows its decision" reads.
    host.last_type = event.type
    step = _FOLDS.get(type(event))
    if step is not None:
        step(host, event)


def _first(host: Host, event: Event) -> None:
    host.branch = event.branch_id
    if isinstance(event, TeamOpenedEvent):
        host.host_team = event.data.kind == "host"
    elif isinstance(event, ThreadStartedEvent):
        host.host_member = event.data.host_member is not MISSING
        host.agent = event.data.agent_name


def _fold_started(host: Host, event: MemberStartedEvent) -> None:
    host.generations[event.data.member.name] = event.data.member.generation


def _fold_received(host: Host, event: MessageReceivedEvent) -> None:
    env = event.data.envelope
    if host.host_member and env.kind in ORDINARY and host.turn_class is None:
        host.turn_class = sender_class(env)
    if host.host_member and env.kind == "ask" and env.ask_id is not MISSING:
        host.turn_asks.append(env.ask_id)
    bounced = env.kind == "bounce" and env.code == "turn_failed"
    if bounced and env.ask_id is not MISSING and env.error is not MISSING:
        host.bounces_in[env.ask_id] = env.error


def _fold_sent(host: Host, event: MessageSentEvent) -> None:
    env = event.data.envelope
    if env.code == "turn_failed" and env.error is not MISSING:
        host.error = env.error
    if env.kind in ("reply", "bounce") and env.ask_id is not MISSING:
        host.answered.add(env.ask_id)
        if env.ask_id in host.turn_asks:
            host.turn_asks.remove(env.ask_id)


def _fold_turn_end(host: Host, event: TurnCompletedEvent) -> None:
    if host.host_member and event.data.reason not in TURN_KEPT:
        code = turn_failure_code(event.data)
        host.failing, host.error, host.code = True, None, "" if code is None else code
        return
    _clear(host)


def _fold_idle(host: Host, event: MemberIdleEvent) -> None:
    if event.data.turn_failed is not MISSING:
        _clear(host)


def _fold_ended(host: Host, _event: Event) -> None:
    """A member_ended in a failed turn's append: the member ends instead, so its taken,
    unanswered asks stay open and close at their deadlines."""
    _clear(host)


def _clear(host: Host) -> None:
    host.failing, host.error, host.code = False, None, ""
    host.turn_asks, host.turn_class = [], None


type _Step = Callable[[Host, Event], None]


def _step[E](cls: type[E], run: Callable[[Host, E], None]) -> tuple[type, _Step]:
    """Binds a fold step typed for one event class into the table's uniform signature."""

    def apply_step(host: Host, event: Event) -> None:
        if not isinstance(event, cls):
            raise TypeError(f"{type(event).__name__} dispatched to the {cls.__name__} step")
        run(host, event)

    return cls, apply_step


_FOLDS: Mapping[type, _Step] = dict(
    [
        _step(MemberStartedEvent, _fold_started),
        _step(MessageReceivedEvent, _fold_received),
        _step(MessageSentEvent, _fold_sent),
        _step(TurnCompletedEvent, _fold_turn_end),
        _step(MemberIdleEvent, _fold_idle),
        _step(MemberEndedEvent, _fold_ended),
        _step(SupervisorDecidedEvent, rules_super.advance),
    ]
)
