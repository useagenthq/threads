"""Framework invariants on the live loop (AGENTS.md): a stale owner never reaches an adapter
(invariant 2), intent is durable before every dispatch (invariant 3), and every request of a
settings epoch declares the same line 0 (invariant 5)."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from corpus import Clock
from hypothesis import given, settings
from hypothesis import strategies as st
from kit import T0, Tools, open_store, spec, start, text, use

from threads.log import EffectBeginEvent, ModelRequestEvent, TextPart, Usage
from threads.loop.drive import drive
from threads.loop.model import (
    LookupUnknown,
    ModelChunk,
    ModelContext,
    ModelRequest,
    ModelResponse,
)
from threads.loop.runtime import Failed, Idle, Runtime, WriterContext
from threads.loop.scripted import ScriptedModel, scripted_model
from threads.loop.tools import Dispatched, Invocation, Output
from threads.render.verify import verify_requests
from threads.result import Ok
from threads.store import MemoryArtifacts, SqliteStore, StoredEvent

if TYPE_CHECKING:
    from pydantic import JsonValue


USAGE = Usage(input_tokens=5, output_tokens=1)


def take_over(path: Path) -> None:
    """Another process takes the lease behind this writer's back, at a higher epoch."""
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE leases SET holder_id = 'thief', epoch = epoch + 1")


def _after(kind: type[StoredEvent], path: Path) -> Callable[[Sequence[StoredEvent]], None]:
    def observe(events: Sequence[StoredEvent]) -> None:
        if any(isinstance(e, kind) for e in events):
            take_over(path)

    return observe


def test_a_stale_owner_never_sends_a_durable_model_request(tmp_path: Path) -> None:
    path = tmp_path / "threads.db"

    async def main() -> tuple[object, ScriptedModel]:
        clock = Clock(T0)
        model = scripted_model({"responses": [text("never")]})
        store = await open_store(path)
        try:
            rt = await start(store, [], model, Tools({}, clock), clock)
            rt = replace(rt, observe=_after(ModelRequestEvent, path))
            return await drive(rt), model
        finally:
            await store.close()

    halt, model = asyncio.run(main())
    assert isinstance(halt, Failed)
    assert halt.code == "branch_busy"
    assert model.sent == []


class _QueuedModel(ScriptedModel):
    """A model whose real send happens after a queue: the lease moves while it waits."""

    path: Path

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        take_over(self.path)
        async for chunk in super().send(request, context):
            yield chunk


def test_a_lease_lost_while_the_send_is_queued_sends_nothing(tmp_path: Path) -> None:
    """The loop's own fence passes; the adapter's fence at its real send point does not."""
    path = tmp_path / "threads.db"

    async def main() -> tuple[object, ScriptedModel]:
        clock = Clock(T0)
        never = ModelResponse((TextPart(type="text", text="never"),), "end_turn", USAGE, None)
        model = _QueuedModel([never], {})
        model.path = path
        store = await open_store(path)
        try:
            rt = await start(store, [], model, Tools({}, clock), clock)
            return await drive(rt), model
        finally:
            await store.close()

    halt, model = asyncio.run(main())
    assert isinstance(halt, Failed)
    assert halt.code == "branch_busy"
    assert model.sent == []


def test_a_lease_lost_while_a_lookup_is_queued_sends_nothing(tmp_path: Path) -> None:
    path = tmp_path / "threads.db"

    async def main() -> tuple[object, ScriptedModel]:
        clock = Clock(T0)
        model = scripted_model({"responses": [], "lookup": {"e1": {"result": "not_found"}}})
        store = await open_store(path)
        try:
            rt = await start(store, [], model, Tools({}, clock), clock)
            take_over(path)
            return await model.lookup(f"{rt.writer.branch_id}:e1", WriterContext(rt)), model
        finally:
            await store.close()

    answer, model = asyncio.run(main())
    assert isinstance(answer, LookupUnknown)
    assert model.looked_up == []


def test_a_stale_owner_never_dispatches_a_begun_effect(tmp_path: Path) -> None:
    path = tmp_path / "threads.db"

    async def main() -> tuple[object, Tools]:
        clock = Clock(T0)
        tools = Tools({"email": [Output("sent")]}, clock)
        model = scripted_model({"responses": [use("email")]})
        store = await open_store(path)
        try:
            rt = await start(store, [spec("email", "unguarded")], model, tools, clock)
            rt = replace(rt, observe=_after(EffectBeginEvent, path))
            return await drive(rt), tools
        finally:
            await store.close()

    halt, tools = asyncio.run(main())
    assert isinstance(halt, Failed)
    assert tools.dispatches["email"] == 0


STEP = st.sampled_from(["text", "read", "email", "charge"])


@settings(max_examples=25, deadline=None)
@given(steps=st.lists(STEP, min_size=1, max_size=5))
def test_intent_is_durable_before_every_dispatch(steps: list[str]) -> None:
    """Invariant 3: the model_request is committed before its send, and an effect's
    effect_begin before its dispatch, whatever the mix of turns."""
    responses: list[JsonValue] = []
    outcomes: dict[str, list[Dispatched]] = {"read": [], "email": [], "charge": []}
    for i, step in enumerate(steps):
        if step != "text":
            responses.append(use(step, f"call_{i}"))
            outcomes[step].append(Output(f"{step} ok"))
    responses.append(text("Done."))
    specs = [
        spec("read", "read_only"),
        spec("email", "unguarded"),
        spec("charge", "idempotent", 60_000),
    ]

    async def main() -> Runtime:
        clock = Clock(T0)
        store = await open_store()
        tools = Tools(outcomes, clock)
        model = scripted_model({"responses": responses})
        rt = await start(store, specs, model, tools, clock)

        def sent(request: ModelRequest) -> None:
            # The writer's fold moves only after a commit returns: this is the durable log.
            last = rt.writer.fold.events[-1]
            assert isinstance(last, ModelRequestEvent)
            assert request.request_id == f"{rt.writer.branch_id}:{last.event_id}"

        def check(call: Invocation) -> None:
            if call.spec.effect_class == "read_only":
                return
            last = rt.writer.fold.events[-1]
            assert isinstance(last, EffectBeginEvent)
            assert last.data.call_id == call.call_id

        model.before_send = sent
        tools.on_dispatch = check
        assert isinstance(await drive(rt), Idle)
        return rt

    rt = asyncio.run(main())
    assert not rt.fold.pending


def test_every_request_of_an_epoch_declares_the_same_line0() -> None:
    """Invariant 5 on a multi-turn run: every recorded request replays byte for byte (C7 per
    epoch plus request_ref), and within the one epoch all declared prefixes are equal."""

    async def main() -> tuple[list[ModelRequestEvent], bool]:
        clock = Clock(T0)
        artifacts = MemoryArtifacts()
        opened = await SqliteStore.open(artifacts=artifacts)
        assert isinstance(opened, Ok)
        store = opened.value
        tools = Tools({"read": [Output("a"), Output("b")]}, clock)
        script: JsonValue = {"responses": [use("read", "c1"), use("read", "c2"), text("ok")]}
        rt = await start(store, [spec("read", "read_only")], scripted_model(script), tools, clock)
        assert isinstance(await drive(rt), Idle)
        requests = [e for e in rt.events if isinstance(e, ModelRequestEvent)]
        replayed = verify_requests(rt.events, artifacts.get)
        return requests, isinstance(replayed, Ok)

    requests, replayed = asyncio.run(main())
    assert replayed
    assert (
        len({(r.data.declared_prefix.bytes, r.data.declared_prefix.sha256) for r in requests}) == 1
    )
