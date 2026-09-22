"""What the loop needs from whatever executes tool bodies: app tools on the host, a sandbox, or a
test kit. Every answer about an operation that may have happened is a value."""

from dataclasses import dataclass
from typing import Literal, Protocol

from threads._strict_model import holds
from threads.log import CallId, JsonObject, ToolSpec
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
class Output:
    """The tool finished: its text result, which may be an error the model sees."""

    text: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class Uncertain:
    """The attempt may have run: timeout, transport error, a lost answer."""

    reason: Literal["timeout", "transport_error"]


@dataclass(frozen=True, slots=True)
class NotSent:
    """Proven never sent. In stub mode, an invocation no recorded stub matches."""

    unmatched: bool = False


type Dispatched = Output | Uncertain | NotSent


def schema_error(spec: ToolSpec, input: JsonObject) -> str | None:
    """Why arguments fail a tool's pinned input schema, or None. A keyword this reader can't
    check fails closed."""
    try:
        ok = holds(dict(spec.input_schema), dict(input))
    except TypeError:
        ok = False
    return None if ok else f"the arguments do not match {spec.name}'s input schema"


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
