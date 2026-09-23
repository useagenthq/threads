"""Thread.replay (spec/api.json): every recorded request re-renders byte for byte from the log,
with no model call and no append; the first failure names its seq."""

import asyncio
from pathlib import Path

import pytest
from corpus import CASES, cases
from pydantic import JsonValue

from threads import agent, scripted_model
from threads._generated.host_api_v1 import SettingsChange
from threads.agents.store import Store, now_ms, open_store, sqlite
from threads.log import BranchId, ModelRef, ModelRequestEvent, ThreadId
from threads.loop.drafts import draft
from threads.loop.scripted import ScriptedModel
from threads.result import Err, Ok
from threads.store import verify_export
from threads.store.lines import uuid7
from threads.thread.control import LOCAL_OPERATOR
from threads.thread.handle import Thread, open_thread

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
STARTED: dict[str, JsonValue] = {
    "agent_name": "test",
    "config_hash": "0" * 64,
    "instructions": "Test.",
    "model": {"provider": "scripted", "name": "scripted-1"},
    "model_params": {"max_tokens": 1024},
    "adapter": {"name": "scripted", "version": "1", "settings": {}},
    "tools": [],
}


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


async def _recorded(store: Store, model: ScriptedModel | None = None) -> Thread:
    """Two turns with a user model change between them: two settings epochs."""
    model = model or scripted_model({"responses": [text("One."), text("Two.")]})
    bot = agent(model=model)
    first = await bot.run("hi", store=store)
    ref = ModelRef(provider="scripted", name="scripted-1")
    assert isinstance(await first.thread.set_model(SettingsChange(model=ref), LOCAL_OPERATOR), Ok)
    second = await bot.run("again", store=store, thread=first.thread)
    return second.thread


async def _requests(thread: Thread) -> list[ModelRequestEvent]:
    read = await (await open_store(thread.store)).read(thread.branch, now_ms())
    assert isinstance(read, Ok)
    return [e for e in read.value.fold.events if isinstance(e, ModelRequestEvent)]


def test_replay_reproduces_every_request_without_a_model_or_an_append(tmp_path: Path) -> None:
    async def main() -> None:
        store = sqlite(str(tmp_path))
        model = scripted_model({"responses": [text("One."), text("Two.")]})
        thread = await _recorded(store, model)
        head = (await _requests(thread))[-1].seq
        assert await thread.replay() == Ok(None)
        recorded_turns = 2
        assert len(model.sent) == recorded_turns  # replay sent nothing
        read = await (await open_store(store)).read(thread.branch, now_ms())
        assert isinstance(read, Ok)
        assert read.value.fold.seq == head + 2  # the response and turn_completed, nothing more

    asyncio.run(main())


def test_a_changed_or_missing_request_artifact_fails_at_its_seq(tmp_path: Path) -> None:
    async def main() -> None:
        store = sqlite(str(tmp_path))
        thread = await _recorded(store)
        first = (await _requests(thread))[0]
        path = tmp_path / "artifacts" / "sha256" / first.data.request_ref.sha256[:2]
        blob = path / first.data.request_ref.sha256
        blob.write_bytes(b"x" * first.data.request_ref.bytes)
        corrupt = await thread.replay()
        assert isinstance(corrupt, Err)
        assert (corrupt.error.code, corrupt.error.seq) == ("artifact_corrupt", first.seq)
        blob.unlink()
        missing = await thread.replay()
        assert isinstance(missing, Err)
        assert (missing.error.code, missing.error.seq) == ("artifact_missing", first.seq)

    asyncio.run(main())


@pytest.mark.parametrize(
    ("change", "code"),
    [("request_ref", "request_hash_mismatch"), ("declared_prefix", "prefix_changed")],
)
def test_bytes_the_running_code_renders_differently_fail(change: str, code: str) -> None:
    """A request whose recorded bytes aren't what this code renders stands in for an upgrade
    that renders differently: the writer doesn't re-render on append, replay does."""

    async def main() -> None:
        store = sqlite(":memory:")
        thread = await _recorded(store)
        last = (await _requests(thread))[-1]
        sq = await open_store(store)
        other = b'{"other":true}\n'
        sha = await sq.put_artifact(other)
        data = last.data.model_dump(mode="json", exclude={"attempt"})
        data["attempt"] = 1
        if change == "request_ref":
            data["request_ref"] = {
                "sha256": sha,
                "bytes": len(other),
                "media_type": "application/x-ndjson",
            }
        else:
            data["declared_prefix"] = {"bytes": 1, "sha256": sha}
        writer = await sq.acquire(thread.branch, "test", now_ms)
        assert isinstance(writer, Ok)
        appended = await writer.value.append([draft("model_request", data)])
        assert isinstance(appended, Ok)
        await writer.value.release()
        replayed = await thread.replay()
        assert isinstance(replayed, Err)
        assert (replayed.error.code, replayed.error.seq) == (code, appended.value[0].seq)

    asyncio.run(main())


def test_a_branch_with_no_request_replays() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        sq = await open_store(store)
        thread, branch = ThreadId(uuid7(now_ms())), BranchId(uuid7(now_ms()))
        assert await sq.create(thread, branch, now_ms()) == Ok(None)
        writer = await sq.acquire(branch, "test", now_ms)
        assert isinstance(writer, Ok)
        assert isinstance(await writer.value.append([draft("thread_started", STARTED)]), Ok)
        await writer.value.release()
        opened = await open_thread(store, thread)
        assert isinstance(opened, Ok)
        assert await opened.value.replay() == Ok(None)

    asyncio.run(main())


def _logs() -> list[Path]:
    """Every corpus log: each recover case's pair (one written under a threads-ts header)."""
    out: list[Path] = []
    for name in cases("render", "recover"):
        case = CASES / name
        out += sorted(case.glob("log*.jsonl"))
    return out


@pytest.mark.parametrize("log", _logs(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_a_log_either_implementation_wrote_replays(log: Path) -> None:
    """A store holding a log either implementation wrote replays Ok in Python."""
    verified = verify_export(log.read_bytes(), now_ms())
    if isinstance(verified, Err):
        pytest.skip("a deliberately broken log")

    async def main() -> None:
        store = sqlite(":memory:")
        sq = await open_store(store)
        for blob in sorted((log.parent / "artifacts").glob("*")):
            await sq.put_artifact(blob.read_bytes())
        if not isinstance(await sq.import_log(verified.value), Ok):
            return  # a case whose expected outcome is an import error
        header = verified.value.segments[-1].header
        opened = await open_thread(store, header.thread_id, branch_id=header.branch_id)
        assert isinstance(opened, Ok)
        assert await opened.value.replay() == Ok(None)

    asyncio.run(main())
