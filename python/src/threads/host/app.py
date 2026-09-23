"""`host()` (spec/api.json `host`, `Host`; ): agents bound to a
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

from threads._generated.host_api_v1 import RunAccepted, StartRunRequest
from threads.agents.agent import Agent
from threads.agents.config import ConfigError
from threads.agents.store import Store, open_store
from threads.host import start, stream
from threads.host.channel import ChannelAdapter, RawRequest, RawResponse
from threads.host.intake import ChannelIntake
from threads.host.runs import Runner
from threads.host.schedules import Schedule, Scheduler
from threads.host.stream import Message
from threads.log import BranchId, EventId, ParseError, Principal, ThreadId
from threads.result import Err, Ok
from threads.store import LOCAL_TENANT
from threads.thread.handle import Thread, open_thread

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.types import ASGIApp

type Authenticate = Callable[["Request"], Awaitable[Principal | None]]
"""Maps an HTTP API request to its principal, or None for 401."""


class Host:
    """spec/api.json `Host`."""

    def __init__(
        self,
        store: Store,
        agents: Mapping[str, Agent[None]],
        channels: Mapping[str, ChannelAdapter],
        schedules: Sequence[Schedule],
        authenticate: Authenticate | None,
    ) -> None:
        self._store = store
        self._agents = agents
        self._channels = channels
        self.authenticate: Authenticate | None = authenticate
        self._runner: Runner = Runner(store, agents, channels)
        self._intake: ChannelIntake = ChannelIntake(self._runner, channels)
        self._runner.on_end = self._intake.consume
        self._scheduler = Scheduler(self._runner, schedules)
        self._ticking: asyncio.Task[None] | None = None
        self._asgi: ASGIApp | None = None

    @property
    def channels(self) -> Mapping[str, ChannelAdapter]:
        return self._channels

    @property
    def asgi(self) -> "ASGIApp":
        """The HTTP API and channel webhooks as an ASGI app (the `host` extra)."""
        if self._asgi is None:
            from threads.host.http import app  # noqa: PLC0415 - starlette only when served

            self._asgi = app(self)
        return self._asgi

    async def ready(self) -> None:
        """Confirms the bindings: every channel and schedule names a host agent. Sends nothing."""
        for name, adapter in self._channels.items():
            if adapter.agent not in self._agents:
                raise ConfigError("invalid_config", f"channel {name}: no agent {adapter.agent}")
        self._scheduler.check(self._runner.agent)
        await open_store(self._store)
        if self._ticking is None:
            self._ticking = asyncio.get_running_loop().create_task(self._tick())

    async def _tick(self) -> None:
        sq = await open_store(self._runner.store(LOCAL_TENANT))
        for tenant, thread in await sq.tables.unconsumed_threads():
            self._intake.consume(self._runner.store(tenant), thread)
        await self._scheduler.run()

    async def stop(self) -> None:
        """Drains intake in flight and ends the runs, which releases their leases."""
        if self._ticking is not None:
            self._ticking.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticking
            self._ticking = None
        await self._intake.drain()
        await self._runner.stop()

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
        """The principal's tenant's thread at the branch (default main), with its agent's
        approvers and sandbox. Another tenant's thread is not_found."""
        store = self._runner.store(principal.tenant)
        bound = await self._runner.bound(store, thread_id)
        sandbox = None if bound is None else bound.definition.sandbox
        opened = await open_thread(store, thread_id, branch_id=branch_id, sandbox=sandbox)
        if isinstance(opened, Err):
            return opened
        thread = opened.value
        approvers = () if bound is None else bound.approvers
        return Ok(Thread(thread.id, thread.branch, store, sandbox=sandbox, approvers=approvers))

    async def resume(self, thread: Thread) -> None:
        """After a control: continue the thread if it can move on."""
        await self._runner.resume(thread.store, thread.id, thread.branch)

    async def receive(self, channel: str, raw: RawRequest) -> Ok[RawResponse] | Err[ParseError]:
        """A channel webhook: verified, its whole batch durable in the inbox, then answered."""
        return await self._intake.receive(channel, raw)


def host(
    *,
    store: Store,
    agents: Mapping[str, Agent[None]],
    channels: Mapping[str, ChannelAdapter] | None = None,
    schedules: Sequence[Schedule] = (),
    authenticate: Authenticate | None = None,
) -> Host:
    """spec/api.json `host`. Starts nothing until `ready()`. Without `authenticate` every /v1
    route answers 401; channel webhooks still work."""
    return Host(store, agents, channels or {}, schedules, authenticate)
