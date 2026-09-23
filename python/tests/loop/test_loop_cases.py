"""Conformance runner for the loop: every `recover` and `stub` case (spec/conformance/README.md,
"What a runner does per kind"). No per-case code.

recover: import the writer's own log read-only, acquire the lease (the next epoch), run semantic
recovery, then resume the loop against the scripts until idle or parked. stub: import, then
continue in stub mode. Both compare `appended`, the scripted counters, and that the result
exports and imports cleanly.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from corpus import (
    CASES,
    Clock,
    ScriptedTools,
    cases,
    json_schema_holds,
    load,
    now_of,
    obj,
    own,
    schema_error,
    stored_artifacts,
)
from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads.log import ToolCallData, ToolSpec
from threads.loop.drive import drive
from threads.loop.model import Model
from threads.loop.recovery import recover
from threads.loop.runtime import Failed, Halt, Runtime
from threads.loop.scripted import ScriptedModel, scripted_model
from threads.loop.stubs import StubGateway, parse_stubs
from threads.loop.tools import ToolRunner
from threads.permissions import Decision
from threads.reduce import Fold
from threads.reduce.fold import policy
from threads.reduce.handlers import to_json
from threads.render.verify import verify_requests
from threads.result import Err, Ok
from threads.store import SqliteStore, StoredEvent, verify_export

MATCHED_EXACTLY = ("type", "seq", "epoch", "branch_id", "critical")


def conformance_allow(_fold: Fold, _call: ToolCallData, _spec: ToolSpec) -> Decision:
    """The fixed conformance policy: every call the runner creates is allowed."""
    return Decision("allow", "policy", "conformance_allow")


@dataclass
class Outcome:
    error: dict[str, JsonValue] | None
    appended: list[StoredEvent]
    model: ScriptedModel
    tools: ScriptedTools | StubGateway
    export: bytes


def _script(case: Path, meta: dict[str, JsonValue], key: str) -> dict[str, JsonValue]:
    name = meta.get(key)
    return load(case, name) if isinstance(name, str) else {}


def _runner(case: Path, meta: dict[str, JsonValue], clock: Clock) -> ScriptedTools | StubGateway:
    if meta["kind"] == "stub":
        return StubGateway(parse_stubs(_script(case, meta, "stub_script")), schema_error)
    return ScriptedTools(_script(case, meta, "sandbox_script"), clock)


async def run_case(case: Path) -> Outcome:
    meta = load(case, "case.json")
    clock = Clock(now_of(meta))
    model = scripted_model(_script(case, meta, "model_script") or {"responses": []})
    tools = _runner(case, meta, clock)
    verified = verify_export(own(case, "log.jsonl").read_bytes(), clock())
    assert isinstance(verified, Ok), verified
    artifacts = stored_artifacts(case)
    opened = await SqliteStore.open(artifacts=artifacts)
    assert isinstance(opened, Ok)
    store = opened.value
    try:
        assert await store.import_log(verified.value) == Ok(None)
        branch = verified.value.segments[-1].header.branch_id
        acquired = await store.acquire(branch, "runner", clock)
        if isinstance(acquired, Err) and acquired.error.code == "branch_not_runnable":
            acquired = await store.repair_torn(branch, "runner", clock)
        if isinstance(acquired, Err):
            refused: dict[str, JsonValue] = {"code": acquired.error.code, "seq": acquired.error.seq}
            return Outcome(refused, [], model, tools, b"")
        writer = acquired.value
        before = verified.value.fold.seq
        output = _output_binding(verified.value.fold)
        rt = Runtime(
            store,
            writer,
            _model(model),
            _tools(tools),
            conformance_allow,
            clock,
            clock.wait_until,
            output,
        )
        halt = await _resume(rt, meta)
        exported = await store.export(branch)
        assert isinstance(exported, Ok)
        read = verify_export(exported.value, clock())
        assert isinstance(read, Ok), read
        # Every request the run made replays byte for byte: C7 per settings epoch (invariant 5).
        assert verify_requests(read.value.fold.events, artifacts.get) == Ok(None)
        appended = [e for e in _events(read.value.segments[-1].events) if e.seq > before]
        error: dict[str, JsonValue] | None = None
        if isinstance(halt, Failed):
            error = {"code": halt.code}
        return Outcome(error, appended, model, tools, exported.value)
    finally:
        await store.close()


def _output_binding(fold: Fold) -> Callable[[JsonValue], str | None] | None:
    """Test-only: the corpus pins its output schema in the log; core binds a Pydantic type."""
    pinned = policy(fold)
    if pinned is None or pinned.output is MISSING:
        return None
    schema: JsonValue = dict(pinned.output.schema_)
    return lambda value: None if json_schema_holds(schema, value) else "does not match the schema"


def _model(model: ScriptedModel) -> Model:
    return model


def _tools(tools: ScriptedTools | StubGateway) -> ToolRunner:
    return tools


def _events(rows: tuple[tuple[StoredEvent, bytes], ...]) -> list[StoredEvent]:
    return [event for event, _ in rows]


async def _resume(rt: Runtime, meta: dict[str, JsonValue]) -> Halt | None:
    """Recovery runs first on every acquire. The loop resumes only where recovery had in-doubt
    work to hand back (recover), or to continue the log (stub)."""
    required = rt.writer.requires_recovery
    halt = await recover(rt)
    if isinstance(halt, Failed) or not (required or meta["kind"] == "stub"):
        return halt
    return await drive(rt)


def _subset(expected: JsonValue, actual: JsonValue) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and _subset(v, actual[k]) for k, v in expected.items()
        )
    return expected == actual and type(expected) is type(actual)


def _matches(matcher: dict[str, JsonValue], event: StoredEvent) -> bool:
    wire = obj(to_json(event))
    for key in MATCHED_EXACTLY:
        if key in matcher and matcher[key] != wire[key]:
            return False
    if "actor_kind" in matcher and matcher["actor_kind"] != obj(wire["actor"])["kind"]:
        return False
    return _subset(matcher.get("data", {}), wire["data"])


def _counters(expected: dict[str, JsonValue], got: dict[str, dict[str, int]]) -> None:
    for counter, per_tool in expected.items():
        for tool, count in obj(per_tool).items():
            assert got[counter].get(tool, 0) == count, (counter, tool)


@pytest.mark.parametrize("name", cases("recover", "stub"))
def test_loop_case(name: str) -> None:
    case = CASES / name
    expected = load(case, "expected.json")
    outcome = asyncio.run(run_case(case))
    if expected["outcome"] == "error":
        want = obj(expected["error"])
        assert outcome.error is not None
        assert {k: outcome.error.get(k) for k in want} == want
    else:
        assert outcome.error is None, outcome.error
    matchers = expected.get("appended", [])
    assert isinstance(matchers, list)
    got = [json.loads(json.dumps(to_json(e))) for e in outcome.appended]
    assert len(outcome.appended) == len(matchers), got
    for matcher, event in zip(matchers, outcome.appended, strict=True):
        assert _matches(obj(matcher), event), (matcher, to_json(event))
    assert outcome.model.remaining == 0, "leftover scripted model responses"
    if isinstance(outcome.tools, ScriptedTools):
        _counters(obj(expected.get("sandbox", {})), outcome.tools.counters())
    else:
        stubs = obj(expected.get("stubs", {}))
        assert (outcome.tools.consumed, outcome.tools.unmatched) == (
            stubs["consumed"],
            stubs["unmatched"],
        )
