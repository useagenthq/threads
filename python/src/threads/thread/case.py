"""`Thread.save_case` (spec/api.json `saveCase`): any completed turn as a `stub` conformance case in
the spec/conformance layout. The log is the branch through the event before the turn's run; the
turn itself becomes input.text, model.json, stubs.json (mediated calls), sandbox.json (read-only
results), extensions.json (hook and recall outcomes), line0.json and `appended`. No sandbox
snapshot is needed: the offline rerun replays recorded results only.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import JsonValue, TypeAdapter, ValidationError

from threads._generated.eval_v1 import Rubric
from threads.evals.compare import first_line, matches
from threads.log import (
    Event,
    EventId,
    ModelRequestEvent,
    ParseError,
    ToolCallEvent,
    UnknownEvent,
)
from threads.log.digest import canonical_sha256, sha256_hex
from threads.log.jcs import canonicalize
from threads.reduce import Fold
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.sandbox.protocol import Sandbox
from threads.store import SqliteStore, VerifiedLog
from threads.thread.case_files import model_script, sandbox_results, stub_script
from threads.thread.case_hooks import extension_script
from threads.thread.case_log import IMPLS, artifact_refs, per_impl
from threads.thread.case_turn import Offline, Turn, find_turn, offline_reason
from threads.thread.fork import fork_point

_NAME: Final = re.compile(r"[a-z0-9][a-z0-9-]*")
_RUBRIC: Final = TypeAdapter[Sequence[str]](Rubric)


@dataclass(frozen=True, slots=True)
class CaseExpectation:
    """spec/api.json `CaseExpectation`: `must` matchers fail the case; `expect` is recorded."""

    must: tuple[Mapping[str, JsonValue], ...]
    expect: tuple[Mapping[str, JsonValue], ...] = ()


@dataclass(frozen=True, slots=True)
class SavedCase:
    path: str
    portable: bool
    """True when the case reruns offline from its directory alone."""
    reason: str | None = None
    """Why it can't, when it can't: artifact_missing, unsettled_effect, content_input,
    child_threads, team_calls or extension_events."""


@dataclass(frozen=True, slots=True)
class CaseRequest:
    name: str
    expect: CaseExpectation
    external_effects: Literal["stub"]
    at: EventId | None
    dir: str
    rubric: tuple[str, ...] | None = None


def _invalid(message: str) -> Err[ParseError]:
    return Err(ParseError("invalid_request", message))


def _check(request: CaseRequest, sandbox: Sandbox | None) -> ParseError | None:
    if not request.expect.must:
        return ParseError("invalid_request", "a case needs an assertion")
    if not _NAME.fullmatch(request.name):
        return ParseError("invalid_request", f"case name {request.name} is not lowercase-kebab")
    if request.rubric is not None:
        try:
            _RUBRIC.validate_python(list(request.rubric), strict=True)
        except ValidationError:
            return ParseError(
                "invalid_request",
                "rubric: 1 to 20 criteria, each 1 to 500 characters; pass rubric=None for none",
            )
    if sandbox is not None and sandbox.info.egress != "enforced":
        why = "a case needs a sandbox that enforces deny-all egress"
        return ParseError("egress_policy_unsupported", why)
    return None


def _wire(event: Event) -> dict[str, JsonValue]:
    wire = to_json(event)
    if not isinstance(wire, dict):
        raise AssertionError("an event is an object")
    return wire


def _turn(
    log: VerifiedLog, sandbox: Sandbox | None, request: CaseRequest
) -> Ok[Turn] | Err[ParseError]:
    """The turn to save, checked before anything is read or written: the request is valid and
    every `must` matcher meets something the recorded turn appended."""
    checked = _check(request, sandbox)
    if checked is not None:
        return Err(checked)
    found = find_turn(log.fold.events, request.at)
    if isinstance(found, Err):
        return found
    wires = [_wire(e) for e in found.value.events]
    missed = next((m for m in request.expect.must if not any(matches(m, w) for w in wires)), None)
    if missed is not None:
        return _invalid(f"must {missed.get('type')} matches nothing the recorded turn appended")
    return found


async def save_case(
    store: SqliteStore, log: VerifiedLog, sandbox: Sandbox | None, request: CaseRequest
) -> Ok[SavedCase] | Err[ParseError]:
    found = _turn(log, sandbox, request)
    if isinstance(found, Err):
        return found
    turn = found.value
    exported = await store.export_through(turn.input.branch_id, turn.restore_seq)
    if isinstance(exported, Err):
        return exported
    files = await _files(store, log.fold, turn, request, exported.value)
    if isinstance(files, Err):
        return files
    written, offline = files.value
    folder = Path(request.dir) / request.name
    (folder / "artifacts").mkdir(parents=True, exist_ok=True)
    for name, data in written.items():
        (folder / name).write_bytes(data)
    if offline is None:
        return Ok(SavedCase(str(folder), portable=True))
    return Ok(SavedCase(str(folder), portable=False, reason=offline.reason))


async def _copy(store: SqliteStore, refs: Sequence[str], into: dict[str, bytes]) -> list[str]:
    """Copies each ref's bytes into artifacts/; the shas that are gone from the store."""
    gone: list[str] = []
    for sha in refs:
        got = await store.get_artifact(sha)
        if isinstance(got, Ok):
            into[f"artifacts/{sha}"] = got.value
        else:
            gone.append(sha)
    return gone


def _turn_refs(turn: Turn) -> list[str]:
    """Every artifact a turn event other than a model request names."""
    lines = [
        canonical_json(_wire(e)).encode("utf-8")
        for e in turn.events
        if not isinstance(e, ModelRequestEvent)
    ]
    return artifact_refs(b"\n".join(lines) + b"\n") if lines else []


async def _line0(store: SqliteStore, turn: Turn) -> bytes | None:
    """Line 0 of the turn's first request artifact: what drift compares the agent against."""
    request = next((e for e in turn.events if isinstance(e, ModelRequestEvent)), None)
    if request is None:
        return None
    got = await store.get_artifact(request.data.request_ref.sha256)
    return first_line(got.value) if isinstance(got, Ok) else None


async def _files(
    store: SqliteStore, fold: Fold, turn: Turn, request: CaseRequest, export: bytes
) -> Ok[tuple[dict[str, bytes], Offline | None]] | Err[ParseError]:
    files: dict[str, bytes] = {}
    built = await stub_script(turn.events, store.get_artifact)
    # A mediated call whose committed output is gone makes the case unreplayable, the same way a
    # missing chain artifact does.
    if isinstance(built, Err):
        return Err(ParseError("case_missing_dependency", built.error.message))
    stubs = built.value
    consumed = len(_list(stubs, "stubs"))
    appended: list[JsonValue] = [_wire(e) for e in turn.events]
    for impl in IMPLS:
        log = per_impl(export, impl, fold.now)
        if isinstance(log, Err):
            return log
        files[f"log.{impl}.jsonl"] = log.value[0]
        files[f"expected.{impl}.json"] = _json(
            {
                "outcome": "ok",
                "state": log.value[1],
                "appended": appended,
                "stubs": {"consumed": consumed, "unmatched": 0},
            }
        )
    gone = await _copy(store, artifact_refs(export), files)
    if gone:
        return Err(ParseError("case_missing_dependency", f"artifact {gone[0]} is gone"))
    later = [e for e in fold.events if e.seq > turn.restore_seq and not isinstance(e, UnknownEvent)]
    missing = await _copy(store, _turn_refs(turn), files)
    offline = Offline("artifact_missing") if missing else offline_reason(turn, later)
    sandbox, extensions = sandbox_results(turn.events), extension_script(turn.events)
    prefix = await _line0(store, turn)
    if sandbox is not None:
        files["sandbox.json"] = _json(sandbox)
    if extensions is not None:
        files["extensions.json"] = _json(extensions)
    if prefix is not None:
        files["line0.json"] = prefix
    files["model.json"] = _json(model_script(turn.events))
    files["stubs.json"] = _json(stubs)
    meta = _case(request, fold, turn, offline, prefix)
    files["case.json"] = _json(meta | _scripts(sandbox, extensions))
    return Ok((files, offline))


def _scripts(sandbox: JsonValue | None, extensions: JsonValue | None) -> dict[str, JsonValue]:
    out: dict[str, JsonValue] = {"model_script": "model.json", "stub_script": "stubs.json"}
    if sandbox is not None:
        out["sandbox_script"] = "sandbox.json"
    if extensions is not None:
        out["extension_script"] = "extensions.json"
    return out


def _snapshot(fold: Fold, seq: int) -> JsonValue | None:
    """A sandbox snapshot fork point exactly at the restore point, for a live run."""
    for at, event_id in fold.fork_points:
        point = fork_point(fold, event_id) if at == seq else None
        if isinstance(point, Ok):
            return {"event_id": event_id, "provider": point.value.data.provider}
    return None


def _case(
    request: CaseRequest, fold: Fold, turn: Turn, offline: Offline | None, prefix: bytes | None
) -> dict[str, JsonValue]:
    text = turn.input.data.text
    expect: dict[str, JsonValue] = {
        "must": [dict(m) for m in request.expect.must],
        "expect": [dict(m) for m in request.expect.expect],
    }
    meta: dict[str, JsonValue] = {
        "name": request.name,
        "family": "log_fork_test",
        "kind": "stub",
        "description": (
            f"Saved from branch {turn.input.branch_id}: replays the turn of input "
            f"{turn.input.event_id} with every mediated operation stubbed."
        ),
        "clock": {"now": fold.now},
        "input": {"text": text} if isinstance(text, str) else {},
        "expect": expect,
    }
    if request.rubric is not None:
        meta["rubric"] = list(request.rubric)
    snapshot = _snapshot(fold, turn.restore_seq)
    if snapshot is not None:
        meta["snapshot"] = snapshot
    if offline is not None:
        why: dict[str, JsonValue] = {"runnable": False, "reason": offline.reason}
        if offline.types is not None:
            why["types"] = list(offline.types)
        meta["offline"] = why
    if prefix is not None:
        meta["line0"] = {"sha256": sha256_hex(prefix)}
    return meta


def _list(value: JsonValue, key: str) -> list[JsonValue]:
    got = value.get(key) if isinstance(value, dict) else None
    return got if isinstance(got, list) else []


def canonical_json(value: JsonValue) -> str:
    match canonicalize(value):
        case Ok(value=text):
            return text
        case Err(error=reason):
            raise ValueError(f"saved case: {reason}")


def _json(value: JsonValue) -> bytes:
    return canonical_json(value).encode("utf-8") + b"\n"


def recorded_stubs(fold: Fold, after: int) -> list[JsonValue]:
    """Each recorded tool result after a snapshot, keyed as stub mode matches it: tool, args_hash
    and occurrence (a stub fork's replay)."""
    stubs: list[JsonValue] = []
    seen: dict[tuple[str, str], int] = {}
    for call in (e for e in fold.events if e.seq > after and isinstance(e, ToolCallEvent)):
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
