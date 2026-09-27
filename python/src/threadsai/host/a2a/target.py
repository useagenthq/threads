"""Where an exposed run goes: the thread its context derives, created on first use. The lookup and
the create are one store transaction (`root_or_create`), so two hosts racing on one context make one
thread rather than two."""

from threadsai.agents.store import now_ms, open_store
from threadsai.host.runs import Bound, Runner
from threadsai.log import BranchId, ParseError, ThreadId
from threadsai.result import Err, Ok
from threadsai.store.lines import uuid7
from threadsai.thread.handle import Thread


async def context_branch(
    runner: Runner, agent: str, tenant: str, thread_id: ThreadId
) -> Ok[tuple[Thread, Bound]] | Err[ParseError]:
    """The context's branch and the binding its run uses, or why this context cannot run here."""
    store = runner.store(tenant)
    now = now_ms()
    branch = await (await open_store(store)).root_or_create(thread_id, BranchId(uuid7(now)), now)
    # The authenticated caller answers this run's questions: ask_user is pinned.
    wanted = runner.bound_to(agent, answerer=True)
    bound = await runner.bound(store, thread_id)
    plain = runner.bound_to(agent).definition
    if bound is not None and all(bound.definition is not d for d in (wanted.definition, plain)):
        why = f"this context's thread does not run agent {agent}"
        return Err(ParseError("invalid_request", why))
    return Ok((Thread(thread_id, branch, store), wanted if bound is None else bound))
