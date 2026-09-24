"""spec/conformance/vectors (lane 22): the judge input bytes, the verdict rule and drift, as both
runtimes must compute them."""

import json
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter
from test_eval_corpus import pins_of

from threads.evals.drift import Recorded, drift
from threads.evals.judge import judge_input, verdicts
from threads.log import Event, ThreadStartedData
from threads.reduce.handlers import to_json

VECTORS = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "vectors"
_EVENTS = TypeAdapter[list[Event]](list[Event])
_OBJ = TypeAdapter[dict[str, JsonValue]](dict[str, JsonValue])


def _vectors(name: str) -> list[dict[str, JsonValue]]:
    doc = _OBJ.validate_json((VECTORS / name).read_bytes())
    items = doc["vectors"]
    assert isinstance(items, list)
    return [v for v in items if isinstance(v, dict)]


@pytest.mark.parametrize("v", _vectors("judge-input.json"), ids=lambda v: str(v["name"]))
def test_judge_input(v: dict[str, JsonValue]) -> None:
    given = v["given"]
    assert isinstance(given, dict)
    task, rubric = given["task"], given["rubric"]
    assert isinstance(task, str)
    assert isinstance(rubric, list)
    events = _EVENTS.validate_json(json.dumps(given["events"]))
    got = judge_input(task, events, given["answer"], [str(r) for r in rubric])
    assert got == v["input"]


@pytest.mark.parametrize("v", _vectors("verdicts.json"), ids=lambda v: str(v["name"]))
def test_verdicts(v: dict[str, JsonValue]) -> None:
    rubric = v["rubric"]
    assert isinstance(rubric, list)
    got = verdicts(v["output"], [str(r) for r in rubric])
    assert ("judge_invalid" if got is None else "accept") == v["expect"]


@pytest.mark.parametrize("v", _vectors("drift.json"), ids=lambda v: str(v["name"]))
def test_drift(v: dict[str, JsonValue], tmp_path: Path) -> None:
    recorded = v["recorded"]
    assert isinstance(recorded, dict)
    started = to_json(ThreadStartedData.model_validate_json(json.dumps(recorded["started"])))
    assert isinstance(started, dict)
    line0 = recorded.get("line0")
    (tmp_path / "agents.json").write_text(json.dumps(v["pins"]))
    pins = pins_of(tmp_path) or ()
    got = drift(Recorded(started, line0.encode() if isinstance(line0, str) else None), pins)
    assert got.to_json() == v["expect"]
