"""A requested compaction whose summary response is refused as a leak (C5, lane 09): the side
request's abandonment, its one compaction_failed and the turn's end land in one batch."""

import asyncio
import json
from collections.abc import AsyncIterator

from compact_kit import kinds, logged, request_of, requests

from threads import agent, sqlite
from threads.log import CompactionFailedEvent, TextPart, TurnCompletedEvent, Usage
from threads.loop.model import ModelChunk, ModelContext, ModelRequest, ModelResponse
from threads.loop.scripted import ScriptedModel
from threads.result import Ok
from threads.secrets import credential
from threads.thread.control import LOCAL_OPERATOR

USAGE = Usage(input_tokens=10, output_tokens=2)


class LeakyOnSecondSend(ScriptedModel):
    """Plays the script, but the second send stores provider material holding a secret."""

    def __init__(self, key: str) -> None:
        self.key = key
        hi = ModelResponse((TextPart(type="text", text="Hi."),), "end_turn", USAGE, None)
        super().__init__([hi, hi], {})
        self.sends = 0

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        self.sends += 1
        if self.sends == 2:  # noqa: PLR2004 - the summary request is the second send
            await context.put(json.dumps({"encrypted": self.key}).encode(), "application/json")
        async for chunk in super().send(request, context):
            yield chunk


def test_a_leaked_summary_answers_the_request_once_and_ends_the_turn() -> None:
    async def main() -> None:
        # Registered in the test: the suite resets the secret registry between tests.
        key = credential("fake", "api_key", "sk-l17-leak-5e6f", "U")()
        store = sqlite(":memory:")
        bot = agent(model=LeakyOnSecondSend(key))
        first = await bot.run("hi", store=store)
        assert isinstance(await first.thread.compact(LOCAL_OPERATOR), Ok)
        await bot.run("next", store=store, thread=first.thread)
        events = await logged(first.thread)
        request = request_of(events)
        side = requests(events, side=True)
        assert len(side) == 1
        failed = [e for e in events if isinstance(e, CompactionFailedEvent)]
        assert len(failed) == 1
        assert failed[0].data.cause_event_id == request.event_id
        assert failed[0].data.request_event_id == side[0].event_id
        assert kinds(events)[-3:] == [
            "model_attempt_abandoned",
            "compaction_failed",
            "turn_completed",
        ]
        ended = events[-1]
        assert isinstance(ended, TurnCompletedEvent)
        assert (ended.data.reason, ended.data.code) == ("error", "secret_in_provider_output")

    asyncio.run(main())
