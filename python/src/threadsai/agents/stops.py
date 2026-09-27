"""A subagent that parks, and one run on under its parent's cancel (spec/schema/README.md, "Subagent
cancellation and parking").

A child run that ends parked records no `agent_finished`: its parent parks once on
`{kind: child, id: <child_thread_id>}` with the child's reason, and the spawn call stays pending.
Under the parent's barrier a child is barred before it runs on; one whose thread was never
created is recorded cancelled without being created.
"""

from pydantic import JsonValue

from threadsai.agents import results
from threadsai.agents.children import CANCELLED, shown
from threadsai.agents.scope import Scope
from threadsai.log import AgentSpawnedEvent, CancelRequestedEvent, ParkAddress
from threadsai.loop.drafts import draft
from threadsai.loop.results import text_ref
from threadsai.loop.runtime import Halt, Parked, Runtime, lost
from threadsai.reduce.handlers import to_json
from threadsai.result import Err
from threadsai.thread.tree import bar_child


def address(spawned: AgentSpawnedEvent) -> ParkAddress:
    return ParkAddress(kind="child", id=spawned.data.child_thread_id)


async def park(rt: Runtime, spawned: AgentSpawnedEvent, child: results.Parked) -> Halt:
    """The parent parks on the child, once, with the child's reason; the run ends parked with
    every open address."""
    at = address(spawned)
    if at not in rt.fold.parked:
        data: dict[str, JsonValue] = {"address": to_json(at), "reason": child.reason}
        done = await rt.append(draft("parked", data))
        if isinstance(done, Err):
            return lost(done.error)
    return Parked(child.reason, tuple(rt.fold.parked))


async def bar[D](scope: Scope[D], spawned: AgentSpawnedEvent, cause: CancelRequestedEvent) -> bool:
    """The child's tree barrier before it runs on; False when its thread was never created."""
    child = spawned.data.child_thread_id
    if isinstance(await scope.sq.root(child), Err):
        return False
    await bar_child(scope.store, child, cause.actor.principal)
    return True


async def never_started(
    rt: Runtime, spawned: AgentSpawnedEvent
) -> tuple[dict[str, JsonValue], str]:
    """A child cancelled before its thread existed: its terminal record, nothing created."""
    data: dict[str, JsonValue] = {
        "child_thread_id": spawned.data.child_thread_id,
        "status": "cancelled",
        "usage": {"input_tokens": None, "output_tokens": None},
        "output_ref": await text_ref(rt, CANCELLED),
    }
    return data, shown("cancelled", CANCELLED)
