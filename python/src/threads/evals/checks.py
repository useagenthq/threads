"""The free checks of one case (spec lane 22, B.1-B.3), cheapest first: replay the recorded
requests with the code running now, rerun the recorded turn offline, and compare the recorded
config with the agent as it is pinned now. Each returns a value (report JSON); only bugs raise."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import JsonValue

from threads.agents.definition import DryPin
from threads.evals.case_dir import CaseDir
from threads.evals.compare import (
    Mismatch,
    Wire,
    canonical,
    first_line,
    first_mismatch,
    matcher_mismatch,
    matches,
)
from threads.evals.drift import DriftResult, Recorded, drift
from threads.evals.offline import offline_block
from threads.evals.rerun import Refused, RerunInput, rerun
from threads.log import (
    Event,
    ModelRequestEvent,
    ThreadStartedEvent,
    ToolSpec,
    UnknownEvent,
    UserInputEvent,
)
from threads.reduce.handlers import to_json
from threads.render.verify import verify_requests
from threads.result import Err
from threads.store import Draft, verify_export
from threads.store.artifacts import MemoryArtifacts


@dataclass(frozen=True, slots=True)
class CaseLog:
    """What the case log holds before the turn: its events and the tools in effect."""

    events: tuple[Event, ...]
    tools: Mapping[str, ToolSpec]


def _artifacts(case: CaseDir) -> MemoryArtifacts:
    store = MemoryArtifacts()
    for data in case.artifacts:
        store.put(data)
    return store


def _failed(code: str, seq: int | None) -> dict[str, JsonValue]:
    out: dict[str, JsonValue] = {"ok": False, "code": code}
    if seq is not None:
        out["seq"] = seq
    return out


def replay_check(case: CaseDir) -> tuple[dict[str, JsonValue], CaseLog | None]:
    """B.1: the verifier Thread.replay() runs, over the case log and its artifacts."""
    verified = verify_export(case.log, case.meta.clock.now)
    if isinstance(verified, Err):
        return _failed(verified.error.code, verified.error.seq), None
    events = tuple(e for e in verified.value.fold.events if not isinstance(e, UnknownEvent))
    checked = verify_requests(events, _artifacts(case).get)
    if isinstance(checked, Err):
        return _failed(checked.error.code, checked.error.seq), None
    return {"ok": True}, CaseLog(events, dict(verified.value.fold.tools))


def skip_reason(case: CaseDir, log: CaseLog) -> str | None:
    """Why the case can't rerun offline, as the report states it."""
    block = offline_block(case, log.tools)
    if block is None:
        return None
    offline = case.meta.offline
    types = offline.types if offline is not None and isinstance(offline.types, list) else []
    listed = f" ({', '.join(types)})" if types else ""
    return f"offline_not_runnable:{block}{listed}"


V1_HINT = "re-save this case (sandbox.json v1 keeps previews only)"


def _wire(event: Event | UnknownEvent) -> Wire:
    wire = to_json(event)
    return wire if isinstance(wire, dict) else {}


def _mismatch(case: CaseDir, got: Sequence[Wire]) -> Mismatch | None:
    if case.recorded is not None:
        found = first_mismatch([_wire(e) for e in case.recorded], got)
    elif case.matchers is not None:
        found = matcher_mismatch(case.matchers, got)
    else:
        found = None
    if found is None:
        return None
    v1 = case.sandbox is not None and case.sandbox.v1 is not None
    return (
        Mismatch(found.index, found.want, found.got, V1_HINT)
        if v1 and found.want == "tool_result"
        else found
    )


def input_of(case: CaseDir) -> Draft | None:
    """What the rerun sends: the recorded user_input as it was appended, or the case's text."""
    recorded = next((e for e in case.recorded or () if isinstance(e, UserInputEvent)), None)
    if recorded is not None:
        wire = _wire(recorded)
        data, actor = wire.get("data"), wire.get("actor")
        if isinstance(data, dict) and isinstance(actor, dict):
            return Draft("user_input", data, actor)
    text = case.meta.input.text if case.meta.input is not None else None
    if text is None:
        return None
    operator: dict[str, JsonValue] = {
        "kind": "user",
        "principal": {"issuer": "api", "tenant": "local", "subject": "operator"},
    }
    return Draft("user_input", {"source": "api", "text": text}, operator)


def _mismatch_json(m: Mismatch) -> JsonValue:
    out: dict[str, JsonValue] = {"index": m.index, "want": m.want, "got": m.got}
    if m.hint is not None:
        out["hint"] = m.hint
    return out


async def rerun_check(case: CaseDir) -> dict[str, JsonValue] | str:
    """B.2: the promoted rerun, and how it compares with the recording; a refusal's reason."""
    out = await rerun(
        RerunInput(
            case.log,
            case.artifacts,
            case.meta.clock.now,
            case.model,
            case.sandbox,
            case.stubs,
            case.extensions,
            input_of(case),
            case.recorded,
        )
    )
    if isinstance(out, Refused):
        return f"rerun: {out.code}"
    got = [_wire(e) for e in out.appended]
    mismatch = _mismatch(case, got)
    unmatched: list[JsonValue] = [dict(m) for m in case.must if not any(matches(m, e) for e in got)]
    stubs_unmatched = 0 if out.stubs is None else out.stubs[1]
    clean = (
        mismatch is None
        and not unmatched
        and out.script_left + out.unexpected + out.left == 0
        and stubs_unmatched + out.unrecorded_calls + out.unrecorded_hooks == 0
    )
    check: dict[str, JsonValue] = {
        "ok": clean,
        "unmatched": unmatched,
        "script_left": out.script_left,
        "stubs_unmatched": stubs_unmatched,
        "unrecorded_calls": out.unrecorded_calls,
        "unrecorded_hooks": out.unrecorded_hooks,
    }
    if mismatch is not None:
        check["mismatch"] = _mismatch_json(mismatch)
    return check


def _described(m: JsonValue) -> str:
    kind = str(m.get("type")) if isinstance(m, dict) else ""
    data = m.get("data") if isinstance(m, dict) else None
    return kind if data is None else f"{kind}{canonical(data)}"


def _int(check: Mapping[str, JsonValue], key: str) -> int:
    value = check.get(key)
    return value if isinstance(value, int) else 0


def rerun_reason(check: Mapping[str, JsonValue]) -> str:
    """Why a rerun failed, in one line: the first of its problems in a fixed order."""
    unmatched = check.get("unmatched")
    first = unmatched[0] if isinstance(unmatched, list) and unmatched else None
    m = check.get("mismatch")
    counted = [
        ("unrecorded_hooks", "unrecorded_hook"),
        ("unrecorded_calls", "unrecorded_call"),
        ("stubs_unmatched", "unmatched_external_op"),
    ]
    code = next((code for key, code in counted if _int(check, key) > 0), None)
    if code is not None:
        return f"rerun: {code}"
    if isinstance(m, dict):
        got, want, hint = m.get("got"), m.get("want"), m.get("hint")
        tail = "" if hint is None else f"; {hint}"
        at = f"event {m.get('index')} is {got or 'missing'}"
        return f"rerun: {at}, recorded {want or 'nothing'}{tail}"
    if first is not None:
        return f"rerun: unmatched {_described(first)}"
    if _int(check, "script_left") > 0:
        return f"rerun: {_int(check, 'script_left')} recorded model replies left"
    return "rerun: recorded results left over"


def drift_check(case: CaseDir, log: CaseLog, pins: Sequence[DryPin]) -> DriftResult | None:
    """B.3: the recorded config against the agents given, by their dry pins."""
    started = next((e for e in log.events if isinstance(e, ThreadStartedEvent)), None)
    if started is None:
        return None
    data = to_json(started.data)
    if not isinstance(data, dict):
        return None
    return drift(Recorded(data, case.line0 or _last_line0(case, log)), pins)


def _last_line0(case: CaseDir, log: CaseLog) -> bytes | None:
    """A case saved before line0.json: line 0 of its log's last recorded request, if any."""
    requests = [e for e in log.events if isinstance(e, ModelRequestEvent)]
    if not requests:
        return None
    got = _artifacts(case).get(requests[-1].data.request_ref.sha256)
    return None if isinstance(got, Err) else first_line(got.value)


def drift_reason(d: DriftResult) -> str:
    """A drift result as its one-line report reason."""
    if d.agents is not None:
        return "agent_not_found"
    tools = d.tools or {}
    names = [
        *(f"+{n}" for n in tools.get("added", ())),
        *(f"-{n}" for n in tools.get("removed", ())),
        *(f"~{n}" for n in tools.get("changed", ())),
    ]
    kinds = [f"tools ({', '.join(names)})" if k == "tools" and names else k for k in d.kinds]
    return f"drift: {', '.join(kinds)}"
