"""Who may answer a thread's approval challenges and settle its parked effects (), checked against the policy of the agent at the root of the thread's tree.

Configured approvers are the only ones. Unconfigured (None), the principal whose input opened
the root run's current turn may approve its own calls, and so may the local operator. Settling
an effect `assume_not_done` accepts duplicate risk, so it needs a configured approver or the
operator, never the requester alone.
"""

from threads.agents.store import Store, open_store
from threads.log import ParseError, Principal, ThreadId
from threads.result import Err, Ok
from threads.thread.control import LOCAL_OPERATOR, forbidden, requester
from threads.thread.tree import root_of

NOT_AN_APPROVER = "not an approver of this thread"


async def refused(
    store: Store,
    thread_id: ThreadId,
    principal: Principal,
    approvers: tuple[Principal, ...] | None,
    *,
    duplicate_risk: bool = False,
) -> Err[ParseError] | None:
    """forbidden unless `principal` has the authority; None when it has."""
    if principal.tenant != store.tenant:
        return forbidden(NOT_AN_APPROVER)
    if approvers is not None:
        return None if principal in approvers else forbidden(NOT_AN_APPROVER)
    if principal == LOCAL_OPERATOR:
        return None
    if duplicate_risk:
        return forbidden("accepting duplicate risk needs an approver or the operator")
    if await _originator(store, thread_id) != principal:
        return forbidden(NOT_AN_APPROVER)
    return None


async def _originator(store: Store, thread_id: ThreadId) -> Principal | None:
    root = await root_of(store, thread_id)
    if root is None:
        return None
    read = await (await open_store(store)).read(root[1], 0)
    return requester(read.value.fold.events) if isinstance(read, Ok) else None
