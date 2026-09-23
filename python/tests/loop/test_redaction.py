"""C5 on every path a resolved credential could be recorded by: a spilled exec output's full
bytes (redacted as they stream, a key split across chunks included), the references a result
injects, and every string of a result's parts, not only text."""

import asyncio
from pathlib import Path

from local_sandbox import LocalSandbox
from pydantic import JsonValue

from threads import Completed, agent, scripted_model, sqlite
from threads.log import CitationPart, Permissions, TextPart, ToolResultEvent
from threads.loop.results import redacted_part, reference_drafts
from threads.loop.tools import Reference
from threads.result import Ok
from threads.secrets import StreamRedactor, credential

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


def test_injected_references_are_redacted() -> None:
    key = credential("fake", "api_key", "sk-lane09-recall-3a9b", "UNUSED")()
    (drafted,) = reference_drafts([Reference("memory", "m1", "1", f"the key is {key}")])
    assert key not in str(drafted)
    assert LABEL in str(drafted)


def test_every_string_of_a_part_is_redacted() -> None:
    key = credential("fake", "api_key", "sk-lane09-cite-5f10", "UNUSED")()
    cited = CitationPart(
        type="citation",
        source_kind="web",
        source_id=f"https://example.test/?k={key}",
        title=f"title {key}",
        cited_text=f"cited {key}",
    )
    shown = redacted_part(cited)
    assert isinstance(shown, CitationPart)
    assert key not in shown.model_dump_json()
    assert shown.title == f"title {LABEL}"
    assert redacted_part(TextPart(type="text", text=key)) == TextPart(type="text", text=LABEL)
