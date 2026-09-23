"""`Thread.save_case`: a saved case is a `stub` conformance
case in exactly the spec/conformance layout, so the same runners replay it.

The log is the branch through the snapshot it restores, once per implementation (only the
writer that a branch's header names may append to it). What the run did after the snapshot
becomes the case's inputs: the next user input (`input.text`), the recorded model responses
(model.json) and the recorded tool results (stubs.json). Everything is built from typed
values, so the case is valid against case.schema.json by construction; tests check it.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import JsonValue

from threads.log import (
    EventId,
    ModelResponseEvent,
    ParseError,
    SnapshotEvent,
    ToolCallEvent,
    UserInputEvent,
)
from threads.log.digest import canonical_sha256
from threads.log.jcs import canonicalize
from threads.reduce import Fold
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.sandbox.protocol import Sandbox
from threads.store import SqliteStore, VerifiedLog
from threads.thread.case_log import IMPLS, artifact_refs, per_impl
from threads.thread.fork import fork_point

_NAME: Final = re.compile(r"[a-z0-9][a-z0-9-]*")


@dataclass(frozen=True, slots=True)
class CaseExpectation:
    """spec/api.json `CaseExpectation`: `must` matchers fail the case; `expect` is recorded."""

    must: tuple[Mapping[str, JsonValue], ...]
    expect: tuple[Mapping[str, JsonValue], ...] = ()


@dataclass(frozen=True, slots=True)
class SavedCase:
    path: str
    portable: bool
    """False when the case depends on a named provider."""


@dataclass(frozen=True, slots=True)
class CaseRequest:
    name: str
    expect: CaseExpectation
    external_effects: Literal["stub"]
    at: EventId | None
    dir: str


async def save_case(
    store: SqliteStore, log: VerifiedLog, sandbox: Sandbox | None, request: CaseRequest
) -> Ok[SavedCase] | Err[ParseError]:
    planned = _plan(log, sandbox, request)
    if isinstance(planned, Err):
        return planned
    snap, text = planned.value
    exported = await store.export_through(snap.branch_id, snap.seq)
    if isinstance(exported, Err):
        return exported
    blobs = await _artifacts(store, exported.value)
    if isinstance(blobs, Err):
        return blobs
    files = _files(request, exported.value, log.fold, snap, text)
    if isinstance(files, Err):
        return files
    folder = Path(request.dir) / request.name
    (folder / "artifacts").mkdir(parents=True, exist_ok=True)
    for name, data in (*files.value.items(), *blobs.value.items()):
        (folder / name).write_bytes(data)
    return Ok(SavedCase(str(folder), snap.data.provider == "fake"))


def _plan(
    log: VerifiedLog, sandbox: Sandbox | None, request: CaseRequest
) -> Ok[tuple[SnapshotEvent, str]] | Err[ParseError]:
    """What the case restores and replays, checked before anything is read or written."""
    if not request.expect.must or not _NAME.fullmatch(request.name):
        return Err(ParseError("invalid_request", "a case needs a name and a `must` assertion"))
    found = _snapshot(log, request.at)
    if isinstance(found, Err):
        return found
    checked = _dependencies(found.value, sandbox)
    if checked is not None:
        return Err(checked)
    text = _next_input(log.fold, found.value.seq)
    if text is None:
        return Err(ParseError("invalid_request", "no user input after the snapshot to replay"))
    return Ok((found.value, text))


def _snapshot(log: VerifiedLog, at: EventId | None) -> Ok[SnapshotEvent] | Err[ParseError]:
    """The snapshot the case restores: `at`, else the branch's latest fork point."""
    if at is None and not log.fold.fork_points:
        return Err(ParseError("no_snapshot_boundary", "the branch has no fork point"))
    point = log.fold.fork_points[-1][1] if at is None else at
    found = fork_point(log.fold, point)
    if isinstance(found, Err):
        # An expired snapshot is simply not a boundary a case can restore.
        return Err(ParseError("no_snapshot_boundary", found.error.message, found.error.seq))
    return found


def _dependencies(snap: SnapshotEvent, sandbox: Sandbox | None) -> ParseError | None:
    """A case must never run with open egress: the snapshot's provider must
    be here and must enforce deny-all egress."""
    provider = snap.data.provider
    if sandbox is None or sandbox.info.provider != provider:
        return ParseError("case_missing_dependency", f"no {provider} sandbox adapter", snap.seq)
    if sandbox.info.egress != "enforced":
        message = f"{provider} can't enforce deny-all egress"
        return ParseError("egress_policy_unsupported", message, snap.seq)
    return None


def _next_input(fold: Fold, after: int) -> str | None:
    event = next((e for e in fold.events if e.seq > after and isinstance(e, UserInputEvent)), None)
    text = None if event is None else event.data.text
    return text if isinstance(text, str) else None


async def _artifacts(store: SqliteStore, export: bytes) -> Ok[dict[str, bytes]] | Err[ParseError]:
    """Every artifact the export's lines reference, read and verified."""
    blobs: dict[str, bytes] = {}
    for sha in artifact_refs(export):
        got = await store.get_artifact(sha)
        if isinstance(got, Err):
            return Err(ParseError("case_missing_dependency", got.error.message))
        blobs[f"artifacts/{sha}"] = got.value
    return Ok(blobs)


def _files(
    request: CaseRequest, export: bytes, fold: Fold, snap: SnapshotEvent, text: str
) -> Ok[dict[str, bytes]] | Err[ParseError]:
    files: dict[str, bytes] = {}
    for impl in IMPLS:
        log = per_impl(export, impl, fold.now)
        if isinstance(log, Err):
            return log
        files[f"log.{impl}.jsonl"] = log.value[0]
        files[f"expected.{impl}.json"] = _json({"outcome": "ok", "state": log.value[1]})
    files["case.json"] = _json(_case(request, fold.now, text))
    files["model.json"] = _json({"responses": _responses(fold, snap.seq)})
    files["stubs.json"] = _json({"stubs": recorded_stubs(fold, snap.seq)})
    return Ok(files)


def _case(request: CaseRequest, now: int, text: str) -> JsonValue:
    expect: dict[str, JsonValue] = {"must": [dict(m) for m in request.expect.must]}
    if request.expect.expect:
        expect["expect"] = [dict(m) for m in request.expect.expect]
    return {
        "name": request.name,
        "family": "log_fork_test",
        "kind": "stub",
        "description": f"Saved case {request.name}: replays its snapshot in stub mode.",
        "clock": {"now": now},
        "model_script": "model.json",
        "stub_script": "stubs.json",
        "input": {"text": text},
        "expect": expect,
    }


def _responses(fold: Fold, after: int) -> list[JsonValue]:
    return [
        {
            "content": [to_json(part) for part in e.data.content],
            "stop_reason": e.data.stop_reason,
            "usage": to_json(e.data.usage),
        }
        for e in fold.events
        if e.seq > after and isinstance(e, ModelResponseEvent)
    ]


def recorded_stubs(fold: Fold, after: int) -> list[JsonValue]:
    """Each recorded tool result after the snapshot, keyed as stub mode matches it: tool,
    args_hash and occurrence."""
    stubs: list[JsonValue] = []
    seen: dict[tuple[str, str], int] = {}
    calls: Sequence[ToolCallEvent] = [
        e for e in fold.events if e.seq > after and isinstance(e, ToolCallEvent)
    ]
    for call in calls:
        result = fold.results.get(call.data.call_id)
        hashed = canonical_sha256(dict(call.data.input))
        if result is None or not isinstance(hashed, Ok):
            continue
        key = (call.data.name, hashed.value)
        seen[key] = seen.get(key, -1) + 1
        stubs.append(
            {
                "tool": key[0],
                "args_hash": key[1],
                "occurrence": seen[key],
                "output": result.preview,
                "is_error": result.is_error,
            }
        )
    return stubs


def _json(value: JsonValue) -> bytes:
    match canonicalize(value):
        case Ok(value=text):
            return text.encode("utf-8") + b"\n"
        case Err(error=reason):
            raise ValueError(f"saved case: {reason}")
