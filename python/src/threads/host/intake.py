"""Channel intake, the same for every channel:

1. verify the raw bytes (failure: 401, nothing stored);
2. parse the whole batch;
3. map the verified identity to the tenant; each conversation maps to its thread by an atomic
   create-or-get, never by message text;
4. insert every item of the batch in one transaction, each under its own key (a redelivered
   item is a no-op);
5. only then answer the webhook;
6. consume the items in order under the branch lease: a message becomes channel_delivery plus
   the user_input it starts, a decision answers its challenge, a control stops the run, each
   marking its inbox row in the same append.
"""

import asyncio
from collections.abc import Mapping
from typing import Literal, assert_never

from pydantic import JsonValue, TypeAdapter

from threads._generated.host_api_v1 import Input
from threads.agents.intake import Intake
from threads.agents.store import Store, now_ms, open_store
from threads.host import answers
from threads.host.channel import (
    ChannelAdapter,
    Control,
    Decision,
    Ignore,
    Inbound,
    Message,
    RawRequest,
    RawResponse,
)
from threads.host.runs import Bound, Runner
from threads.log import BranchId, ParseError, TextPart, ThreadId
from threads.log.jcs import canonicalize
from threads.reduce import Fold
from threads.reduce.fold import loop_parked
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.store import Draft, StoredEvent, inbox
from threads.store.lines import uuid7
from threads.thread import approvals, control, tree
from threads.thread.handle import Thread

_INBOUND: TypeAdapter[Inbound] = TypeAdapter(Inbound)
RETRY_S = 0.5
"""How soon an answer that met another process's lease is tried again."""


class ChannelIntake:
    def __init__(self, runner: Runner, channels: Mapping[str, ChannelAdapter]) -> None:
        self._runner = runner
        self._channels = channels
        self._locks: dict[ThreadId, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._retries: set[asyncio.TimerHandle] = set()

    async def receive(self, channel: str, raw: RawRequest) -> Ok[RawResponse] | Err[ParseError]:
        """Steps 1-5: the webhook's answer, after its whole batch is durable."""
        adapter = self._channels.get(channel)
        if adapter is None:
            return Err(ParseError("not_found", f"no channel {channel}"))
        verified = adapter.verify(raw)
        if isinstance(verified, Err):
            return Err(ParseError("unverified", verified.error.message))
        parsed = adapter.parse(raw)
        if isinstance(parsed, Err):
            return Err(ParseError("invalid_request", parsed.error.message))
        delivery = verified.value
        items: list[inbox.Item] = []
        for item in parsed.value:
            if isinstance(item, Ignore):
                continue
            if item.principal.tenant != delivery.tenant:
                # A sender outside the verified workspace is not a verified identity.
                return Err(ParseError("unverified", "an item's sender is outside the tenant"))
            row = inbox.Item(
                channel,
                delivery.installation_id,
                item.item_key,
                delivery.delivery_id,
                item.address,
                _canonical(item),
            )
            items.append(row)
        store = self._runner.store(delivery.tenant)
        now = now_ms()
        tables = (await open_store(store)).tables
        threads = await tables.intake(items, now, lambda: ThreadId(uuid7(now)))
        for thread in threads:
            self.consume(store, thread)
        return Ok(adapter.ack(raw))

    def consume(self, store: Store, thread_id: ThreadId) -> None:
        """Step 6 in the background, one consumer per thread."""
        task = asyncio.get_running_loop().create_task(self._drain(store, thread_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Waits for intake in flight (stop). Pending retries are dropped: the rows are durable
        and the next start consumes them."""
        for handle in self._retries:
            handle.cancel()
        self._retries.clear()
        # Only unfinished tasks: a finished one's discard may still be queued, and gathering
        # finished tasks completes without yielding to it, so the loop would spin.
        while pending := [t for t in self._tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _drain(self, store: Store, thread_id: ThreadId) -> None:
        """Consumes the thread's items in arrival order. A message waits while the thread can't
        take input (a run in flight, a park), and every later message waits behind it; answers
        and controls never wait, since the answer a park needs may be queued behind a message."""
        target = await self._runner.follow(store, thread_id)
        if target is not None:
            # The conversation handed off: its items and replies are the target's now.
            await self._runner.redeliver(store, target)
            self.consume(store, target)
            return
        lock = self._locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            tables = (await open_store(store)).tables
            progressed = True
            while progressed:
                progressed = False
                blocked = False
                for row in await tables.pending(thread_id):
                    item = _INBOUND.validate_json(row.item)
                    if isinstance(item, Message) and blocked:
                        continue
                    took = await self._one(store, row, item)
                    if took == "held":
                        continue
                    if took is None:
                        # The branch is held elsewhere: this answer and everything after it wait,
                        # in order, until the lease frees.
                        self._later(store, thread_id)
                        return
                    blocked = blocked or not took
                    progressed = progressed or took

    def _later(self, store: Store, thread_id: ThreadId) -> None:
        # ponytail: a fixed retry while another process holds the branch; a lease-expiry wake
        # would need the holder's TTL.
        handle = asyncio.get_running_loop().call_later(RETRY_S, self.consume, store, thread_id)
        self._retries.add(handle)

    async def _one(
        self, store: Store, row: inbox.Row, item: Inbound
    ) -> bool | Literal["held"] | None:
        """Consumes one item; False when it must wait for the thread to move on, None when an
        answer or control must wait for another process's lease, held when a message waits
        behind an open question without blocking the asker's reply."""
        bound = await self._runner.bound(store, row.thread_id)
        branch = await _branch(store, row.thread_id)
        if bound is None or isinstance(branch, Err):
            return False
        thread = Thread(row.thread_id, branch.value, store)
        match item:
            case Message():
                if self._runner.running(branch.value):
                    return False
                fold = await _fold(store, branch.value)
                if fold is not None and answers.oldest(fold) is not None:
                    return await self._reply(thread, row, item)
                if fold is None or loop_parked(fold):
                    return False
                return await self._message(bound, thread, row, item)
            case Decision() | Control():
                return await self._control(bound, thread, row, item)
            case Ignore():
                raise AssertionError("ignored items are never stored")
            case _:
                assert_never(item)

    async def _reply(
        self, thread: Thread, row: inbox.Row, item: Message
    ) -> bool | Literal["held"] | None:
        """A message while a question is open: the asker's answers it (or is rejected, and the
        correction goes out), anyone else's waits."""
        text = _text(item.content)
        consume = inbox.consume(row.inbox_id)
        taken = await answers.reply(thread, _delivery(row, item), text, item.principal, consume)
        match taken:
            case "answered" | "rejected":
                # The run goes on after an answer; after a rejection it sends the correction.
                await self._runner.resume(thread.store, thread.id, thread.branch)
                return True
            case "held":
                return "held"
            case "busy":
                return None
            case "no_question":
                return False

    async def _message(self, bound: Bound, thread: Thread, row: inbox.Row, item: Message) -> bool:
        delivery = _delivery(row, item)
        delivered = delivery.event_id
        recorded: asyncio.Future[StoredEvent] = asyncio.get_running_loop().create_future()
        intake = Intake(
            "channel",
            recorded,
            before=(delivery,),
            delivery_event_id=delivered,
            companion=inbox.consume(row.inbox_id),
        )
        task = self._runner.launch(bound, item.content, thread, item.principal, intake=intake)
        await asyncio.wait({recorded, task}, return_when=asyncio.FIRST_COMPLETED)
        # Once the input is durable the run goes on alone: a later answer or cancel reaches it
        # through its writer, and the run's end drains what waited.
        return recorded.done()

    async def _control(
        self, bound: Bound, thread: Thread, row: inbox.Row, item: Decision | Control
    ) -> bool | None:
        consume = inbox.consume(row.inbox_id)
        if isinstance(item, Decision):
            decision = "granted" if item.decision == "grant" else "denied"
            at = (thread.id, thread.branch)
            done = await approvals.decide(
                thread.store,
                at,
                item.challenge_id,
                item.principal,
                decision,
                authority=await self._runner.authority(thread.store, thread.id),
                installation=row.installation_id,
                companion=consume,
            )
        else:
            # The barrier itself, not Thread.cancel's durable path: an item this host can't apply
            # stays queued (branch_busy below), so applying one never queues a second.
            hard = item.command == "cancel"
            done = await control.cancel(
                thread.store,
                thread.branch,
                item.principal,
                kind="cancel_requested" if hard else "stop_when_idle",
                companion=consume,
            )
            # Only a hard cancel reaches the tree; a soft stop lets running children finish.
            if hard and isinstance(done, Ok):
                await tree.cancel_children(thread.store, thread.branch, item.principal)
        if isinstance(done, Err) and done.error.code == "branch_busy":
            return None
        if isinstance(done, Err):
            # Refused (not an approver, a stale button): consumed, and nothing appended.
            await (await open_store(thread.store)).tables.discard(row.inbox_id)
            return True
        await self._runner.resume(thread.store, thread.id, thread.branch)
        return True


async def _branch(store: Store, thread_id: ThreadId) -> Ok[BranchId] | Err[ParseError]:
    """The conversation thread's main branch, created on its first message."""
    now = now_ms()
    # Atomic: two hosts consuming the first item at once must not each create a root.
    return Ok(await (await open_store(store)).root_or_create(thread_id, BranchId(uuid7(now)), now))


async def _fold(store: Store, branch: BranchId) -> Fold | None:
    read = await (await open_store(store)).read(branch, now_ms())
    return read.value.fold if isinstance(read, Ok) else None


def _delivery(row: inbox.Row, item: Message) -> Draft:
    """The message's channel_delivery, with the id the draft after it names."""
    data: dict[str, JsonValue] = {
        "channel": row.channel,
        "installation": row.installation_id,
        "conversation": item.address,
        "delivery_id": row.delivery_id,
        "item_key": row.item_key,
        "text": _text(item.content),
    }
    by = control.actor("user", item.principal) | {"kind": "channel"}
    return Draft("channel_delivery", data, by, True, uuid7(now_ms()))


def _text(content: Input) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(p.text for p in content if isinstance(p, TextPart))


def _canonical(item: Message | Decision | Control) -> bytes:
    text = canonicalize(to_json(item))
    if not isinstance(text, Ok):
        raise AssertionError("a parsed item always canonicalizes")
    return text.value.encode("utf-8")
