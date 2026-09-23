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
    finished = {e.data.child_thread_id: e for e in events if isinstance(e, AgentFinishedEvent)}
    for spawn in (e for e in events if isinstance(e, AgentSpawnedEvent)):
        child = spawn.data.child_thread_id
        read = await _child_log(store, spawn, finished.get(child))
        below = (
            await _visit(store, child, read.value, parts, seen)
            if isinstance(read, Ok) and read.value is not None
            else read
        )
        if isinstance(below, Err):
            e = below.error
            return Err(ParseError(e.code, f"child {child}: {e.message}", e.seq))
    return Ok(None)


def _never_created(finish: AgentFinishedEvent) -> bool:
    """spec/schema/README.md, Subagent cancellation: a child with no thread is recorded
    cancelled, with unknown usage, and never created. A started child cancelled with unknown
    usage writes the same record, so its lost log can't be told apart and counts nothing."""
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
    other finished child's missing log is log_corrupt: counting nothing for it would be a
    partial sum."""
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
