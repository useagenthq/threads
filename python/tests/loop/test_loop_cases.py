"""Conformance runner for the loop: every `recover` and `stub` case (spec/conformance/README.md,
"What a runner does per kind"). No per-case code.

recover: import the writer's own log, acquire the lease (the next epoch), run semantic recovery,
then resume the loop against the scripts until idle or parked. stub: import, then continue in
stub mode. The run itself is the eval runner's rerun (threads.evals.rerun): one implementation
for the corpus and for saved cases. Both compare `appended`, the scripted counters, and that the
result exports and imports cleanly.
"""

import asyncio
import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from corpus import CASES, cases, load, matches, now_of, obj, own
from pydantic import JsonValue, TypeAdapter

from threads.evals.case_dir import CaseSandbox
from threads.evals.rerun import Ran, RerunInput, rerun
from threads.loop.drafts import draft
from threads.loop.runtime import Failed
from threads.reduce.handlers import to_json
from threads.store import StoredEvent

_V1 = TypeAdapter[dict[str, dict[str, JsonValue]]](dict[str, dict[str, JsonValue]])


@dataclass
class Outcome:
    error: dict[str, JsonValue] | None
    appended: list[StoredEvent]
    remaining: int
    counters: dict[str, dict[str, int]]
    stubs: tuple[int, int] | None


def _script(case: Path, meta: dict[str, JsonValue], key: str) -> JsonValue:
    name = meta.get(key)
    return load(case, name) if isinstance(name, str) else None


RUNNER: dict[str, JsonValue] = {
    "kind": "user",
    "principal": {"issuer": "api", "tenant": "local", "subject": "conformance"},
}


async def run_case(case: Path) -> Outcome:
    meta = load(case, "case.json")
    sandbox = _script(case, meta, "sandbox_script")
    text = obj(meta.get("input", {})).get("text")
    user = (
        replace(draft("user_input", {"source": "api", "text": text}), actor=RUNNER)
        if isinstance(text, str)
        else None
    )
    got = await rerun(
        RerunInput(
            log=own(case, "log.jsonl").read_bytes(),
            artifacts=tuple(p.read_bytes() for p in sorted((case / "artifacts").glob("*"))),
            now=now_of(meta),
            model=_script(case, meta, "model_script"),
            sandbox=None
            if sandbox is None
            else CaseSandbox(v1=_V1.validate_python(obj(sandbox).get("tools", {}))),
            stubs=_script(case, meta, "stub_script"),
            extensions=None,
            input=user,
            recorded=None,
        )
    )
    if not isinstance(got, Ran):
        return Outcome({"code": got.code, "seq": got.seq}, [], 0, {}, None)
    error: dict[str, JsonValue] | None = (
        {"code": got.halt.code} if isinstance(got.halt, Failed) else None
    )
    counters = {k: dict(v) for k, v in got.counters.items()}
    return Outcome(error, list(got.appended), got.script_left, counters, got.stubs)


def check_expect(meta: dict[str, JsonValue], appended: list[StoredEvent]) -> None:
    """A case's `expect.must`: each matcher matches at least one appended event."""
    must = obj(meta.get("expect", {"must": []})).get("must", [])
    assert isinstance(must, list)
    for matcher in must:
        assert any(matches(obj(matcher), e) for e in appended), matcher


def _counters(expected: dict[str, JsonValue], got: dict[str, dict[str, int]]) -> None:
    for counter, per_tool in expected.items():
        for tool, count in obj(per_tool).items():
            assert got.get(counter, {}).get(tool, 0) == count, (counter, tool)


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
        assert matches(obj(matcher), event), (matcher, to_json(event))
    assert outcome.remaining == 0, "leftover scripted model responses"
    check_expect(load(case, "case.json"), outcome.appended)
    if outcome.stubs is None:
        _counters(obj(expected.get("sandbox", {})), outcome.counters)
    else:
        stubs = obj(expected.get("stubs", {}))
        assert outcome.stubs == (stubs["consumed"], stubs["unmatched"])
