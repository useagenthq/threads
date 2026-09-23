"""Approval authority (spec/schema/README.md, "Approval authority"): who may
answer a thread's challenges and settle its parked effects.

A host handle carries `Checked`: decisions are taken under the approver set of the root run (the
top of the thread's tree through subagent and handoff parents). Configured approvers are the only
ones; unconfigured, the root run's originating principal (the root thread's latest user_input
principal) is. The in-process `Thread` API has operator authority: no `Checked`, and it records
the principal it is given.
"""

from dataclasses import dataclass

from threads.agents.store import Store, open_store
from threads.log import ParseError, Principal, ThreadId
from threads.result import Err, Ok
from threads.thread.control import forbidden, requester
from threads.thread.tree import root_of

NOT_AN_APPROVER = "not an approver of this thread"


@dataclass(frozen=True, slots=True)
class Checked:
    """A host handle's authority: the root agent's configured approvers, or None."""

    approvers: tuple[Principal, ...] | None


async def refused(
    store: Store, thread_id: ThreadId, principal: Principal, authority: Checked | None
) -> Err[ParseError] | None:
    """forbidden unless `principal` may decide on this thread; None when it may."""
    if principal.tenant != store.tenant:
        return forbidden(NOT_AN_APPROVER)
    if authority is None:
        return None
    if authority.approvers is not None:
        return None if principal in authority.approvers else forbidden(NOT_AN_APPROVER)
    if await _originator(store, thread_id) != principal:
        return forbidden(NOT_AN_APPROVER)
    return None


async def _originator(store: Store, thread_id: ThreadId) -> Principal | None:
    root = await root_of(store, thread_id, through=("subagent", "handoff"))
    if root is None:
        return None
    read = await (await open_store(store)).read(root[1], 0)
    return requester(read.value.fold.events) if isinstance(read, Ok) else None
