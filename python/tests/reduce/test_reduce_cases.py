"""Conformance runner for the log: every `reduce` and `render` case in full, and the reader's
view of every other case's log (spec/conformance/README.md, "What a runner does per kind").

A `reduce` case imports its log read-only into a fresh SQLite store, reads it back through the
same boundary, and compares `state` and `projections`, or the error `code` and `seq`. A `render`
case also replays every recorded request and renders the next one. The other kinds need the
loop, fork or host, which don't exist yet. Their logs must still import to the pinned `state`,
and each of them is listed below as skipped, with the reason.
"""

import asyncio
import json
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from corpus import CASES, cases, load, now_of, own, stored_artifacts

from threads.log import (
    CompactedEvent,
    ContextEditedEvent,
    Event,
    HookDecisionEvent,
    ModelRequestEvent,
    ParseError,
    SettingsChangedEvent,
)
from threads.log.digest import sha256_hex
from threads.reduce import PROJECTIONS
from threads.render import render
from threads.result import Err, Ok
from threads.store import SqliteStore, VerifiedLog, verify_export

CASE_KEYS = frozenset(
    {"name", "family", "kind", "description", "clock", "model_script", "sandbox_script"}
    | {"stub_script", "input"}
)
EXPECTED_KEYS = frozenset(
    {"outcome", "error", "state", "committed_bytes", "appended", "sandbox", "fork", "resources"}
    | {"render", "head_verified", "stubs", "responses", "inbox", "decisions", "projections"}
)
LATER = {
    "fork": "the fork operation (eligibility, sandbox restore, resource ledger) is not built yet",
    "intake": "the host intake pipeline is not built yet",
}
OWN_RUNNER = frozenset({"policy", "recover", "stub"})
"""Kinds another runner owns: policy (tests/permissions), recover and stub (tests/loop)."""


def _error(error: ParseError) -> Err[str]:
    return Err(json.dumps({"code": error.code, "seq": error.seq}))


async def import_and_read(case: Path, log: bytes, now: int) -> Ok[VerifiedLog] | Err[str]:
    """Imports an export into a fresh store holding the case's artifacts and reads the last
    branch back from SQLite."""
    verified = verify_export(log, now)
    if isinstance(verified, Err):
        return _error(verified.error)
    opened = await SqliteStore.open(artifacts=stored_artifacts(case))
    assert isinstance(opened, Ok)
    store = opened.value
    try:
        stored = await store.import_log(verified.value)
        if isinstance(stored, Err):
            return _error(stored.error)
        branch = verified.value.segments[-1].header.branch_id
        if verified.value.head_verified:
            # SQLite and JSONL are one contract: the export is the imported bytes.
            assert await store.export(branch) == Ok(log)
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
        assert meta["kind"] in {"reduce", "render", *LATER, *OWN_RUNNER}, case.name


@pytest.mark.parametrize("name", cases("reduce"))
def test_reduce_case(name: str) -> None:
    case = CASES / name
    meta, expected = load(case, "case.json"), load(case, "expected.json")
    log = own(case, "log.jsonl").read_bytes()
    result = asyncio.run(import_and_read(case, log, now_of(meta)))
    assert own(case, "log.jsonl").read_bytes() == log
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


def artifacts(case: Path) -> Callable[[str], Ok[bytes] | Err[ParseError]]:
    """The case's artifacts as an artifact store's `get`; the renderer verifies each hash."""
    folder = case / "artifacts"

    def get(sha256: str) -> Ok[bytes] | Err[ParseError]:
        path = folder / sha256
        if not path.exists():
            return Err(ParseError("artifact_missing", f"no artifact {sha256}"))
        return Ok(path.read_bytes())

    return get


def _next(case: Path, events: Sequence[Event]) -> Ok[bytes] | Err[str]:
    """Step 4: the next request. Import already replayed every recorded one (step 3)."""
    rendered = render(events, artifacts(case))
    if isinstance(rendered, Err):
        return _error(rendered.error)
    expected = load(case, "expected.json")["render"]
    assert isinstance(expected, dict)
    line0 = rendered.value.line0
    assert expected["declared_prefix"] == {"bytes": len(line0), "sha256": sha256_hex(line0)}
    body = rendered.value.body
    assert sha256_hex(body) == expected["next_request_sha256"]
    assert body == (case / "request.bytes").read_bytes()
    return Ok(body)


def _breaks_history(event: Event) -> bool:
    if isinstance(event, HookDecisionEvent):
        d = event.data
        return d.hook == "before_input" and d.decision in ("deny", "failed")
    return isinstance(event, CompactedEvent | ContextEditedEvent | SettingsChangedEvent)


def _history_is_prefix(case: Path, events: Sequence[Event], next_body: bytes) -> None:
    """Step 5, the cache-reuse property: each turn request is a byte prefix of the next turn
    request unless an edit, compaction, settings change or denied input lies between."""
    read = artifacts(case)
    previous: bytes | None = None
    for event in [*events, None]:
        if event is not None and _breaks_history(event):
            previous = None
            continue
        if event is None:
            body = next_body
        elif isinstance(event, ModelRequestEvent) and event.data.purpose != "compaction":
            got = read(event.data.request_ref.sha256)
            assert isinstance(got, Ok)
            body = got.value
        else:
            continue
        assert previous is None or body.startswith(previous)
        previous = body


@pytest.mark.parametrize("name", cases("render"))
def test_render_case(name: str) -> None:
    case = CASES / name
    meta, expected = load(case, "case.json"), load(case, "expected.json")
    log = (case / "log.jsonl").read_bytes()
    read = asyncio.run(import_and_read(case, log, now_of(meta)))
    result = _next(case, read.value.fold.events) if isinstance(read, Ok) else read
    if expected["outcome"] == "error":
        assert isinstance(result, Err), result
        assert json.loads(result.error) == expected["error"]
        return
    assert isinstance(read, Ok), read
    if "state" in expected:
        assert read.value.state.to_json() == expected["state"]
    events = read.value.fold.events
    assert isinstance(result, Ok), result
    _history_is_prefix(case, events, result.value)


READER_VIEW = [n for n in cases(*LATER, *OWN_RUNNER) if own(CASES / n, "log.jsonl").exists()]


@pytest.mark.parametrize("name", READER_VIEW)
def test_reader_view(name: str) -> None:
    """Every later-kind log imports; its pinned reader state and valid prefix match."""
    case = CASES / name
    meta, expected = load(case, "case.json"), load(case, "expected.json")
    log = own(case, "log.jsonl").read_bytes()
    verified = verify_export(log, now_of(meta))
    assert isinstance(verified, Ok), verified
    if "state" in expected:
        assert verified.value.state.to_json() == expected["state"]
    assert verified.value.head_verified == expected.get("head_verified", True)
    if "committed_bytes" in expected:
        assert verified.value.committed_bytes == expected["committed_bytes"]
    # Import replays every recorded request in the corpus: C7 and its request_ref bytes.
    read = asyncio.run(import_and_read(case, log, now_of(meta)))
    assert isinstance(read, Ok), read
    assert read.value.state == verified.value.state


@pytest.mark.parametrize("name", cases(*LATER))
def test_later_kind_steps(name: str) -> None:
    kind = load(CASES / name, "case.json")["kind"]
    assert isinstance(kind, str)
    pytest.skip(f"{kind}: {LATER[kind]}")
