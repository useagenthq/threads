"""Thread.cost(tree=True): this thread's cost plus every descendant's at any depth, each read from
its own main branch, so the logs stay the only truth (the budget ledger is a cache, never reused
here)."""

from functools import reduce

from threads._generated.host_api_v1 import Cost
from threads.agents.store import Store, open_store
from threads.log import ParseError, ThreadId
from threads.reduce.projections import cost, merge_cost
from threads.result import Err, Ok
from threads.store import VerifiedLog
from threads.thread.read import read_log


async def tree_cost(store: Store, log: VerifiedLog) -> Ok[Cost | None] | Err[ParseError]:
    parts = await _descendant_costs(store, log)
    if isinstance(parts, Err):
        return parts
    own = cost(log.fold)
    return Ok(None if own is None else reduce(merge_cost, parts.value, own))


async def _descendant_costs(
    store: Store, log: VerifiedLog
) -> Ok[tuple[Cost | None, ...]] | Err[ParseError]:
    """Each descendant's own cost (None when unpriced), depth first. An unpriced descendant
    doesn't stop the walk, and any unreadable one is the error."""
    sq = await open_store(store)
    out: list[Cost | None] = []
    for child in log.fold.children:
        root = await sq.root(child)
        # A child with no thread yet was never started: it spent nothing.
        if isinstance(root, Err):
            continue
        read = await read_log(store, root.value)
        if isinstance(read, Err):
            return Err(_in_child(child, read.error))
        below = await _descendant_costs(store, read.value)
        if isinstance(below, Err):
            return Err(_in_child(child, below.error))
        out += [cost(read.value.fold), *below.value]
    return Ok(tuple(out))


def _in_child(child: ThreadId, error: ParseError) -> ParseError:
    """The error, keeping its code, with the path to the descendant that failed."""
    return ParseError(error.code, f"child {child}: {error.message}", error.seq)
