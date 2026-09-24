"""`dynamic_agent()` (spec/api.json `dynamicAgent`): a template for team members the lead defines
at start, within what this code pins. Pure, like `agent()`."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Final, Required, Unpack

from pydantic import BaseModel

from threads.agents.bindings import AppTool, ToolServer
from threads.agents.config import ConfigError
from threads.agents.dynamic_agent import DynamicAgent
from threads.agents.factory import CommonOptions, Links, build_definition, output_model
from threads.loop.model import Model

_KEY: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REFUSED: Final = frozenset({"team", "team_limits", "subagents", "handoffs"})


class DynamicAgentOptions(CommonOptions, total=False):
    models: Required[Mapping[str, Model]]
    """The models a starter may choose from, by key, in order: the first is the default."""
    tools: Sequence[AppTool[None] | ToolServer]
    """The most a member may be given: a start chooses a subset."""
    output: type[BaseModel]
    """The structured final output every member returns."""


def dynamic_agent(**options: Unpack[DynamicAgentOptions]) -> DynamicAgent[None, object]:
    """spec/api.json `dynamicAgent`. Pure: no I/O. Raises ConfigError invalid_config for missing,
    empty or badly keyed models, and for team, subagents or handoffs."""
    given = set[str](options)
    if given & _REFUSED:
        why = "a dynamic agent can't start or hand off to other agents"
        raise ConfigError("invalid_config", why)
    if "model" in given:
        raise ConfigError("invalid_config", "a dynamic agent takes models, not model")
    models = tuple(options.get("models", {}).items())
    if not models:
        raise ConfigError("invalid_config", "a dynamic agent needs at least one of models")
    bad = next((k for k, _ in models if not _KEY.match(k)), None)
    if bad is not None:
        why = f"models key {bad!r}: lowercase letters, digits and underscores, from a letter"
        raise ConfigError("invalid_config", why)
    tools = options.get("tools", ())
    servers = tuple(t for t in tools if isinstance(t, ToolServer))
    own = tuple(t for t in tools if not isinstance(t, ToolServer))
    output = output_model(options.get("output"))
    template = build_definition(options, models[0][1], (own, servers), output, Links())
    return DynamicAgent(replace(template, models=models))
