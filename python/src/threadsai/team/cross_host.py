"""The Teams Phase 2 clauses of rule 43 (spec/schema/README.md, "Semantic rules"): what only a
host team's logs and its callers' logs together show. `cross` runs them with the Phase 1 clauses.
The reference is spec/tools/fixtures/ref_host_cross.py.

Rule 51's cross-log clause is `supervised` at the end of this module: only the host team's log and
the member's own log together show that a decision named its own member's end.
"""

from collections.abc import Mapping, Sequence

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threadsai.log import (
    BranchId,
    CallerAddress,
    Event,
    MailAddress,
    MailAddress1,
    MailEnvelope,
    MemberEndedEvent,
    MemberRef,
    MemberStartedEvent,
    OperatorSender,
    PosInt,
    SupervisorDecidedEvent,
    ThreadId,
    ThreadStartedEvent,
    TurnCompletedEvent,
)
from threadsai.reduce.handlers import to_json
from threadsai.team.host_team import TURN_KEPT


def reply_address(ask: MailEnvelope) -> MailAddress:
    """Where an answer to this ask goes: back to its sender."""
    sender = ask.from_
    if isinstance(sender, CallerAddress):
        return sender
    if isinstance(sender, OperatorSender):
        return "team_log"
    return MailAddress1(name=sender.name, generation=sender.generation)


def reply_to(ask: MailEnvelope) -> JsonValue:
    """`reply_address` as an envelope's `to` records it."""
    back = reply_address(ask)
    return back if isinstance(back, str) else to_json(back)


def turn_failed_bounce(
    env: MailEnvelope, events: Sequence[Event], sent: Mapping[str, MailEnvelope]
) -> str | None:
    """A turn_failed bounce's causal is its failed turn's turn_completed in the sender's log, and
    it goes back to the ask's sender with the ask's provenance."""
    end = next((e for e in events if e.event_id == env.causal.event_id), None)
    if not isinstance(end, TurnCompletedEvent) or end.data.reason in TURN_KEPT:
        return "a turn_failed bounce's causal is its failed turn's turn_completed"
    ask = None if env.ask_id is MISSING else sent.get(env.ask_id)
    if ask is None:
        return None
    ok = env.provenance == ask.provenance and env.to == reply_address(ask)
    return None if ok else "a turn_failed bounce goes back to its asker, with its provenance"


def reply_to_caller(env: MailEnvelope, sent: Mapping[str, MailEnvelope]) -> str | None:
    """A reply to a caller answers an ask that caller sent."""
    to = env.to
    if env.kind != "reply" or not isinstance(to, CallerAddress) or env.ask_id is MISSING:
        return None
    ask = sent.get(env.ask_id)
    return (
        None if ask is None or ask.from_ == to else "a reply to a caller answers that caller's ask"
    )


def host_member_thread(
    e: ThreadStartedEvent, started: Mapping[ThreadId, MemberStartedEvent]
) -> str | None:
    """A host member's log is the one its thread_started.host_member names, and that is its
    member_started{host_member}'s thread."""
    hm = e.data.host_member
    if hm is MISSING:
        return None
    start = started.get(e.thread_id)
    if start is None:
        return None
    m = start.data.member
    same = (m.team, m.name, m.generation) == (hm.team, hm.name, hm.generation)
    ok = same and start.data.host_member is not MISSING
    return None if ok else "a host member's thread is not its member_started's"


def ended_bounce(
    env: MailEnvelope, end: MemberEndedEvent, sent: Mapping[str, MailEnvelope]
) -> str | None:
    """A host member's member_ended bounces every ask its last turn had taken and never
    answered (coordinator decision 6, 2026-09-26): there is no mail_refused for such an ask, so
    its causal is that member_ended, and it carries the end's result and the ask's provenance
    back to the asker."""
    if env.ask_id is MISSING or env.result is MISSING:
        return "a member_ended bounce names its ask and carries the end's result"
    if env.result != end.data.result:
        return "a member_ended bounce's result is not its end's"
    ask = sent.get(env.ask_id)
    if ask is None:
        return None
    ok = env.provenance == ask.provenance and env.to == reply_address(ask)
    return None if ok else "a member_ended bounce goes back to its asker, with its provenance"


def supervised(
    e: SupervisorDecidedEvent, lines: Mapping[tuple[BranchId, PosInt], Event]
) -> str | None:
    """Rule 51 across the logs: a decision's `ended` is its own member's `member_ended`, and its
    action is `restart` exactly when the policy allows it and that end's result is `failed`. A
    position among no log given here proves nothing, so it passes.
    Reference: spec/tools/fixtures/ref_host_cross.py::supervised."""
    d = e.data
    end = lines.get((d.ended.branch_id, d.ended.seq))
    if end is None:
        return None
    if not isinstance(end, MemberEndedEvent):
        return "a decision's ended names no member_ended"
    if end.thread_id != _started_thread(d.member, lines):
        return "a decision's ended is not its member's own member_ended"
    failed = end.data.result.status == "failed"
    allowed = d.policy.restart == "on_failure" and d.restarts_in_window < d.policy.max_restarts
    if (d.action == "restart") != (allowed and failed):
        return "restart exactly when allowed and failed"
    return None


def _started_thread(
    member: MemberRef, lines: Mapping[tuple[BranchId, PosInt], Event]
) -> ThreadId | None:
    """The thread the `member_started` of this exact generation named."""
    for line in lines.values():
        if isinstance(line, MemberStartedEvent) and line.data.member == member:
            return line.data.thread_id
    return None
