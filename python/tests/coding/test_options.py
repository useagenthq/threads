"""The preset's option types are `agent()`'s, key for key, with `model` optional.

They have to be written out (a TypedDict can't un-require an inherited key), so this is what
stops them drifting: every preset TypedDict is compared with its `agent()` counterpart, and the
only difference allowed is `model`. The `assert_type` calls are the overloads: pyright strict
fails the suite if `coding_agent` stops narrowing the way `agent` does.
"""

from collections.abc import Collection
from dataclasses import dataclass
from typing import TypeGuard, assert_type

import pytest
from pydantic import BaseModel, JsonValue

from threads import RunContext, fake_sandbox, scripted_model, tool
from threads.agents.agent import Agent
from threads.agents.factory import (
    AgentOptions,
    OutputAgentOptions,
    ServerAgentOptions,
    ServerOutputAgentOptions,
    TeamAgentOptions,
    TeamOutputAgentOptions,
    TeamServerAgentOptions,
    TeamServerOutputAgentOptions,
    TeamToolAgentOptions,
    TeamToolOutputAgentOptions,
    ToolAgentOptions,
    ToolOutputAgentOptions,
)
from threads.agents.team_agent import TeamAgent
from threads.coding import (
    CodingAgentOptions,
    CodingOutputOptions,
    CodingServerOptions,
    CodingServerOutputOptions,
    CodingTeamOptions,
    CodingTeamOutputOptions,
    CodingTeamServerOptions,
    CodingTeamServerOutputOptions,
    CodingTeamToolOptions,
    CodingTeamToolOutputOptions,
    CodingToolOptions,
    CodingToolOutputOptions,
    coding_agent,
)
from threads.loop.model import Model

PAIRS = [
    (CodingAgentOptions, AgentOptions),
    (CodingServerOptions, ServerAgentOptions),
    (CodingToolOptions, ToolAgentOptions),
    (CodingOutputOptions, OutputAgentOptions),
    (CodingServerOutputOptions, ServerOutputAgentOptions),
    (CodingToolOutputOptions, ToolOutputAgentOptions),
    (CodingTeamOptions, TeamAgentOptions),
    (CodingTeamServerOptions, TeamServerAgentOptions),
    (CodingTeamToolOptions, TeamToolAgentOptions),
    (CodingTeamOutputOptions, TeamOutputAgentOptions),
    (CodingTeamServerOutputOptions, TeamServerOutputAgentOptions),
    (CodingTeamToolOutputOptions, TeamToolOutputAgentOptions),
]
IDS = [preset.__name__ for preset, _ in PAIRS]


def _collection(value: object) -> TypeGuard[Collection[object]]:
    return isinstance(value, frozenset | set | tuple | list)


def _names(value: object) -> frozenset[str]:
    """A TypedDict's key set, read the way the surface gate reads it."""
    if not _collection(value):
        raise AssertionError("a TypedDict carries its key sets")
    return frozenset(name for name in value if isinstance(name, str))


def _required(td: type) -> frozenset[str]:
    keys: object = getattr(td, "__required_keys__", None)
    return _names(keys)


def _keys(td: type) -> frozenset[str]:
    optional: object = getattr(td, "__optional_keys__", None)
    return _required(td) | _names(optional)


@pytest.mark.parametrize(("preset", "same"), PAIRS, ids=IDS)
def test_a_preset_option_set_is_agents_own(preset: type, same: type) -> None:
    assert _keys(preset) == _keys(same)
    # model is the one difference: the preset has a default, so it can be left out.
    assert _required(preset) == _required(same) - {"model"}
    assert "model" in _required(same)


class Answer(BaseModel):
    diff: str


@dataclass(frozen=True)
class Deps:
    user: str


class Question(BaseModel):
    q: str


def _model() -> Model:
    reply: JsonValue = {
        "content": [{"type": "text", "text": "done"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }
    return scripted_model({"responses": [reply]})


def test_the_overloads_output_narrows_tools_carry_deps_and_team_leads() -> None:
    async def ping(args: Question, ctx: RunContext[Deps]) -> str:
        return f"{args.q}:{ctx.deps.user}"

    asked = tool(
        name="ping",
        description="Ask the deps.",
        input=Question,
        runs="host",
        execute=ping,
        effect="read_only",
    )
    plain = coding_agent()
    assert_type(plain, Agent[None, str])
    with_deps = coding_agent(model=_model(), sandbox=fake_sandbox(), tools=[asked])
    assert_type(with_deps, Agent[Deps, str])
    typed = coding_agent(model=_model(), sandbox=fake_sandbox(), output=Answer)
    assert_type(typed, Agent[None, Answer])
    lead = coding_agent(model=_model(), sandbox=fake_sandbox(), team=[])
    assert_type(lead, TeamAgent[None, str])
    dropped = coding_agent(model=_model(), memory=None, sandbox=None)
    assert_type(dropped, Agent[None, str])
    assert [a.definition.name for a in (plain, with_deps, typed, lead, dropped)] == ["agent"] * 5
    # assert_type is static only; this is the one claim worth checking at run time too.
    assert isinstance(lead, TeamAgent)
    assert not isinstance(plain, TeamAgent)
