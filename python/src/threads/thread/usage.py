"""Thread.cost(tree=True): the tree merge over this thread and every descendant, each read from
its own main branch, so the logs stay the only truth (the budget ledger is a cache, never reused
here)."""

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.store import Store, open_store
from threads.log import (
    AgentFinishedEvent,
    AgentSpawnedEvent,
    Cost,
    Event,
    ModelRequestEvent,
    ParseError,
    ThreadId,
    ThreadStartedEvent,
)
from threads.reduce.projections import TreePart, cost, merge_tree
from threads.result import Err, Ok
from threads.store import VerifiedLog
from threads.team.members import Member, open_member, team_members
from threads.thread.read import read_log


@dataclass(frozen=True, slots=True)
class _Pending:
    """A spawned child still to read and walk, with the path of child ids that leads to it."""

    spawn: AgentSpawnedEvent
    finish: AgentFinishedEvent | None
    path: str


@dataclass(frozen=True, slots=True)
class _Member:
    """A team member of a visited lead, still to read and walk."""

    member: Member
    lead: VerifiedLog
    path: str


type _Child = _Pending | _Member


async def tree_cost(
    store: Store, root: ThreadId, log: VerifiedLog
) -> Ok[Cost | None] | Err[ParseError]:
    """Every thread of the tree, depth first in spawn order, with an explicit stack so a tree of
    any depth is walked, and each child read only on its turn, so the first broken path depth
    first is the one reported. Each child must name, as its parent, the agent_spawned that
    started it; a child that doesn't, or a thread named twice (a cycle, or two spawns of one id),
    makes the tree log_corrupt. A child with no log counts as an unpriced thread that ran:
    nothing proves it spent nothing (its log may have been deleted), so the total is incomplete
    and unbounded, never falsely complete. A lead's team members are its children too (after its
    spawns); one in the starting window counts zero."""
    parts: list[TreePart] = []
    seen = {root}
    stack: list[_Child] = []
    walk: tuple[VerifiedLog, str] | None = (log, "")
    while walk is not None:
        at, path = walk
        own = cost(at.fold)
        if isinstance(own, Err):
            return _within(path, own)
        events = at.fold.events
        parts.append(TreePart(own.value, any(isinstance(e, ModelRequestEvent) for e in events)))
        members = [
            _Member(m, at, f"{path}member {m.name}: ") for m in await team_members(store, at)
        ]
        stack.extend(reversed([*_pending(events, path), *members]))
        read = await _next_child(store, stack, seen, parts)
        if isinstance(read, Err):
            return read
        walk = read.value
    return merge_tree(parts)


def _pending(events: Sequence[Event], path: str) -> list[_Pending]:
    """The spawns of `events`, in order, each with the parent's agent_finished for its child."""
    finished = {e.data.child_thread_id: e for e in events if isinstance(e, AgentFinishedEvent)}
    return [
        _Pending(e, finished.get(e.data.child_thread_id), f"{path}child {e.data.child_thread_id}: ")
        for e in events
        if isinstance(e, AgentSpawnedEvent)
    ]


async def _next_child(
    store: Store, stack: list[_Child], seen: set[ThreadId], parts: list[TreePart]
) -> Ok[tuple[VerifiedLog, str] | None] | Err[ParseError]:
    """Pops pending children until one has a log to walk; a spawned child without a log adds
    its unpriced part instead, a member in the starting window nothing. None when the stack is
    empty."""
    while stack:
        top = stack.pop()
        child = top.member.thread_id if isinstance(top, _Member) else top.spawn.data.child_thread_id
        if child in seen:
            why = f"{top.path}thread {child} appears twice in the tree"
            return Err(ParseError("log_corrupt", why))
        seen.add(child)
        read = await _open(store, top)
        if isinstance(read, Err):
            return _within(top.path, read)
        if isinstance(read.value, VerifiedLog):
            return Ok((read.value, top.path))
        if read.value is None:
            parts.append(TreePart(None, ran=True))
    return Ok(None)


async def _open(store: Store, child: _Child) -> Ok[VerifiedLog | str | None] | Err[ParseError]:
    if isinstance(child, _Member):
        return await open_member(store, child.lead, child.member)
    return await _child_log(store, child.spawn, child.finish)


def _within(path: str, failed: Err[ParseError]) -> Err[ParseError]:
    """The error, keeping its code, with the path to the descendant that failed."""
    e = failed.error
    return Err(ParseError(e.code, f"{path}{e.message}", e.seq)) if path else failed


def _never_created(finish: AgentFinishedEvent) -> bool:
    """spec/schema/README.md, Subagent cancellation: a child with no thread is recorded
    cancelled, with unknown usage, and never created. A started child cancelled with unknown
    usage writes the same record, so it can't prove the child spent nothing."""
    usage = finish.data.usage
    return finish.data.status == "cancelled" and (usage.input_tokens, usage.output_tokens) == (
        None,
        None,
    )


async def _child_log(
    store: Store, spawn: AgentSpawnedEvent, finish: AgentFinishedEvent | None
) -> Ok[VerifiedLog | None] | Err[ParseError]:
    """The spawned child's log; None when it has no thread and its parent's record allows that:
    no agent_finished, or the one a cancelled parent writes for a child it never created. Any
    other finished child's missing log is log_corrupt."""
    root = await (await open_store(store)).root(spawn.data.child_thread_id)
    if isinstance(root, Err) and finish is not None and not _never_created(finish):
        why = f"its log is missing, though its parent recorded it {finish.data.status}"
        return Err(ParseError("log_corrupt", why))
    if isinstance(root, Err):
        return Ok(None)
    read = await read_log(store, root.value)
    if isinstance(read, Err):
        return read
    started = next((e for e in read.value.fold.events if isinstance(e, ThreadStartedEvent)), None)
    link = MISSING if started is None else started.data.parent
    backlinked = link is not MISSING and (
        link.relation == "subagent"
        and link.thread_id == spawn.thread_id
        and link.branch_id == spawn.branch_id
        and link.event_id == spawn.event_id
    )
    if not backlinked:
        why = "its thread_started doesn't name the agent_spawned that started it"
        return Err(ParseError("log_corrupt", why))
    return read
