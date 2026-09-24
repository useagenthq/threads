"""A cancel that lands during a model attempt: a model that has the run cancelled at a chosen send
(and, with `leak`, then refuses its response as a leak, C5), and what the log should show."""

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from compact_kit import kinds
from kit import USER
from pydantic import JsonValue

from threads.log import Context, Event, TextPart, TurnCompletedEvent, Usage
from threads.loop.defaults import CONTEXT
from threads.loop.drafts import draft
from threads.loop.model import ModelChunk, ModelContext, ModelRequest, ModelResponse
from threads.loop.scripted import Entry, ScriptedModel
from threads.result import Ok
from threads.thread.control import LOCAL_OPERATOR

if TYPE_CHECKING:
    from threads.loop.runtime import Runtime
    from threads.thread.handle import Thread


def said(text: str = "Hi.", reported: int = 10) -> ModelResponse:
    """A response; `reported` is the prompt size it reports, where the next estimate starts."""
    usage = Usage(input_tokens=reported, output_tokens=2)
    return ModelResponse((TextPart(type="text", text=text),), "end_turn", usage, None)


class Cancel:
    """Appends the cancel through whatever the test has at hand once the run is live."""

    def __init__(self) -> None:
        self.rt: Runtime | None = None
        self.thread: Thread | None = None

    async def __call__(self) -> None:
        if self.rt is not None:
            barrier = replace(draft("cancel_requested", {"scope": "turn"}), actor=USER)
            assert isinstance(await self.rt.append(barrier), Ok)
        elif self.thread is not None:
            assert isinstance(await self.thread.cancel(LOCAL_OPERATOR), Ok)


class CancelAt(ScriptedModel):
    """Plays `entries`, but send number `at` first has the run cancelled, and with `leak` then
    stores provider material holding that registered value."""

    def __init__(
        self, entries: Sequence[Entry], at: int, cancel: Cancel, leak: str | None = None
    ) -> None:
        super().__init__(entries, {})
        self.at, self.cancel, self.leak, self.sends = at, cancel, leak, 0

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        self.sends += 1
        if self.sends == self.at:
            await self.cancel()
            if self.leak is not None:
                material = json.dumps({"encrypted": self.leak}).encode()
                await context.put(material, "application/json")
        async for chunk in super().send(request, context):
            yield chunk


TAIL = [
    "cancel_requested",
    "model_attempt_abandoned",
    "compaction_failed",
    "cancelled",
    "turn_completed",
]
"""A compaction's side attempt stopped by a cancel: its failure, then the cancellation."""


def after_cancel(events: Sequence[Event]) -> list[str]:
    names = kinds(events)
    return names[names.index("cancel_requested") :]


def ended_cancelled(events: Sequence[Event]) -> None:
    ended = events[-1]
    assert isinstance(ended, TurnCompletedEvent)
    assert ended.data.reason == "cancelled"


def context(**over: JsonValue) -> Context:
    """The default context policy, a one-token tail, the compaction `trigger`, and `over`."""
    base = CONTEXT.model_dump(mode="json")
    compact: dict[str, JsonValue] = {"keep_tail": {"tokens": 1}, "max_failures": 3}
    return Context.model_validate(
        {**base, "compact": {**compact, "trigger": over["trigger"]}}
        | {k: v for k, v in over.items() if k != "trigger"}
    )
