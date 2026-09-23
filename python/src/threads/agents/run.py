"""One run of an agent, start to result: take the branch lease, recover
whatever a crash left, record the input, drive the loop, and read the result off the log."""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from functools import partial
from typing import TypedDict

from threads.adapters.loop_resources import holding
from threads.agents import narrowing
from threads.agents.bindings import AppTool, AppTools, Fence, ToolServer, capped
from threads.agents.builtins import Routed, sandbox_tools, snapshot_turn_end
from threads.agents.catalog import gateways
from threads.agents.config import ConfigError
from threads.agents.context import RunContext
from threads.agents.definition import Definition
from threads.agents.framework import Agents
from threads.agents.intake import Intake
from threads.agents.launch import Launch
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
from threads.agents.scope import Execute, Scope
from threads.agents.setup import redacted_error, set_up
from threads.agents.skills import SkillLoader
from threads.agents.start import (
    Recorded,
    handed_off,
    launched,
    prepare,
    record_input,
    wants_input,
)
from threads.agents.store import LIVE, Store, now_ms, open_store, sqlite
from threads.agents.stubbed import stub_mode
from threads.agents.tool import invalid
from threads.hooks.extension import bind, extension_tools
from threads.hooks.observers import ObserverPump
from threads.log import (
    BranchId,
    Budget,
    InputPart,
    ParseError,
    Permissions,
    Principal,
    ThreadId,
    ThreadStartedEvent,
)
from threads.loop import gates
from threads.loop.drafts import draft
from threads.loop.drive import drive
from threads.loop.runtime import Halt, Idle, Parked, RunErrorCode, Runtime, serving
from threads.loop.stubs import Stub
from threads.memory.authority import with_memory_write
from threads.memory.setup import Providers, RunBinding, provider_tools
from threads.result import Err, Ok
from threads.sandbox.protocol import Sandbox
from threads.store import Draft, SqliteStore, StoredEvent, Writer
from threads.store.lines import uuid7
from threads.thread.control import LOCAL_OPERATOR
from threads.tools import ReadResults, SandboxTools
from threads.tools.specs import SKILL

RENEW_EVERY_S = 10.0
"""Lease renewal interval, a third of the TTL."""

type Input = str | Sequence[InputPart]
type Emit = Callable[[StreamEvent], None]


class _RunOptions(TypedDict, total=False):
    thread: Thread
    store: Store
    budget: Budget
    principal: Principal
    ceiling: Permissions
    """The principal and host ceiling: this run, its subagents and every handoff target it
    starts are also decided under it."""


class RunOptions[D](_RunOptions, total=False):
    """`Agent.run` options. `deps` may be omitted when the agent's tools take none."""

    deps: D


class RunOptionsWithDeps[D](_RunOptions):
    """`Agent.run` options for an agent whose tools read deps: `deps` is required."""

    deps: D


@dataclass(frozen=True, slots=True)
class _Stream:
    emit: Emit
    pump: ObserverPump

    def observe(self, events: Sequence[StoredEvent]) -> None:
        for event in events:
            self.emit(EventItem(event))
        self.pump.poke()

    async def wait_until(self, when: int) -> None:
        self.emit(StatusItem(when))
        await asyncio.sleep(max(0, when - now_ms()) / 1000)


async def execute[D](  # noqa: PLR0913, PLR0917 - the run, plus how it was launched
    definition: Definition[D],
    input: Input | None,
    options: RunOptions[D],
    deps: D,
    emit: Emit,
    launch: Launch | None = None,
    intake: Intake | None = None,
) -> RunResult[str]:
    # The run holds its loop's adapter connections; the last holder on a loop closes them.
    async with holding():
        return await _execute(definition, input, options, deps, emit, launch, intake)


async def _execute[D](  # noqa: PLR0913, PLR0917 - execute's arguments
    definition: Definition[D],
    input: Input | None,
    options: RunOptions[D],
    deps: D,
    emit: Emit,
    launch: Launch | None,
    intake: Intake | None,
) -> RunResult[str]:
    await _set_up(definition, options.get("budget"))
    thread = options.get("thread")
    store = options.get("store") or (thread.store if thread is not None else sqlite(".threads"))
    sq = await open_store(store)
    # Each run is its own executor: a second run on a busy branch is branch_busy.
    holder = uuid.uuid4().hex
    opened = await (_open(sq, thread, holder) if launch is None else launched(sq, launch, holder))
    if isinstance(opened, Err):
        handle = thread or Thread(ThreadId(uuid7(now_ms())), BranchId(uuid7(now_ms())), store)
        return Failed(RunError(_refusal(opened.error), opened.error.message), handle)
    writer, fresh = opened.value
    if intake is not None and intake.servers:
        definition = _serving(definition, intake.servers)
    async with _held(writer), AsyncExitStack() as servers:
        definition = await with_servers(definition, servers, fenced(writer))
        thread_id = writer.fold.thread_id
        if thread_id is None:
            raise AssertionError("an acquired branch has a thread")
        sandbox = definition.sandbox or (None if thread is None else thread.sandbox)
        handle = Thread(thread_id, writer.branch_id, store, sandbox=sandbox)
        principal = options.get("principal", LOCAL_OPERATOR) if launch is None else launch.principal
        ctx = RunContext(deps, handle.id, handle.branch, principal)
        observers = {e.name: e.on for e in definition.extensions}
        pump = ObserverPump(sq.cursors, writer.branch_id, lambda: writer.fold.events, observers)
        pump.poke()
        stream = _Stream(emit, pump)
        box = definition.sandbox
        shared = None if launch is None else launch.shared
        builtins = shared or (None if box is None else _sandbox_tools(sq, box, writer, definition))
        results = ReadResults(sq, lambda: writer.fold.events)
        providers = Providers(definition.memory, definition.knowledge)
        lent = RunBinding(now_ms, fenced(writer), lambda: writer.fold.events)
        provided = await provider_tools(sq, providers, definition.name, principal, lent)
        hook_ctx = RunContext(None, handle.id, handle.branch, principal)
        ext = AppTools(extension_tools(definition.extensions), hook_ctx)
        routes = gateways(definition.catalog, builtins, sq, fenced(writer))
        if definition.skills:
            routes[SKILL] = SkillLoader(definition.skills)
        app = AppTools(definition.tools, ctx)
        tools = stub_mode(
            Routed(builtins, results, app, provided, ext, gateways=routes),
            _stubs(thread, launch),
            definition.model.info,
        )
        frame = Scope(
            definition,
            principal,
            store,
            sq,
            _child_runner(store, _stubs(thread, launch)),
            _ceilings(options, launch),
            builtins,
            None if launch is None else launch.team,
        )
        agents = Agents(frame)
        rt = Runtime(
            sq,
            writer,
            serving(definition.model, *definition.fallback),
            tools,
            with_memory_write(capped(frame.ceilings), definition.memory_write),
            now_ms,
            stream.wait_until,
            # A final_output candidate is checked against the output model, strictly.
            None if definition.output is None else partial(invalid, definition.output),
            observe=stream.observe,
            read_file=None if builtins is None else builtins.read_file,
            hooks=bind(definition.extensions, hook_ctx),
            budgets=() if launch is None else launch.budgets,
            framework=agents,
            concurrent=definition.concurrent_tools(),
        )
        moved = rt.fold.handed_off
        recorded = Recorded(input, principal, options.get("budget"), launch, intake)
        halt = await _turn(rt, definition, recorded, fresh=fresh)
        halt = await agents.finish(rt, halt)
        if isinstance(halt, Idle) and box is not None and builtins is not None and shared is None:
            revision = None if provided is None else provided.knowledge_revision
            await snapshot_turn_end(sq, writer, box, builtins, now_ms, knowledge_revision=revision)
        await gates.observe(rt, "session_end")
        if rt.fold.handed_off:
            return await handed_off(frame, rt, handle, again=moved)
        return result(rt, halt, handle)


async def _set_up[D](definition: Definition[D], budget: Budget | None) -> None:
    """Setup, before the store is touched: a failure raises ConfigError and appends nothing."""
    narrowing.run_budget(definition, budget)
    await set_up(definition)


async def _turn[D](
    rt: Runtime, definition: Definition[D], recorded: Recorded, *, fresh: bool
) -> Halt:
    """Pins or recovers the thread, records the input unless the branch can't take one, and
    drives the loop to its halt; a host intake's after work runs at an idle or parked halt.
    Without an input the run only continues what the log holds (a host resuming a thread)."""
    launch, intake = recorded.launch, recorded.intake
    after = None if intake is None else intake.after
    halt = await prepare(rt, definition, fresh=fresh, launch=launch)
    takes = recorded.input is not None and not rt.fold.handed_off and wants_input(rt, launch)
    if halt is None and takes:
        halt = await record_input(rt, recorded)
    halt = halt or await drive(rt)
    if after is not None and isinstance(halt, Idle | Parked):
        halt = await after(rt) or halt
    return halt


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
    LIVE[writer.branch_id] = writer
    try:
        yield
    finally:
        LIVE.pop(writer.branch_id, None)
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
    if isinstance(acquired, Err):
        return acquired
    # A branch the host created for a new thread has no pin yet: this run starts it.
    events = acquired.value.fold.events
    return Ok((acquired.value, not any(isinstance(e, ThreadStartedEvent) for e in events)))


def _sandbox_tools[D](
    sq: SqliteStore, box: Sandbox, writer: Writer, definition: Definition[D]
) -> SandboxTools:
    return sandbox_tools(sq, box, writer, now_ms, definition.catalog.servers())


def fenced(writer: Writer) -> Fence:
    """Whether this run still owns its branch, for a tool or provider transport's send point."""

    async def fence() -> bool:
        return isinstance(await writer.fence(), Ok)

    return fence


@asynccontextmanager
async def _closed_quietly[T](session: AbstractAsyncContextManager[T]) -> AsyncGenerator[T]:
    """`session`, closed best effort: a failed close never replaces the error, or the result, a
    run or check ends with (and its message could hold a secret)."""
    stack = AsyncExitStack()
    entered = await stack.enter_async_context(session)
    try:
        yield entered
    finally:
        with contextlib.suppress(Exception):
            await stack.aclose()


async def with_servers[D](
    definition: Definition[D], stack: AsyncExitStack, fence: Fence
) -> Definition[D]:
    """The definition with its tool servers' tools, on sessions `stack` closes: app tools in
    declared order, then server tools sorted by name. A run fences them by its writer; check()
    lists them outside any branch. A server tool named like another tool is duplicate_name."""
    if not definition.servers:
        return definition

    found: list[AppTool[object]] = []
    for server in definition.servers:
        try:
            found.extend(await stack.enter_async_context(_closed_quietly(server.connect(fence))))
        except Exception as error:
            # check() returns it and a run raises it: redacted, whatever it was (C5).
            raise redacted_error(error, "mcp_unreachable", f"MCP server {server.name}") from None
    extra = sorted(found, key=lambda t: t.name)
    connected = replace(definition, tools=(*definition.tools, *extra))
    names = [s.name for s in connected.specs()]
    if len(set(names)) != len(names):
        raise ConfigError("duplicate_name", f"tool names repeat: {names}")
    return connected


async def pinned_start[D](definition: Definition[D], store: Store) -> Draft:
    """The thread_started a new thread of this agent opens with, its config durable first: what a
    host appends itself when it creates the thread (a schedule's). Its tool servers are connected
    only to list their tools, which dispatches nothing, so no writer fences them."""
    async with AsyncExitStack() as stack:
        started, config = (await with_servers(definition, stack, outside_any_branch)).pin()
    await (await open_store(store)).put_artifact(config)
    return draft("thread_started", started)


async def outside_any_branch() -> bool:
    """The fence for listing tools on no branch (check(), a host pinning a new thread): there is
    no lease to lose, and listing dispatches nothing."""
    return True


def _ceilings[D](options: RunOptions[D], launch: Launch | None) -> tuple[Permissions, ...]:
    """A launched thread's ceilings come with its launch; a run's own is its option."""
    if launch is not None:
        return launch.ceilings
    ceiling = options.get("ceiling")
    return () if ceiling is None else (ceiling,)


def _refusal(error: ParseError) -> RunErrorCode:
    return "branch_busy" if error.code in ("branch_busy", "stale_epoch") else "branch_not_runnable"


def _stubs(thread: Thread | None, launch: Launch | None) -> tuple[Stub, ...] | None:
    """Stub mode comes with the thread handle, or with the launch of a stub run's child."""
    if thread is not None:
        return thread.stubs
    return None if launch is None else launch.stubs


def _child_runner(store: Store, stubs: tuple[Stub, ...] | None) -> Execute:
    """How this run starts a launched thread: the same pipeline, in the same store, in the same
    mode (a stub run's children never go live either)."""

    async def run(definition: Definition[None], text: str, launch: Launch) -> RunResult[str]:
        how = replace(launch, stubs=stubs)
        return await execute(definition, text, {"store": store}, None, _drop, how)

    return run


def _serving[T](definition: Definition[T], servers: tuple[ToolServer, ...]) -> Definition[T]:
    """A host's servers (a channel's send tool) go with the conversation: a handoff target
    pins them too, so the host can run it on when the conversation moves there."""
    handoffs = tuple(_serving(h, servers) for h in definition.handoffs)
    return replace(definition, servers=(*definition.servers, *servers), handoffs=handoffs)


def _drop(_item: StreamEvent) -> None:
    pass
