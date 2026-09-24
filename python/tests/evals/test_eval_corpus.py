"""spec/conformance/evals (lane 22): every directory's saved cases give its report.json, byte for
byte, offline, with the dry pins in its agents.json standing in for the user's agents."""

import asyncio
import json
from pathlib import Path
from typing import Literal

import pytest
from pydantic import JsonValue, TypeAdapter

from threads.agents.definition import DryPin
from threads.evals.case_dir import case_names
from threads.evals.compare import canonical
from threads.evals.run import Plan, evals_of
from threads.log import ThreadStartedData
from threads.loop import guard
from threads.reduce.handlers import to_json

ROOT = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "evals"
_PINS = TypeAdapter[list[dict[str, JsonValue]]](list[dict[str, JsonValue]])
_PROVIDERS = TypeAdapter[list[Literal["memory", "knowledge"]]](list[Literal["memory", "knowledge"]])
_NAMES = TypeAdapter[list[str]](list[str])


def pins_of(folder: Path) -> tuple[DryPin, ...] | None:
    path = folder / "agents.json"
    if not path.exists():
        return None
    out: list[DryPin] = []
    for p in _PINS.validate_json(path.read_bytes()):
        started = to_json(ThreadStartedData.model_validate_json(json.dumps(p["started"])))
        assert isinstance(started, dict)
        out.append(
            DryPin(
                started,
                tuple(_NAMES.validate_python(p["mcp"])),
                tuple(_NAMES.validate_python(p["setup_extensions"])),
                tuple(_PROVIDERS.validate_python(p["setup_providers"])),
                leads_team=False,
            )
        )
    return tuple(out)


@pytest.mark.parametrize("name", [d.name for d in sorted(ROOT.iterdir())])
def test_eval_conformance(name: str) -> None:
    folder = ROOT / name
    root = folder / "cases"
    before = guard.requests_seen()
    report = asyncio.run(evals_of(Plan(root, pins_of(folder)), case_names(root)))
    assert guard.requests_seen() == before, "an offline eval made a model request"
    got = canonical(to_json(report)) + "\n"
    want = (folder / "report.json").read_text()
    assert json.loads(got) == json.loads(want)
    assert got == want
