"""A custom knowledge provider implements `revision` (spec/api.json `KnowledgeProvider`), and a
revision that fails costs that turn end its fork point: a snapshot on a knowledge-bound thread
always records knowledge_revision, so a pinned fork always has something to pin to."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import JsonValue

from threads import (
    Completed,
    Doc,
    DocVersion,
    KnowledgeHit,
    KnowledgeProvider,
    KnowledgeSource,
    Scope,
    agent,
    fake_sandbox,
    scripted_model,
    sqlite,
)
from threads.log import Permissions, SnapshotEvent
from threads.memory.types import Outcome, ProviderError
from threads.result import Err, Ok

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
BYPASS = Permissions(
    mode="bypass",
    allow=[],
    ask=[],
    deny=[],
    protected_paths=[],
    allow_bypass=True,
    plan_exit_mode="default",
)
READ_ONLY = ProviderError("invalid", "this corpus is read-only")


@dataclass
class Corpus:
    """An empty, read-only corpus whose revision can be made to fail."""

    failing: bool = False
    reads: int = 0

    async def ingest(self, scope: Scope, source: KnowledgeSource, key: str) -> Outcome[DocVersion]:
        return Err(READ_ONLY)

    async def remove(self, scope: Scope, doc_id: str, key: str) -> Outcome[None]:
        return Err(READ_ONLY)

    async def search(
        self,
        scope: Scope,
        query: str,
        *,
        k: int = 5,
        sources: Sequence[str] | None = None,
        as_of: int | None = None,
    ) -> Outcome[Sequence[KnowledgeHit]]:
        return Ok(())

    async def get(self, scope: Scope, doc_id: str, version: str) -> Outcome[Doc]:
        return Err(ProviderError("not_found", doc_id))

    async def revision(self, scope: Scope) -> Outcome[int]:
        self.reads += 1
        return Err(ProviderError("unavailable", "the index is down")) if self.failing else Ok(7)


@dataclass
class NoRevision:
    """A provider written against the old protocol: everything but `revision`."""

    async def ingest(self, scope: Scope, source: KnowledgeSource, key: str) -> Outcome[DocVersion]:
        return Err(READ_ONLY)

    async def remove(self, scope: Scope, doc_id: str, key: str) -> Outcome[None]:
        return Err(READ_ONLY)

    async def search(
        self,
        scope: Scope,
        query: str,
        *,
        k: int = 5,
        sources: Sequence[str] | None = None,
        as_of: int | None = None,
    ) -> Outcome[Sequence[KnowledgeHit]]:
        return Ok(())

    async def get(self, scope: Scope, doc_id: str, version: str) -> Outcome[Doc]:
        return Err(ProviderError("not_found", doc_id))


def test_a_knowledge_provider_must_declare_revision() -> None:
    # reportUnnecessaryTypeIgnoreComment fails this line if revision ever becomes optional.
    provider: KnowledgeProvider = NoRevision()  # pyright: ignore[reportAssignmentType]  # reason: revision is required
    assert not hasattr(provider, "revision")


def write(name: str, call_id: str) -> JsonValue:
    part: JsonValue = {
        "type": "tool_use",
        "call_id": call_id,
        "name": "write",
        "input": {"path": name, "content": name},
    }
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def done() -> JsonValue:
    content: JsonValue = [{"type": "text", "text": "Done."}]
    return {"content": content, "stop_reason": "end_turn", "usage": USAGE}


def test_revision_error_skips_the_snapshot() -> None:
    async def main() -> None:
        box, corpus = fake_sandbox(), Corpus()
        script: JsonValue = {
            "responses": [write("a.txt", "c1"), done(), write("b.txt", "c2"), done()]
        }
        bot = agent(model=scripted_model(script), sandbox=box, knowledge=corpus, permissions=BYPASS)
        first = await bot.run("one", store=sqlite(":memory:"))
        assert isinstance(first, Completed)
        creates = box.creates
        corpus.failing = True
        second = await bot.run("two", thread=first.thread)
        assert isinstance(second, Completed)
        assert box.creates == creates, "no scratch sandbox for a capture that can't be recorded"
        timeline = await second.thread.timeline()
        assert isinstance(timeline, Ok)
        events = [e.event for e in timeline.value.entries]
        snaps = [e for e in events if isinstance(e, SnapshotEvent)]
        assert [s.data.knowledge_revision for s in snaps] == [7]
        assert events[-1].type == "turn_completed"
        points = await second.thread.fork_points()
        assert isinstance(points, Ok)
        assert [p.seq for p in points.value] == [snaps[0].seq]

    asyncio.run(main())


def test_the_revision_is_read_only_when_a_capture_follows() -> None:
    async def main() -> None:
        corpus = Corpus()
        script: JsonValue = {"responses": [done(), write("a.txt", "c1"), done()]}
        bot = agent(
            model=scripted_model(script),
            sandbox=fake_sandbox(),
            knowledge=corpus,
            permissions=BYPASS,
        )
        first = await bot.run("chat", store=sqlite(":memory:"))
        assert isinstance(first, Completed)
        assert corpus.reads == 0, "a turn that never opened the sandbox captures nothing"
        second = await bot.run("write", thread=first.thread)
        assert isinstance(second, Completed)
        assert corpus.reads == 1

    asyncio.run(main())
