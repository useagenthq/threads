"""`Host.subscribe` (GET /v1/threads/{thread_id}/runs/{run_id}/events):
one run's committed events, read from the log and bounded to that run, then one result message
naming the run.

A subscription takes no input and starts nothing: it reads the log, and a run of this host
wakes it on each append. It resumes after `after_seq` (the SSE Last-Event-ID).
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass

from pydantic import JsonValue

from threads.agents.store import Store, now_ms, open_store
from threads.host.outcome import logged, outcome, run_events, run_start
from threads.host.runs import Runner
from threads.log import BranchId, EventId, ParseError, Principal, ThreadId
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.store import VerifiedLog
from threads.thread.handle import Thread


@dataclass(frozen=True, slots=True)
class Message:
    """One SSE message: `data` is a host-api SseMessage; committed events carry their seq."""

    data: JsonValue
    id: int | None = None


async def subscribe(
    runner: Runner, thread_id: ThreadId, run_id: EventId, principal: Principal, after_seq: int
) -> Ok[AsyncIterator[Message]] | Err[ParseError]:
    """The run's stream; not_found when no branch of this tenant's thread holds the run."""
    store = runner.store(principal.tenant)
    sq = await open_store(store)
    for row in await sq.tables.branches(thread_id):
        read = await sq.read(row.branch_id, now_ms())
        if isinstance(read, Ok) and run_start(read.value.fold.events, run_id) is not None:
            thread = Thread(thread_id, row.branch_id, store)
            return Ok(_follow(runner, thread, run_id, after_seq))
    return Err(ParseError("not_found", f"no run {run_id} on thread {thread_id}"))


async def _follow(
    runner: Runner, thread: Thread, run_id: EventId, after_seq: int
) -> AsyncIterator[Message]:
    seen = after_seq
    while True:
        read = await _read(thread.store, thread.branch)
        if read is None:
            return
        events = read.fold.events
        start = run_start(events, run_id)
        if start is None:
            return
        for event in run_events(events, start):
            if event.seq > seen:
                seen = event.seq
                yield Message({"kind": "event", "event": to_json(event)}, event.seq)
        end = logged(events, start, read.fold.parked, thread) or _unlogged(runner, thread.branch)
        if end is not None:
            yield Message({"kind": "result", "run_id": run_id, "result": end})
            return
        await runner.wait(thread.branch)


def _unlogged(runner: Runner, branch: BranchId) -> JsonValue | None:
    """A run of this host that ended with nothing in the log to say so (refused before its
    first append): its returned result."""
    if runner.running(branch):
        return None
    last = runner.last.get(branch)
    return None if last is None else outcome(last)


async def _read(store: Store, branch: BranchId) -> VerifiedLog | None:
    read = await (await open_store(store)).read(branch, now_ms())
    return read.value if isinstance(read, Ok) else None
