"""What a rerun plays back instead of the user's world: the recorded model replies, under the
settings each epoch pinned, and the recorded permission decisions. Nothing here reaches a
provider: the one model it makes is the scripted test kit."""

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import replace
from typing import Final

from pydantic import JsonValue

from threads.log import (
    Event,
    HookDecisionEvent,
    ModelRef,
    PermissionDecisionEvent,
    ToolCallData,
    ToolSpec,
)
from threads.log import Model as ModelLimits
from threads.loop.model import Model, ModelChunk, ModelContext, ModelInfo, ModelRequest, Rejected
from threads.loop.scripted import ScriptedModel, scripted_model
from threads.permissions import Decision
from threads.reduce import Fold

_ACCEPTS: Final = ("text", "image_ref", "document_ref", "audio_ref")


class Replayed(ScriptedModel):
    """The recorded replies, in order, played by every settings epoch. For a recorded turn each
    epoch sees it as the model the log pinned for it, accepting every input the recording sent,
    so the loop's capability and limit checks decide as they did then. A call past the end is a
    provider error the rerun records, as TypeScript's scripted model does, and is counted."""

    def __init__(self, script: Mapping[str, JsonValue], limits: Sequence[ModelLimits]) -> None:
        played = scripted_model(script)
        super().__init__([], played._lookups)
        self._entries = played._entries
        self._limits = tuple(limits)
        self._current: ModelInfo | None = None
        self.unexpected: int = 0

    def as_pinned(self, ref: ModelRef) -> Model:
        """This replay as the model `ref` names, with its pinned limits."""
        limits = next(
            (m for m in self._limits if (m.provider, m.name) == (ref.provider, ref.name)), None
        )
        base = super().info
        self._current = replace(
            base, model=ref, accepts=_ACCEPTS, limits=base.limits if limits is None else limits
        )
        return self

    @property
    def info(self) -> ModelInfo:
        return self._current or super().info

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        if self.remaining == 0:
            self.unexpected += 1
            yield Rejected("provider_error", 400)
            return
        async for chunk in super().send(request, context):
            yield chunk


def scripted_policy(
    v1: Mapping[str, Mapping[str, JsonValue]],
) -> Callable[[Fold, ToolCallData, ToolSpec], Decision]:
    """The corpus's policy: the decision a scripted tool names, else allow."""

    def authorize(_fold: Fold, _call: ToolCallData, spec: ToolSpec) -> Decision:
        match v1.get(spec.name, {}).get("decision"):
            case "ask":
                return Decision("ask", "policy", "conformance_ask")
            case "deny":
                return Decision("deny", "policy", "conformance_deny")
            case _:
                return Decision("allow", "policy", "conformance_allow")

    return authorize


def recorded_authorizer(
    recorded: Sequence[Event] | None,
) -> Callable[[Fold, ToolCallData, ToolSpec], Decision]:
    """The recorded permission decisions as the policy's answers. A decision a hook made records
    no policy answer: the policy then allowed, or asked when a permission_request hook answered."""

    def authorize(_fold: Fold, call: ToolCallData, _spec: ToolSpec) -> Decision:
        decided = next(
            (
                e
                for e in recorded or ()
                if isinstance(e, PermissionDecisionEvent) and e.data.call_id == call.call_id
            ),
            None,
        )
        if decided is None:
            return Decision("allow", "policy", "conformance_allow")
        d = decided.data
        if d.source != "hook":
            return Decision(d.decision, d.source, d.rule_id if isinstance(d.rule_id, str) else None)
        asked = any(
            isinstance(e, HookDecisionEvent)
            and e.data.hook == "permission_request"
            and e.data.call_id == call.call_id
            for e in recorded or ()
        )
        return Decision("ask" if asked else "allow", "policy")

    return authorize
