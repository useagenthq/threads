"""The ModelContext the loop hands an adapter: its fencing pair and verified artifact access."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from corpus import Clock
from kit import T0, Tools, open_store, start
from pydantic import JsonValue

from threads.log import ArtifactRef, ParseError, TextPart, Usage
from threads.loop.drive import drive
from threads.loop.model import (
    ModelChunk,
    ModelContext,
    ModelRequest,
    ModelResponse,
    Rejected,
    RejectReason,
)
from threads.loop.runtime import Failed, Halt, Idle
from threads.loop.scripted import ScriptedModel
from threads.reduce.handlers import to_json
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


def _run_rejected(reason: RejectReason) -> tuple[Halt, list[str], list[JsonValue], int]:
    async def main() -> tuple[Halt, list[str], list[JsonValue], int]:
        clock = Clock(T0)
        model = ScriptedModel([Rejected(reason)], {})
        rt = await start(await open_store(), [], model, Tools({}, clock), clock)
        halt = await drive(rt)
        kinds = [e.type for e in rt.events]
        data = [to_json(e.data) for e in rt.events if e.type in _ENDINGS]
        return halt, kinds, data, len(model.sent)

    return asyncio.run(main())


_ENDINGS = ("model_attempt_abandoned", "turn_completed")


@pytest.mark.parametrize(
    "code", ["content_unsupported", "continuation_unsupported", "transport_fence_unsupported"]
)
def test_a_send_time_refusal_ends_the_turn_with_its_code_and_is_never_resent(
    code: RejectReason,
) -> None:
    halt, kinds, data, sent = _run_rejected(code)
    assert sent == 1
    assert kinds.count("model_request") == 1
    abandoned, ended = data
    assert isinstance(abandoned, dict)
    assert abandoned["provider_outcome"] == "not_sent"
    assert ended == {"reason": "error", "code": code}
    assert isinstance(halt, Idle)


def test_a_stale_epoch_rejection_appends_nothing() -> None:
    halt, kinds, _, _ = _run_rejected("stale_epoch")
    assert kinds[-1] == "model_request"
    assert halt == Failed("branch_busy", "the lease moved before the send")


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
