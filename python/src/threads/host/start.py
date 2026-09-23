"""`Host.start_run` (POST /v1/runs, ): the user_input is made durable together
with its idempotency receipt, and the run goes on in the host.

The key binds the tenant, the full principal, the operation and the request's hash. The same
key, principal and request replays the receipt and starts nothing; another request under the
key is idempotency_key_reused; another principal of the tenant is
idempotency_key_principal_mismatch and never sees the receipt.
"""

import asyncio
from typing import Final

from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.host_api_v1 import RunAccepted, StartRunRequest
from threads.agents.config import ConfigError
from threads.agents.intake import Intake
from threads.agents.results import Failed
from threads.agents.store import Store, now_ms, open_store
from threads.host.runs import Bound, Runner
from threads.log import BranchId, ParseError, Principal, ThreadId
from threads.log.digest import canonical_sha256
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.store import StoredEvent, receipts
from threads.store.lines import uuid7
from threads.thread.control import principal_key
from threads.thread.handle import Thread, open_thread

OPERATION: Final = "startRun"
MAX_KEY: Final = 255

type Started = Ok[RunAccepted] | Err[ParseError]


async def start_run(
    runner: Runner, request: StartRunRequest, principal: Principal, idempotency_key: str
) -> Started:
    if not 0 < len(idempotency_key) <= MAX_KEY:
        return Err(ParseError("invalid_request", "Idempotency-Key must be 1 to 255 characters"))
    if runner.agent(request.agent) is None:
        return Err(ParseError("not_found", f"no agent {request.agent} on this host"))
    store = runner.store(principal.tenant)
    body = canonical_sha256(to_json(request))
    if not isinstance(body, Ok):
        raise AssertionError("a parsed request always canonicalizes")
    key = receipts.Key(
        principal.tenant, OPERATION, idempotency_key, principal_key(principal), body.value
    )
    replayed = await _replay(store, key)
    if replayed is not None:
        return replayed
    target = await _target(runner, store, request)
    if isinstance(target, Err):
        return target
    thread, bound = target.value
    recorded: asyncio.Future[StoredEvent] = asyncio.get_running_loop().create_future()
    intake = Intake("api", recorded, companion=receipts.insert(key, now_ms()))
    budget = None if request.budget is MISSING else request.budget
    task = runner.launch(bound, request.input, thread, principal, intake=intake, budget=budget)
    await asyncio.wait({recorded, task}, return_when=asyncio.FIRST_COMPLETED)
    if recorded.done():
        run = recorded.result()
        return Ok(
            RunAccepted(thread_id=run.thread_id, branch_id=run.branch_id, run_id=run.event_id)
        )
    return await _refused(store, key, task)


async def _refused(store: Store, key: receipts.Key, task: "asyncio.Task[object]") -> Started:
    """The run recorded no input: another request took the key first, or the branch can't take
    one now (busy, parked on something else, inspection-only, pinned to another agent)."""
    try:
        result = task.result()
    except ConfigError as error:
        return Err(ParseError("invalid_request", error.message))
    replayed = await _replay(store, key)
    if replayed is not None:
        return replayed
    if isinstance(result, Failed) and result.error.code == "branch_busy":
        return Err(ParseError("branch_busy", result.error.message))
    return Err(ParseError("branch_not_runnable", "the branch takes no input now"))


async def _replay(store: Store, key: receipts.Key) -> Started | None:
    found = await (await open_store(store)).tables.receipt(key)
    if found is None:
        return None
    if found.principal_key != key.principal_key:
        why = "the key belongs to another principal"
        return Err(ParseError("idempotency_key_principal_mismatch", why))
    if found.body_hash != key.body_hash:
        return Err(ParseError("idempotency_key_reused", "the key was used for another request"))
    return Ok(
        RunAccepted(thread_id=found.thread_id, branch_id=found.branch_id, run_id=found.run_id)
    )


async def _target(
    runner: Runner, store: Store, request: StartRunRequest
) -> Ok[tuple[Thread, Bound]] | Err[ParseError]:
    """The branch the input goes to: a new thread of the named agent, or the given (or main)
    branch of an existing thread of this tenant, which must run the named agent."""
    wanted = runner.bound_to(request.agent)
    sq = await open_store(store)
    if request.thread_id is MISSING:
        now = now_ms()
        thread_id, branch_id = ThreadId(uuid7(now)), BranchId(uuid7(now))
        created = await sq.create(thread_id, branch_id, now)
        if isinstance(created, Err):
            return created
        return Ok((Thread(thread_id, branch_id, store), wanted))
    branch = None if request.branch_id is MISSING else request.branch_id
    opened = await open_thread(store, request.thread_id, branch_id=branch)
    if isinstance(opened, Err):
        code = "not_found" if opened.error.code == "not_found" else "branch_not_runnable"
        return Err(ParseError(code, opened.error.message))
    bound = await runner.bound(store, request.thread_id)
    if bound is None or bound.definition is not wanted.definition:
        return Err(ParseError("invalid_request", f"the thread does not run {request.agent}"))
    thread = opened.value
    return Ok((Thread(thread.id, thread.branch, store), bound))
