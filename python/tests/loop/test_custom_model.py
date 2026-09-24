"""A custom model is `info` plus `send` (spec/api.json `Model`); `lookup` is the optional
`LooksUp` capability. A model that declares a lookup without the method is refused by check()
and the first run, and a lookup whose fence is refused ends recovery `branch_busy`."""

import asyncio
from collections.abc import AsyncIterator, Generator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from corpus import CASES, Clock, ScriptedTools, load, now_of, own, stored_artifacts

from threads import (
    Completed,
    ConfigError,
    LooksUp,
    LookupResult,
    Model,
    ModelChunk,
    ModelContext,
    ModelInfo,
    ModelRequest,
    ModelResponse,
    agent,
    scripted_model,
    sqlite,
)
from threads.log import TextPart, Usage
from threads.loop.guard import block_model_requests
from threads.loop.model import Done, LookupCapability, PartChunk, StaleEpoch
from threads.loop.recovery import recover
from threads.loop.runtime import Failed, Runtime
from threads.loop.scripted import SCRIPTED_INFO, ScriptedModel
from threads.permissions import Decision
from threads.result import Err, Ok
from threads.store import SqliteStore, verify_export


@dataclass(frozen=True)
class Hello:
    """A model with no lookup: it answers every request with "hello"."""

    lookup_capability: LookupCapability = "none"

    @property
    def info(self) -> ModelInfo:
        return replace(SCRIPTED_INFO, lookup=self.lookup_capability)

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        yield PartChunk(TextPart(type="text", text="hello"))
        yield Done("end_turn", Usage(input_tokens=1, output_tokens=1))


@pytest.fixture
def offline_model() -> Generator[None]:
    """Hello never leaves the process; the guard only knows the scripted model."""
    block_model_requests(blocked=False)
    yield
    block_model_requests()


@pytest.mark.usefixtures("offline_model")
def test_a_model_without_lookup_is_a_model() -> None:
    model: Model = Hello()
    assert not isinstance(model, LooksUp)
    result = asyncio.run(agent(model=model).run("hi", store=sqlite(":memory:")))
    assert isinstance(result, Completed)
    assert result.output == "hello"


def test_declared_lookup_without_method_is_rejected(tmp_path: Path) -> None:
    claims = Hello("final")
    bot = agent(model=claims)  # agent() stays pure: nothing is checked yet
    checked = asyncio.run(bot.check())
    assert isinstance(checked, Err)
    assert checked.error.code == "capability_missing"
    assert checked.error.message == (
        "model scripted/scripted-1 declares lookup 'final' but has no lookup method: "
        "implement lookup, or declare lookup='none'"
    )
    store = tmp_path / "store"
    with pytest.raises(ConfigError) as raised:
        asyncio.run(bot.run("hi", store=sqlite(str(store))))
    assert raised.value.code == "capability_missing"
    assert not store.exists(), "a refused run touches no store"


def test_a_fallback_or_subagent_model_declaring_lookup_without_method_is_rejected() -> None:
    fine = scripted_model({"responses": []})
    helper = agent(name="helper", model=Hello("nonfinal"))
    for bot in (
        agent(model=fine, fallback=[Hello("final")]),
        agent(model=fine, subagents=[helper]),
        agent(model=fine, handoffs=[helper]),
    ):
        checked = asyncio.run(bot.check())
        assert isinstance(checked, Err)
        assert checked.error.code == "capability_missing"


class _Refused(ScriptedModel):
    """The lease moves between recovery's fence and the lookup's own send point."""

    async def lookup(
        self, request_id: str, context: ModelContext
    ) -> Ok[LookupResult[ModelResponse]] | Err[StaleEpoch]:
        return Err(StaleEpoch("the lease moved"))


def _allow(*_args: object) -> Decision:
    return Decision("allow", "policy", "test")


def test_lookup_stale_fence_is_branch_busy() -> None:
    case = CASES / "model-response-recovered-by-lookup"
    lookups = load(case, "model.json")["lookup"]
    assert isinstance(lookups, dict)
    model = _Refused([], lookups)

    async def main() -> tuple[object, int, int]:
        clock = Clock(now_of(load(case, "case.json")))
        log = verify_export(own(case, "log.jsonl").read_bytes(), clock())
        assert isinstance(log, Ok)
        opened = await SqliteStore.open(artifacts=stored_artifacts(case))
        assert isinstance(opened, Ok)
        store = opened.value
        try:
            assert await store.import_log(log.value) == Ok(None)
            branch = log.value.segments[-1].header.branch_id
            writer = await store.acquire(branch, "runner", clock)
            assert isinstance(writer, Ok)
            before = writer.value.fold.seq
            tools = ScriptedTools({}, clock)
            rt = Runtime(
                store, writer.value, lambda _ref: model, tools, _allow, clock, clock.wait_until
            )
            halt = await recover(rt)
            return halt, before, rt.fold.seq
        finally:
            await store.close()

    halt, before, after = asyncio.run(main())
    assert isinstance(halt, Failed)
    assert halt.code == "branch_busy"
    assert after == before, "recovery appended nothing"
