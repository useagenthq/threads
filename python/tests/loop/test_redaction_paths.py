"""C5, round 4: a value overlapping a longer one across stream chunks, model output, a compaction
summary, hook text, extension setup errors and the run's own input are all recorded redacted.
Every end-to-end case scans every file of a directory store and every request the model got."""

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, JsonValue

from threads import Completed, RunContext, agent, scripted_model, sqlite, tool
from threads.hooks.extension import Extension, extension
from threads.hooks.types import ToolGate
from threads.log import Context, Permissions, ToolCallData
from threads.loop.scripted import ScriptedModel
from threads.redaction import StreamRedactor
from threads.result import Err
from threads.secrets import credential

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


def say(reply: str, input_tokens: int = 1) -> JsonValue:
    usage: JsonValue = {"input_tokens": input_tokens, "output_tokens": 1}
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": usage}


def use(name: str) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": "c1", "name": name, "input": {}}
    usage: JsonValue = {"input_tokens": 1, "output_tokens": 1}
    return {"content": [part], "stop_reason": "tool_use", "usage": usage}


def nothing_holds(store: Path, model: ScriptedModel, key: str) -> None:
    """Every file of the store and every request the model got."""
    for stored in (p for p in store.rglob("*") if p.is_file()):
        assert key.encode() not in stored.read_bytes(), stored
    assert model.sent
    assert all(key.encode() not in r.body for r in model.sent)


def fed(stream: StreamRedactor, chunks: Sequence[bytes]) -> bytes:
    return b"".join(stream.feed(c) for c in chunks) + stream.end()


def test_a_value_that_a_longer_one_continues_is_held_until_decided() -> None:
    credential("short", "api_key", "abc-lane9", "U")()
    long = credential("long", "api_key", "abc-lane9-123", "U")()
    shown = fed(StreamRedactor(), [b"x abc-lane9", b"-123 y"])
    assert shown == b"x [secret long.api_key] y"
    assert long.encode() not in shown
    assert fed(StreamRedactor(), [b"x abc-lane9"]) == b"x [secret short.api_key]"


def test_a_value_split_inside_a_multi_byte_character_is_replaced() -> None:
    key = credential("fake", "api_key", "ключ-l9-секрет", "U")()
    raw = f"a {key} b".encode()
    shown = fed(StreamRedactor(), [raw[i : i + 1] for i in range(len(raw))])
    assert shown == b"a [secret fake.api_key] b"


def test_model_output_and_the_run_input_are_recorded_redacted(tmp_path: Path) -> None:
    key = credential("fake", "api_key", "sk-l9-echo-1a2b", "U")()
    model = scripted_model({"responses": [say(f"the key is {key}"), say("ok")]})
    bot = agent(model=model)
    store = tmp_path / "store"

    async def main() -> None:
        first = await bot.run(f"mine is {key}", store=sqlite(str(store)))
        assert isinstance(first, Completed)
        assert first.output == "the key is [secret fake.api_key]"
        again = await bot.run("more", store=sqlite(str(store)), thread=first.thread)
        assert isinstance(again, Completed)

    asyncio.run(main())
    nothing_holds(store, model, key)


def test_a_compaction_summary_is_recorded_redacted(tmp_path: Path) -> None:
    key = credential("fake", "api_key", "sk-l9-summary-2e3f", "U")()
    replies = [say("one", 1000), say(f"The key was {key}."), say("two")]
    model = scripted_model({"responses": replies})
    small = Context.model_validate(
        {
            "reserve_tokens": 0,
            "cache_ttl_ms": 300_000,
            "clear_results": {
                "trigger": {"tokens": 1_000_000},
                "keep_recent": 5,
                "exclude_tools": [],
            },
            "spill": {
                "threshold_bytes": 32768,
                "head_bytes": 2048,
                "tail_bytes": 1024,
                "request_budget_bytes": 204800,
            },
            "compact": {"trigger": {"tokens": 500}, "keep_tail": {"tokens": 1}, "max_failures": 3},
            "restore": {
                "max_files": 5,
                "file_tokens": 5000,
                "skill_tokens": 5000,
                "skills_total_tokens": 25000,
            },
            "max_output_continuations": 3,
            "defer_tools": "auto",
            "defer_threshold": {"permille": 100},
            "server_edits": "disabled",
        }
    )
    bot = agent(model=model, context=small)
    store = tmp_path / "store"

    async def main() -> None:
        first = await bot.run("first", store=sqlite(str(store)))
        assert isinstance(first, Completed)
        second = await bot.run("second", store=sqlite(str(store)), thread=first.thread)
        assert isinstance(second, Completed)

    asyncio.run(main())
    assert model.remaining == 0  # the summary request was made
    nothing_holds(store, model, key)


class NoInput(BaseModel):
    pass


def test_hook_text_is_recorded_redacted(tmp_path: Path) -> None:
    key = credential("fake", "api_key", "sk-l9-hook-5e6f", "U")()

    async def started(
        _source: Literal["startup", "resume", "fork", "compact"], _ctx: RunContext[None]
    ) -> Sequence[str]:
        return [f"context {key}"]

    async def gate(_call: ToolCallData, _ctx: RunContext[None]) -> ToolGate:
        raise RuntimeError(f"gate down {key}")

    async def noop(_args: NoInput, _ctx: RunContext[None]) -> str:
        return "ok"

    hooked: Extension[None] = extension(
        name="audit",
        hooks={"session_start": started, "before_tool": gate},
    )
    model = scripted_model({"responses": [use("noop"), say("ok")]})
    none = tool(name="noop", description="Nothing.", input=NoInput, execute=noop)
    bot = agent(model=model, tools=[none], extensions=[hooked], permissions=BYPASS)
    store = tmp_path / "store"
    assert isinstance(asyncio.run(bot.run("go", store=sqlite(str(store)), deps=None)), Completed)
    nothing_holds(store, model, key)


def test_an_extension_setup_error_is_returned_redacted() -> None:
    key = credential("fake", "api_key", "sk-l9-setup-7a8b", "U")()

    async def boot() -> None:
        raise RuntimeError(f"login failed for {key}")

    bot = agent(
        model=scripted_model({"responses": []}), extensions=[extension(name="boot", setup=boot)]
    )
    checked = asyncio.run(bot.check())
    assert isinstance(checked, Err)
    assert "login failed for [secret fake.api_key]" in checked.error.message
    assert key not in checked.error.message
