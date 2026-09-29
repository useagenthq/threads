"""What the loop needs from whatever executes tool bodies: app tools on the host, a sandbox, or a
test kit. Every answer about an operation that may have happened is a value."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from threadsai.log import (
    ArtifactRef,
    BranchId,
    CallId,
    Event,
    JsonObject,
    ResultPart,
    ThreadId,
    ToolSpec,
)
from threadsai.loop.model import LookupResult
from threadsai.store import Draft
from threadsai.web.http import Fence

type Termination = Literal["terminated", "already_exited", "unknown"]

type Put = Callable[[bytes | str, str], Awaitable[ArtifactRef]]
"""Stores bytes before any event names them, and returns their ref. Text is redacted."""

type Read = Callable[[ArtifactRef], Awaitable[bytes | None]]
"""An artifact this branch's log names, with its hash and length verified; None when unreadable."""


async def _nothing_stored(_data: bytes | str, _media_type: str) -> ArtifactRef:
    raise AssertionError("this invocation stores nothing")


async def _nothing_read(_ref: ArtifactRef) -> bytes | None:
    return None


async def _unfenced() -> bool:
    """No authority was given this invocation, so nothing may be sent under it (invariant 2). A
    default that answered True would let a dispatch built without a fence send unfenced."""
    return False


@dataclass(frozen=True, slots=True)
class Invocation:
    """One dispatch of a validated call under its derived effect key.

    `events`, `put` and `read` are what a tool that derives its send from its own log needs: an
    A2A send reads the card its thread pinned and replays the exact bytes its first attempt
    stored, rather than keeping process state a crash would lose. `events` is read when it is
    called, not when the invocation is built, so a dispatch sees the `remote_call` its own begin
    appended."""

    spec: ToolSpec
    call_id: CallId
    input: JsonObject
    effect_key: str
    thread_id: ThreadId | None = None
    branch_id: BranchId | None = None
    events: Callable[[], Sequence[Event]] = tuple
    put: Put = field(default=_nothing_stored)
    read: Read = field(default=_nothing_read)
    fence: Fence = field(default=_unfenced)
    """This dispatch's authority, awaited at the tool's real send point: the loop's fence before
    the dispatch cannot cover a lease lost while a name resolves and a socket opens."""


@dataclass(frozen=True, slots=True)
class Reference:
    """Recalled memory or retrieved knowledge: one `injected{trust: untrusted_reference}` event
    appended right after the call's result, so the model sees it only inside the reference
    wrapper and replay reads it from the log. A loaded skill rides the
    same way but is `trusted_instruction`: its body is host-pinned config."""

    source: Literal["memory", "knowledge", "skill"]
    id: str
    version: str
    text: str
    location: str | None = None
    """A knowledge excerpt's byte span, `<start>-<end>`."""


@dataclass(frozen=True, slots=True)
class Output:
    """The tool finished: its text result, which may be an error the model sees."""

    text: str
    is_error: bool = False
    full_output: ArtifactRef | None = None
    """Output spilled at the source (a sandbox exec over its preview): the result's `ref`, so
    `read_tool_result` reads it."""
    references: tuple[Reference, ...] = ()
    content: tuple[ResultPart, ...] = ()
    """Ordered media and citation parts: when present, the model sees exactly these
    and `text` is their plain-text rendering for logs and channels."""
    events: tuple[Draft, ...] = ()
    """What this run observed, appended after the call's `effect_commit` and before its result, so
    a rule that reads a receipt first (a `remote_task_state` after its commit) holds."""
    receipt: str | None = None
    """The provider's own id for the effect (an A2A task id), recorded as `effect_commit`'s
    `provider_receipt`: the only thing that lets a later process resume without re-sending."""


@dataclass(frozen=True, slots=True)
class Uncertain:
    """The attempt may have run: timeout, transport error, a lost answer."""

    reason: Literal["timeout", "transport_error"]


@dataclass(frozen=True, slots=True)
class NotSent:
    """Proven never sent. In stub mode, an invocation no recorded stub matches."""

    unmatched: bool = False
    refused: Literal["rate_limited", "transient", "permanent"] | None = None
    """A channel's refusal: rate_limited and transient are sent again under the same key."""


type Dispatched = Output | Uncertain | NotSent


type Validate = Callable[[ToolSpec, JsonObject], str | None]
"""Why arguments fail a tool's schema binding, or None when they parse."""


def unbound(spec: ToolSpec, _input: JsonObject) -> str | None:
    """A tool with no schema binding in this process (an MCP or log-only tool) fails closed:
    core never validates against an arbitrary JSON Schema."""
    return f"unsupported: no schema binding for {spec.name}"


@dataclass(frozen=True, slots=True)
class Refused:
    """A call refused before anything is durable: nothing begins and nothing is sent."""

    why: str


type Prepared = tuple[Draft, ...] | Refused
"""What an attempt must make durable in the same append as its `effect_begin`."""


@runtime_checkable
class Begins(Protocol):
    """A runner whose tools record something with their `effect_begin`: an A2A send's
    `remote_card` and `remote_call` (semantic rules 56 and 58), written before any byte leaves.
    Optional, and checked for, because it is one runner's concern and not every runner's."""

    async def begin(self, call: Invocation) -> Prepared: ...


class ToolRunner(Protocol):
    def invalid(self, spec: ToolSpec, input: JsonObject) -> str | None:
        """Why the arguments fail the tool's schema, or None when they parse."""
        ...

    async def dispatch(self, call: Invocation) -> Dispatched: ...

    async def lookup(self, call: Invocation) -> LookupResult[str]:
        """Reconciliation for a reconcilable tool: did the effect under this key happen?"""
        ...

    async def terminate(self, call: Invocation) -> Termination:
        """Kill a sandbox_local call's process group and confirm it is gone."""
        ...

    def provider_now(self) -> int | None:
        """The provider's clock for dedup windows, or None to use the host clock."""
        ...


async def prepared(runner: ToolRunner, call: Invocation) -> Prepared:
    """The drafts this attempt begins with. A runner that records nothing prepares nothing."""
    return await runner.begin(call) if isinstance(runner, Begins) else ()
