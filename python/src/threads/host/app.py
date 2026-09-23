"""`host()` (spec/api.json `host`, `Host`): agents bound to a
store, channels and schedules, served over the typed HTTP API.

`host()` starts nothing. `ready()` confirms the bindings, sends nothing and starts no run; it
starts the scheduler, whose first tick also drains what a restart left in the inbox. `stop()`
drains intake in flight and ends the runs, releasing their leases. As an async context manager,
entering calls `ready` and leaving calls `stop`. Mount `asgi` in any ASGI server.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from types import TracebackType
from typing import TYPE_CHECKING, Self
from weakref import WeakKeyDictionary

from threads._generated.host_api_v1 import RunAccepted, StartRunRequest
from threads.agents.agent import Agent
from threads.agents.config import ConfigError
from threads.agents.store import Store, open_store
from threads.host import start, stream
from threads.host.channel import Challenged, ChannelAdapter, RawRequest, RawResponse
from threads.host.intake import ChannelIntake
from threads.host.runs import Runner, RunTask
from threads.host.schedules import Schedule, Scheduler
from threads.host.stream import Message
from threads.log import BranchId, EventId, ParseError, Permissions, Principal, ThreadId
from threads.result import Err, Ok
from threads.sandbox.protocol import Sandbox
from threads.store import LOCAL_TENANT
from threads.thread import tree
from threads.thread.handle import Thread, open_thread

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.types import ASGIApp

type Authenticate = Callable[["Request"], Awaitable[Principal | None]]
"""Maps an HTTP API request to its principal, or None for 401."""


_RECOVERY: "WeakKeyDictionary[Host, tuple[asyncio.Event, list[RunTask], Runner]]" = (
    WeakKeyDictionary()
)
"""Each host's start-up recovery pass (set once it has finished), the runs it started and the
runner that follows them: what the `recovered` seam waits on, never an unrelated run."""


class Host:
    """spec/api.json `Host`."""

    def __init__(  # noqa: PLR0913 - spec/api.json host's options
        self,
        store: Store,
        agents: Mapping[str, Agent[None, object]],
        channels: Mapping[str, ChannelAdapter],
        schedules: Sequence[Schedule],
        authenticate: Authenticate | None,
        *,
        ceiling: Permissions | None = None,
    ) -> None:
        self._store = store
        self._agents = agents
        self._channels = channels
        self.authenticate: Authenticate | None = authenticate
        self._runner: Runner = Runner(store, agents, channels, ceiling)
        self._intake: ChannelIntake = ChannelIntake(self._runner, channels)
        self._runner.on_end = self._intake.consume
        self._scheduler = Scheduler(self._runner, schedules)
        self._ticking: asyncio.Task[None] | None = None
        _RECOVERY[self] = (asyncio.Event(), [], self._runner)
        self._asgi: ASGIApp | None = None

    @property
    def channels(self) -> tuple[str, ...]:
        """The host(channels=...) keys; each is served at /channels/<key>/events."""
        return tuple(self._channels)

    def sandboxes(self) -> tuple[Sandbox, ...]:
        """One sandbox adapter per provider the agents use: what `threads gc` releases with."""
        by_provider = {
            a.definition.sandbox.info.provider: a.definition.sandbox
            for a in self._agents.values()
            if a.definition.sandbox is not None
        }
        return tuple(by_provider.values())

    @property
    def asgi(self) -> "ASGIApp":
        """The HTTP API and channel webhooks as an ASGI app (the `host` extra)."""
        if self._asgi is None:
            from threads.host.http import app  # noqa: PLC0415 - starlette only when served

            self._asgi = app(self)
        return self._asgi

    async def ready(self) -> None:
        """Confirms the bindings: every channel and schedule names a host agent, and every
        channel secret resolves (missing_secret). Sends nothing."""
        for name, adapter in self._channels.items():
            if adapter.agent not in self._agents:
                raise ConfigError("invalid_config", f"channel {name}: no agent {adapter.agent}")
        self._scheduler.check(self._runner.agent)
        self._runner.resolve_secrets()
        await open_store(self._store)
        self._runner.open()
        if self._ticking is None:
            # Each start has its own pass; cleared, not replaced, so a wait begun before this
            # start still sees it.
            _RECOVERY[self][0].clear()
            _RECOVERY[self][1].clear()
            self._ticking = asyncio.get_running_loop().create_task(self._tick())

    async def _tick(self) -> None:
        try:
            sq = await open_store(self._runner.store(LOCAL_TENANT))
            waiting = await sq.tables.unconsumed_threads()
            for tenant, thread in waiting:
                self._intake.consume(self._runner.store(tenant), thread)
            # ponytail: reads every conversation's log once per start; track unsent replies
            # in a table if hosts carry many conversations.
            for tenant, thread in await sq.tables.channel_threads():
                if (tenant, thread) not in waiting:
                    run = await self._runner.redeliver(self._runner.store(tenant), thread)
                    if run is not None:
                        _RECOVERY[self][1].append(run)
            # ponytail: API runs are found at start only; a live peer's crash waits for a restart.
            for tenant, thread, branch in await sq.tables.unfinished_runs():
                run = await self._runner.reopen(self._runner.store(tenant), thread, branch)
                if run is not None:
                    _RECOVERY[self][1].append(run)
        finally:
            _RECOVERY[self][0].set()
        await self._scheduler.run()

    async def stop(self) -> None:
        """Aborts first: every run and follow-on resume is cancelled and none starts, so no
        consumer waits on its run. A send whose request never reached the fence is abandoned (in
        doubt, for the next start to reconcile); one that passed it keeps the run's lease until
        it settles. Then it drains intake in flight. There is no deadline: a tool that ignores
        cancellation is waited on, since returning while it can act would break the fence.
        Deadlines belong at the tool or provider boundary."""
        if self._ticking is not None:
            self._ticking.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticking
            self._ticking = None
        await self._runner.stop()
        await self._intake.drain()

    async def __aenter__(self) -> Self:
        await self.ready()
        return self

    async def __aexit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        await self.stop()

    async def start_run(
        self, request: StartRunRequest, *, principal: Principal, idempotency_key: str
    ) -> Ok[RunAccepted] | Err[ParseError]:
        """POST /v1/runs: the user_input is durable with its idempotency receipt; the run goes
        on in the host. `principal` is the authenticated caller, never the local default."""
        return await start.start_run(self._runner, request, principal, idempotency_key)

    async def subscribe(
        self, thread_id: ThreadId, run_id: EventId, *, principal: Principal, after_seq: int = 0
    ) -> Ok[AsyncIterator[Message]] | Err[ParseError]:
        """GET /v1/threads/{thread_id}/runs/{run_id}/events: the run's events from the log,
        then its result. Takes no input and starts nothing."""
        return await stream.subscribe(self._runner, thread_id, run_id, principal, after_seq)

    async def thread(
        self, principal: Principal, thread_id: ThreadId, branch_id: BranchId | None
    ) -> Ok[Thread] | Err[ParseError]:
        """The principal's tenant's thread at the branch (default main), with its approval
        authority and sandbox. Another tenant's thread is not_found."""
        store = self._runner.store(principal.tenant)
        # A subagent's thread is governed by the agent at the root of its tree.
        root = await tree.root_of(store, thread_id)
        bound = None if root is None else await self._runner.bound(store, root[0])
        sandbox = None if bound is None else bound.definition.sandbox
        opened = await open_thread(store, thread_id, branch_id=branch_id, sandbox=sandbox)
        if isinstance(opened, Err):
            return opened
        thread = opened.value
        authority = await self._runner.authority(store, thread_id)
        return Ok(Thread(thread.id, thread.branch, store, sandbox=sandbox, authority=authority))

    async def resume(self, thread: Thread) -> None:
        """After a control: continue the thread if it can move on. A resume a stop overtook
        starts nothing."""
        since = self._runner.generation
        await self._runner.resume(thread.store, thread.id, thread.branch, since)

    def challenge(
        self, channel: str, query: Mapping[str, str]
    ) -> Ok[RawResponse] | Err[ParseError]:
        """A provider's GET subscription check: the adapter's challenge, if it has one."""
        adapter = self._channels.get(channel)
        if not isinstance(adapter, Challenged):
            return Err(ParseError("not_found", f"no channel {channel} with a challenge"))
        return adapter.challenge(query)

    async def receive(self, channel: str, raw: RawRequest) -> Ok[RawResponse] | Err[ParseError]:
        """A channel webhook: verified, its whole batch durable in the inbox, then answered."""
        return await self._intake.receive(channel, raw)


def host(  # noqa: PLR0913 - spec/api.json host's options
    *,
    store: Store,
    agents: Mapping[str, Agent[None, object]],
    channels: Mapping[str, ChannelAdapter] | None = None,
    schedules: Sequence[Schedule] = (),
    authenticate: Authenticate | None = None,
    ceiling: Permissions | None = None,
) -> Host:
    """spec/api.json `host`. Starts nothing until `ready()`. Without `authenticate` every /v1
    route answers 401; channel webhooks still work. `ceiling` caps every run this host starts
    or resumes (Agent.run `ceiling`)."""
    return Host(store, agents, channels or {}, schedules, authenticate, ceiling=ceiling)


async def recovered(served: Host) -> None:
    """After the recovery pass the latest `ready()` began has finished and the runs it started
    to redeliver replies have ended, with each follow-on resume one of them queued, so a test
    asserts what recovery did or didn't do without sleeping. Python recovers once per start
    (each start, including a restart of the same host), not on a timer as TS does, so there is
    no later pass to wait for. Internal: not exported."""
    done, runs, runner = _RECOVERY[served]
    await done.wait()
    for run in runs:
        await runner.through(run)
