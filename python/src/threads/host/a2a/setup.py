"""The `a2a` option checked at ready(): which agents may be exposed at all, and the approvers rule.

Every refusal is `ConfigError("invalid_config", ...)` with a message that names the fix. The lane
text named `invalid_option`, `handoff_not_exposable` and `unknown_agent`, none of which are
`ConfigError` codes; `invalid_config` is the established code for a wrong host config."""

from collections.abc import Mapping
from urllib.parse import urlsplit

from threads.agents.agent import Agent
from threads.agents.config import ConfigError
from threads.host.a2a.config import (
    DEFAULT_SCHEMES,
    A2aExposure,
    A2aOptions,
    Exposed,
    ExposedAgent,
    default_budget,
)
from threads.log import Budget


def expose_a2a(options: A2aOptions, agents: Mapping[str, Agent[None, object]]) -> Exposed:
    """The exposed map, or the ConfigError that says what to change."""
    base_url = _base_origin(options.get("base_url"))
    exposed: dict[str, ExposedAgent] = {}
    for name, exposure in options["expose"].items():
        found = agents.get(name)
        if found is None:
            raise ConfigError("invalid_config", f"a2a.expose.{name} names no host agent")
        exposed[name] = _checked(name, found, exposure)
    schemes = options.get("security_schemes") or DEFAULT_SCHEMES
    return Exposed(exposed, schemes, base_url)


def _base_origin(given: object) -> str:
    """The origin partners reach us at.

    A path, a query or a fragment is refused rather than dropped: the card builder appends
    `/a2a/<name>` to this, so a base URL carrying a path would publish endpoints the operator did
    not write. Checked rather than trusted to the type, because a host's options are a dict."""
    if not isinstance(given, str) or given == "":
        raise ConfigError(
            "invalid_config",
            "a2a.base_url is required: a card says where a partner sends work, and that cannot "
            "be read off a request",
        )
    parts = urlsplit(given)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigError(
            "invalid_config",
            f"a2a.base_url is a scheme and host, like https://agents.acme.example: {given}",
        )
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ConfigError(
            "invalid_config",
            f"a2a.base_url is a scheme and host only, with no path: {given}",
        )
    return f"{parts.scheme}://{parts.netloc}"


def _checked(name: str, agent: Agent[None, object], exposure: A2aExposure) -> ExposedAgent:
    definition = agent.definition
    if definition.handoffs:
        raise ConfigError(
            "invalid_config",
            f"a2a.expose.{name}: {name} has handoffs and can't be exposed over A2A; a handoff "
            "moves the conversation to another thread, which a remote task can't follow",
        )
    # Read the pinned specs, so a deferred or MCP tool counts as much as one written inline.
    acts = any(spec.effect_class != "read_only" for spec in definition.specs())
    if acts and definition.approvers is None:
        raise ConfigError(
            "invalid_config",
            f"a2a.expose.{name}: {name} is exposed over A2A and has actions that can need "
            f"approval; add approvers to agent '{definition.name}'",
        )
    pinned, _ = definition.pin()
    config_hash = pinned["config_hash"]
    if not isinstance(config_hash, str):
        raise AssertionError("a pin's config_hash is a string")
    description = exposure["description"]
    if description == "":
        raise ConfigError(
            "invalid_config", f"a2a.expose.{name} needs a description: the card requires one"
        )
    given = exposure.get("budget")
    return ExposedAgent(
        definition.name,
        description,
        default_budget() if given is None else Budget.model_validate(given),
        config_hash,
    )
