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
from pydantic import JsonValue, TypeAdapter

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
from threads.render import render, verify_requests
from threads.result import Err, Ok
from threads.store import SqliteStore, VerifiedLog, Writer, verify_export

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
IMPL = "threads-py"
LATER = {
    "recover": "leases exist, but semantic recovery and the loop are not built yet",
    "fork": "the fork operation (eligibility, sandbox restore, resource ledger) is not built yet",
    "stub": "stub mode is not built yet",
    "intake": "the host intake pipeline is not built yet",
    "policy": "the permission engine is not built yet",
}


def own(case: Path, name: str) -> Path:
    """A recover case ships one file per writer; this runner uses its own."""
    stem, dot, ext = name.partition(".")
    mine = case / f"{stem}.{IMPL}{dot}{ext}"
    return mine if mine.exists() else case / name


def load(case: Path, name: str) -> dict[str, JsonValue]:
    return _JSON.validate_json(own(case, name).read_bytes())


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
    opened = await SqliteStore.open()
    assert isinstance(opened, Ok)
    store = opened.value
    try:
        stored = await store.import_log(verified.value)
        assert stored == Ok(None)
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
        assert meta["kind"] in ("reduce", "render") or meta["kind"] in LATER, case.name


@pytest.mark.parametrize("name", cases("reduce"))
def test_reduce_case(name: str) -> None:
    case = CASES / name
    meta, expected = load(case, "case.json"), load(case, "expected.json")
    log = own(case, "log.jsonl").read_bytes()
    result = asyncio.run(import_and_read(log, now_of(meta)))
    assert own(case, "log.jsonl").read_bytes() == log
    if expected["outcome"] == "error":
        assert isinstance(result, Err)
        assert json.loads(result.error) == expected["error"]
        assert expected.get("appended", []) == []
        return
    assert isinstance(result, Ok), result
    assert verify_requests(result.value.fold.events, artifacts(case)) == Ok(None)
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


def _replay(case: Path, events: Sequence[Event]) -> Ok[bytes] | Err[ParseError]:
    """Steps 2-4: C7 and every recorded request, then the next request."""
    read = artifacts(case)
    verified = verify_requests(events, read)
    if isinstance(verified, Err):
        return verified
    rendered = render(events, read)
    if isinstance(rendered, Err):
        return rendered
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
    read = asyncio.run(import_and_read(log, now_of(meta)))
    assert isinstance(read, Ok), read
    if "state" in expected:
        assert read.value.state.to_json() == expected["state"]
    events = read.value.fold.events
    result = _replay(case, events)
    if expected["outcome"] == "error":
        assert isinstance(result, Err), result
        assert {"code": result.error.code, "seq": result.error.seq} == expected["error"]
        return
    assert isinstance(result, Ok), result
    _history_is_prefix(case, events, result.value)


READER_VIEW = [n for n in cases(*LATER) if own(CASES / n, "log.jsonl").exists()]


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
    read = asyncio.run(import_and_read(log, now_of(meta)))
    assert isinstance(read, Ok), read
    assert read.value.state == verified.value.state
    # Every recorded request in the corpus replays: C7 and its request_ref bytes.
    assert verify_requests(read.value.fold.events, artifacts(case)) == Ok(None)


@pytest.mark.parametrize("name", cases(*LATER))
def test_later_kind_steps(name: str) -> None:
    kind = load(CASES / name, "case.json")["kind"]
    assert isinstance(kind, str)
    pytest.skip(f"{kind}: {LATER[kind]}")


def test_foreign_writer_branch_refuses_to_append() -> None:
    """recover-foreign-writer-refused: the branch reads fine, but this runner may not append."""
    case = CASES / "recover-foreign-writer-refused"
    meta, expected = load(case, "case.json"), load(case, "expected.json")
    verified = verify_export(own(case, "log.jsonl").read_bytes(), now_of(meta))
    assert isinstance(verified, Ok)

    async def refused() -> Err[ParseError] | Ok[Writer]:
        opened = await SqliteStore.open()
        assert isinstance(opened, Ok)
        store = opened.value
        try:
            assert await store.import_log(verified.value) == Ok(None)
            branch = verified.value.segments[-1].header.branch_id
            return await store.acquire(branch, "runner", lambda: now_of(meta))
        finally:
            await store.close()

    result = asyncio.run(refused())
    assert isinstance(result, Err)
    assert {"code": result.error.code, "seq": result.error.seq} == expected["error"]
