"""Setup: what `Agent.check()` or the first run resolves before anything is pinned.

Extension setups, then each adapter's `setup` (credentials and configuration: model, sandbox,
memory, knowledge), then the same for every agent this one may start. A setup is remembered
per object on success only, so a failure is retried by the next check() or run, and an adapter
shared by a parent and its subagent is set up once. Setup opens no connection and makes no
client: those belong to a run, on the run's own event loop. MCP is not part of it either: each
check() and each run opens its own sessions.
"""

import asyncio
import weakref
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Protocol, runtime_checkable

from threads.agents.config import ConfigError, ConfigErrorCode
from threads.agents.definition import Definition
from threads.hooks.extension import Extension
from threads.redaction import redact_secrets


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

_RUNNING: dict[int, asyncio.Future[None]] = {}
"""Setups in flight, by identity: a concurrent check() or run waits for the same attempt."""


async def _once(target: object, setup: Callable[[], Awaitable[None]]) -> None:
    """Runs `setup` once per object on success. A caller that finds an attempt in flight waits
    for it: its failure is theirs too; if it was cancelled, the next caller tries again."""
    while _READY.get(id(target)) is not target:
        running = _RUNNING.get(id(target))
        if running is None:
            await _attempt(target, setup)
        else:
            await asyncio.shield(running)


async def _attempt(target: object, setup: Callable[[], Awaitable[None]]) -> None:
    _weakly_held(target)
    key = id(target)
    outcome = asyncio.get_running_loop().create_future()
    _RUNNING[key] = outcome
    try:
        await redacted(setup)
        _READY[key] = target
    except Exception as error:
        outcome.set_exception(error)
        outcome.exception()  # marked retrieved: waiters re-raise it, and there may be none
        raise
    finally:
        del _RUNNING[key]
        if not outcome.done():
            outcome.set_result(None)  # done or cancelled: waiters look at _READY again


async def redacted(setup: Callable[[], Awaitable[object]]) -> None:
    """Runs `setup`. Whatever it raises is what check() returns and a run raises: always a
    ConfigError, its message redacted (C5); an unexpected exception is invalid_config."""
    try:
        await setup()
    except Exception as error:
        raise redacted_error(error, "invalid_config", "adapter setup failed") from None


def redacted_error(error: Exception, fallback: ConfigErrorCode, what: str) -> ConfigError:
    """`error` as a ConfigError with its message redacted; an unexpected one becomes
    `fallback`, naming what failed."""
    if isinstance(error, ConfigError):
        return ConfigError(error.code, redact_secrets(error.message))
    return ConfigError(fallback, redact_secrets(f"{what}: {error}"))


def _weakly_held(target: object) -> None:
    """The setup memory holds objects weakly, so one without weak references can't be
    remembered: it is refused rather than set up again on every run."""
    try:
        weakref.ref(target)
    except TypeError as error:
        name = type(target).__name__
        raise ConfigError(
            "invalid_config",
            f"{name}: an adapter with setup must allow weak references; "
            "add '__weakref__' to its __slots__",
        ) from error


async def _extension(e: Extension) -> None:
    if e.setup is None:
        return
    try:
        await e.setup()
    except Exception as error:
        # check() returns this text: a resolved secret in the error is redacted.
        why = redact_secrets(str(error))
        raise ConfigError("invalid_config", f"extension {e.name}: setup failed: {why}") from error


async def set_up[D](definition: Definition[D]) -> None:
    """Sets up `definition` and every agent it may start. Raises `ConfigError`."""
    for e in definition.extensions:
        await _once(e, partial(_extension, e))
    for adapter in (definition.model, definition.sandbox, definition.memory, definition.knowledge):
        if isinstance(adapter, SetsUp):
            await _once(adapter, adapter.setup)
    for child in (*definition.subagents, *definition.handoffs):
        await set_up(child)
