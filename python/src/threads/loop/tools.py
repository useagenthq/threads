"""What the loop needs from whatever executes tool bodies: app tools on the host, a sandbox, or a
test kit. Every answer about an operation that may have happened is a value."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from threads.log import ArtifactRef, CallId, JsonObject, ToolSpec
from threads.loop.model import LookupResult

type Termination = Literal["terminated", "already_exited", "unknown"]


@dataclass(frozen=True, slots=True)
class Invocation:
    """One dispatch of a validated call under its derived effect key."""

    spec: ToolSpec
    call_id: CallId
    input: JsonObject
    effect_key: str


@dataclass(frozen=True, slots=True)
class Reference:
    """Recalled memory or retrieved knowledge: one `injected{trust: untrusted_reference}` event
    appended right after the call's result, so the model sees it only inside the reference
    wrapper and replay reads it from the log."""

    source: Literal["memory", "knowledge"]
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


@dataclass(frozen=True, slots=True)
class Uncertain:
    """The attempt may have run: timeout, transport error, a lost answer."""

    reason: Literal["timeout", "transport_error"]


@dataclass(frozen=True, slots=True)
class NotSent:
    """Proven never sent. In stub mode, an invocation no recorded stub matches."""

    unmatched: bool = False


type Dispatched = Output | Uncertain | NotSent


type Validate = Callable[[ToolSpec, JsonObject], str | None]
"""Why arguments fail a tool's schema binding, or None when they parse."""


def unbound(spec: ToolSpec, _input: JsonObject) -> str | None:
    """A tool with no schema binding in this process (an MCP or log-only tool) fails closed:
    core never validates against an arbitrary JSON Schema."""
    return f"unsupported: no schema binding for {spec.name}"


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
