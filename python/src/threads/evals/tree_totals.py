"""What a live thread and its subagents spent, read off the log (spec lane 32, B.5). Counting from
a baseline seq is what makes a continued prefix free: the imported real turns are below it, so only
what this conversation appended is charged to its budget."""

from dataclasses import dataclass

from threads.agents.store import Store, open_store
from threads.log import (
    AgentSpawnedEvent,
    BranchId,
    Event,
    ModelRequestEvent,
    ModelResponseEvent,
    TurnCompletedEvent,
    UnknownEvent,
)
from threads.result import Ok
from threads.thread.read import read_log


@dataclass(frozen=True, slots=True)
class Totals:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0


def _add_totals(a: Totals, b: Totals) -> Totals:
    return Totals(
        a.requests + b.requests,
        a.input_tokens + b.input_tokens,
        a.output_tokens + b.output_tokens,
        a.turns + b.turns,
    )


def _known(value: object) -> int:
    """A token count the provider reported; an unknown one is never summed as zero."""
    return value if isinstance(value, int) else 0


async def _events(store: Store, branch: BranchId) -> tuple[Event, ...]:
    read = await read_log(store, branch)
    if not isinstance(read, Ok):
        return ()
    return tuple(e for e in read.value.fold.events if not isinstance(e, UnknownEvent))


async def tree_totals(store: Store, branch: BranchId, since_seq: int = 0) -> Totals:
    """model_requests, known token counts and completed turns of a thread and its subagents."""
    events = [e for e in await _events(store, branch) if e.seq > since_seq]
    totals = Totals(
        requests=sum(1 for e in events if isinstance(e, ModelRequestEvent)),
        turns=sum(1 for e in events if isinstance(e, TurnCompletedEvent)),
    )
    sq = await open_store(store)
    for e in events:
        if isinstance(e, ModelResponseEvent):
            usage = e.data.usage
            totals = _add_totals(
                totals,
                Totals(
                    input_tokens=_known(usage.input_tokens),
                    output_tokens=_known(usage.output_tokens),
                ),
            )
        if not isinstance(e, AgentSpawnedEvent):
            continue
        root = await sq.root(e.data.child_thread_id)
        if isinstance(root, Ok):
            totals = _add_totals(totals, await tree_totals(store, root.value))
    return totals
