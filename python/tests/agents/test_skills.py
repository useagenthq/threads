"""Skills: host-pinned, listed in line 0, loaded on demand by
load_skill, and pinned in config_hash. A run produces what the F4 conformance cases record."""

import asyncio
import json

import pytest
from corpus import CASES
from pydantic import JsonValue

from threads import Completed, ConfigError, Skill, agent, scripted_model, sqlite
from threads.log import Event, InjectedEvent, ThreadStartedEvent, ToolResultEvent
from threads.reduce.handlers import to_json
from threads.result import Ok

NONE: dict[str, JsonValue] = {"responses": []}
USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
DONE: JsonValue = {
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": USAGE,
}
SKILLS = (
    Skill(
        "deploy", "Deploy the app to staging.", "Run make deploy ENV=staging, then check /health."
    ),
    Skill("review", "Review a diff for bugs.", "Read the whole diff before commenting."),
)


def _case_lines(case: str) -> list[dict[str, JsonValue]]:
    text = (CASES / case / "log.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()[1:]]


def _case_event(case: str, kind: str) -> dict[str, JsonValue]:
    line = next(e for e in _case_lines(case) if e.get("type") == kind)
    data = line["data"]
    assert isinstance(data, dict)
    return data


def _load(name: str) -> JsonValue:
    use: JsonValue = {
        "type": "tool_use",
        "call_id": "call_1",
        "name": "load_skill",
        "input": {"name": name},
    }
    return {"content": [use], "stop_reason": "tool_use", "usage": USAGE}


async def _run(name: str, skills: tuple[Skill, ...] = SKILLS) -> list[Event]:
    bot = agent(
        model=scripted_model({"responses": [_load(name), DONE]}),
        instructions="You are a helpful agent.",
        skills=skills,
    )
    result = await bot.run("Deploy the app.", store=sqlite(":memory:"))
    assert isinstance(result, Completed)
    timeline = await result.thread.timeline()
    assert isinstance(timeline, Ok)
    return [e.event for e in timeline.value.entries]


def test_a_skill_name_must_be_a_wire_name() -> None:
    with pytest.raises(ConfigError) as caught:
        agent(model=scripted_model(NONE), skills=[Skill("Bad-Name", "d", "b")])
    assert caught.value.code == "invalid_config"


@pytest.mark.parametrize("description", ["", "two\nlines", "   "])
def test_a_description_is_one_non_empty_line(description: str) -> None:
    with pytest.raises(ConfigError) as caught:
        agent(model=scripted_model(NONE), skills=[Skill("review", description, "b")])
    assert caught.value.code == "invalid_config"


def test_skill_names_are_unique() -> None:
    twice = [Skill("review", "a", "x"), Skill("review", "b", "y")]
    with pytest.raises(ConfigError) as caught:
        agent(model=scripted_model(NONE), skills=twice)
    assert caught.value.code == "duplicate_name"


def test_load_skill_is_pinned_only_with_skills() -> None:
    bare = agent(model=scripted_model(NONE))
    assert "load_skill" not in {s.name for s in bare.definition.specs()}
    assert "Skills you can load" not in bare.definition.full_instructions


def test_config_hash_pins_each_skill_body() -> None:
    def pinned(skills: tuple[Skill, ...]) -> JsonValue:
        bot = agent(model=scripted_model(NONE), skills=skills)
        return bot.definition.thread_started()["config_hash"]

    changed = (SKILLS[0], Skill("review", SKILLS[1].description, "Approve everything."))
    assert pinned(SKILLS) != pinned(changed)
    assert pinned(SKILLS) == pinned(SKILLS)


def test_a_run_lists_then_loads_as_the_case_records() -> None:
    events = asyncio.run(_run("deploy"))
    started = next(e for e in events if isinstance(e, ThreadStartedEvent))
    want = _case_event("skills-listing-then-load", "thread_started")
    assert started.data.instructions == want["instructions"]
    tools = want["tools"]
    assert isinstance(tools, list)
    load = [to_json(t) for t in started.data.tools if t.name == "load_skill"]
    assert load == [t for t in tools if isinstance(t, dict) and t["name"] == "load_skill"]
    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert (result.data.preview, result.data.is_error) == ("loaded skill deploy", False)
    injected = [e for e in events if isinstance(e, InjectedEvent)]
    assert [to_json(e.data) for e in injected] == [
        _case_event("skills-listing-then-load", "injected")
    ]


def test_an_unlisted_skill_is_not_found_and_injects_nothing() -> None:
    events = asyncio.run(_run("evil"))
    result = next(e for e in events if isinstance(e, ToolResultEvent))
    want = [
        e["data"]
        for e in _case_lines("skills-repo-file-not-trusted")
        if e.get("type") == "tool_result"
    ][-1]
    assert isinstance(want, dict)
    assert (result.data.preview, result.data.is_error) == (want["preview"], True)
    assert not any(isinstance(e, InjectedEvent) for e in events)
