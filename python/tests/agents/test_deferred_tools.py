"""Deferred tools at pin time (spec/api.json `tool.defer`, `mcp.defer`, `agent.context`): the
reference form, tool_search's pinned spec, defer_tools and what children inherit."""

import pytest
from pydantic import BaseModel, Field, JsonValue

from threads import ConfigError, RunContext, Tool, agent, scripted_model, tool
from threads.agents.deferral import reference_form
from threads.log import ArtifactRef, ToolSpec
from threads.log.digest import sha256_hex
from threads.loop.defaults import CONTEXT
from threads.tools.specs import search_tool_spec

NO_MODEL: dict[str, JsonValue] = {"responses": []}


class Refund(BaseModel):
    charge: str


class Wider(BaseModel):
    charge: str
    reason: str = Field(description="Why.")


async def _run(_args: BaseModel, _ctx: RunContext[None]) -> str:
    return "ok"


def _tool(
    name: str = "refund", *, defer: bool = False, wide: bool = False
) -> Tool[BaseModel, str, None]:
    return tool(
        name=name,
        description="Refund a charge.\nReturns the refund id.",
        input=Wider if wide else Refund,
        execute=_run,
        defer=defer,
    )


def test_a_deferred_tool_is_pinned_in_reference_form_with_its_artifact() -> None:
    bot = agent(model=scripted_model(NO_MODEL), tools=[_tool(defer=True)])
    specs = {s.name: s for s in bot.definition.specs()}
    stub = specs["refund"]
    assert stub.defer_loading is True
    assert "input_schema" not in stub.model_dump(exclude_unset=True)
    (artifact,) = bot.definition.spec_artifacts()
    assert isinstance(stub.spec_ref, ArtifactRef)
    assert stub.spec_ref.sha256 == sha256_hex(artifact)
    full = ToolSpec.model_validate_json(artifact)
    assert full == _tool().spec()
    assert specs["tool_search"] == search_tool_spec(["refund"])
    assert specs["tool_search"].description.endswith("Deferred tools (search to load): refund")
    started = bot.definition.thread_started()
    tools = started["tools"]
    assert isinstance(tools, list)
    assert [t["name"] for t in tools if isinstance(t, dict)].index("tool_search") < [
        t["name"] for t in tools if isinstance(t, dict)
    ].index("refund")


def test_no_deferral_pins_no_tool_search_and_the_same_config() -> None:
    plain = agent(model=scripted_model(NO_MODEL), tools=[_tool()])
    assert "tool_search" not in {s.name for s in plain.definition.specs()}
    assert plain.definition.spec_artifacts() == ()
    never = agent(
        model=scripted_model(NO_MODEL),
        tools=[_tool(defer=True)],
        context=CONTEXT.model_copy(update={"defer_tools": "never"}),
    )
    assert "tool_search" not in {s.name for s in never.definition.specs()}
    assert never.definition.specs()[-1].defer_loading is not True


def test_always_defers_every_user_tool() -> None:
    always = agent(
        model=scripted_model(NO_MODEL),
        tools=[_tool(), _tool("other")],
        context=CONTEXT.model_copy(update={"defer_tools": "always"}),
    )
    specs = {s.name: s for s in always.definition.specs()}
    assert specs["refund"].defer_loading is True
    assert specs["other"].defer_loading is True
    assert specs["todo_write"].defer_loading is not True
    artifacts = always.definition.spec_artifacts()
    assert [ToolSpec.model_validate_json(a).name for a in artifacts] == ["refund", "other"]


def test_a_changed_schema_changes_spec_ref_and_config_hash() -> None:
    def pinned(wide: bool) -> tuple[JsonValue, JsonValue]:
        bot = agent(model=scripted_model(NO_MODEL), tools=[_tool(defer=True, wide=wide)])
        started = bot.definition.thread_started()
        tools = started["tools"]
        assert isinstance(tools, list)
        stub = next(t for t in tools if isinstance(t, dict) and t["name"] == "refund")
        assert isinstance(stub, dict)
        return stub["spec_ref"], started["config_hash"]

    (ref_a, hash_a), (ref_b, hash_b) = pinned(False), pinned(True)
    assert ref_a != ref_b
    assert hash_a != hash_b


def test_setup_errors_name_the_tool() -> None:
    with pytest.raises(ConfigError, match="tool refund: defer can't be combined with ends_turn"):
        tool(
            name="refund",
            description="R.",
            input=Refund,
            execute=_run,
            defer=True,
            ends_turn=True,
        )
    with pytest.raises(ConfigError, match="tool tool_search") as taken:
        agent(model=scripted_model(NO_MODEL), tools=[_tool(defer=True), _tool("tool_search")])
    assert taken.value.code == "invalid_config"


def test_reference_form_keeps_the_effect_fields() -> None:
    spec = ToolSpec.model_validate(
        {
            "name": "charge",
            "description": "Charge.",
            "input_schema": {"type": "object"},
            "effect_class": "idempotent",
            "dedup_window_ms": 1000,
        }
    )
    stub, raw = reference_form(spec)
    assert (stub.effect_class, stub.dedup_window_ms) == ("idempotent", 1000)
    assert ToolSpec.model_validate_json(raw) == spec


def test_children_inherit_the_parent_defer_tools_unless_they_set_it() -> None:
    always = CONTEXT.model_copy(update={"defer_tools": "always"})
    child = agent(name="child", model=scripted_model(NO_MODEL), tools=[_tool("lookup")])
    own = agent(
        name="own",
        model=scripted_model(NO_MODEL),
        tools=[_tool("lookup")],
        context=CONTEXT.model_copy(update={"defer_tools": "never"}),
    )
    target = agent(name="target", model=scripted_model(NO_MODEL), tools=[_tool("lookup")])
    member = agent(name="member", model=scripted_model(NO_MODEL), tools=[_tool("lookup")])
    lead = agent(
        name="lead",
        model=scripted_model(NO_MODEL),
        tools=[_tool("lookup")],
        context=always,
        subagents=[child, own],
        handoffs=[target],
        team=[member],
    )
    d = lead.definition
    inherited = [*d.subagents, *d.handoffs, *(d.team or ())]
    by_name = {c.name: c for c in inherited}
    for name in ("child", "target", "member"):
        assert by_name[name].defer_tools() == "always"
        policy = by_name[name].policy()
        context = policy["context"]
        assert isinstance(context, dict)
        assert context["defer_tools"] == "always"
        names = {s.name for s in by_name[name].specs()}
        assert "tool_search" in names  # its own, from its own deferred set
    assert by_name["own"].defer_tools() == "never"
    assert "tool_search" not in {s.name for s in by_name["own"].specs()}


def test_a_partial_child_context_without_defer_tools_still_inherits_it() -> None:
    """As in TypeScript: only a defer_tools the child names keeps it from inheriting."""
    child = agent(
        name="child",
        model=scripted_model(NO_MODEL),
        tools=[_tool("lookup")],
        context={"reserve_tokens": 1234},
    )
    named = agent(
        name="named",
        model=scripted_model(NO_MODEL),
        tools=[_tool("lookup")],
        context={"defer_tools": "never"},
    )
    lead = agent(
        name="lead",
        model=scripted_model(NO_MODEL),
        tools=[_tool("lookup")],
        context=CONTEXT.model_copy(update={"defer_tools": "always"}),
        subagents=[child, named],
    )
    by_name = {c.name: c for c in lead.definition.subagents}
    assert by_name["child"].defer_tools() == "always"
    context = by_name["child"].policy()["context"]
    assert isinstance(context, dict)
    assert (context["defer_tools"], context["reserve_tokens"]) == ("always", 1234)
    assert by_name["named"].defer_tools() == "never"
