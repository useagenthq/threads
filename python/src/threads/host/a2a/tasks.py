"""GetTask, ListTasks and CancelTask. Each resolves a task only through the caller's own receipts,
so another principal's task is TaskNotFoundError and is indistinguishable from one that never
existed. ListTasks is how a peer reconciles with us the same way we reconcile with a peer."""

import re
from collections.abc import Sequence
from typing import Final

from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.a2a_v1 import ListTasksRequest, ListTasksResponse, Task
from threads.a2a.protocol import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    A2aFault,
    fault,
    is_terminal,
)
from threads.agents.store import open_store
from threads.host.a2a.keys import a2a_thread_id
from threads.host.a2a.read import Located, located, task_at
from threads.host.runs import Runner
from threads.log import Principal
from threads.log.keys import principal_key
from threads.result import Err
from threads.store import receipts
from threads.thread.handle import Thread

_OFFSET: Final = re.compile(r"^\d+$")


async def locate(runner: Runner, principal: Principal, task_id: str) -> Located | A2aFault:
    """The task's run, found through the caller's own receipts."""
    sq = await open_store(runner.store(principal.tenant))
    found = await sq.tables.a2a_task(principal_key(principal), task_id)
    if found is None:
        return fault("TaskNotFoundError", f"no task {task_id}")
    return Located(found.thread_id, found.branch_id, found.run_id)


async def get_task(runner: Runner, principal: Principal, task_id: str) -> Task | A2aFault:
    at = await locate(runner, principal, task_id)
    if isinstance(at, A2aFault):
        return at
    task = await task_at(runner, principal.tenant, at)
    return fault("TaskNotFoundError", f"no task {task_id}") if task is None else task


async def list_tasks(
    runner: Runner, principal: Principal, name: str, request: ListTasksRequest
) -> ListTasksResponse | A2aFault:
    sq = await open_store(runner.store(principal.tenant))
    rows = _newest_per_run(await sq.tables.a2a_tasks(principal_key(principal)))
    # Filtered by deriving the context's thread id and comparing, because the derivation is one-way.
    if request.contextId is not MISSING:
        thread = a2a_thread_id(principal, name, request.contextId)
        rows = tuple(row for row in rows if row.thread_id == thread)
    size = _page_size(None if request.pageSize is MISSING else request.pageSize)
    start = _offset(None if request.pageToken is MISSING else request.pageToken)
    if isinstance(start, A2aFault):
        return start
    tasks: list[Task] = []
    for row in rows[start : start + size]:
        task = await task_at(
            runner, principal.tenant, Located(row.thread_id, row.branch_id, row.run_id)
        )
        # A row whose branch cannot be read is left out rather than failing the page.
        if task is not None:
            tasks.append(task)
    return ListTasksResponse(
        tasks=tasks,
        nextPageToken=str(start + size) if start + size < len(rows) else "",
        pageSize=size,
        totalSize=len(rows),
    )


def _newest_per_run(rows: Sequence[receipts.TaskReceipt]) -> tuple[receipts.TaskReceipt, ...]:
    """One task can hold several receipts (a send and each of its continuations), so the newest row
    per run is the task, and a continued task is the most recently touched one."""
    seen: set[str] = set()
    found: list[receipts.TaskReceipt] = []
    for row in rows:
        if row.run_id not in seen:
            seen.add(row.run_id)
            found.append(row)
    return tuple(found)


def _page_size(asked: int | None) -> int:
    if asked is None or asked <= 0:
        return DEFAULT_PAGE_SIZE
    return min(asked, MAX_PAGE_SIZE)


def _offset(token: str | None) -> int | A2aFault:
    """A page token is the offset it resumes at; anything else is a malformed cursor."""
    if token is None or token == "":
        return 0
    if _OFFSET.match(token) is None:
        return fault("InvalidParamsError", f"pageToken {token} is not a page token")
    return int(token)


async def cancel_task(runner: Runner, principal: Principal, task_id: str) -> Task | A2aFault:
    """The durable barrier on the task's thread. The answer is the task in its current state, which
    becomes CANCELED when the barrier applies: a cancel never claims to undo what happened."""
    at = await locate(runner, principal, task_id)
    if isinstance(at, A2aFault):
        return at
    current = await task_at(runner, principal.tenant, at)
    if current is None:
        return fault("TaskNotFoundError", f"no task {task_id}")
    if is_terminal(current.status.state):
        why = f"task {task_id} has already ended as {current.status.state}"
        return fault("TaskNotCancelableError", why)
    store = runner.store(principal.tenant)
    thread = Thread(at.thread, at.branch, store)
    done = await thread.cancel(principal)
    if isinstance(done, Err):
        return fault("InternalError", done.error.message)
    await runner.resume(store, at.thread, at.branch, runner.generation)
    answered = await located(runner, principal, at)
    return answered if isinstance(answered, A2aFault) else answered.task
