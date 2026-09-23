"""C5 on every path a resolved credential could be recorded by: a spilled exec output's full
bytes (redacted as they stream, a key split across chunks included), the references a result
injects, and every string of a result's parts, not only text."""

import asyncio
from pathlib import Path

from local_sandbox import LocalSandbox
from pydantic import JsonValue

from threads import Completed, agent, scripted_model, sqlite
from threads.log import BranchId, CitationPart, Permissions, TextPart, ThreadId, ToolResultEvent
from threads.loop.drafts import draft
from threads.loop.results import reference_drafts
from threads.loop.tools import Reference
from threads.redaction import StreamRedactor
from threads.reduce.handlers import to_json
from threads.result import Ok
from threads.secrets import credential
from threads.store import Draft
from threads.store.lines import Position, event_line, uuid7

USAGE: JsonValue = {"input_tokens": 1, "output_tokens": 1}
BYPASS = Permissions.model_validate(
    {
        "mode": "bypass",
        "allow": [],
        "ask": [],
        "deny": [],
        "protected_paths": [],
        "allow_bypass": True,
        "plan_exit_mode": "default",
    }
)
LABEL = "[secret fake.api_key]"


def use(name: str, args: JsonValue, call_id: str) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def say(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def test_a_key_split_across_chunks_is_redacted_as_the_stream_is_recorded() -> None:
    key = credential("fake", "api_key", "sk-lane09-split-7d2e", "UNUSED")()
    stream = StreamRedactor()
    raw = f"before {key} after".encode()
    written = b"".join(stream.feed(raw[i : i + 3]) for i in range(0, len(raw), 3))
    written += stream.end()
    assert written == f"before {LABEL} after".encode()


def test_a_spilled_exec_output_is_stored_redacted_and_read_back_redacted(tmp_path: Path) -> None:
    key = credential("fake", "api_key", "sk-lane09-42-spill", "UNUSED")()
    (tmp_path / "ws").mkdir()
    box = LocalSandbox(tmp_path / "ws")
    # The command builds the key inside the sandbox, so the call's own input never holds it.
    command = "head -c 40000 /dev/zero | tr '\\\\0' a; echo sk-lane09-$((6*7))-spill"
    read: JsonValue = {"call_id": "c1", "offset": 39_990, "length": 200}
    script: JsonValue = {
        "responses": [
            use("bash", {"command": command}, "c1"),
            use("read_tool_result", read, "c2"),
            say("done"),
        ]
    }
    store_dir = tmp_path / "store"
    bot = agent(model=scripted_model(script), sandbox=box, permissions=BYPASS)

    async def main() -> str:
        result = await bot.run("go", store=sqlite(str(store_dir)))
        assert isinstance(result, Completed)
        timeline = await result.thread.timeline()
        assert isinstance(timeline, Ok)
        (_, again) = [
            e.event for e in timeline.value.entries if isinstance(e.event, ToolResultEvent)
        ]
        return again.data.preview

    assert LABEL in asyncio.run(main())
    for stored in (p for p in store_dir.rglob("*") if p.is_file()):
        assert key.encode() not in stored.read_bytes(), stored


def test_injected_references_and_every_string_of_a_part_are_stored_redacted() -> None:
    key = credential("fake", "api_key", "sk-lane09-recall-3a9b", "UNUSED")()
    cited = CitationPart(
        type="citation",
        source_kind="web",
        source_id=f"https://example.test/?k={key}",
        title=f"title {key}",
        cited_text=f"cited {key}",
    )
    (recalled,) = reference_drafts([Reference("memory", "m1", "1", f"the key is {key}")])
    parts: list[JsonValue] = [
        to_json(TextPart(type="text", text=key)),
        to_json(cited),
    ]
    result = draft(
        "tool_result",
        {
            "call_id": "c1",
            "completeness": "complete",
            "is_error": False,
            "origin": "executed",
            "preview": key,
            "content": parts,
        },
    )
    stored = [stored_line(d) for d in (recalled, result)]
    assert all(key not in line for line in stored)
    assert f"title {LABEL}" in stored[1]
    assert f"the key is {LABEL}" in stored[0]


def stored_line(d: Draft) -> str:
    """The line the writer would store for `d`."""
    at = Position(ThreadId(uuid7(0)), BranchId(uuid7(1)), 1, 1, b"", 0)
    built = event_line(d, at)
    assert isinstance(built, Ok)
    return built.value[1].decode()
