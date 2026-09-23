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
    parts: list[TreePart] = []
    walked = await _visit(store, root, log, parts, set())
    return walked if isinstance(walked, Err) else merge_tree(parts)


async def _visit(
    store: Store, thread: ThreadId, log: VerifiedLog, parts: list[TreePart], seen: set[ThreadId]
) -> Ok[None] | Err[ParseError]:
    """Appends `thread` and its descendants, depth first in spawn order. Each child must name,
    as its parent, the agent_spawned that started it; a child that doesn't, or a thread met twice
    (a cycle), makes the tree log_corrupt."""
    if thread in seen:
        return Err(ParseError("log_corrupt", f"thread {thread} appears twice in the tree"))
    seen.add(thread)
    own = cost(log.fold)
    if isinstance(own, Err):
        return own
    events = log.fold.events
    parts.append(TreePart(own.value, any(isinstance(e, ModelRequestEvent) for e in events)))
    finished = {e.data.child_thread_id for e in events if isinstance(e, AgentFinishedEvent)}
    for spawn in (e for e in events if isinstance(e, AgentSpawnedEvent)):
        child = spawn.data.child_thread_id
        read = await _child_log(store, spawn, finished=child in finished)
        below = (
            await _visit(store, child, read.value, parts, seen)
            if isinstance(read, Ok) and read.value is not None
            else read
        )
        if isinstance(below, Err):
            e = below.error
            return Err(ParseError(e.code, f"child {child}: {e.message}", e.seq))
    return Ok(None)


async def _child_log(
    store: Store, spawn: AgentSpawnedEvent, *, finished: bool
) -> Ok[VerifiedLog | None] | Err[ParseError]:
    """The spawned child's log; None when it has no thread and its parent never recorded it
    finishing, so it never started. A finished child's missing log is log_corrupt: counting
    nothing for it would be a partial sum."""
    root = await (await open_store(store)).root(spawn.data.child_thread_id)
    if isinstance(root, Err) and finished:
        why = "its log is missing, though its parent recorded agent_finished for it"
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
