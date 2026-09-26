"""Thread.export and import_thread (spec/api.json; spec/schema/README.md, "Portable bundles"): a
bundle carries the chain and its artifacts, is published only when bundle.json lands, and is
validated whole before the destination store is touched."""

import asyncio
from pathlib import Path

from pydantic import JsonValue

from threads import agent, import_thread, scripted_model, sqlite
from threads.agents.store import Store, now_ms, open_store
from threads.log import ParseError
from threads.result import Err, Ok
from threads.store import LOCAL_TENANT, deletion
from threads.thread.handle import Thread, open_thread

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def use(call_id: str = "c1") -> JsonValue:
    part: JsonValue = {
        "type": "tool_use",
        "call_id": call_id,
        "name": "todo_write",
        "input": {"todos": []},
    }
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


async def saved(store: Store) -> Thread:
    """A thread with a tool call, two model requests and the artifacts they need."""
    bot = agent(model=scripted_model({"responses": [use(), text("Hi.")]}))
    result = await bot.run("hello", store=store)
    assert result.status == "completed", result.status
    opened = await open_thread(store, result.thread.id)
    assert isinstance(opened, Ok)
    return opened.value


async def branches(store: Store) -> int:
    """How many branches the store holds: a refused import leaves none."""
    sq = await open_store(store)
    rows = await sq.run(
        lambda c: c.execute("SELECT branch_id FROM branches").fetchall(), read_only=True
    )
    return len(rows)


def code(result: Ok[object] | Err[ParseError]) -> str:
    return "ok" if isinstance(result, Ok) else result.error.code


async def events(store: Store, branch: str) -> list[str]:
    read = await (await open_store(store)).read(branch, now_ms())  # pyright: ignore[reportArgumentType] - a BranchId
    assert isinstance(read, Ok)
    return [str(e.event_id) for e in read.value.fold.events]


def test_export_writes_a_bundle_import_thread_reproduces_the_thread(tmp_path: Path) -> None:
    async def main() -> None:
        source = sqlite(str(tmp_path / "source"))
        thread = await saved(source)
        path = tmp_path / "case"
        written = await thread.export(str(path))
        assert isinstance(written, Ok)
        assert written.value.branch_id == thread.branch
        assert written.value.artifacts > 0
        assert sorted(p.name for p in path.iterdir()) == ["artifacts", "bundle.json", "log.jsonl"]
        assert len(list((path / "artifacts").iterdir())) == written.value.artifacts

        into = sqlite(str(tmp_path / "into"))
        imported = await import_thread(into, str(path))
        assert isinstance(imported, Ok), imported
        assert (imported.value.id, imported.value.branch) == (thread.id, thread.branch)
        # Every recorded request re-renders from the bundle's own artifacts (C7, Render v1).
        assert await imported.value.replay() == Ok(None)
        assert await events(into, imported.value.branch) == await events(source, thread.branch)

    asyncio.run(main())


def test_an_existing_path_is_path_exists_and_is_left_untouched(tmp_path: Path) -> None:
    async def main() -> None:
        thread = await saved(sqlite(str(tmp_path / "source")))
        file = tmp_path / "a-file"
        file.write_text("keep me")
        empty = tmp_path / "empty"
        empty.mkdir()
        full = tmp_path / "full"
        full.mkdir()
        (full / "keep").write_text("keep me too")
        for path in (file, empty, full):
            assert code(await thread.export(str(path))) == "path_exists"
        assert file.read_text() == "keep me"
        assert list(empty.iterdir()) == []
        assert [p.name for p in full.iterdir()] == ["keep"]

    asyncio.run(main())


def test_two_exports_to_one_path_leave_one_bundle(tmp_path: Path) -> None:
    async def main() -> None:
        thread = await saved(sqlite(str(tmp_path / "source")))
        path = tmp_path / "case"
        first, second = await asyncio.gather(thread.export(str(path)), thread.export(str(path)))
        assert sorted([code(first), code(second)]) == ["ok", "path_exists"]
        assert (path / "bundle.json").is_file()

    asyncio.run(main())


def test_a_directory_without_bundle_json_is_bundle_incomplete(tmp_path: Path) -> None:
    async def main() -> None:
        thread = await saved(sqlite(str(tmp_path / "source")))
        path = tmp_path / "case"
        assert isinstance(await thread.export(str(path)), Ok)
        (path / "bundle.json").unlink()
        into = sqlite(str(tmp_path / "into"))
        assert code(await import_thread(into, str(path))) == "bundle_incomplete"
        assert await branches(into) == 0

    asyncio.run(main())


def test_a_tampered_or_missing_artifact_leaves_the_destination_empty(tmp_path: Path) -> None:
    async def main() -> None:
        thread = await saved(sqlite(str(tmp_path / "source")))
        path = tmp_path / "case"
        assert isinstance(await thread.export(str(path)), Ok)
        first = sorted((path / "artifacts").iterdir())[0]
        first.write_text("not the bytes that were exported")
        into = sqlite(str(tmp_path / "into"))
        assert code(await import_thread(into, str(path))) == "artifact_corrupt"
        assert await branches(into) == 0
        first.unlink()
        assert code(await import_thread(into, str(path))) == "bundle_incomplete"
        assert await branches(into) == 0

    asyncio.run(main())


def test_a_bare_jsonl_without_its_artifacts_is_artifact_missing(tmp_path: Path) -> None:
    async def main() -> None:
        thread = await saved(sqlite(str(tmp_path / "source")))
        path = tmp_path / "case"
        assert isinstance(await thread.export(str(path)), Ok)
        bare = tmp_path / "log.jsonl"
        bare.write_bytes((path / "log.jsonl").read_bytes())
        into = sqlite(str(tmp_path / "into"))
        assert code(await import_thread(into, str(bare))) == "artifact_missing"
        assert await branches(into) == 0

    asyncio.run(main())


def test_export_delete_import_never_brings_a_deleted_thread_back(tmp_path: Path) -> None:
    async def main() -> None:
        store = sqlite(str(tmp_path / "source"))
        thread = await saved(store)
        path = tmp_path / "case"
        assert isinstance(await thread.export(str(path)), Ok)
        sq = await open_store(store)
        removed = await sq.run(
            lambda c: deletion.delete_thread(c, LOCAL_TENANT, thread.id, now_ms())
        )
        assert isinstance(removed, Ok), removed
        back = await import_thread(store, str(path))
        assert code(back) == "branch_exists"
        assert isinstance(back, Err)
        assert "was deleted" in back.error.message
        assert await branches(store) == 0

    asyncio.run(main())


def test_importing_the_same_bundle_twice_is_idempotent(tmp_path: Path) -> None:
    async def main() -> None:
        thread = await saved(sqlite(str(tmp_path / "source")))
        path = tmp_path / "case"
        assert isinstance(await thread.export(str(path)), Ok)
        into = sqlite(str(tmp_path / "into"))
        first = await import_thread(into, str(path))
        again = await import_thread(into, str(path))
        assert isinstance(first, Ok)
        assert isinstance(again, Ok)
        assert again.value.branch == first.value.branch
        assert await branches(into) == 1

    asyncio.run(main())
