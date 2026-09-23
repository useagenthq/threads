"""Outbound channel replies are effects.

Every op is a `tool_call` of the channel's `channel_send` tool, whether the model called it or
the host issued it to deliver a final response. The tool's effect class comes from what the
channel can prove: a lookup makes it reconcilable, a provider dedup window idempotent, and
otherwise it is unguarded, so an unknown outcome parks and is never re-sent. The send runs
fenced: the adapter's transport checks the run's lease where the request's first byte leaves.
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Coroutine, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, Field, JsonValue, ValidationError

from threads.agents.bindings import AppTool, Fence
from threads.agents.context import RunContext
from threads.agents.store import Store, now_ms, open_store
from threads.host.channel import ChannelAdapter, DeliveryError, Sent
from threads.log import BranchId, CallId, JsonObject, ToolSpec
from threads.log.jcs import canonicalize
from threads.loop.model import LookupResult, LookupUnknown, NotFound, NotFoundNonfinal
from threads.loop.tools import Dispatched, NotSent, Output, Uncertain
from threads.memory.fence import bound, refused
from threads.result import Ok

NAME: Final = "channel_send"


class SendInput(BaseModel):
    """One message to this conversation."""

    text: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class Conversation:
    """Where a channel thread's sends go: the adapter, the conversation the host mapped the
    thread to (never chosen by the model), and the credentials the host resolved at ready()."""

    adapter: ChannelAdapter
    address: str
    credentials: Mapping[str, str]
    installation: str


@dataclass(frozen=True, slots=True)
class ChannelSend:
    """The send tool of one conversation."""

    to: Conversation
    fence: Fence
    store: Store
    """The run's store: a lookup after a crash reads the op its tool_call recorded."""

    @property
    def name(self) -> str:
        return NAME

    def spec(self) -> ToolSpec:
        caps = self.to.adapter.capabilities
        data: dict[str, JsonValue] = {
            "name": NAME,
            "description": "Send a message to this conversation.",
            "input_schema": SendInput.model_json_schema(),
            "effect_class": "unguarded",
        }
        if caps.lookup != "none":
            data["effect_class"] = "reconcilable"
        elif caps.dedup_window_ms is not None:
            data["effect_class"] = "idempotent"
            data["dedup_window_ms"] = caps.dedup_window_ms
        return ToolSpec.model_validate(data)

    def invalid(self, input: JsonObject) -> str | None:
        text = canonicalize(dict(input))
        if not isinstance(text, Ok):
            return "the arguments are not canonical JSON"
        try:
            SendInput.model_validate_json(text.value, strict=True)
        except ValidationError as error:
            return f"invalid arguments for {NAME}: {error.error_count()} error(s)"
        return None

    async def run(self, input: JsonObject, ctx: RunContext[object]) -> Dispatched:
        if ctx.effect_key is None:
            raise AssertionError("a tool runs under its effect key")
        op: JsonObject = {**input, "address": self.to.address}
        try:
            outcome = await _sent(
                self.fence, self.to.adapter.perform(op, ctx.effect_key, self.to.credentials)
            )
        except Exception as error:
            if refused(error):
                # Refused at the send point: no byte of the request was written.
                return NotSent()
            # An adapter that failed without saying what reached the provider: in doubt.
            return Uncertain("transport_error")
        match outcome:
            case Sent(platform_ref=ref):
                return Output(ref)
            case DeliveryError(sent="definite_not_sent"):
                return NotSent()
            case DeliveryError():
                # Whatever its kind, an unknown outcome is never re-sent on a backoff.
                return Uncertain("transport_error")

    async def lookup(self, effect_key: str, ctx: RunContext[object]) -> LookupResult[str]:
        op = await self._recorded(ctx.branch_id, ctx.call_id)
        if op is None:
            return LookupUnknown("the send's tool_call is not in the log")
        with bound(self.fence):
            answer = await self.to.adapter.lookup(effect_key, op)
        if isinstance(answer, NotFound) and self.to.adapter.capabilities.lookup != "final":
            # Finality is the channel's declared capability, never inferred from an answer.
            return NotFoundNonfinal()
        return answer

    async def _recorded(self, branch: BranchId, call_id: CallId | None) -> JsonObject | None:
        read = await (await open_store(self.store)).read(branch, now_ms())
        if not isinstance(read, Ok) or call_id is None:
            return None
        call = read.value.fold.calls.get(call_id)
        return None if call is None else {**call.data.input, "address": self.to.address}


async def _sent[T](fence: Fence, perform: Coroutine[object, object, T]) -> T:
    """`perform` under `fence`, riding out the run's cancellation (a stopping host) once one of
    its requests passed the fence: it may still land, so the run keeps its lease until it
    settles; released, a lookup elsewhere could find nothing and send again. Its outcome is
    then returned, for the loop to record, with the cancellation requested again so the run
    stops right after. One cancelled before the fence is closed there and stays begun, for the
    next run to reconcile."""
    closed = False
    passed = False

    async def gate() -> bool:
        nonlocal passed
        # Closed is read after the lease check, so a stop during it still closes the gate.
        if not await fence() or closed:
            return False
        passed = True
        return True

    async def fenced() -> T:
        with bound(gate):
            return await perform

    sending = asyncio.ensure_future(fenced())
    try:
        return await asyncio.shield(sending)
    except asyncio.CancelledError:
        closed = True
        if not passed:
            sending.cancel()
            raise
        while not sending.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(sending)
        stopping = asyncio.current_task()
        if stopping is not None:
            stopping.cancel()
        return sending.result()


@dataclass(frozen=True, slots=True)
class SendServer:
    """A tool server with the one send tool, connected per run so the tool carries the run's
    fence (the same path as MCP tools, so it is pinned in thread_started)."""

    to: Conversation
    store: Store

    @property
    def name(self) -> str:
        return NAME

    def connect(self, fence: Fence) -> AbstractAsyncContextManager[Sequence[AppTool[object]]]:
        return self._connected(fence)

    @asynccontextmanager
    async def _connected(self, fence: Fence) -> AsyncGenerator[Sequence[AppTool[object]]]:
        yield (ChannelSend(self.to, fence, self.store),)
