"""Live text (spec/schema/ui/README.md, "Live text"): an attempt's deltas are redacted per part
before they go out, so each part's live text is a prefix of its committed text, with a secret
split across deltas and at every split point; and a run's stream carries them."""

import asyncio
from collections.abc import AsyncIterator

from pydantic import JsonValue

from threads import DeltaItem, scripted_model, sqlite
from threads.agents.agent import agent
from threads.log import EventId
from threads.loop.model import Delta, ModelChunk, ModelContext, ModelRequest
from threads.loop.scripted import ScriptedModel
from threads.loop.shown import Shown
from threads.redaction import redact_secrets, register

REQUEST = EventId("0192e000-0000-7000-8000-000000000003")
VALUE = "sk-live-0123456789"  # a registered value, not a real key
PARTS = ("First Sk sk-live-0123456789 done.", "Second part.")


def shown(splits: list[tuple[int, str]]) -> dict[int, str]:
    out: dict[int, str] = {}

    def send(request: EventId, part: int, text: str) -> None:
        assert request == REQUEST
        out[part] = out.get(part, "") + text

    live = Shown(REQUEST, send)
    for part, text in splits:
        live.feed(part, text)
    live.end()
    return out


def test_each_parts_live_text_is_a_prefix_of_its_committed_text_at_every_split() -> None:
    register(VALUE, "key")
    # Parts 0 and 2 are text, part 1 a tool call between them (it streams nothing).
    first, second = PARTS
    committed = {0: redact_secrets(first), 2: redact_secrets(second)}
    for cut in range(len(first) + 1):
        for cut2 in range(len(second) + 1):
            chunks = [(0, first[:cut]), (0, first[cut:]), (2, second[:cut2]), (2, second[cut2:])]
            live = shown([c for c in chunks if c[1]])
            assert VALUE not in "".join(live.values())
            for part, text in live.items():
                assert committed[part].startswith(text), (cut, cut2, part, text)
            assert live == committed


def test_a_late_delta_for_a_finished_part_is_dropped() -> None:
    assert shown([(0, "ab"), (2, "cd"), (0, "late")]) == {0: "ab", 2: "cd"}


class Streams(ScriptedModel):
    """Scripted, with each text part streamed in two deltas."""

    def __init__(self, *replies: JsonValue) -> None:
        super().__init__(scripted_model({"responses": list(replies)})._entries, {})

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        async for chunk in super().send(request, context):
            if isinstance(chunk, Delta):
                half = len(chunk.text) // 2
                yield Delta(chunk.part, chunk.text[:half])
                yield Delta(chunk.part, chunk.text[half:])
            else:
                yield chunk


def test_a_run_streams_its_text_as_delta_items() -> None:
    usage: JsonValue = {"input_tokens": 1, "output_tokens": 1}
    reply: JsonValue = {
        "content": [{"type": "text", "text": "Hello there."}],
        "stop_reason": "end_turn",
        "usage": usage,
    }

    async def go() -> str:
        run = agent(model=Streams(reply)).stream("hi", store=sqlite(":memory:"))
        return "".join([i.text async for i in run if isinstance(i, DeltaItem)])

    assert asyncio.run(go()) == "Hello there."
