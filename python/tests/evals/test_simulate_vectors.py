"""spec/conformance/vectors (lane 32): the bytes the simulated user is sent, the output the runner
accepts from it, and a simulated case's judge input, as both runtimes must compute them."""

import json
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from threads.evals.judge import judge_input
from threads.evals.simulated_user import VisibleMessage, simulated_user_input, user_turn
from threads.log import Event

VECTORS = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "vectors"
_EVENTS = TypeAdapter[list[Event]](list[Event])
_OBJ = TypeAdapter[dict[str, JsonValue]](dict[str, JsonValue])


def _vectors(name: str) -> list[dict[str, JsonValue]]:
    doc = _OBJ.validate_json((VECTORS / name).read_bytes())
    items = doc["vectors"]
    assert isinstance(items, list)
    return [v for v in items if isinstance(v, dict)]


@pytest.mark.parametrize("v", _vectors("simulated-user-input.json"), ids=lambda v: str(v["name"]))
def test_simulated_user_input(v: dict[str, JsonValue]) -> None:
    given = v["given"]
    assert isinstance(given, dict)
    messages = given["messages"]
    assert isinstance(messages, list)
    seen: list[VisibleMessage] = []
    for m in messages:
        assert isinstance(m, dict)
        sender = str(m["from"])
        assert sender in ("user", "agent")
        seen.append(VisibleMessage("user" if sender == "user" else "agent", str(m["text"])))
    assert simulated_user_input(seen) == v["input"]


@pytest.mark.parametrize("v", _vectors("user-turn.json"), ids=lambda v: str(v["name"]))
def test_user_turn(v: dict[str, JsonValue]) -> None:
    got = user_turn(v["output"])
    assert ("simulator_invalid" if got is None else "accept") == v["expect"]


@pytest.mark.parametrize(
    "v", _vectors("judge-conversation-input.json"), ids=lambda v: str(v["name"])
)
def test_judge_conversation_input(v: dict[str, JsonValue]) -> None:
    given = v["given"]
    assert isinstance(given, dict)
    task, rubric = given["task"], given["rubric"]
    assert isinstance(task, str)
    assert isinstance(rubric, list)
    prefix_turns = given["prefix_turns"]
    assert isinstance(prefix_turns, int)
    goal = given.get("goal")
    events = _EVENTS.validate_json(json.dumps(given["events"]))
    got = judge_input(
        task,
        events,
        given["answer"],
        [str(r) for r in rubric],
        prefix_turns,
        str(goal) if isinstance(goal, str) else None,
    )
    assert got == v["input"]
