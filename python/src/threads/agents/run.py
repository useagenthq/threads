"""One run of an agent, start to result: take the branch lease, recover
whatever a crash left, record the input, drive the loop, and read the result off the log."""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from functools import partial
from typing import TypedDict

from threads.adapters.loop_resources import holding
from threads.agents import narrowing
from threads.agents.bindings import AppTools, capped
from threads.agents.builtins import Routed, egress_denied, sandbox_tools, snapshot_turn_end
from threads.agents.catalog import gateways
from threads.agents.config import ConfigError
from threads.agents.context import RunContext
from threads.agents.definition import Definition
from threads.agents.framework import Agents
from threads.agents.intake import Intake
from threads.agents.launch import Launch
from threads.agents.outcome import result
from threads.agents.results import (
    Failed,
    RunError,
    RunResult,
    StreamEvent,
    Thread,
)
from threads.agents.run_stream import Emit, OnDelta, RunStream
from threads.agents.scope import Execute, Scope
from threads.agents.servers import fenced, host_serving, with_servers
from threads.agents.setup import set_up
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
from threads.agents.team_run import covered, finish, notifying, team_side, unparked
from threads.agents.team_worker import MemberRun
from threads.agents.teams import team_of
from threads.agents.tool import invalid
from threads.hooks.extension import bind, extension_tools
from threads.hooks.observers import ObserverPump
from threads.log import (
    BranchId,
    Budget,
    Event,
    InputPart,
    ParseError,
    Permissions,
    Principal,
    ThreadId,
    ThreadStartedEvent,
)
from threads.loop import gates
from threads.loop.drive import drive
from threads.loop.runtime import Halt, Idle, Parked, RunErrorCode, Runtime, serving
from threads.loop.stubs import Stub
from threads.memory.authority import with_memory_write
from threads.memory.setup import Providers, RunBinding, provider_tools
from threads.result import Err, Ok
from threads.sandbox.protocol import Sandbox
from threads.store import SqliteStore, Writer
from threads.store.lines import uuid7
from threads.thread.control import LOCAL_OPERATOR
from threads.thread.frozen_stubs import frozen_stubs, stub_fork_ref
from threads.tools import ReadResults, SandboxTools
from threads.tools.specs import SKILL

RENEW_EVERY_S = 10.0
"""Lease renewal interval, a third of the TTL."""

type Input = str | Sequence[InputPart]


class _RunOptions(TypedDict, total=False):
    thread: Thread | ThreadId
    """A Thread handle continues its branch; a thread id continues its main branch in `store`."""
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


async def execute[D](  # noqa: PLR0913, PLR0917 - the run, plus how it was launched
    definition: Definition[D],
    input: Input | None,
    options: RunOptions[D],
    deps: D,
    emit: Emit,
    launch: Launch | None = None,
    intake: Intake | None = None,
    member: MemberRun | None = None,
    on_delta: OnDelta | None = None,
    stubs: tuple[Stub, ...] | None = None,
) -> RunResult[str]:
    # The run holds its loop's adapter connections; the last holder on a loop closes them.
    async with holding():
        return await _execute(
            definition, input, options, deps, emit, on_delta, launch, intake, member, stubs
        )


async def _execute[D](  # noqa: PLR0913, PLR0917 - execute's arguments
    definition: Definition[D],
    input: Input | None,
    options: RunOptions[D],
    deps: D,
    emit: Emit,
    on_delta: OnDelta | None,
    launch: Launch | None,
    intake: Intake | None,
    member: MemberRun | None,
    stubs: tuple[Stub, ...] | None = None,
) -> RunResult[str]:
    await _set_up(definition, options.get("budget"))
    store, sq, thread = await _where(options.get("store"), options.get("thread"))
    # Each run is its own executor: a second run on a busy branch is branch_busy.
    holder = uuid.uuid4().hex if member is None else member.holder
    opened = await (_open(sq, thread, holder) if launch is None else launched(sq, launch, holder))
    if isinstance(opened, Err):
        handle = thread or Thread(ThreadId(uuid7(now_ms())), BranchId(uuid7(now_ms())), store)
        return Failed(RunError(_refusal(opened.error), opened.error.message), handle)
    writer, fresh = opened.value
    if intake is not None and intake.servers:
        definition = host_serving(definition, intake.servers)
    principal = options.get("principal", LOCAL_OPERATOR) if launch is None else launch.principal
    async with _held(writer), AsyncExitStack() as servers:
        definition = await with_servers(definition, servers, fenced(writer))
        runner = member_runner(store)
        side = team_side(servers, team_of(definition, member, writer, store, sq, runner, principal))
        thread_id = writer.fold.thread_id
        if thread_id is None:
            raise AssertionError("an acquired branch has a thread")
        sandbox = definition.sandbox or (None if thread is None else thread.sandbox)
        handle = Thread(thread_id, writer.branch_id, store, sandbox=sandbox)
        ctx = RunContext(deps, handle.id, handle.branch, principal)
        observers = {e.name: e.on for e in definition.extensions}
        pump = ObserverPump(sq.cursors, writer.branch_id, lambda: writer.fold.events, observers)
        pump.poke()
        stream = RunStream(emit, pump, on_delta)
        box = definition.sandbox
        shared = None if launch is None else launch.shared
        builtins = shared or (None if box is None else _sandbox_tools(sq, box, writer, definition))
        results = ReadResults(sq, lambda: writer.fold.events)
        providers = Providers(definition.memory, definition.knowledge)
        lent = RunBinding(now_ms, fenced(writer), lambda: writer.fold.events)
        provided = await provider_tools(sq, providers, definition.name, principal, lent)
        # Hooks and extension tools get the run's deps, as app tools do.
        ext = AppTools(extension_tools(definition.extensions), ctx)
        routes = gateways(definition.catalog, builtins, sq, fenced(writer))
        if definition.skills:
            routes[SKILL] = SkillLoader(definition.skills)
        app = AppTools(definition.tools, ctx)
        frozen = await _stubs(sq, writer.fold.events, launch, stubs)
        if isinstance(frozen, Err):
            return Failed(RunError(_refusal(frozen.error), frozen.error.message), handle)
        tools = stub_mode(
            Routed(builtins, results, app, provided, ext, gateways=routes),
            frozen.value,
            definition.model.info,
            sealed=box is not None
            and box.info.egress == "enforced"
            and egress_denied(definition.egress),
        )
        frame = Scope(
            definition,
            principal,
            store,
            sq,
            _child_runner(store, frozen.value),
            _ceilings(options, launch),
            builtins,
            None if launch is None else launch.team,
        )
        agents = Agents(frame, team=side is not None)
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
            observe=stream.observe if side is None else notifying(stream.observe, side.runtime),
            delta=stream.delta,
            read_file=None if builtins is None else builtins.read_file,
            hooks=bind(definition.extensions, ctx),
            budgets=covered(launch, member),
            framework=agents,
            concurrent=definition.concurrent_tools(),
            team=None if side is None else side.runtime,
        )
        moved = rt.fold.handed_off
        recorded = Recorded(input, principal, options.get("budget"), launch, intake)
        halt = await finish(
            rt,
            agents,
            await _turn(
                rt, definition, recorded, fresh=fresh, first=partial(unparked, rt, agents, side)
            ),
            side,
        )
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
    rt: Runtime,
    definition: Definition[D],
    recorded: Recorded,
    *,
    fresh: bool,
    first: Callable[[], Awaitable[Halt | None]],
) -> Halt:
    """Pins or recovers the thread, runs `first` (a lead parked on its members waits for
    them), records the input unless the branch can't take one, and drives the loop to its
    halt; a host intake's after work runs at an idle or parked halt.
    Without an input the run only continues what the log holds (a host resuming a thread)."""
    launch, intake = recorded.launch, recorded.intake
    after = None if intake is None else intake.after
    halt = await prepare(rt, definition, fresh=fresh, launch=launch)
    if halt is None:
        halt = await first()
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


async def _where(
    given: Store | None, thread: Thread | ThreadId | None
) -> tuple[Store, SqliteStore, Thread | None]:
    """The run's store and thread: a thread id is its main branch in the store. Raises
    ConfigError for a thread the store doesn't have."""
    store = given or (thread.store if isinstance(thread, Thread) else sqlite(".threads"))
    sq = await open_store(store)
    if not isinstance(thread, str):
        return store, sq, thread
    root = await sq.root(thread)
    if isinstance(root, Err):
        raise ConfigError("invalid_config", f"thread {thread} is not in this store")
    return store, sq, Thread(thread, root.value, store)


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


def _ceilings[D](options: RunOptions[D], launch: Launch | None) -> tuple[Permissions, ...]:
    """A launched thread's ceilings come with its launch; a run's own is its option."""
    if launch is not None:
        return launch.ceilings
    ceiling = options.get("ceiling")
    return () if ceiling is None else (ceiling,)


def _refusal(error: ParseError) -> RunErrorCode:
    return "branch_busy" if error.code in ("branch_busy", "stale_epoch") else "branch_not_runnable"


async def _stubs(
    sq: SqliteStore, chain: Sequence[Event], launch: Launch | None, given: tuple[Stub, ...] | None
) -> Ok[tuple[Stub, ...] | None] | Err[ParseError]:
    """Stub mode comes from the branch's own stub fork first: its frozen script is the durable
    record of how this branch runs. Otherwise from the launch of a stub run's child, or from a
    live eval's recorded case."""
    ref = stub_fork_ref(chain)
    if ref is not None:
        data = await sq.get_artifact(ref.sha256)
        return data if isinstance(data, Err) else frozen_stubs(chain, data.value)
    if launch is not None and launch.stubs is not None:
        return Ok(launch.stubs)
    return Ok(given)


def _child_runner(store: Store, stubs: tuple[Stub, ...] | None) -> Execute:
    """How this run starts a launched thread: the same pipeline, in the same store, in the same
    mode (a stub run's children never go live either)."""

    async def run(definition: Definition[None], text: str, launch: Launch) -> RunResult[str]:
        how = replace(launch, stubs=stubs)
        return await execute(definition, text, {"store": store}, None, _drop, how)

    return run


def member_runner(store: Store) -> Callable[[Definition[None], MemberRun], Awaitable[None]]:
    """How the team worker runs a member branch: this pipeline, as a team member, under the
    member's own lease holder and budgets, until the branch is idle, parked or ended."""

    async def run(definition: Definition[None], m: MemberRun) -> None:
        options: RunOptions[None] = {
            "thread": Thread(m.thread, m.branch, store),
            "store": store,
            "principal": m.principal,
        }
        member = replace(definition, in_team=True)
        await execute(member, None, options, None, _drop, member=m)

    return run


def _drop(_item: StreamEvent) -> None:
    pass
