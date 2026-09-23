"""Thread.cost(tree=True): this thread's cost plus every descendant's, each read from its own
main branch, so the logs stay the only truth (the budget ledger is a cache, never reused here)."""

from threads._generated.host_api_v1 import Cost
from threads.agents.store import Store, open_store
from threads.log import ParseError
from threads.reduce.projections import cost, merge_cost
from threads.result import Err, Ok
from threads.store import VerifiedLog
from threads.thread.read import read_log


async def tree_cost(store: Store, log: VerifiedLog) -> Ok[Cost | None] | Err[ParseError]:
    total = cost(log.fold)
    if total is None:
        return Ok(None)
    sq = await open_store(store)
    for child in log.fold.children:
        root = await sq.root(child)
        # A child with no thread yet was never started: it spent nothing.
        if isinstance(root, Err):
            continue
        read = await read_log(store, root.value)
        part = await tree_cost(store, read.value) if isinstance(read, Ok) else read
        if isinstance(part, Err):
            e = part.error
            return Err(ParseError(e.code, f"child {child}: {e.message}", e.seq))
        total = merge_cost(total, part.value)
    return Ok(total)
