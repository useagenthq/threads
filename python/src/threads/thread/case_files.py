"""The scripts a saved case replays its turn against (spec/conformance/case.schema.json
ModelScript, StubScript, SandboxScript v2), read off the recorded turn. Built from typed values,
so they are valid by construction."""

from collections.abc import Awaitable, Callable, Sequence

from pydantic import JsonValue

from threads.evals.offline import FRAMEWORK_TOOLS
from threads.log import (
    EffectBeginEvent,
    EffectCommitEvent,
    Event,
    JsonObject,
    ModelResponseEvent,
    ParseError,
    ToolCallEvent,
    ToolResultEvent,
)
from threads.log.digest import canonical_sha256
from threads.reduce.handlers import to_json
from threads.result import Err, Ok

type ReadArtifact = Callable[[str], Awaitable[Ok[bytes] | Err[ParseError]]]


def model_script(turn: Sequence[Event]) -> JsonValue:
    """The recorded model responses, in order: the case's model.json."""
    return {
        "responses": [
            {
                "content": [to_json(p) for p in e.data.content],
                "stop_reason": e.data.stop_reason,
                "usage": to_json(e.data.usage),
            }
            for e in turn
            if isinstance(e, ModelResponseEvent)
        ]
    }


def args_hash(input: JsonObject) -> str:
    """sha256 of a call's RFC 8785 input: how stubs and sandbox results are keyed."""
    hashed = canonical_sha256(dict(input))
    if not isinstance(hashed, Ok):
        raise AssertionError("a tool_call input is canonical JSON")
    return hashed.value


def _result(turn: Sequence[Event], call_id: str) -> ToolResultEvent | None:
    return next(
        (e for e in turn if isinstance(e, ToolResultEvent) and e.data.call_id == call_id), None
    )


def _begun(turn: Sequence[Event], call_id: str) -> bool:
    return any(isinstance(e, EffectBeginEvent) and e.data.call_id == call_id for e in turn)


async def _output(turn: Sequence[Event], call_id: str, read: ReadArtifact) -> str:
    """A mediated call's full recorded output: the committed artifact, else the result preview."""
    commit = next(
        (e for e in turn if isinstance(e, EffectCommitEvent) and e.data.call_id == call_id), None
    )
    if commit is not None:
        got = await read(commit.data.result_ref.sha256)
        if isinstance(got, Ok):
            return got.value.decode("utf-8")
    result = _result(turn, call_id)
    return "" if result is None else result.data.preview


async def stub_script(turn: Sequence[Event], read: ReadArtifact) -> JsonValue:
    """Every mediated call (one with effect events) as a stub, by (tool, args_hash, occurrence)."""
    seen: dict[tuple[str, str], int] = {}
    stubs: list[JsonValue] = []
    for e in turn:
        if not isinstance(e, ToolCallEvent) or not _begun(turn, e.data.call_id):
            continue
        key = (e.data.name, args_hash(e.data.input))
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        result = _result(turn, e.data.call_id)
        stubs.append(
            {
                "tool": key[0],
                "args_hash": key[1],
                "occurrence": occurrence,
                "output": await _output(turn, e.data.call_id, read),
                "is_error": False if result is None else result.data.is_error,
            }
        )
    return {"stubs": stubs}


def sandbox_results(turn: Sequence[Event]) -> JsonValue | None:
    """sandbox.json v2: every executed read-only call's recorded result, keyed like stubs.json but
    counting occurrences from 1, with its content parts and spill ref so it replays in full.
    Framework tools run from the log in the rerun too, so they are never recorded."""
    seen: dict[tuple[str, str], int] = {}
    results: list[JsonValue] = []
    for e in turn:
        if not isinstance(e, ToolCallEvent) or e.data.name in FRAMEWORK_TOOLS:
            continue
        result = _result(turn, e.data.call_id)
        if _begun(turn, e.data.call_id) or result is None or result.data.origin != "executed":
            continue
        key = (e.data.name, args_hash(e.data.input))
        seen[key] = seen.get(key, 0) + 1
        wire = to_json(result.data)
        if not isinstance(wire, dict):
            raise AssertionError("a result is an object")
        entry: dict[str, JsonValue] = {
            "tool": key[0],
            "args_hash": key[1],
            "occurrence": seen[key],
            "is_error": result.data.is_error,
            "preview": result.data.preview,
        }
        entry |= {k: wire[k] for k in ("content", "ref") if k in wire}
        results.append(entry)
    return {"version": 2, "results": results} if results else None
