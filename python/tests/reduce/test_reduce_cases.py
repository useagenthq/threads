"""Conformance runner for the log: every `reduce` case in full, and the reader's view of every
other case's log (spec/conformance/README.md, "What a runner does per kind").

A `reduce` case imports its log read-only into a fresh SQLite store, reads it back through the
same boundary, and compares `state` and `projections`, or the error `code` and `seq`. The other
kinds need the loop, render, fork or host, which don't exist yet. Their logs must still import
to the pinned `state`, and each of them is listed below as skipped, with the reason.
"""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from threads.reduce import PROJECTIONS
from threads.result import Err, Ok
from threads.store import SqliteStore, VerifiedLog, verify_export

CASES = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "cases"
_JSON: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
CASE_KEYS = frozenset(
    {"name", "family", "kind", "description", "clock", "model_script", "sandbox_script"}
    | {"stub_script", "input"}
)
EXPECTED_KEYS = frozenset(
    {"outcome", "error", "state", "committed_bytes", "appended", "sandbox", "fork", "resources"}
    | {"render", "head_verified", "stubs", "responses", "inbox", "decisions", "projections"}
)
LATER = {
    "render": "Render v1 re-rendering, C7 and the request hash checks are not built yet",
    "recover": "leases exist, but semantic recovery and the loop are not built yet",
    "fork": "the fork operation (eligibility, sandbox restore, resource ledger) is not built yet",
    "stub": "stub mode is not built yet",
    "intake": "the host intake pipeline is not built yet",
    "policy": "the permission engine is not built yet",
}
SPEC_BUGS = {
    "render-reference-framing-escaped": (
        "its compacted.summary_ref is not the text of the compaction response it names, which "
        "semantic rule 10 rejects (as compaction-summary-mismatch-rejected pins)"
    ),
}


def load(case: Path, name: str) -> dict[str, JsonValue]:
    return _JSON.validate_json((case / name).read_bytes())


def cases(*kinds: str) -> list[str]:
    return sorted(d.name for d in CASES.iterdir() if load(d, "case.json")["kind"] in kinds)


def now_of(case: dict[str, JsonValue]) -> int:
    clock = case["clock"]
    assert isinstance(clock, dict)
    now = clock["now"]
    assert isinstance(now, int)
    return now


async def import_and_read(log: bytes, now: int) -> Ok[VerifiedLog] | Err[str]:
    """Imports an export into a fresh store and reads the last branch back from SQLite."""
    verified = verify_export(log, now)
    if isinstance(verified, Err):
        return Err(json.dumps({"code": verified.error.code, "seq": verified.error.seq}))
    store = await SqliteStore.open()
    try:
        stored = await store.import_log(verified.value)
        assert stored == Ok(None)
        branch = verified.value.segments[-1].header.branch_id
        if verified.value.head_verified:
            # SQLite and JSONL are one contract: the export is the imported bytes.
            assert await store.export(branch) == log
        read = await store.read(branch, now)
    finally:
        await store.close()
    assert isinstance(read, Ok), read
    return read


def test_corpus_kinds_and_keys_are_known() -> None:
    for case in CASES.iterdir():
        meta, expected = load(case, "case.json"), load(case, "expected.json")
        assert set(meta) <= CASE_KEYS, case.name
        assert set(expected) <= EXPECTED_KEYS, case.name
        assert meta["kind"] == "reduce" or meta["kind"] in LATER, case.name


@pytest.mark.parametrize("name", cases("reduce"))
def test_reduce_case(name: str) -> None:
    case = CASES / name
    meta, expected = load(case, "case.json"), load(case, "expected.json")
    log = (case / "log.jsonl").read_bytes()
    result = asyncio.run(import_and_read(log, now_of(meta)))
    assert (case / "log.jsonl").read_bytes() == log
    if expected["outcome"] == "error":
        assert isinstance(result, Err)
        assert json.loads(result.error) == expected["error"]
        assert expected.get("appended", []) == []
        return
    assert isinstance(result, Ok), result
    assert result.value.state.to_json() == expected["state"]
    reader = verify_export(log, now_of(meta))
    assert isinstance(reader, Ok)
    assert reader.value.head_verified == expected.get("head_verified", True)
    if "committed_bytes" in expected:
        assert reader.value.committed_bytes == expected["committed_bytes"]
    projections = expected.get("projections", {})
    assert isinstance(projections, dict)
    for key, value in projections.items():
        assert key in PROJECTIONS, f"projection {key} is not implemented"
        assert PROJECTIONS[key](result.value.fold) == value, key


READER_VIEW = [
    pytest.param(n, marks=pytest.mark.xfail(reason=SPEC_BUGS[n])) if n in SPEC_BUGS else n
    for n in cases(*LATER)
    if (CASES / n / "log.jsonl").exists()
]


@pytest.mark.parametrize("name", READER_VIEW)
def test_reader_view(name: str) -> None:
    """Every later-kind log imports; its pinned reader state and valid prefix match."""
    case = CASES / name
    meta, expected = load(case, "case.json"), load(case, "expected.json")
    log = (case / "log.jsonl").read_bytes()
    verified = verify_export(log, now_of(meta))
    assert isinstance(verified, Ok), verified
    if "state" in expected:
        assert verified.value.state.to_json() == expected["state"]
    assert verified.value.head_verified == expected.get("head_verified", True)
    if "committed_bytes" in expected:
        assert verified.value.committed_bytes == expected["committed_bytes"]
    read = asyncio.run(import_and_read(log, now_of(meta)))
    assert isinstance(read, Ok), read
    assert read.value.state == verified.value.state


@pytest.mark.parametrize("name", cases(*LATER))
def test_later_kind_steps(name: str) -> None:
    kind = load(CASES / name, "case.json")["kind"]
    assert isinstance(kind, str)
    pytest.skip(f"{kind}: {LATER[kind]}")
