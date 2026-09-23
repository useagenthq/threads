"""Runs in the host: which agent a thread belongs to, starting and resuming runs as tasks, and
waking subscribers when a run appends.

A thread's agent is fixed by its pin: a channel conversation's thread is its channel's agent,
with the channel's send tool; any other thread is the host agent whose name it pinned. A run
in flight is one task per branch; a resume starts one only when none is in flight.
"""

import asyncio
import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from threads.agents.agent import Agent
from threads.agents.definition import Definition
from threads.agents.intake import Intake
from threads.agents.results import Failed, RunResult, StreamEvent
from threads.agents.run import Emit, Input, RunOptions, execute
from threads.agents.store import Store, open_store, scoped
from threads.host.channel import ChannelAdapter
from threads.host.send import SendServer, deliver_final
from threads.log import BranchId, Budget, Principal, ThreadId, ThreadStartedEvent, UserInputEvent
from threads.result import Ok
from threads.thread.control import LOCAL_OPERATOR
from threads.thread.handle import Thread

WAKE_S = 1.0
"""How long a subscriber waits for a wake-up before re-reading the log: runs of another process
never wake this one."""


@dataclass(frozen=True, slots=True)
class Bound:
    """How a thread runs here: its agent's definition, who may approve, and its channel."""

    definition: Definition[None]
    approvers: tuple[Principal, ...]
    channel: tuple[ChannelAdapter, str] | None = None
    """The adapter and the conversation address of a channel thread."""

    def intake(self, intake: Intake | None) -> Intake | None:
        """A channel thread's runs carry the send tool and deliver their final response."""
        if self.channel is None:
            return intake
        adapter, address = self.channel
        base = intake or Intake("channel", asyncio.get_running_loop().create_future())
        return replace(base, servers=(SendServer(adapter, address),), after=deliver_final(adapter))


class Runner:
    def __init__(
        self,
        root: Store,
        agents: Mapping[str, Agent[None]],
        channels: Mapping[str, ChannelAdapter],
    ) -> None:
        self._root = root
        self._agents = agents
        self._channels = channels
        self._stores: dict[str, Store] = {}
        self._tasks: dict[BranchId, asyncio.Task[RunResult[str]]] = {}
        self._wake: dict[BranchId, asyncio.Event] = {}
        self._again: set[BranchId] = set()
        self._pending: set[asyncio.Task[None]] = set()
        self.on_end: Callable[[Store, ThreadId], None] | None = None
        """Called when a run of a thread ends here: the channel intake drains what waited."""
        self.last: dict[BranchId, Failed] = {}
        """Each branch's latest failure in this process: a run that failed with nothing in the
        log to say so (a refusal, an unavailable model) is answered from here. Every other
        outcome is read from the log."""

    def store(self, tenant: str) -> Store:
        """The tenant's view of the host store, one per tenant."""
        found = self._stores.get(tenant)
        if found is None:
            found = self._stores[tenant] = scoped(self._root, tenant)
        return found

    def agent(self, key: str) -> Agent[None] | None:
        return self._agents.get(key)

    def bound_to(self, key: str, *, channel: tuple[ChannelAdapter, str] | None = None) -> Bound:
        """An agent key's binding. A channel thread's approvers default to nobody (): approval then comes through the host API."""
        definition = self._agents[key].definition
        default = () if channel is not None else None
        approvers = definition.approvers if definition.approvers is not None else default
        return Bound(definition, approvers if approvers is not None else (LOCAL_OPERATOR,), channel)

    async def bound(self, store: Store, thread_id: ThreadId) -> Bound | None:
        """The binding of an existing thread, or None when no host agent owns it."""
        sq = await open_store(store)
        conversation = await sq.tables.conversation(thread_id)
        if conversation is not None:
            adapter = self._channels.get(conversation.channel)
            if adapter is None or adapter.agent not in self._agents:
                return None
            return self.bound_to(adapter.agent, channel=(adapter, conversation.address))
        root = await sq.root(thread_id)
        read = None if not isinstance(root, Ok) else await sq.read(root.value, 0)
        if read is None or not isinstance(read, Ok):
            return None
        started = next(
            (e for e in read.value.fold.events if isinstance(e, ThreadStartedEvent)), None
        )
        name = None if started is None else started.data.agent_name
        key = next((k for k, a in self._agents.items() if a.definition.name == name), None)
        return None if key is None else self.bound_to(key)

    def running(self, branch: BranchId) -> bool:
        task = self._tasks.get(branch)
        return task is not None and not task.done()

    def launch(  # noqa: PLR0913 - one run and how it came
        self,
        bound: Bound,
        input: Input | None,
        thread: Thread,
        principal: Principal,
        *,
        intake: Intake | None = None,
        budget: Budget | None = None,
    ) -> "asyncio.Task[RunResult[str]]":
        """Starts the run as a task of this host; the branch's subscribers wake on each append."""
        options: RunOptions[None] = {"thread": thread, "store": thread.store}
        options["principal"] = principal
        if budget is not None:
            options["budget"] = budget
        how = bound.intake(intake)
        branch = thread.branch
        run = execute(bound.definition, input, options, None, self._emit(branch), None, how)
        task = asyncio.get_running_loop().create_task(run)
        self._tasks[branch] = task
        task.add_done_callback(lambda done: self._ended(thread, done))
        return task

    async def resume(self, store: Store, thread_id: ThreadId, branch: BranchId) -> None:
        """Continues a thread a control unparked (or cancelled). It records nothing new; the
        loop takes up what the log holds. A run still in flight (unwinding from the park the
        control answered) is followed by the resume once it ends."""
        if self.running(branch):
            self._again.add(branch)
            return
        bound = await self.bound(store, thread_id)
        if bound is None:
            return
        read = await (await open_store(store)).read(branch, 0)
        if not isinstance(read, Ok):
            return
        who = next(
            (
                e.actor.principal
                for e in reversed(read.value.fold.events)
                if isinstance(e, UserInputEvent)
            ),
            None,
        )
        if who is None:
            return
        thread = Thread(thread_id, branch, store, approvers=bound.approvers)
        self.launch(bound, None, thread, who)

    def _emit(self, branch: BranchId) -> Emit:
        def emit(_item: StreamEvent) -> None:
            self.wake(branch)

        return emit

    def _ended(self, thread: Thread, task: "asyncio.Task[RunResult[str]]") -> None:
        branch = thread.branch
        if self._tasks.get(branch) is task:
            del self._tasks[branch]
        if not task.cancelled() and task.exception() is None:
            result = task.result()
            if isinstance(result, Failed):
                self.last[branch] = result
        self.wake(branch)
        if task.cancelled():
            return
        if branch in self._again:
            self._again.discard(branch)
            again = self.resume(thread.store, thread.id, branch)
            self._pending.add(asyncio.get_running_loop().create_task(again))
            self._pending = {t for t in self._pending if not t.done()}
        elif self.on_end is not None:
            self.on_end(thread.store, thread.id)

    def wake(self, branch: BranchId) -> None:
        event = self._wake.pop(branch, None)
        if event is not None:
            event.set()

    async def wait(self, branch: BranchId) -> None:
        """Until the branch's next append here, or WAKE_S."""
        event = self._wake.setdefault(branch, asyncio.Event())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), WAKE_S)

    async def stop(self) -> None:
        """Ends every run in flight: each hands its lease back as it unwinds, and whatever it
        left in doubt is recovered by the next run of its branch."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
