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
from weakref import WeakKeyDictionary

from threads.agents.agent import Agent
from threads.agents.definition import Definition
from threads.agents.intake import Intake
from threads.agents.results import Failed, RunResult, StreamEvent
from threads.agents.run import Emit, Input, RunOptions, execute
from threads.agents.store import Store, open_store, scoped
from threads.host.channel import ChannelAdapter
from threads.host.deliver import deliver, undelivered
from threads.host.send import Conversation, SendServer
from threads.log import (
    BranchId,
    Budget,
    HandoffEvent,
    Permissions,
    Principal,
    ThreadId,
    ThreadStartedEvent,
    UserInputEvent,
)
from threads.result import Ok
from threads.secrets import resolve
from threads.store import SqliteStore
from threads.thread import tree
from threads.thread.authority import Checked
from threads.thread.handle import Thread

type RunTask = asyncio.Task[RunResult[str]]
"""One run of a branch, as a task of this host."""

WAKE_S = 1.0
"""How long a subscriber waits for a wake-up before re-reading the log: runs of another process
never wake this one."""


@dataclass(frozen=True, slots=True)
class Bound:
    """How a thread runs here: its agent's definition and its channel."""

    definition: Definition[None]
    channel: Conversation | None = None
    """Where a channel thread's replies go."""

    def intake(self, intake: Intake | None, store: Store) -> Intake | None:
        """A channel thread's runs carry the send tool and deliver their final response."""
        if self.channel is None:
            return intake
        base = intake or Intake("channel", asyncio.get_running_loop().create_future())
        server = SendServer(self.channel, store)
        return replace(base, servers=(server,), after=deliver(self.channel))


class Runner:
    def __init__(
        self,
        root: Store,
        agents: Mapping[str, Agent[None, object]],
        channels: Mapping[str, ChannelAdapter],
        ceiling: Permissions | None = None,
    ) -> None:
        self._root = root
        self._ceiling = ceiling
        self._agents = agents
        self._channels = channels
        self._stores: dict[str, Store] = {}
        self._credentials: dict[str, Mapping[str, str]] = {}
        self._tasks: dict[BranchId, RunTask] = {}
        """Each branch's run in flight here."""
        self._live: set[RunTask] = set()
        """Every run in flight here, until it ends: what stop() ends."""
        self._wake: dict[BranchId, asyncio.Event] = {}
        self._again: set[BranchId] = set()
        self._pending: set[asyncio.Task[RunTask | None]] = set()
        """Every follow-on resume (a control answered while its run was ending), until it ends."""
        self._next: WeakKeyDictionary[RunTask, asyncio.Task[RunTask | None]] = WeakKeyDictionary()
        """A run's follow-on, for `through`; kept only while someone holds the run."""
        self._stopping = False
        self._generation = 0
        """Bumped by each stop: work a caller began before it never launches a run after it."""
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

    def agent(self, key: str) -> Agent[None, object] | None:
        return self._agents.get(key)

    def resolve_secrets(self) -> None:
        """ready(): every channel's secrets, resolved once on the host (missing_secret)."""
        for name, adapter in self._channels.items():
            self._credentials[name] = {k: resolve(v) for k, v in adapter.secrets.items()}

    def bound_to(self, key: str, *, channel: Conversation | None = None) -> Bound:
        """An agent key's binding."""
        definition = self._agents[key].definition
        return Bound(definition, channel)

    async def bound(self, store: Store, thread_id: ThreadId) -> Bound | None:
        """The binding of an existing thread, or None when no host agent owns it."""
        sq = await open_store(store)
        conversation = await sq.tables.conversation(thread_id)
        if conversation is not None:
            adapter = self._channels.get(conversation.channel)
            if adapter is None or adapter.agent not in self._agents:
                return None
            if conversation.channel not in self._credentials:
                self.resolve_secrets()
            credentials = self._credentials[conversation.channel]
            to = Conversation(
                adapter, conversation.address, credentials, conversation.installation_id
            )
            # After a handoff the conversation's thread runs the target the channel agent names.
            name = await _agent_name(sq, thread_id)
            base = self._agents[adapter.agent].definition
            found = base if name is None else _named(base, name)
            return None if found is None else Bound(found, to)
        name = await _agent_name(sq, thread_id)
        key = next((k for k, a in self._agents.items() if a.definition.name == name), None)
        return None if key is None else self.bound_to(key)

    async def follow(self, store: Store, thread_id: ThreadId) -> ThreadId | None:
        """The thread a channel conversation handed off to, with the route moved there (one
        conditional update; another process may have moved it first). None: no handoff."""
        sq = await open_store(store)
        root = await sq.root(thread_id)
        read = None if not isinstance(root, Ok) else await sq.read(root.value, 0)
        if read is None or not isinstance(read, Ok) or not read.value.fold.handed_off:
            return None
        moved = next(e for e in reversed(read.value.fold.events) if isinstance(e, HandoffEvent))
        target = ThreadId(moved.data.to_thread_id)
        await sq.tables.move(thread_id, target)
        return target

    async def authority(self, store: Store, thread_id: ThreadId) -> Checked:
        """Approval authority on a thread: its root run's agent's approvers, through subagent
        and handoff parents; a root no host agent owns has none."""
        root = await tree.root_of(store, thread_id, through=("subagent", "handoff"))
        bound = None if root is None else await self.bound(store, root[0])
        return Checked(() if bound is None else bound.definition.approvers)

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
        since: int | None = None,
    ) -> RunTask:
        """Starts the run as a task of this host; the branch's subscribers wake on each append.
        `since` is the `generation` the caller began in: from before a stop, nothing starts."""
        options: RunOptions[None] = {"thread": thread, "store": thread.store}
        options["principal"] = principal
        if self._ceiling is not None:
            options["ceiling"] = self._ceiling
        if budget is not None:
            options["budget"] = budget
        how = bound.intake(intake, thread.store)
        branch = thread.branch
        run = execute(bound.definition, input, options, None, self._emit(branch), None, how)
        task = asyncio.get_running_loop().create_task(run)
        if self._stopping or (since is not None and since != self._generation):
            # A stopping host, or a request from before its last stop, starts nothing: cancelled
            # before its first step and never the branch's run, so it can't stand in for one.
            task.cancel()
            self._live.add(task)
            task.add_done_callback(self._live.discard)
            return task
        current = self._tasks.get(branch)
        if current is None or current.done():
            self._tasks[branch] = task
        self._live.add(task)
        task.add_done_callback(lambda done: self._ended(thread, done))
        return task

    @property
    def generation(self) -> int:
        """The lifecycle this host is in; a request passes it to `launch` or `resume`."""
        return self._generation

    async def resume(
        self, store: Store, thread_id: ThreadId, branch: BranchId, since: int | None = None
    ) -> RunTask | None:
        """Continues a thread a control unparked (or cancelled). It records nothing new; the
        loop takes up what the log holds. A run still in flight (unwinding from the park the
        control answered) is followed by the resume once it ends. A control on a subagent's
        thread resumes the root of its tree, which runs the child on. Returns the run it
        started, if any."""
        root = await tree.root_of(store, thread_id)
        if root is not None and root[0] != thread_id:
            thread_id, branch = root
        if self.running(branch):
            self._again.add(branch)
            return None
        bound = await self.bound(store, thread_id)
        if bound is None:
            return None
        read = await (await open_store(store)).read(branch, 0)
        if not isinstance(read, Ok):
            return None
        who = next(
            (
                e.actor.principal
                for e in reversed(read.value.fold.events)
                if isinstance(e, UserInputEvent)
            ),
            None,
        )
        if who is None:
            return None
        return self.launch(bound, None, Thread(thread_id, branch, store), who, since=since)

    async def redeliver(self, store: Store, thread_id: ThreadId) -> RunTask | None:
        """A restarted host: a channel thread whose run a crash cut short (its turn still open,
        not parked) runs on from the log, and one whose log holds a reply it never sent (a crash
        after the turn ended) runs again to send it. A thread that handed off moves its
        conversation to the target, whose replies are sent from there. Returns the run it
        started, if any: a thread with a run in flight here is left to that run."""
        target = await self.follow(store, thread_id)
        if target is not None:
            return await self.redeliver(store, target)
        bound = await self.bound(store, thread_id)
        sq = await open_store(store)
        root = await sq.root(thread_id)
        if bound is None or bound.channel is None or not isinstance(root, Ok):
            return None
        read = await sq.read(root.value, 0)
        if not isinstance(read, Ok):
            return None
        fold = read.value.fold
        cut_short = fold.in_turn and not fold.parked
        if cut_short or undelivered(fold, bound.channel):
            return await self.resume(store, thread_id, root.value)
        return None

    def _emit(self, branch: BranchId) -> Emit:
        def emit(_item: StreamEvent) -> None:
            self.wake(branch)

        return emit

    def _ended(self, thread: Thread, task: RunTask) -> None:
        branch = thread.branch
        self._live.discard(task)
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
            # Bound to this generation: a stop before it launches leaves it nothing to start.
            again = self.resume(thread.store, thread.id, branch, since=self._generation)
            follow = asyncio.get_running_loop().create_task(again)
            self._pending.add(follow)
            follow.add_done_callback(self._pending.discard)
            self._next[task] = follow
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

    async def settled(self) -> None:
        """Until no run is in flight here, including a follow-on resume a finished run queued
        (`_pending`) and the run it starts."""
        while tasks := self._in_flight():
            await asyncio.wait(tasks)

    async def through(self, run: RunTask) -> None:
        """Until `run` has ended, and each follow-on resume it queued and the run that started;
        other runs, of its branch too, are not waited on."""
        current: RunTask | None = run
        while current is not None:
            await asyncio.wait({current})
            follow = self._next.get(current)
            if follow is None:
                return
            await asyncio.wait({follow})
            current = None if follow.cancelled() or follow.exception() else follow.result()

    def _in_flight(self) -> list[asyncio.Task[object]]:
        return [t for t in (*self._live, *self._pending) if not t.done()]

    def open(self) -> None:
        """Runs start again (a host started after a stop)."""
        self._stopping = False

    async def stop(self) -> None:
        """Ends every run in flight and starts no new one: each hands its lease back as it
        unwinds, and whatever it left in doubt (a send begun, its outcome unknown) is recovered
        by the next run of its branch."""
        self._stopping = True
        self._generation += 1
        self._again.clear()
        # Follow-on resumes too: one left pending could launch a run once the host starts again.
        while tasks := self._in_flight():
            for task in tasks:
                task.cancel()
            await asyncio.wait(tasks)


async def _agent_name(sq: SqliteStore, thread_id: ThreadId) -> str | None:
    """The agent a thread pinned at its start; None before it started."""
    root = await sq.root(thread_id)
    read = None if not isinstance(root, Ok) else await sq.read(root.value, 0)
    if read is None or not isinstance(read, Ok):
        return None
    events = read.value.fold.events
    started = next((e for e in events if isinstance(e, ThreadStartedEvent)), None)
    return None if started is None else started.data.agent_name


def _named(definition: Definition[None], name: str) -> Definition[None] | None:
    """The agent, or a handoff target reachable from it, with this name."""
    if definition.name == name:
        return definition
    return next((d for h in definition.handoffs if (d := _named(h, name)) is not None), None)
