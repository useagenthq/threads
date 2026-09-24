"""Deferred tools at pin time (spec/schema/README.md, "Deferred tools and tool_search"): which
user tools `context.defer_tools` defers, their reference form, and the spec artifacts that must
be durable before the thread_started naming them."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads._tool_names import SEARCH
from threads.agents.config import ConfigError
from threads.hooks.extension import Namespaced
from threads.log import ToolSpec
from threads.log.digest import sha256_hex
from threads.log.jcs import canonicalize
from threads.reduce.handlers import to_json
from threads.result import Ok
from threads.tools.specs import search_tool_spec

type DeferTools = Literal["auto", "always", "never"]
_STUB = ("name", "description", "effect_class", "dedup_window_ms", "ends_turn")


@runtime_checkable
class Deferrable(Protocol):
    """A tool that can be deferred: `tool()`'s and an MCP server's."""

    @property
    def defer(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class Pinned:
    specs: tuple[ToolSpec, ...]
    """The user tools' specs, each deferred one in reference form."""
    artifacts: tuple[bytes, ...]
    """Each deferred tool's full spec, RFC 8785: put before thread_started."""
    search: ToolSpec | None
    """tool_search, when anything is deferred."""


def _deferred(tool: object, spec: ToolSpec, mode: DeferTools) -> bool:
    inner = tool.tool if isinstance(tool, Namespaced) else tool
    if mode == "never" or not isinstance(inner, Deferrable):
        return False
    return spec.ends_turn is MISSING if mode == "always" else inner.defer


def reference_form(spec: ToolSpec) -> tuple[ToolSpec, bytes]:
    """The stub pinned in thread_started and the artifact bytes its spec_ref names."""
    full = spec.model_copy(update={"defer_loading": MISSING, "spec_ref": MISSING})
    text = canonicalize(to_json(full))
    if not isinstance(text, Ok):
        raise AssertionError("a parsed spec always canonicalizes")
    raw = text.value.encode("utf-8")
    ref: JsonValue = {
        "sha256": sha256_hex(raw),
        "bytes": len(raw),
        "media_type": "application/json",
    }
    stub = {k: v for k, v in _object(to_json(spec)).items() if k in _STUB}
    return ToolSpec.model_validate({**stub, "defer_loading": True, "spec_ref": ref}), raw


def pinned_tools(tools: Sequence[tuple[object, ToolSpec]], mode: DeferTools) -> Pinned:
    """User tools (app, MCP and extension, each with its spec) under `mode`."""
    specs: list[ToolSpec] = []
    artifacts: list[bytes] = []
    deferred: list[str] = []
    for tool, spec in tools:
        if not _deferred(tool, spec, mode):
            specs.append(spec)
            continue
        stub, raw = reference_form(spec)
        specs.append(stub)
        artifacts.append(raw)
        deferred.append(spec.name)
    if deferred and any(s.name == SEARCH for s in specs):
        raise ConfigError(
            "invalid_config", f"tool {SEARCH}: the name is taken by the framework's tool_search"
        )
    search = search_tool_spec(deferred) if deferred else None
    return Pinned(tuple(specs), tuple(artifacts), search)


def _object(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise AssertionError("a spec is an object")
    return value
