"""Replay: a log reduces to the same state wherever its bytes go (store, export, re-import)."""

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import JsonValue

from threads.log import BranchId, ThreadId
from threads.reduce import ReducedState
from threads.result import Ok
from threads.store import Draft, SqliteStore, verify_export

THREAD = ThreadId("0192a000-0000-7000-8000-000000000001")
ROOT = BranchId("0192b000-0000-7000-8000-000000000001")
NOW = 1_790_000_000_000
ALICE: dict[str, JsonValue] = {
    "kind": "user",
    "principal": {"issuer": "api", "tenant": "acme", "subject": "alice"},
}
STARTED = Draft(
    "thread_started",
    {
        "agent_name": "demo",
        "config_hash": "0" * 64,
        "instructions": "You are a helpful agent.",
        "model": {"provider": "scripted", "name": "scripted-1"},
        "model_params": {"max_tokens": 1024},
        "adapter": {"name": "scripted", "version": "1", "settings": {}},
        "tools": [],
    },
)

# A turn is an input plus zero or more steers and ignorable events; open turns may end the log.
TURN = st.tuples(st.text(max_size=20), st.integers(0, 2), st.booleans())


def drafts(turns: list[tuple[str, int, bool]]) -> list[Draft]:
    out = [STARTED]
    for text, steers, completed in turns:
        out.append(Draft("user_input", {"source": "api", "text": text}, actor=ALICE))
        out += [Draft("steer", {"source": "api", "text": "more"}, actor=ALICE)] * steers
        out.append(Draft("telemetry_ping", {"n": len(text)}, critical=False))
        if completed:
            out.append(Draft("turn_completed", {"reason": "end_turn"}))
    return out


async def write_and_replay(batches: list[list[Draft]]) -> tuple[ReducedState, bytes]:
    store = await SqliteStore.open()
    try:
        assert await store.create(THREAD, ROOT, NOW) == Ok(None)
        writer = await store.acquire(ROOT, "a", lambda: NOW)
        assert isinstance(writer, Ok)
        for batch in batches:
            assert isinstance(await writer.value.append(batch), Ok)
        read = await store.read(ROOT, NOW)
        assert isinstance(read, Ok)
        export = await store.export(ROOT)
    finally:
        await store.close()
    copy = await SqliteStore.open()
    try:
        verified = verify_export(export, NOW)
        assert isinstance(verified, Ok)
        assert verified.value.state == read.value.state
        assert await copy.import_log(verified.value) == Ok(None)
        assert await copy.export(ROOT) == export
    finally:
        await copy.close()
    return read.value.state, export


@settings(max_examples=40, deadline=None)
@given(st.lists(TURN, max_size=6).filter(lambda ts: all(done for _, _, done in ts[:-1])))
def test_reduce_counts_what_was_appended(turns: list[tuple[str, int, bool]]) -> None:
    script = drafts(turns)
    state, _ = asyncio.run(write_and_replay([script]))
    completed = sum(done for _, _, done in turns)
    assert state.turns_completed == completed
    assert state.head.seq == len(script)
    assert state.status == ("in_turn" if turns and not turns[-1][2] else "idle")
    users = [t for t in state.transcript if t.role == "user"]
    assert len(users) == sum(1 + steers for _, steers, _ in turns)


@settings(max_examples=20, deadline=None)
@given(st.lists(TURN, min_size=1, max_size=4).filter(lambda ts: all(d for _, _, d in ts)))
def test_batching_never_changes_the_state(turns: list[tuple[str, int, bool]]) -> None:
    """One transaction or one per event: the same reduced state (ids and times aside)."""
    script = drafts(turns)
    whole, _ = asyncio.run(write_and_replay([script]))
    split, _ = asyncio.run(write_and_replay([[d] for d in script]))
    assert (whole.turns_completed, whole.status, whole.head.seq) == (
        split.turns_completed,
        split.status,
        split.head.seq,
    )
    assert [t.role for t in whole.transcript] == [t.role for t in split.transcript]
