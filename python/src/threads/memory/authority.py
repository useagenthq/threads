"""Memory write authority: who may make `save_memory` and `forget_memory`
happen, and the origin a record keeps.

"The user started this turn" is not "the user authored this memory". A write counts as the
principal's only when the turn was started by a verified principal's `user_input` and nothing
untrusted is in context: no tool output, no untrusted `injected` reference, no compaction
summary, no channel content from anyone else. The check is conservative: it looks at the whole
branch, not just what the last request rendered.
"""

from collections.abc import Sequence
from typing import Final, Literal

from threads.log import (
    ChannelDeliveryEvent,
    CompactedEvent,
    Event,
    InjectedEvent,
    ToolCallData,
    ToolResultEvent,
    ToolSpec,
    UserInputEvent,
)
from threads.loop.runtime import Authorize
from threads.memory.types import Origin
from threads.permissions import Decision
from threads.reduce import Fold

type MemoryWrite = Literal["deny", "ask", "allow_principal", "allow"]

WRITES: Final = frozenset({"save_memory", "forget_memory"})


def _untrusted(event: Event) -> bool:
    if isinstance(event, ToolResultEvent | CompactedEvent | ChannelDeliveryEvent):
        return True
    return isinstance(event, InjectedEvent) and event.data.trust == "untrusted_reference"


def principal_authored(events: Sequence[Event]) -> bool:
    """The open turn was started by one principal and nothing untrusted is in context."""
    inputs = [e for e in events if isinstance(e, UserInputEvent)]
    if not inputs or any(_untrusted(e) for e in events):
        return False
    # Every user_input carries its verified principal (wire rule 9).
    principal = inputs[-1].actor.principal
    return all(e.actor.principal == principal for e in inputs)


def origin(events: Sequence[Event]) -> Origin:
    """Derived by the host from the recorded sources, never from the model's claim."""
    if principal_authored(events):
        return "user"
    return "tool_output" if any(_untrusted(e) for e in events) else "model"


def with_memory_write(base: Authorize, write: MemoryWrite) -> Authorize:
    """The permission fold, then the memory write policy for writes. Deny absorbs: the policy
    can only make a decision stricter, never turn a deny or ask into an allow."""

    def authorize(fold: Fold, call: ToolCallData, spec: ToolSpec) -> Decision:
        decided = base(fold, call, spec)
        if spec.name not in WRITES or decided.decision == "deny":
            return decided
        match write:
            case "deny":
                return Decision("deny", "policy")
            case "allow":
                return decided
            case "allow_principal" if principal_authored(fold.events):
                return decided
            case "allow_principal" | "ask":
                return Decision("ask", "policy")

    return authorize
