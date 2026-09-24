"""A lead's members, as the tree walks find them (spec/schema/README.md, "Tree walks with teams"):
every `team_members` row of role member (never the lead's own), every generation, model- or
operator-started. A member with a branch is counted once its thread_started names the lead's
member_started (or, for an operator start, the lead's thread_started). A member in the starting
window counts zero; any other member without a branch makes the tree log_corrupt."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.store import Store, open_store
from threads.log import (
    BranchId,
    Event,
    MemberStartedEvent,
    MessageReceivedEvent,
    ParseError,
    ThreadId,
    ThreadStartedEvent,
)
from threads.result import Err, Ok
from threads.store import VerifiedLog
from threads.store.sql import int_of, text_of
from threads.thread.read import read_log

PENDING: Final = "pending"
"""A member in the starting window: no branch, no model request, nothing spent."""
_NOTICES: Final = frozenset({"member_settled", "member_ended"})


@dataclass(frozen=True, slots=True)
class Member:
    """One member row of a lead's team."""

    team_id: str
    name: str
    generation: int
    thread_id: ThreadId
    branch_id: BranchId | None


def _started(log: VerifiedLog) -> ThreadStartedEvent | None:
    return next((e for e in log.fold.events if isinstance(e, ThreadStartedEvent)), None)


async def team_members(store: Store, lead: VerifiedLog) -> tuple[Member, ...]:
    """The member rows of the team `lead` leads, by name then generation; none if it leads none."""
    started = _started(lead)
    if started is None or started.data.team is MISSING:
        return ()
    team = started.data.team.id
    rows: list[tuple[object, object, object, object]] = await (await open_store(store)).run(
        lambda c: c.execute(
            "SELECT name, generation, thread_id, branch_id FROM team_members"
            " WHERE team_id = ? AND role = 'member' ORDER BY name, generation",
            (team,),
        ).fetchall()
    )
    return tuple(
        Member(
            team,
            text_of(n),
            int_of(g),
            ThreadId(text_of(t)),
            None if b is None else BranchId(text_of(b)),
        )
        for n, g, t, b in rows
    )


async def open_member(
    store: Store, lead: VerifiedLog, member: Member
) -> Ok[VerifiedLog | Literal["pending"]] | Err[ParseError]:
    """The member's log, checked against the lead; PENDING in the starting window."""
    start = _member_started(lead.fold.events, member)
    if member.branch_id is None:
        return await _without_branch(store, lead, member, start)
    read = await read_log(store, member.branch_id)
    if isinstance(read, Err):
        return read
    lead_started = _started(lead)
    parent = _started(read.value)
    link = MISSING if parent is None else parent.data.parent
    expected = start if start is not None else lead_started
    header = lead.segments[-1].header
    backlinked = (
        link is not MISSING
        and expected is not None
        and link.relation == "team_member"
        and (link.thread_id, link.branch_id) == (header.thread_id, header.branch_id)
        and link.event_id == expected.event_id
    )
    if not backlinked:
        why = "its thread_started doesn't name the member_started that started it"
        return Err(ParseError("log_corrupt", why))
    return read


def _member_started(events: Sequence[Event], member: Member) -> MemberStartedEvent | None:
    return next(
        (
            e
            for e in events
            if isinstance(e, MemberStartedEvent)
            and (e.data.member.team, e.data.member.name, e.data.member.generation)
            == (member.team_id, member.name, member.generation)
        ),
        None,
    )


async def _without_branch(
    store: Store, lead: VerifiedLog, member: Member, start: MemberStartedEvent | None
) -> Ok[VerifiedLog | Literal["pending"]] | Err[ParseError]:
    """The starting window, when the starter's log (the lead's, else the team log) has no task
    notification for the member; any other member without a branch is log_corrupt."""
    starter = lead
    if start is None:
        team_log = await _team_log(store, lead)
        if isinstance(team_log, Err):
            return team_log
        starter = team_log.value
        start = _member_started(starter.fold.events, member)
    if start is None:
        return Err(ParseError("log_corrupt", f"no member_started for {member.name}"))
    monitor = f"{starter.segments[-1].header.branch_id}:{start.event_id}:task"
    notified = any(
        isinstance(e, MessageReceivedEvent)
        and e.data.envelope.kind in _NOTICES
        and e.data.envelope.monitor_id == monitor
        for e in starter.fold.events
    )
    if notified:
        why = f"{member.name} has no log, though its task notification arrived"
        return Err(ParseError("log_corrupt", why))
    return Ok(PENDING)


async def _team_log(store: Store, lead: VerifiedLog) -> Ok[VerifiedLog] | Err[ParseError]:
    started = _started(lead)
    if started is None or started.data.team is MISSING:
        return Err(ParseError("log_corrupt", "a member row of a thread that leads no team"))
    return await read_log(store, started.data.team.log_branch_id)
