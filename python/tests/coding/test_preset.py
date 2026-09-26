"""What the preset promises (spec lane 31): it is `agent()` with five options chosen, so its pin
is an `agent()` pin, and every default is one option away.

Nothing here reaches Docker or a provider: the pin is drawn without setup (`dry_pin`), which is
also why no request can be dispatched.
"""

import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from threads import Agent, ConfigError, agent, fake_sandbox, local_memory, scripted_model
from threads.agents.definition import dry_pin
from threads.anthropic import anthropic
from threads.coding import CODING_INSTRUCTIONS, coding_agent
from threads.docker import docker
from threads.loop.model import Model
from threads.sandbox.protocol import Sandbox

# What the preset pins when nothing is passed, as both languages must pin it.
GOLDEN = Path(__file__).resolve().parents[3] / "spec/conformance/vectors/coding-preset.json"


DONE: JsonValue = {
    "content": [{"type": "text", "text": "done"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 2},
}


def scripted() -> Model:
    return scripted_model({"responses": [DONE]})


def written(model: Model, box: Sandbox) -> Agent[None, str]:
    """The hand-written agent() the preset must be indistinguishable from."""
    return agent(
        model=model,
        sandbox=box,
        permissions={"mode": "accept_edits", "allow": ["bash(*)"]},
        memory=local_memory(),
        instructions=CODING_INSTRUCTIONS,
    )


def pin_of[D, O](handle: Agent[D, O]) -> dict[str, JsonValue]:
    return dict(dry_pin(handle.definition).started)


def obj(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict), f"expected an object, got {value!r}"
    return value


def tool_names[D, O](handle: Agent[D, O]) -> list[str]:
    tools = pin_of(handle)["tools"]
    assert isinstance(tools, list)
    return sorted(str(obj(t)["name"]) for t in tools)


def permissions_of[D, O](handle: Agent[D, O]) -> dict[str, JsonValue]:
    return obj(obj(pin_of(handle)["policy"])["permissions"])


def test_its_pin_is_the_equivalent_agent_pin_config_hash_included() -> None:
    model, box = scripted(), fake_sandbox()
    preset = pin_of(coding_agent(model=model, sandbox=box))
    hand = pin_of(written(model, box))
    assert preset["config_hash"] == hand["config_hash"]
    # Byte-equal, not just equal-hashed: the ids a new thread mints are not in the dry pin.
    assert json.dumps(preset, sort_keys=True) == json.dumps(hand, sort_keys=True)


def test_the_defaults_are_claude_sonnet_5_docker_bash_any_and_local_memory() -> None:
    pin = pin_of(coding_agent())
    model = obj(pin["model"])
    assert (model["provider"], model["name"]) == ("anthropic", "claude-sonnet-5")
    permissions = permissions_of(coding_agent())
    assert permissions["mode"] == "accept_edits"
    assert permissions["allow"] == ["bash(*)"]
    # Kept, not replaced: the protected paths and the bypass refusal are still the built-ins.
    assert permissions["allow_bypass"] is False
    assert permissions["protected_paths"]
    assert pin["instructions"] == CODING_INSTRUCTIONS
    # Docker's provider and egress are hashed, not pin fields, so the hand-written agent() is
    # what says which sandbox the preset built. cpus and memory_mb are not pinned at all: the
    # live Docker job reads them back off the container it creates.
    hand = pin_of(written(anthropic("claude-sonnet-5"), docker(cpus=2, memory_mb=4096)))
    assert pin["config_hash"] == hand["config_hash"]
    assert json.dumps(pin, sort_keys=True) == json.dumps(hand, sort_keys=True)


def test_the_pinned_tools_are_the_sandbox_tools_memory_and_todo_write() -> None:
    assert tool_names(coding_agent(model=scripted(), sandbox=fake_sandbox())) == [
        "bash",
        "edit",
        "forget_memory",
        "glob",
        "grep",
        "ls",
        "notebook_edit",
        "read",
        "read_tool_result",
        "save_memory",
        "search_memory",
        "todo_write",
        "write",
    ]


def test_the_instructions_model_and_permissions_are_what_both_languages_pin() -> None:
    permissions = permissions_of(coding_agent())
    model = obj(pin_of(coding_agent())["model"])
    assert json.loads(GOLDEN.read_text()) == {
        "instructions": CODING_INSTRUCTIONS,
        "model": model["name"],
        "permissions": {"mode": permissions["mode"], "allow": permissions["allow"]},
    }


def test_permissions_replace_the_presets_object_bash_any_with_it() -> None:
    permissions = permissions_of(
        coding_agent(model=scripted(), sandbox=fake_sandbox(), permissions={"mode": "plan"})
    )
    assert (permissions["mode"], permissions["allow"]) == ("plan", [])


def test_instructions_replace_the_default_and_extending_keeps_it() -> None:
    extended = CODING_INSTRUCTIONS + "\n\nThe repo is acme/app."
    pin = pin_of(coding_agent(model=scripted(), sandbox=fake_sandbox(), instructions=extended))
    assert pin["instructions"] == extended


def test_memory_none_pins_no_memory_tools() -> None:
    names = tool_names(coding_agent(model=scripted(), sandbox=fake_sandbox(), memory=None))
    assert "save_memory" not in names
    assert "bash" in names


def test_sandbox_none_pins_no_sandbox_tools() -> None:
    names = tool_names(coding_agent(model=scripted(), sandbox=None))
    assert "bash" not in names
    assert "read" not in names
    assert "todo_write" in names


def test_a_model_override_is_the_model_that_is_pinned() -> None:
    model = obj(pin_of(coding_agent(model=scripted(), sandbox=fake_sandbox()))["model"])
    assert model["provider"] != "anthropic"


def test_an_egress_allowlist_is_refused_as_it_is_for_any_agent() -> None:
    # Python builds the definition in the factory, so this raises where TypeScript answers it
    # from check(); either way the preset adds nothing of its own to the refusal.
    with pytest.raises(ConfigError) as refused:
        coding_agent(model=scripted(), sandbox=fake_sandbox(), egress=["x.com"])
    assert refused.value.code == "egress_policy_unsupported"
