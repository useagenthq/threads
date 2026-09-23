"""One run of an agent, start to result: take the branch lease, recover
whatever a crash left, record the input, drive the loop, and read the result off the log."""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final, TypedDict

from threads.agents.bindings import AppTools, authorize
from threads.agents.builtins import Routed, sandbox_tools, snapshot_turn_end
from threads.agents.config import ConfigError
from threads.agents.context import RunContext
from threads.agents.definition import Definition
from threads.agents.outcome import result
from threads.agents.results import (
    EventItem,
    Failed,
    RunError,
    RunResult,
    StatusItem,
    StreamEvent,
    Thread,
)
from threads.agents.store import Store, now_ms, open_store, sqlite
from threads.hooks.extension import bind
from threads.log import (
    BranchId,
    Budget,
    InputPart,
    ParseError,
    Principal,
    ThreadId,
    ThreadStartedEvent,
)
from threads.loop import gates
from threads.loop.drafts import draft
from threads.loop.drive import drive
from threads.loop.recovery import recover
from threads.loop.runtime import Halt, Idle, RunErrorCode, Runtime, lost
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.store import SqliteStore, StoredEvent, Writer
from threads.store.lines import uuid7
from threads.tools import ReadResults

if TYPE_CHECKING:
    from pydantic import JsonValue


LOCAL_OPERATOR: Final = Principal(issuer="api", tenant="local", subject="operator")
"""The default principal of a local run."""
RENEW_EVERY_S = 10.0
"""Lease renewal interval, a third of the TTL."""

type Input = str | Sequence[InputPart]
type Emit = Callable[[StreamEvent], None]


class RunOptions[D](TypedDict, total=False):
    thread: Thread
    store: Store
    deps: D
    budget: Budget
    principal: Principal


@dataclass(frozen=True, slots=True)
class _Stream:
    emit: Emit

    def observe(self, events: Sequence[StoredEvent]) -> None:
        for event in events:
            self.emit(EventItem(event))

    async def wait_until(self, when: int) -> None:
        self.emit(StatusItem(when))
        await asyncio.sleep(max(0, when - now_ms()) / 1000)


async def execute[D](
    definition: Definition[D], input: Input, options: RunOptions[D], deps: D, emit: Emit
) -> RunResult[str]:
    thread = options.get("thread")
    store = options.get("store") or (thread.store if thread is not None else sqlite(".threads"))
    sq = await open_store(store)
    # Each run is its own executor: a second run on a busy branch is branch_busy.
    opened = await _open(sq, thread, uuid.uuid4().hex)
    if isinstance(opened, Err):
        handle = thread or Thread(ThreadId(uuid7(now_ms())), BranchId(uuid7(now_ms())), store)
        return Failed(RunError(_refusal(opened.error), opened.error.message), handle)
    writer, fresh = opened.value
    async with _held(writer):
        thread_id = writer.fold.thread_id
        if thread_id is None:
            raise AssertionError("an acquired branch has a thread")
        sandbox = definition.sandbox or (None if thread is None else thread.sandbox)
        handle = Thread(thread_id, writer.branch_id, store, sandbox=sandbox)
        principal = options.get("principal", LOCAL_OPERATOR)
        ctx = RunContext(deps, handle.id, handle.branch, principal)
        stream = _Stream(emit)
        box = definition.sandbox
        builtins = None if box is None else sandbox_tools(sq, box, writer, now_ms)
        results = ReadResults(sq, lambda: writer.fold.events)
        tools = Routed(builtins, results, AppTools(definition.tools, ctx))
        rt = Runtime(
            sq,
            writer,
            definition.model,
            tools,
            authorize,
            now_ms,
            stream.wait_until,
            observe=stream.observe,
            hooks=bind(
                definition.extensions, RunContext(None, handle.id, handle.branch, principal)
            ),
        )
        halt = await _prepare(rt, definition, fresh=fresh)
        if halt is None:
            halt = await _input(rt, input, principal, options.get("budget"))
        halt = halt or await drive(rt)
        if isinstance(halt, Idle) and box is not None and builtins is not None:
            await snapshot_turn_end(sq, writer, box, builtins, now_ms)
        await gates.observe(rt, "session_end")
        return result(rt, halt, handle)


@asynccontextmanager
async def _held(writer: Writer) -> AsyncGenerator[None]:
    """Renews the lease while the run is in flight, so slow model and tool calls keep it, then
    hands it back so the next run starts at once. A failed renewal poisons the writer, which
    fences every later dispatch and append."""

    async def beat() -> None:
        while True:
            await asyncio.sleep(RENEW_EVERY_S)
            if isinstance(await writer.renew(), Err):
                return

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await writer.release()


async def _open(
    sq: SqliteStore, thread: Thread | None, holder: str
) -> Ok[tuple[Writer, bool]] | Err[ParseError]:
    """A new thread's root branch, or the given thread's branch; a torn import is handed over
    with its log_repaired (the store's first append for it)."""
    if thread is None:
        now = now_ms()
        thread_id, branch_id = ThreadId(uuid7(now)), BranchId(uuid7(now))
        created = await sq.create(thread_id, branch_id, now)
        if isinstance(created, Err):
            return created
        acquired = await sq.acquire(branch_id, holder, now_ms)
        return acquired if isinstance(acquired, Err) else Ok((acquired.value, True))
    acquired = await sq.acquire(thread.branch, holder, now_ms)
    if isinstance(acquired, Err) and acquired.error.code == "branch_not_runnable":
        acquired = await sq.repair_torn(thread.branch, holder, now_ms)
    return acquired if isinstance(acquired, Err) else Ok((acquired.value, False))


def _refusal(error: ParseError) -> RunErrorCode:
    return "branch_busy" if error.code in ("branch_busy", "stale_epoch") else "branch_not_runnable"


async def _prepare[D](rt: Runtime, definition: Definition[D], *, fresh: bool) -> Halt | None:
    """A new thread pins its config; a continued one first recovers and finishes an open turn."""
    started, config = definition.pin()
    if fresh:
        # The resolved config, hooks included, is durable in the content-addressed store under
        # its config_hash before the pin that names it.
        await rt.store.put_artifact(config)
        done = await rt.append(draft("thread_started", started))
        return (
            lost(done.error) if isinstance(done, Err) else await gates.session_start(rt, "startup")
        )
    _check_pin(rt, started["config_hash"])
    halt = await recover(rt)
    if halt is None and rt.fold.in_turn:
        halt = await drive(rt)
    # A turn that ended is out of the way; a park or a failure is this run's result.
    if halt is None or isinstance(halt, Idle):
        return await gates.session_start(rt, "resume")
    return halt


def _check_pin(rt: Runtime, config_hash: object) -> None:
    """A pin never changes in place: continuing a thread needs the config it started with, checked
    before recovery can dispatch anything."""
    pinned = next((e for e in rt.events if isinstance(e, ThreadStartedEvent)), None)
    if pinned is not None and pinned.data.config_hash != config_hash:
        raise ConfigError(
            "invalid_config",
            "this thread was started with another config; a config change starts a new thread",
        )


async def _input(
    rt: Runtime, input: Input, principal: Principal, budget: Budget | None
) -> Halt | None:
    data: dict[str, JsonValue] = {"source": "api"}
    if isinstance(input, str):
        data["text"] = input
    else:
        data["content"] = [to_json(part) for part in input]
    if budget is not None:
        data["budget"] = to_json(budget)
    actor: dict[str, JsonValue] = {"kind": "user", "principal": to_json(principal)}
    done = await rt.append(replace(draft("user_input", data), actor=actor))
    return lost(done.error) if isinstance(done, Err) else None
