"""Semantic rule 43: what only a team's logs together show (spec/schema/README.md, "Semantic
rules"). Checked by the `team` runner and every index rebuild, never by one log's validate_next.

Every receipt copies its sender's envelope byte for byte, and each mail leaves the log its
`from` names and arrives in the log its `to` names. A bounce's causal is the refuser's
mail_refused, naming the ask exactly when the refused mail is an ask. A member's
thread_started.parent is its member_started's parent. A task's input principal is the task's
provenance principal. The reference is spec/tools/fixtures/ref_team.py.
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


def check_team_logs(logs: Sequence[TeamLogEvents]) -> CrossFailure | None:
    """The first failure, in the given log order, then seq."""
    team = _team(logs)
    for log in logs:
        mine = team.identities.get(log.thread_id, frozenset[Identity]())
        for e in log.events:
            why = _check(e, log, mine, team)
            if why is not None:
                return CrossFailure(log.branch_id, e.seq, f"43: {why}")
    return None


def _team(logs: Sequence[TeamLogEvents]) -> _Team:
    events = [(log, e) for log in logs for e in log.events]
    started = {e.data.thread_id: e for _, e in events if isinstance(e, MemberStartedEvent)}
    identities: dict[ThreadId, set[Identity]] = {}
    for log, e in events:
        if isinstance(e, TeamOpenedEvent):
            identities.setdefault(log.thread_id, set()).add("team_log")
        elif isinstance(e, ThreadStartedEvent) and e.data.team is not MISSING:
            lead = (e.data.team.id, e.data.agent_name, 1)
            identities.setdefault(log.thread_id, set()).add(lead)
    for thread, s in started.items():
        m = s.data.member
        identities.setdefault(thread, set()).add((m.team, m.name, m.generation))
    sent = {
        e.data.envelope.mail_id: e.data.envelope
        for _, e in events
        if isinstance(e, MessageSentEvent)
    }
    return _Team(sent, started, {t: frozenset(i) for t, i in identities.items()})


def _check(e: Event, log: TeamLogEvents, mine: frozenset[Identity], team: _Team) -> str | None:
    if isinstance(e, MessageReceivedEvent):
        return _receipt(e.data.envelope, mine, team)
    if isinstance(e, MessageSentEvent):
        return _sent(e.data.envelope, log, mine, team)
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
    if env.kind != "bounce":
        return None
    cause = env.causal.event_id
    refusal = next((e for e in log.events if e.event_id == cause), None)
    if not isinstance(refusal, MailRefusedEvent):
        return "a bounce's causal is not its mail_refused"
    refused = team.sent.get(refusal.data.mail_id)
    if refused is None:
        return None
    if refused.kind == "ask":
        ok = env.ask_id == refused.ask_id and env.result is not MISSING
        return None if ok else "an ask's bounce must name the ask and carry the result"
    return "only an ask's bounce names an ask" if env.ask_id is not MISSING else None
