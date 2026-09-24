"""The size budget of deferral (lane 24, Tests 4): 500 deferred tools with ~1.5 KB schemas and ten
loads of three cost O(names) in the log, each request grows by only what it loads, and a second
thread of the agent writes no new artifact file."""

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field, JsonValue

from threads import Completed, RunContext, Tool, agent, scripted_model, sqlite, tool
from threads.adapters.models.render import parse
from threads.agents.pinned import pinned_start
from threads.agents.results import Thread
from threads.log import ArtifactRef, Event, ThreadStartedEvent, ToolsLoadedEvent
from threads.log.jcs import canonicalize
from threads.reduce.handlers import to_json
from threads.result import Ok

TOOLS = 500
LOADS = 10
PER_LOAD = 3
LOG_BUDGET = 16 * 1024
SLACK = 1024
USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}


class Wide(BaseModel):
    """A schema of about 1.5 KB, as a typical MCP tool's."""

    target: str = Field(description="What to act on. " + "Explain the target precisely. " * 45)


async def _run(_args: Wide, _ctx: RunContext[None]) -> str:
    return "ok"


def _tools() -> list[Tool[Wide, str, None]]:
    return [
        tool(
            name=f"bulk_{i:03d}",
            description=f"Bulk tool {i}.",
            input=Wide,
            execute=_run,
            defer=True,
        )
        for i in range(TOOLS)
    ]


def _search(n: int) -> JsonValue:
    names = ", ".join(f"bulk_{PER_LOAD * n + k:03d}" for k in range(PER_LOAD))
    use: JsonValue = {
        "type": "tool_use",
        "call_id": f"call_{n}",
        "name": "tool_search",
        "input": {"query": names},
    }
    return {"content": [use], "stop_reason": "tool_use", "usage": USAGE}


def _files(root: Path) -> list[Path]:
    return [p for p in (root / "artifacts" / "sha256").glob("*/*") if not p.name.startswith(".")]


async def _events(thread: Thread) -> list[Event]:
    timeline = await thread.timeline()
    assert isinstance(timeline, Ok)
    return [e.event for e in timeline.value.entries]


def _line_bytes(event: Event) -> int:
    text = canonicalize(to_json(event))
    assert isinstance(text, Ok)
    return len(text.value.encode("utf-8"))


def test_ten_loads_of_500_deferred_tools_stay_within_the_budget(tmp_path: Path) -> None:
    done: JsonValue = {
        "content": [{"type": "text", "text": "Done."}],
        "stop_reason": "end_turn",
        "usage": USAGE,
    }
    model = scripted_model({"responses": [*(_search(n) for n in range(LOADS)), done]})
    bot = agent(model=model, tools=_tools())
    store = sqlite(str(tmp_path))

    async def main() -> list[Event]:
        result = await bot.run("Load thirty tools.", store=store)
        assert isinstance(result, Completed), result
        return await _events(result.thread)

    log = asyncio.run(main())
    started = next(e for e in log if isinstance(e, ThreadStartedEvent))
    deferred = [t for t in started.data.tools if t.name.startswith("bulk_")]
    assert len(deferred) == TOOLS
    assert all("input_schema" not in t.model_dump(exclude_unset=True) for t in deferred)
    loads = [e for e in log if isinstance(e, ToolsLoadedEvent)]
    assert len(loads) == LOADS
    assert sum(_line_bytes(e) for e in loads) < LOG_BUDGET
    for before, after in zip(model.sent, model.sent[1:], strict=False):
        loaded = [line for line in after.body.splitlines() if b'"role":"tools_loaded"' in line]
        assert after.body.startswith(before.body)
        assert len(after.body) - len(before.body) <= len(loaded[-1]) + SLACK
    assert len(parse(model.sent[-1].body).tools) >= LOADS * PER_LOAD
    # One spec artifact per deferred tool, each named by the pin.
    shas = {t.spec_ref.sha256 for t in deferred if isinstance(t.spec_ref, ArtifactRef)}
    assert {p.name for p in _files(tmp_path)} >= shas
    assert len(shas) == TOOLS
    # A second thread of the same agent re-puts them: no new file and no new link.
    before = sorted(_files(tmp_path))
    asyncio.run(pinned_start(bot.definition, store))
    after = sorted(_files(tmp_path))
    assert after == before
    assert all(p.stat().st_nlink == 1 for p in after)
