"""Outbound channel replies are effects.

Every op is a `tool_call` of the channel's `channel_send` tool, whether the model called it or
the host issued it to deliver a final response. The tool's effect class comes from what the
channel can prove: a lookup makes it reconcilable, a provider dedup window idempotent, and
otherwise it is unguarded, so an unknown outcome parks and is never re-sent. The send runs
fenced: the adapter's transport checks the run's lease where the request's first byte leaves.
"""

from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, Field, JsonValue, ValidationError

from threads.agents.bindings import AppTool, Fence
from threads.agents.context import RunContext
from threads.agents.intake import After
from threads.agents.store import Store, now_ms, open_store
from threads.host.channel import ChannelAdapter, DeliveryError, Sent
from threads.log import BranchId, CallId, JsonObject, ModelResponseEvent, ToolSpec
from threads.log.jcs import canonicalize
from threads.loop import calls
from threads.loop.drafts import draft
from threads.loop.model import LookupResult, LookupUnknown, NotFound, NotFoundNonfinal
from threads.loop.runtime import Halt, Idle, Runtime, lost
from threads.loop.tools import Dispatched, NotSent, Output, Uncertain
from threads.memory.fence import bound, refused
from threads.result import Err, Ok
from threads.store import Draft

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
            with bound(self.fence):
                outcome = await self.to.adapter.perform(op, ctx.effect_key, self.to.credentials)
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


def deliver_final(adapter: ChannelAdapter) -> After:
    """After a turn ends normally, the host sends its final response: each rendered op is a
    host-issued channel_send call with the deterministic id `send_<response seq>_<op index>`, so
    a recovered run finds the same call instead of minting another."""

    async def after(rt: Runtime, halt: Halt) -> Halt:
        response = _final_response(rt, halt)
        if response is None:
            return halt
        for index, op in enumerate(adapter.render(response)):
            call_id = CallId(f"send_{response.seq}_{index}")
            if call_id not in rt.fold.calls:
                issued = await rt.append(*_issue(call_id, op, response))
                if isinstance(issued, Err):
                    return lost(issued.error)
            stopped = await calls.run_call(rt, call_id)
            if stopped is not None:
                return stopped
        return halt

    return after


def _final_response(rt: Runtime, halt: Halt) -> ModelResponseEvent | None:
    if not (isinstance(halt, Idle) and halt.reason == "end_turn"):
        return None
    # ponytail: only the turn that just ended is delivered; a crash between its end and the
    # first send's tool_call loses that delivery. Scan earlier turns if that matters.
    return next((e for e in reversed(rt.events) if isinstance(e, ModelResponseEvent)), None)


def _issue(call_id: CallId, op: JsonObject, response: ModelResponseEvent) -> tuple[Draft, Draft]:
    call: dict[str, JsonValue] = {
        "call_id": call_id,
        "name": NAME,
        "input": dict(op),
        "request_event_id": response.data.request_event_id,
    }
    allow: dict[str, JsonValue] = {
        "call_id": call_id,
        "decision": "allow",
        "source": "policy",
        "rule_id": "host_delivery",
    }
    return draft("tool_call", call), draft("permission_decision", allow)
