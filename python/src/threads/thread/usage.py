"""Thread.cost(tree=True): the tree merge over this thread and every descendant, each read from
its own main branch, so the logs stay the only truth (the budget ledger is a cache, never reused
here)."""

from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.store import Store, open_store
from threads.log import (
    AgentFinishedEvent,
    AgentSpawnedEvent,
    Cost,
    ModelRequestEvent,
    ParseError,
    ThreadId,
    ThreadStartedEvent,
)
from threads.reduce.projections import TreePart, cost, merge_tree
from threads.result import Err, Ok
from threads.store import VerifiedLog
from threads.thread.read import read_log


async def tree_cost(
    store: Store, root: ThreadId, log: VerifiedLog
) -> Ok[Cost | None] | Err[ParseError]:
    """Every thread of the tree, depth first in spawn order, with an explicit stack so a tree of
    any depth is walked. Each child must name, as its parent, the agent_spawned that started it;
    a child that doesn't, or a thread named twice (a cycle, or two spawns of one id), makes the
    tree log_corrupt. A child with no log counts as an unpriced thread that ran: nothing proves
    it spent nothing (its log may have been deleted), so the total is incomplete and unbounded,
    never falsely complete."""
    parts: list[TreePart] = []
    seen = {root}
    stack = [(log, "")]
    while stack:
        at, path = stack.pop()
        own = cost(at.fold)
        if isinstance(own, Err):
            return _within(path, own)
        parts.append(
            TreePart(own.value, any(isinstance(e, ModelRequestEvent) for e in at.fold.events))
        )
        children = await _children(store, at, path, seen, parts)
        if isinstance(children, Err):
            return children
        stack.extend(reversed(children.value))
    return merge_tree(parts)


async def _children(
    store: Store, at: VerifiedLog, path: str, seen: set[ThreadId], parts: list[TreePart]
) -> Ok[list[tuple[VerifiedLog, str]]] | Err[ParseError]:
    """The logs of `at`'s spawned children to walk next, in spawn order; a child with no log
    adds its unpriced part to `parts` instead."""
    events = at.fold.events
    finished = {e.data.child_thread_id: e for e in events if isinstance(e, AgentFinishedEvent)}
    found: list[tuple[VerifiedLog, str]] = []
    for spawn in (e for e in events if isinstance(e, AgentSpawnedEvent)):
        child = spawn.data.child_thread_id
        where = f"{path}child {child}: "
        if child in seen:
            why = f"{where}thread {child} appears twice in the tree"
            return Err(ParseError("log_corrupt", why))
        seen.add(child)
        read = await _child_log(store, spawn, finished.get(child))
        if isinstance(read, Err):
            return _within(where, read)
        if read.value is None:
            parts.append(TreePart(None, ran=True))
        else:
            found.append((read.value, where))
    return Ok(found)


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
