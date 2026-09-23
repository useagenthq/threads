"""Binding an agent's memory and knowledge to one run: the scope comes from
host config and the verified principal, the built-in providers open their tables in the run's
store, and `local_knowledge` paths are ingested by the host."""

import hashlib
import mimetypes
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from threads.agents.config import ConfigError
from threads.log import Event, Principal
from threads.memory.fence import Fence
from threads.memory.guard import (
    Binder,
    ScopedKnowledge,
    ScopedMemory,
    scoped_knowledge,
    scoped_memory,
)
from threads.memory.local_knowledge import LocalKnowledge
from threads.memory.local_memory import LocalMemory
from threads.memory.protocol import DeclaresWrites, KnowledgeProvider, MemoryProvider
from threads.memory.tools import ProviderTools
from threads.memory.types import Scope
from threads.result import Err
from threads.store import Clock, SqliteStore
from threads.tools.specs import Writes

KNOWLEDGE_SCOPE = "knowledge"
"""Knowledge is the agent's corpus within a tenant; memory is per principal."""


def memory_scope(agent: str, principal: Principal) -> Scope:
    """Never mixed across tenants, agents or users: the tenant and the user are the verified
    principal's, never a tool argument."""
    user = f"{_part(principal.issuer)}/{_part(principal.subject)}"
    return Scope(tenant_id=principal.tenant, agent=agent, scope=user)


def _part(text: str) -> str:
    """Escapes the separator, so ("a/b", "c") and ("a", "b/c") never share a scope. `%` first;
    a part with neither character is unchanged, so existing scopes keep their memories."""
    return text.replace("%", "%25").replace("/", "%2F")


def writes(memory: MemoryProvider | None) -> Writes | None:
    """What the pinned save_memory and forget_memory declare."""
    if memory is None:
        return None
    if isinstance(memory, DeclaresWrites):
        if memory.write_effect == "read_only":
            # A write is an effect: read_only would skip effect_begin.
            raise ConfigError("invalid_config", "a memory provider's writes can't be read_only")
        return Writes(memory.write_effect, memory.dedup_window_ms)
    return Writes()


@dataclass(frozen=True, slots=True)
class RunBinding:
    """What a run lends its providers: its clock, its fence and its branch's events."""

    clock: Clock
    fence: Fence
    events: Callable[[], Sequence[Event]]


@dataclass(frozen=True, slots=True)
class Providers:
    memory: MemoryProvider | None
    knowledge: KnowledgeProvider | None


async def provider_tools(
    store: SqliteStore,
    providers: Providers,
    agent: str,
    principal: Principal,
    run: RunBinding,
) -> ProviderTools | None:
    """`principal` is the run's; memory calls act for the current input's principal."""
    memory, knowledge = providers.memory, providers.knowledge
    if memory is None and knowledge is None:
        return None
    if isinstance(memory, LocalMemory):
        memory = await memory.bind(store)
    scoped = None if memory is None else _per_principal(store, memory, agent, run)
    corpus = None
    if knowledge is not None:
        paths: Sequence[str] = ()
        if isinstance(knowledge, LocalKnowledge):
            paths = knowledge.paths
            knowledge = await knowledge.bind(store)
        shared = Scope(tenant_id=principal.tenant, agent=agent, scope=KNOWLEDGE_SCOPE)
        by = Binder(store.bindings("knowledge"), shared, run.clock, run.fence)
        corpus = scoped_knowledge(knowledge, by)
        await _ingest(corpus, paths)
    return ProviderTools(scoped, corpus, run.events, principal)


def _per_principal(
    store: SqliteStore, memory: MemoryProvider, agent: str, run: RunBinding
) -> Callable[[Principal], ScopedMemory]:
    def scoped(principal: Principal) -> ScopedMemory:
        by = Binder(store.bindings("memory"), memory_scope(agent, principal), run.clock, run.fence)
        return scoped_memory(memory, by)

    return scoped


async def _ingest(corpus: ScopedKnowledge, paths: Sequence[str]) -> None:
    """Admits each path's current bytes. Same bytes: a no-op; changed bytes: a new version."""
    for path in paths:
        try:
            content = Path(path).read_bytes()
        except OSError as error:
            raise ConfigError("invalid_config", f"knowledge path {path}: {error}") from error
        media_type = mimetypes.guess_type(path)[0] or "text/plain"
        key = f"{path}@{hashlib.sha256(content).hexdigest()}"
        done = await corpus.ingest(path, media_type, content, path, key)
        if isinstance(done, Err):
            raise ConfigError("invalid_config", f"knowledge path {path}: {done.error.message}")
