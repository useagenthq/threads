"""`extension()` (spec/api.json): the one extension primitive. Trusted host code,
not a security boundary. Hooks and observers come only through it."""

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Required, TypedDict, Unpack

from pydantic import JsonValue
from typing_extensions import TypeVar

from threads.agents.bindings import AppTool
from threads.agents.config import ConfigError
from threads.agents.context import RunContext
from threads.hooks.runner import Bound, Call, HookRunner
from threads.hooks.types import HookName, Hooks, wire_name
from threads.log import Event, JsonObject, Origin, ToolSpec
from threads.loop.model import LookupResult
from threads.loop.tools import Dispatched

type Observer = Callable[[Event], Awaitable[None]]

D = TypeVar("D", default=None)
"""The deps an extension's hooks and tools read. PEP 696's default (typing_extensions, which
pydantic already depends on): an extension with no hooks or tools is Extension[None], never
Extension[Unknown]."""

_NAME: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
DEFAULT_TIMEOUT_MS: Final = 5000


class ExtensionOptions[D](TypedDict, total=False):
    name: Required[str]
    tools: Sequence[AppTool[D]]
    """Namespaced `<name>__<tool>`. Like hooks, they get the run's deps."""
    instructions: str
    hooks: Hooks[D]
    on: Mapping[str, Observer]
    setup: Callable[[], Awaitable[None]]
    hook_timeout_ms: int


# No slots: the setup memory holds an extension weakly, and before 3.12.4 a slotted generic
# dataclass drops its weakref slot (CPython gh-118033), so every run failed setup.
@dataclass(frozen=True)
class Extension[D]:
    """spec/api.json `Extension`. Build it with `extension()`. D is the deps its hooks and tools
    read: the agent's Deps, None when they read none."""

    name: str
    instructions: str = ""
    hooks: Hooks[D] = field(default_factory=Hooks[D])
    on: Mapping[str, Observer] = field(default_factory=dict[str, Observer])
    setup: Callable[[], Awaitable[None]] | None = None
    hook_timeout_ms: int = DEFAULT_TIMEOUT_MS
    tools: tuple[AppTool[D], ...] = ()

    def manifest(self) -> JsonValue:
        """What the config pin covers: which hooks this extension declares, in wire-enum order,
        its observers, sorted, and its timeout. Code is identified by the host, not the
        log; the pin makes a changed hook set a changed config."""
        hooks: list[JsonValue] = [wire_name(h) for h in _names(self.hooks)]
        observers: list[JsonValue] = [*sorted(self.on)]
        return {
            "name": self.name,
            "hooks": hooks,
            "observers": observers,
            "hook_timeout_ms": self.hook_timeout_ms,
        }


@dataclass(frozen=True, slots=True)
class Namespaced[D]:
    """An extension's tool under its pinned `<ext>__<tool>` name."""

    name: str
    tool: AppTool[D]
    extension: str

    def spec(self) -> ToolSpec:
        # Not model-visible: line 0 leaves the origin out, config_hash covers it (drift reads it).
        origin = Origin(extension=self.extension)
        return self.tool.spec().model_copy(update={"name": self.name, "origin": origin})

    def inner(self) -> object:
        """The extension's own tool, whatever deps it reads (what deferral looks at)."""
        return self.tool

    def invalid(self, input: JsonObject) -> str | None:
        return self.tool.invalid(input)

    async def run(self, input: JsonObject, ctx: RunContext[D]) -> Dispatched:
        return await self.tool.run(input, ctx)

    async def lookup(self, effect_key: str, ctx: RunContext[D]) -> LookupResult[str]:
        return await self.tool.lookup(effect_key, ctx)


def extension_tools[D](extensions: Sequence[Extension[D]]) -> tuple[Namespaced[D], ...]:
    """Every extension's tools, namespaced and sorted by that name."""
    tools = [Namespaced(f"{e.name}__{t.name}", t, e.name) for e in extensions for t in e.tools]
    return tuple(sorted(tools, key=lambda t: t.name))


def _names[D](hooks: Hooks[D]) -> list[HookName]:
    return [name for name in _ALL if name in hooks]


_ALL: Final[tuple[HookName, ...]] = (
    "session_start",
    "session_end",
    "before_input",
    "before_model",
    "after_model",
    "before_tool",
    "permission_request",
    "permission_denied",
    "after_tool",
    "before_tool_result",
    "after_tool_batch",
    "before_compact",
    "after_compact",
    "on_stop",
    "on_stop_failure",
    "subagent_start",
    "subagent_stop",
    "before_model_switch",
    "after_model_switch",
    "notification",
)


def extension(**options: Unpack[ExtensionOptions[D]]) -> Extension[D]:
    """spec/api.json `extension`. Pure. Raises ConfigError for a name the wire can't hold or a
    non-positive timeout."""
    name = options["name"]
    if _NAME.fullmatch(name) is None:
        raise ConfigError("invalid_config", f"extension name {name!r} is not a wire Name")
    timeout = options.get("hook_timeout_ms", DEFAULT_TIMEOUT_MS)
    if timeout <= 0:
        raise ConfigError("invalid_config", f"{name}: hook_timeout_ms must be positive")
    return Extension(
        name,
        options.get("instructions", ""),
        options.get("hooks", Hooks[D]()),
        dict(options.get("on", {})),
        options.get("setup"),
        timeout,
        tuple(options.get("tools", ())),
    )


def bind[D](extensions: Sequence[Extension[D]], ctx: RunContext[D]) -> HookRunner:
    """The run's hooks, each with its context bound (spec/api.json `RunContext`)."""
    return HookRunner(
        [
            Bound(
                e.name,
                e.hook_timeout_ms,
                {name: _with(fn, ctx) for name in _ALL if (fn := e.hooks.get(name)) is not None},
            )
            for e in extensions
        ]
    )


def _with(fn: Call, ctx: object) -> Call:
    async def call(*args: object) -> object:
        return await fn(*args, ctx)

    return call
