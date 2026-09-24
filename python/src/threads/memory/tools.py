"""The memory and knowledge tools on the host: save_memory,
search_memory, forget_memory and search_knowledge, each through the scoped guard.

Scope is never an argument: it is bound when the run starts. A recall's items reach the model
only as `injected` untrusted references appended with the call's result. A failed recall is an
error result and the run goes on without it.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.tools_v1 import (
    ForgetMemoryInput,
    SaveMemoryInput,
    SearchKnowledgeInput,
    SearchMemoryInput,
)
from threads.log import (
    Event,
    InjectedEvent,
    JsonObject,
    Principal,
    ToolCallEvent,
    ToolSpec,
    UserInputEvent,
)
from threads.loop.model import LookupResult, LookupUnknown
from threads.loop.tools import (
    Dispatched,
    Invocation,
    NotSent,
    Output,
    Reference,
    Termination,
    Uncertain,
)
from threads.memory.authority import origin
from threads.memory.guard import ScopedKnowledge, ScopedMemory
from threads.memory.types import Outcome, Provenance, ProviderError
from threads.reduce.view import input_principal
from threads.result import Err
from threads.thread.fork import knowledge_revision
from threads.tools.runner import parse

DEFAULT_K = 5


def _k(k: int | MISSING) -> int:
    return DEFAULT_K if k is MISSING else k


def _failed(error: ProviderError) -> Output:
    return Output(f"{error.code}: {error.message}", is_error=True)


def _write_failed(error: ProviderError) -> Dispatched:
    """A write that timed out or broke may have happened: uncertainty, settled by the tool's
    effect class, never an error result that invites a blind retry."""
    if not error.sent:
        return NotSent()
    match error.code:
        case "timeout":
            return Uncertain("timeout")
        case "unavailable":
            return Uncertain("transport_error")
        case _:
            return _failed(error)


@dataclass(frozen=True, slots=True)
class ProviderTools:
    """The run's provider tools; a tool whose provider isn't configured is never pinned."""

    memory: Callable[[Principal], ScopedMemory] | None
    """The memory of one principal's scope."""
    knowledge: ScopedKnowledge | None
    events: Callable[[], Sequence[Event]]
    principal: Principal
    """The run's: memory acts for it only when no input names one."""

    def _memory(self) -> ScopedMemory | None:
        # The current input's principal (spec/schema/README.md, Memory in shared threads).
        if self.memory is None:
            return None
        return self.memory(input_principal(self.events()) or self.principal)

    def invalid(self, spec: ToolSpec, input: JsonObject) -> str | None:
        parsed = parse(spec.name, input)
        return parsed.error if isinstance(parsed, Err) else None

    async def dispatch(self, call: Invocation) -> Dispatched:
        parsed = parse(call.spec.name, call.input)
        if isinstance(parsed, Err):
            raise AssertionError("a dispatched call was parsed first")
        memory = self._memory()
        match parsed.value:
            case SaveMemoryInput(text=text) if memory is not None:
                return await self._save(memory, text, call)
            case SearchMemoryInput(query=query, k=k) if memory is not None:
                return await self._recall(memory, query, _k(k))
            case ForgetMemoryInput(id=id) if memory is not None:
                return await self._forget(memory, id, call.effect_key)
            case SearchKnowledgeInput(query=query, k=k, sources=sources) if self.knowledge:
                only = None if sources is MISSING else sources
                return await self._search(self.knowledge, query, _k(k), only)
            case _:
                raise AssertionError(f"{call.spec.name} is pinned only with its provider")

    async def _save(self, memory: ScopedMemory, text: str, call: Invocation) -> Dispatched:
        events = self.events()
        ids = [
            e.event_id
            for e in events
            if isinstance(e, UserInputEvent)
            or (isinstance(e, ToolCallEvent) and e.data.call_id == call.call_id)
        ]
        thread = next(e.thread_id for e in events)
        provenance = Provenance(thread_id=thread, event_ids=tuple(ids[-2:]))
        saved = await memory.remember(text, origin(events), provenance, call.effect_key)
        if isinstance(saved, Err):
            return _write_failed(saved.error)
        return Output(json.dumps({"id": saved.value.id, "version": saved.value.version}))

    async def _recall(self, memory: ScopedMemory, query: str, k: int) -> Output:
        got = await memory.recall(query, k)
        if isinstance(got, Err):
            return _failed(got.error)
        refs = tuple(Reference("memory", h.id, h.version, h.text) for h in got.value)
        # Provider ids appear only in each reference's id, never in the listing (spec pin).
        listing = f"{len(refs)} memories, shown below as untrusted references"
        return Output(listing if refs else "no memories found", False, None, refs)

    async def _forget(self, memory: ScopedMemory, id: str, key: str) -> Dispatched:
        # Only an id this branch recalled, so passed the scope check, can be forgotten.
        recalled = any(
            isinstance(e, InjectedEvent) and e.data.source == "memory" and e.data.origin.id == id
            for e in self.events()
        )
        if not recalled:
            return Output(f"not_found: memory {id} was not recalled in this thread", True)
        done = await memory.forget(id, key)
        return _write_failed(done.error) if isinstance(done, Err) else Output(f"forgot {id}")

    async def _search(
        self, knowledge: ScopedKnowledge, query: str, k: int, sources: Sequence[str] | None
    ) -> Output:
        # A pinned fork searches as of its snapshot's revision.
        as_of = knowledge_revision(list(self.events()))
        got = await knowledge.search(query, k, sources, as_of)
        if isinstance(got, Err):
            return _failed(got.error)
        refs = tuple(
            Reference("knowledge", h.doc_id, h.version, h.text, f"{h.span.start}-{h.span.end}")
            for h in got.value
        )
        cites = [f"[doc:{r.id}@{r.version}#{r.location}]" for r in refs]
        return Output(_listing("excerpts", cites), False, None, refs)

    async def knowledge_revision(self) -> Outcome[int] | None:
        """The corpus revision a snapshot records; None without knowledge."""
        k = self.knowledge
        return None if k is None else await k.provider.revision(k.owner.scope)

    async def lookup(self, call: Invocation) -> LookupResult[str]:
        return LookupUnknown(f"{call.spec.name} has no lookup")

    async def terminate(self, call: Invocation) -> Termination:
        return "unknown"

    def provider_now(self) -> int | None:
        return None


def _listing(kind: str, items: Sequence[str]) -> str:
    if not items:
        return f"no {kind} found"
    return f"{len(items)} {kind}, shown below as untrusted references: " + ", ".join(items)
