"""The run slice a task is, as the log has it now. Every operation reads through here, so a GetTask,
a stream frame and a two-host race all see one reading of one log."""

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic.experimental.missing_sentinel import MISSING

from threadsai._generated.a2a_v1 import Task
from threadsai.a2a.protocol import A2aFault, fault
from threadsai.agents.store import now_ms, open_store
from threadsai.host.a2a.question import open_ask
from threadsai.host.a2a.state import Slice, task_of
from threadsai.host.outcome import logged, run_events, run_start
from threadsai.host.runs import Runner
from threadsai.log import BranchId, Event, EventId, Principal, ThreadId, UserInputEvent
from threadsai.result import Ok
from threadsai.thread.handle import Thread


@dataclass(frozen=True, slots=True)
class Located:
    """Where a task's run lives."""

    thread: ThreadId
    branch: BranchId
    run_id: EventId


@dataclass(frozen=True, slots=True)
class Accepted:
    task: Task
    at: Located | None
    """Where the task's run lives; None for a task we refused before it existed."""


async def slices_of(runner: Runner, tenant: str, at: Located) -> tuple[Slice, ...] | None:
    """Every state the run has passed through, one slice per event of its own segment, oldest first.
    Each slice's outcome and open question are computed over that prefix and no further, which is
    what makes a frame a function of the log rather than of when it was read. The last one is the
    task as it stands now.

    ponytail: the outcome is recomputed per prefix, so this is O(events**2) per read. A run is tens
    to hundreds of events; if a long one ever makes it matter, memoize by prefix length."""
    store = runner.store(tenant)
    read = await (await open_store(store)).read(at.branch, now_ms())
    if not isinstance(read, Ok):
        return None
    events = read.value.fold.events
    start = run_start(events, at.run_id)
    if start is None:
        return None
    thread = Thread(at.thread, at.branch, store)
    end = start + len(run_events(events, start))
    context = context_of(events, at.run_id) or at.thread
    return tuple(
        Slice(
            task_id=at.run_id,
            context_id=context,
            own=events[start : i + 1],
            # `logged` only fills RunOutcome.pending from the parked list, which A2A never shows, so
            # the empty one here costs nothing and saves re-folding the log at every prefix.
            outcome=logged(events[: i + 1], start, (), thread),
            question=open_ask(events[: i + 1]),
        )
        for i in range(start, end)
    )


async def slice_of(runner: Runner, tenant: str, at: Located) -> Slice | None:
    """The run slice as it stands now, or None when the branch cannot be read."""
    found = await slices_of(runner, tenant, at)
    return None if found is None else found[-1]


async def task_at(runner: Runner, tenant: str, at: Located) -> Task | None:
    """The slice as a Task, or None when the branch cannot be read."""
    found = await slice_of(runner, tenant, at)
    return None if found is None else task_of(found)


async def located(runner: Runner, principal: Principal, at: Located) -> Accepted | A2aFault:
    """The task as the log now shows it: what every operation answers with."""
    task = await task_at(runner, principal.tenant, at)
    if task is None:
        return fault("InternalError", f"run {at.run_id} could not be read back")
    return Accepted(task, at)


def context_of(events: Sequence[Event], run_id: EventId) -> str | None:
    """The contextId the run was started with, read back from its own user_input. That is how a
    retry and every later GetTask answer the same contextId without a column for it."""
    input_event = next(
        (e for e in events if isinstance(e, UserInputEvent) and e.event_id == run_id), None
    )
    if input_event is None or input_event.data.a2a is MISSING:
        return None
    return input_event.data.a2a.context_id


async def open_run(runner: Runner, principal: Principal, thread: ThreadId) -> bool:
    """Whether the thread's main branch has a run still going: one run at a time per context."""
    store = runner.store(principal.tenant)
    sq = await open_store(store)
    root = await sq.root(thread)
    if not isinstance(root, Ok):
        return False
    read = await sq.read(root.value, now_ms())
    if not isinstance(read, Ok):
        return False
    events = read.value.fold.events
    latest = next((e for e in reversed(events) if isinstance(e, UserInputEvent)), None)
    if latest is None:
        return False
    at = Located(thread, root.value, latest.event_id)
    found = await slice_of(runner, principal.tenant, at)
    state = None if found is None else task_of(found).status.state
    return state in ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING")
