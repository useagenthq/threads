"""The ModelContext the loop hands an adapter: its fencing pair and verified artifact access."""

import asyncio
from collections.abc import AsyncIterator

from corpus import Clock
from kit import T0, Tools, open_store, start

from threads.log import ArtifactRef, ParseError, TextPart, Usage
from threads.loop.drive import drive
from threads.loop.model import ModelChunk, ModelContext, ModelRequest, ModelResponse
from threads.loop.scripted import ScriptedModel
from threads.result import Err, Ok

USAGE = Usage(input_tokens=5, output_tokens=1)


class _Probe(ScriptedModel):
    """Exercises the context it is given before playing its script."""

    pair: tuple[str, int]
    fenced: Ok[None] | Err[ParseError]
    ref: ArtifactRef
    reads: tuple[Ok[bytes] | Err[ParseError], ...]

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        ref = await context.put(b'{"thinking":"x"}', "application/json")
        wrong = ArtifactRef(sha256=ref.sha256, bytes=ref.bytes + 1, media_type=ref.media_type)
        absent = ArtifactRef(sha256="0" * 64, bytes=1, media_type="text/plain")
        self.pair = (context.branch_id, context.epoch)
        self.fenced = await context.fence()
        self.ref = ref
        self.reads = (
            await context.read(ref),
            await context.read(wrong),
            await context.read(absent),
        )
        async for chunk in super().send(request, context):
            yield chunk


def test_the_loop_hands_the_adapter_its_fencing_pair_and_verified_artifacts() -> None:
    async def main() -> tuple[_Probe, tuple[str, int]]:
        clock = Clock(T0)
        ok = ModelResponse((TextPart(type="text", text="ok"),), "end_turn", USAGE, None)
        model = _Probe([ok], {})
        store = await open_store()
        rt = await start(store, [], model, Tools({}, clock), clock)
        await drive(rt)
        return model, (rt.writer.branch_id, rt.writer.epoch)

    model, owner = asyncio.run(main())
    good, wrong, absent = model.reads
    assert model.pair == owner
    assert model.fenced == Ok(None)
    assert model.ref.media_type == "application/json"
    assert good == Ok(b'{"thinking":"x"}')
    assert isinstance(wrong, Err)
    assert wrong.error.code == "artifact_corrupt"
    assert isinstance(absent, Err)
    assert absent.error.code == "artifact_missing"
