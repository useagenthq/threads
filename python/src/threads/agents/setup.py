"""Setup: what `Agent.check()` or the first run resolves before anything is pinned.

Extension setups, then each adapter's `setup` (credentials and configuration: model, sandbox,
memory, knowledge), then the same for every agent this one may start. A setup is remembered
per object on success only, so a failure is retried by the next check() or run, and an adapter
shared by a parent and its subagent is set up once. Setup opens no connection and makes no
client: those belong to a run, on the run's own event loop. MCP is not part of it either: each
check() and each run opens its own sessions.
"""

import contextlib
import weakref
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Protocol, runtime_checkable

from threads.agents.config import ConfigError
from threads.agents.definition import Definition
from threads.hooks.extension import Extension


@runtime_checkable
class SetsUp(Protocol):
    """spec/api.json `setup`, the optional capability of a `Model`, `Sandbox`,
    `MemoryProvider` or `KnowledgeProvider`: resolve credentials and check configuration on the
    host. Raises `ConfigError`; creates no connection or client."""

    async def setup(self) -> None: ...


_READY: weakref.WeakValueDictionary[int, object] = weakref.WeakValueDictionary()
"""Objects whose setup succeeded, by identity, held weakly: an entry goes when its object does,
so a long-running host that builds agents per request keeps nothing alive, and a reused id
never matches a dead object."""


async def _once(target: object, setup: Callable[[], Awaitable[None]]) -> None:
    if _READY.get(id(target)) is target:
        return
    await setup()
    # An object without weak references (a custom adapter with __slots__) is simply set up
    # again next time; setup is idempotent by contract.
    with contextlib.suppress(TypeError):
        _READY[id(target)] = target


async def _extension(e: Extension) -> None:
    if e.setup is None:
        return
    try:
        await e.setup()
    except Exception as error:
        raise ConfigError("invalid_config", f"extension {e.name}: setup failed: {error}") from error


async def set_up[D](definition: Definition[D]) -> None:
    """Sets up `definition` and every agent it may start. Raises `ConfigError`."""
    for e in definition.extensions:
        await _once(e, partial(_extension, e))
    for adapter in (definition.model, definition.sandbox, definition.memory, definition.knowledge):
        if isinstance(adapter, SetsUp):
            await _once(adapter, adapter.setup)
    for child in (*definition.subagents, *definition.handoffs):
        await set_up(child)
