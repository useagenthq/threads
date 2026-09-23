"""`Thread.save_case`: a saved case is data in the app repo. It holds the
branch export through the snapshot it restores, every artifact that export references, and
case.json with the assertion, the effects policy, the snapshot and the declared dependencies.
A missing artifact or snapshot fails before anything is written."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import JsonValue

from threads.log import EventId, ParseError, SnapshotEvent
from threads.log.jcs import canonicalize
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.sandbox.protocol import Sandbox
from threads.store import SqliteStore, VerifiedLog, verify_export
from threads.thread.fork import fork_point

_NAME: Final = re.compile(r"[a-z0-9][a-z0-9-]*")
_REF_KEYS: Final = frozenset({"sha256", "bytes", "media_type"})


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
    if not request.expect.must or not _NAME.fullmatch(request.name):
        return Err(ParseError("invalid_request", "a case needs a name and a `must` assertion"))
    found = _snapshot(log, request.at)
    if isinstance(found, Err):
        return found
    snap = found.value
    checked = _dependencies(snap, sandbox)
    if checked is not None:
        return Err(checked)
    exported = await store.export_through(snap.branch_id, snap.seq)
    if isinstance(exported, Err):
        return exported
    blobs = await _artifacts(store, exported.value, log.fold.now)
    if isinstance(blobs, Err):
        return blobs
    folder = Path(request.dir) / request.name
    (folder / "artifacts").mkdir(parents=True, exist_ok=True)
    (folder / "log.jsonl").write_bytes(exported.value)
    for sha, data in blobs.value.items():
        (folder / "artifacts" / sha).write_bytes(data)
    portable = snap.data.provider == "fake"
    (folder / "case.json").write_bytes(_case_json(request, snap, portable))
    return Ok(SavedCase(str(folder), portable))


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
    """The case restores the snapshot and stubs sandbox egress, so the provider must be here
    and must enforce egress."""
    provider = snap.data.provider
    if sandbox is None or sandbox.info.provider != provider:
        return ParseError("case_missing_dependency", f"no {provider} sandbox adapter", snap.seq)
    if sandbox.info.egress != "enforced":
        message = f"{provider} can't enforce deny-all egress"
        return ParseError("egress_policy_unsupported", message, snap.seq)
    return None


async def _artifacts(
    store: SqliteStore, export: bytes, now: int
) -> Ok[dict[str, bytes]] | Err[ParseError]:
    """Every artifact the export's events reference, read and verified."""
    read = verify_export(export, now)
    if isinstance(read, Err):
        return read
    refs: set[str] = set()
    for event in read.value.fold.events:
        _refs(to_json(event), refs)
    blobs: dict[str, bytes] = {}
    for sha in sorted(refs):
        got = await store.get_artifact(sha)
        if isinstance(got, Err):
            return Err(ParseError("case_missing_dependency", got.error.message))
        blobs[sha] = got.value
    return Ok(blobs)


def _refs(value: JsonValue, into: set[str]) -> None:
    if isinstance(value, list):
        for item in value:
            _refs(item, into)
    elif isinstance(value, dict):
        sha = value.get("sha256")
        if frozenset(value) == _REF_KEYS and isinstance(sha, str):
            into.add(sha)
        for item in value.values():
            _refs(item, into)


def _case_json(request: CaseRequest, snap: SnapshotEvent, portable: bool) -> bytes:
    data = snap.data
    case: JsonValue = {
        "name": request.name,
        "external_effects": request.external_effects,
        "expect": {
            "must": [dict(m) for m in request.expect.must],
            "expect": [dict(m) for m in request.expect.expect],
        },
        "snapshot": {
            "event_id": snap.event_id,
            "branch_id": snap.branch_id,
            "seq": snap.seq,
            "provider": data.provider,
            "snapshot_id": data.snapshot_id,
            "capture_class": data.capture_class,
            "expires_at": data.expires_at,
        },
        "dependencies": {
            "sandbox": {"provider": data.provider, "capture_classes": [data.capture_class]},
            "model": "recorded",
        },
        "portable": portable,
    }
    match canonicalize(case):
        case Ok(value=text):
            return text.encode("utf-8") + b"\n"
        case Err(error=reason):
            raise ValueError(f"case.json: {reason}")
