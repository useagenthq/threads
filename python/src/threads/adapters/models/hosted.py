"""Provider-hosted tools. The provider runs them inside one model attempt, so
only tools that are read-only toward the outside world are allowed: web search and web fetch.
They are declared in line 0 adapter settings, so the declared prefix pins them, and every use
is recorded as a `hosted_tool` part holding the exact provider block. Anything else (code
execution, file search, hosted MCP, computer use, image generation) is refused at setup."""

from collections.abc import Callable, Mapping, Sequence

from pydantic import JsonValue

from threads.agents.config import ConfigError

type HostedTool = Mapping[str, JsonValue]


def declare(
    tools: Sequence[HostedTool], allowed: Callable[[str], bool], name_key: str
) -> tuple[dict[str, JsonValue], tuple[str, ...]]:
    """The adapter settings that pin `tools`, and their names for `ModelInfo.hosted_tools`.
    Raises `hosted_tool_unsupported` for a tool outside the adapter's allowlist."""
    names: list[str] = []
    for tool in tools:
        kind = tool.get("type")
        if not isinstance(kind, str) or not allowed(kind):
            raise ConfigError(
                "hosted_tool_unsupported",
                f"hosted tool {kind!r} may act outside the log; only web search and fetch run "
                "hosted",
            )
        name = tool.get(name_key)
        names.append(name if isinstance(name, str) else kind)
    settings: dict[str, JsonValue] = {"hosted_tools": [dict(t) for t in tools]} if tools else {}
    return settings, tuple(names)


def pinned(settings: Mapping[str, JsonValue]) -> list[JsonValue]:
    """The hosted tool declarations line 0 pins, sent as recorded. A bare name (a test
    adapter's declaration) is no provider tool and is not sent."""
    hosted = settings.get("hosted_tools")
    return [t for t in hosted if isinstance(t, dict)] if isinstance(hosted, list) else []
