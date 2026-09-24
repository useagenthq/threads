"""Semantic rules 43 and 46: what only a team's logs together show (spec/schema/README.md, "Semantic
rules"). Checked by the `team` runner and every index rebuild, never by one log's validate_next.

Every receipt copies its sender's envelope byte for byte, and each mail leaves the log its
`from` names and arrives in the log its `to` names. A bounce's causal is the refuser's
mail_refused, naming the ask exactly when the refused mail is an ask. A member's
thread_started.parent is its member_started's parent, and a team log is the thread and branch its
lead's thread_started.team names. A task's input principal is the task's
provenance principal. A dynamic member pins exactly its define's tools and F, and its line 0
ends with the block of its define's instructions (rule 46). The reference is
spec/tools/fixtures/ref_team.py.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    BranchId,
    Event,
    MailEnvelope,
    MailRefusedEvent,
    MemberRef,
    MemberStartedEvent,
    MessageReceivedEvent,
    MessageSentEvent,
    Parent,
    Parent1,
    TeamOpenedEvent,
    ThreadId,
    ThreadStartedEvent,
    UserInputEvent,
)
from threads.reduce import Fold, apply
from threads.reduce.team_fold import mail_renders
from threads.team.dynamic import KEPT, OPERATOR, block
from threads.team.index import json_bytes

type Identity = tuple[str, str, int] | str
"""A member (team, name, generation), or "team_log"."""


@dataclass(frozen=True, slots=True)
class TeamLogEvents:
    thread_id: ThreadId
    branch_id: BranchId
    events: Sequence[Event]


@dataclass(frozen=True, slots=True)
class CrossFailure:
    """The first event that breaks rule 43: invalid_transition at `seq` of `branch_id`."""

    branch_id: BranchId
    seq: int
    message: str


@dataclass(frozen=True, slots=True)
class _Team:
    sent: Mapping[str, MailEnvelope]
    started: Mapping[ThreadId, MemberStartedEvent]
    identities: Mapping[ThreadId, frozenset[Identity]]
    team_logs: Mapping[str, tuple[str, str]]
    """Each team's log (thread, branch), as its lead's thread_started names it."""
    starters: Mapping[ThreadId, str]
    """Each member's starter: operator for a start in a team log, else the lead's agent name."""


def check_team_logs(
    logs: Sequence[TeamLogEvents], team_id: str | None = None
) -> CrossFailure | None:
    """The first failure, in the given log order, then seq. With `team_id`, the clauses about one
    mail check only that team's mail: a nested lead's log also carries its parent team's mail,
    whose other ends are not among its own team's logs. The task-turn clause checks all."""
    team = _team(logs)
    for log in logs:
        found = _first_break(log, team, team_id) or _task_turn(log, team)
        if found is not None:
            return found
    return None


def _first_break(log: TeamLogEvents, team: _Team, team_id: str | None) -> CrossFailure | None:
    mine = team.identities.get(log.thread_id, frozenset[Identity]())
    for e in log.events:
        if team_id is not None and _mail_of_another(e, team_id):
            continue
        why = _check(e, log, mine, team)
        if why is not None:
            return CrossFailure(log.branch_id, e.seq, f"43: {why}")
        why = _dynamic(e, team)
        if why is not None:
            return CrossFailure(log.branch_id, e.seq, f"46: {why}")
    return None


def _dynamic(e: Event, team: _Team) -> str | None:
    """A dynamic member's thread_started pins define.tools and F, and its instructions end
    with the block for define.instructions."""
    start = team.started.get(e.thread_id) if isinstance(e, ThreadStartedEvent) else None
    if not isinstance(e, ThreadStartedEvent) or start is None or start.data.define is MISSING:
        return None
    define = start.data.define
    if [t.name for t in e.data.tools if t.name not in KEPT] != list(define.tools):
        return "a dynamic member's pinned tools are not define.tools and F"
    written = define.instructions
    starter = team.starters.get(e.thread_id, "")
    if written is not MISSING and not e.data.instructions.endswith(block(starter, written)):
        return "a dynamic member's instructions don't end with its define's block"
    return None


def _task_turn(log: TeamLogEvents, team: _Team) -> CrossFailure | None:
    """Mail that renders inside a member's task turn belongs to the task's run: one log shows
    only the task turn's principal (rule 34), and the task envelope, in the starter's log, holds
    its root request. The log is re-folded with the reducer to know its turns. Never limited to
    one team's mail: a nested lead's own-team mail can arrive in the task turn its outer team
    gave it, and only the outer team's rebuild knows that task."""
    fold = Fold(now=0, thread_id=log.thread_id)
    root: tuple[str, str] | None = None
    for e in log.events:
        turn = fold.team.turn
        if (
            isinstance(e, MessageReceivedEvent)
            and turn is not None
            and turn.root is None
            and root is not None
            and mail_renders(e.data.envelope, fold.team.settle)
            and _root(e.data.envelope) != root
        ):
            why = "43: mail of another run joins a member's task turn"
            return CrossFailure(log.branch_id, e.seq, why)
        if isinstance(e, UserInputEvent) and e.data.mail_id is not MISSING:
            task = team.sent.get(e.data.mail_id)
            root = root if task is None else _root(task)
        fold.segment = e.branch_id  # the chain was verified; only its state is wanted here
        apply(fold, e)
    return None


def _root(env: MailEnvelope) -> tuple[str, str]:
    r = env.provenance.root_request
    return (r.thread_id, r.event_id)


def _mail_of_another(e: Event, team_id: str) -> bool:
    return (
        isinstance(e, MessageSentEvent | MessageReceivedEvent) and e.data.envelope.team != team_id
    )


def _team(logs: Sequence[TeamLogEvents]) -> _Team:
    events = [(log, e) for log in logs for e in log.events]
    started = {e.data.thread_id: e for _, e in events if isinstance(e, MemberStartedEvent)}
    identities: dict[ThreadId, set[Identity]] = {}
    team_logs: dict[str, tuple[str, str]] = {}
    starters: dict[ThreadId, str] = {}
    logged = {e.thread_id for _, e in events if isinstance(e, TeamOpenedEvent)}
    leads = {e.thread_id: e.data.agent_name for _, e in events if isinstance(e, ThreadStartedEvent)}
    for log, e in events:
        if isinstance(e, TeamOpenedEvent):
            identities.setdefault(log.thread_id, set()).add("team_log")
        elif isinstance(e, ThreadStartedEvent) and e.data.team is not MISSING:
            t = e.data.team
            identities.setdefault(log.thread_id, set()).add((t.id, e.data.agent_name, 1))
            team_logs[t.id] = (t.log_thread_id, t.log_branch_id)
    for thread, s in started.items():
        m = s.data.member
        starters[thread] = OPERATOR if s.thread_id in logged else leads.get(s.thread_id, "")
        identities.setdefault(thread, set()).add((m.team, m.name, m.generation))
    sent = {
        e.data.envelope.mail_id: e.data.envelope
        for _, e in events
        if isinstance(e, MessageSentEvent)
    }
    frozen = {t: frozenset(i) for t, i in identities.items()}
    return _Team(sent, started, frozen, team_logs, starters)


def _check(e: Event, log: TeamLogEvents, mine: frozenset[Identity], team: _Team) -> str | None:
    if isinstance(e, MessageReceivedEvent):
        return _receipt(e.data.envelope, mine, team)
    if isinstance(e, MessageSentEvent):
        return _sent(e.data.envelope, log, mine, team)
    if isinstance(e, TeamOpenedEvent):
        return _named(e, log, team)
    if isinstance(e, ThreadStartedEvent):
        start = team.started.get(e.thread_id)
        parent = None if e.data.parent is MISSING else e.data.parent
        if start is not None and _link(parent) != _link(start.data.parent):
            return "a member's parent is not its member_started's"
    if isinstance(e, UserInputEvent) and e.data.mail_id is not MISSING:
        task = team.sent.get(e.data.mail_id)
        if task is not None and e.actor.principal != task.provenance.principal:
            return "a task's input principal is not its mail's provenance principal"
    return None


def _named(e: TeamOpenedEvent, log: TeamLogEvents, team: _Team) -> str | None:
    """A team log is the thread and branch its lead's thread_started.team names."""
    named = team.team_logs.get(e.data.team)
    if named is None or named == (log.thread_id, log.branch_id):
        return None
    return "a team log is not the thread its lead names"


def _link(p: Parent | Parent1 | None) -> tuple[str, str, str, str] | None:
    return None if p is None else (p.relation, p.thread_id, p.branch_id, p.event_id)


def _to(env: MailEnvelope) -> Identity:
    return "team_log" if isinstance(env.to, str) else (env.team, env.to.name, env.to.generation)


def _from(env: MailEnvelope) -> Identity:
    f = env.from_
    return (f.team, f.name, f.generation) if isinstance(f, MemberRef) else "team_log"


def _receipt(env: MailEnvelope, mine: frozenset[Identity], team: _Team) -> str | None:
    sender = team.sent.get(env.mail_id)
    if sender is not None and json_bytes(sender) != json_bytes(env):
        return "a receipt differs from its sender's mail"
    if _to(env) not in mine:
        return "a receipt is not in the log its mail is addressed to"
    return None


def _sent(
    env: MailEnvelope, log: TeamLogEvents, mine: frozenset[Identity], team: _Team
) -> str | None:
    if _from(env) not in mine:
        return "a mail is not in the log its from names"
    return _bounce(env, log, team) if env.kind == "bounce" else None


def _bounce(env: MailEnvelope, log: TeamLogEvents, team: _Team) -> str | None:
    """A bounce's causal is its refuser's mail_refused; it carries the refused mail's provenance,
    and names an ask exactly when it refused one."""
    cause = env.causal.event_id
    refusal = next((e for e in log.events if e.event_id == cause), None)
    if not isinstance(refusal, MailRefusedEvent):
        return "a bounce's causal is not its mail_refused"
    refused = team.sent.get(refusal.data.mail_id)
    if refused is None:
        return None
    if env.provenance != refused.provenance:
        return "a bounce's provenance is not its refused mail's"
    if refused.kind == "ask":
        ok = env.ask_id == refused.ask_id and env.result is not MISSING
        return None if ok else "an ask's bounce must name the ask and carry the result"
    return "only an ask's bounce names an ask" if env.ask_id is not MISSING else None
