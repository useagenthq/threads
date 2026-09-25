"""The read side of every staged case whose family waits for a later build
(spec/conformance/README.md, "Staged cases"): each log passes semantic rules 31-45 but 43 and
reduces to the state it expects. A case that ships its artifacts is imported, so every request's
Render v1 bytes are checked too. Projections, tree walks and the index rebuild are the later
builds' to prove."""

import asyncio
from pathlib import Path

import pytest
from corpus import CASES, import_and_read, load, now_of
from pydantic import JsonValue

from threads.result import Ok
from threads.store import verify_export

STAGED = CASES.parent / "staged"


def _staged() -> list[Path]:
    """The staged cases: none when staged/ is absent (git keeps no empty directory)."""
    return sorted(STAGED.iterdir()) if STAGED.exists() else []


def _logs(case: Path) -> list[tuple[str, Path]]:
    single = case / "log.jsonl"
    if single.exists():
        return [("log", single)]
    return [(p.stem, p) for p in sorted((case / "logs").glob("*.jsonl"))]


def _want(expected: dict[str, JsonValue], label: str) -> JsonValue:
    if expected["outcome"] != "ok":
        return None
    states = expected.get("states")
    return states.get(label) if isinstance(states, dict) else expected.get("state")


@pytest.mark.parametrize(
    ("case", "label", "log"),
    [(c, label, log) for c in _staged() for label, log in _logs(c)],
    ids=lambda v: v.name if isinstance(v, Path) and v.is_dir() else str(v),
)
def test_a_staged_log_reads(case: Path, label: str, log: Path) -> None:
    now = now_of(load(case, "case.json"))
    data = log.read_bytes()
    if (case / "artifacts").is_dir():
        read = asyncio.run(import_and_read(case, data, now))
        assert isinstance(read, Ok), read
        state = read.value.state
    else:
        verified = verify_export(data, now)
        assert isinstance(verified, Ok), verified
        state = verified.value.state
    want = _want(load(case, "expected.json"), label)
    if want is not None:
        assert state.to_json() == want
