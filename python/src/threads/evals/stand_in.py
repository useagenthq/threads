"""Stand-in extensions (spec lane 22, A.2): the offline rerun loads no user code, so each pinned
extension is replaced by one with its name that answers from the turn's records. A recorded hook
is defined only when the turn has a record for it and answers with the next record; every
observation hook is defined and answers only at the call its record is keyed to."""

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, assert_never

from pydantic import JsonValue

from threads._generated.eval_v1 import At, At1, ExtensionScript, HookRecord
from threads.evals.kinds import HOOK_KINDS
from threads.hooks.runner import Bound, Call, HookRunner
from threads.hooks.types import HookName
from threads.log import ToolCallData
from threads.reduce.handlers import to_json

_TIMED_OUT: Final = re.compile(r"timed out after (\d+) ms")
_DEFAULT_TIMEOUT_MS: Final = 5000
_SHARED: Final = 2
"""Extensions at one hook point that make their order observable."""


def _api(wire: str) -> HookName | None:
    """A wire hook name as the API names it (only stop_failure differs)."""
    name = "on_stop_failure" if wire == "stop_failure" else wire
    return next((h for h in _API_NAMES if h == name), None)


_API_NAMES: Final[tuple[HookName, ...]] = (
    "session_start",
    "session_end",
    "before_input",
    "before_model",
    "after_model",
    "before_tool",
    "permission_request",
    "permission_denied",
    "after_tool",
    "before_tool_result",
    "after_tool_batch",
    "before_compact",
    "after_compact",
    "on_stop",
    "on_stop_failure",
    "subagent_start",
    "subagent_stop",
    "before_model_switch",
    "after_model_switch",
    "notification",
)


def _texts(r: HookRecord) -> list[JsonValue]:
    return [i.text for i in r.injected if isinstance(i.text, str)]


def _reason(r: HookRecord) -> str:
    return r.reason if isinstance(r.reason, str) else ""


def _gate(r: HookRecord, go: str) -> dict[str, JsonValue]:
    """A gate's recorded decision: deny (and retry, continue) with the reason, else plain."""
    if r.decision in ("deny", "retry", "continue"):
        return {"decision": r.decision, "reason": _reason(r)}
    if r.decision == "guide":
        return {"decision": "guide", "text": _reason(r)}
    if r.decision == "ask":
        return (
            {"decision": "ask", "rule": _reason(r)}
            if isinstance(r.reason, str)
            else {"decision": "ask"}
        )
    if r.decision == "redact":
        spans: list[JsonValue] = (
            [to_json(s) for s in r.spans] if isinstance(r.spans, list | tuple) else []
        )
        return {"decision": "redact", "spans": spans}
    return {"decision": r.decision if r.decision in ("stop", "allow") else go}


def _injecting(r: HookRecord, go: str) -> JsonValue:
    """A gate that injects when it lets the work through (before_input, before_model)."""
    gate = _gate(r, go)
    if r.decision != "deny":
        gate["injections"] = _texts(r)
    return gate


def _value(r: HookRecord, hook: HookName) -> JsonValue:  # noqa: PLR0911 - one answer per hook class
    """The recorded outcome as the value the hook would return, so the loop appends it again."""
    match hook:
        case "session_start" | "after_tool_batch" | "after_compact":
            return _texts(r)
        case "after_tool":
            return [_reason(r)]
        case "before_input":
            return _injecting(r, "allow")
        case "before_model":
            return _injecting(r, "proceed")
        case "after_model" | "before_compact" | "before_tool_result":
            return _gate(r, "proceed")
        case "before_tool" | "permission_request" | "subagent_start" | "before_model_switch":
            return _gate(r, "allow")
        case "on_stop" | "subagent_stop":
            return _gate(r, "stop")
        case (
            "permission_denied"
            | "on_stop_failure"
            | "session_end"
            | "notification"
            | "after_model_switch"
        ):
            return None
        case _:
            assert_never(hook)


async def _fail(reason: str) -> JsonValue:
    """Reproduces a failed outcome: the same timeout, or a raise the runner records as `reason`
    (it records `<type>: <message>`)."""
    if _TIMED_OUT.fullmatch(reason):
        await asyncio.Event().wait()
    kind, _, message = reason.partition(": ")
    raise type(kind, (Exception,), {})(message)


@dataclass
class _Count:
    overrun: int = 0
    used: set[int] = field(default_factory=set[int])


def _call_id(args: Sequence[object]) -> str | None:
    first = args[0] if args else None
    return first.call_id if isinstance(first, ToolCallData) else None


def _keyed(records: Sequence[HookRecord], n: int, call_id: str | None) -> HookRecord | None:
    """The observation record this call belongs to: its call_id, else its trigger's occurrence."""
    for r in records:
        at = r.at
        if isinstance(at, At):
            if at.call_id == call_id:
                return r
        elif (at.occurrence if isinstance(at, At1) else 1) == n:
            return r
    return None


def _stand_in(name: str, records: Sequence[HookRecord], count: _Count) -> Bound:
    hooks: dict[HookName, Call] = {}
    for wire, kind in HOOK_KINDS.items():
        hook = _api(wire)
        own = [r for r in records if r.hook == wire]
        if hook is None or (kind == "recorded" and not own):
            continue
        hooks[hook] = _answering(name, hook, kind == "observation", own, count)
    timeouts = [m.group(1) for r in records if (m := _TIMED_OUT.fullmatch(_reason(r)))]
    return Bound(name, int(timeouts[0]) if timeouts else _DEFAULT_TIMEOUT_MS, hooks)


def _answering(
    name: str, hook: HookName, observation: bool, own: Sequence[HookRecord], count: _Count
) -> Call:
    calls = [0]

    async def answer(*args: object) -> object:
        calls[0] += 1
        if observation:
            record = _keyed(own, calls[0], _call_id(args))
            if record is None:
                return [] if hook == "after_tool" else None
        else:
            record = next((r for r in own if r.occurrence == calls[0]), None)
            if record is None:
                count.overrun += 1
                raise LookupError(f"unrecorded_hook: {name} {hook} call {calls[0]}")
        count.used.add(id(record))
        if record.decision == "failed":
            return await _fail(record.reason if isinstance(record.reason, str) else "")
        return _value(record, hook)

    return answer


def stand_in_order(records: Sequence[HookRecord]) -> list[str]:
    """Extension names in the order that reproduces the recorded order at shared hook points."""
    names = sorted({r.extension for r in records})
    before: dict[str, set[str]] = {n: set() for n in names}
    seen: set[tuple[str, str]] = set()
    for point in _points(records):
        order = list(dict.fromkeys(r.extension for r in point))
        if len(order) < _SHARED:
            continue
        declared = order[::-1] if point[0].hook.startswith("after_") else order
        for i, a in enumerate(declared):
            for b in declared[i + 1 :]:
                if (a, b) in seen or (b, a) in seen:
                    continue
                seen.add((a, b))
                before[b].add(a)
    out: list[str] = []
    while len(out) < len(names):
        ready = [n for n in names if n not in out and before[n] <= set(out)]
        # A cycle can't come from one loop's records; fall back to the name order.
        out.append(ready[0] if ready else next(n for n in names if n not in out))
    return out


def _points(records: Sequence[HookRecord]) -> list[list[HookRecord]]:
    """Runs of consecutive records at one hook point: the same hook and the same call."""

    def call(r: HookRecord) -> str:
        return r.at.call_id if isinstance(r.at, At) else ""

    points: list[list[HookRecord]] = []
    for r in records:
        if points and points[-1][0].hook == r.hook and call(points[-1][0]) == call(r):
            points[-1].append(r)
        else:
            points.append([r])
    return points


@dataclass(frozen=True, slots=True)
class StandIns:
    hooks: HookRunner
    _records: int
    _count: _Count

    def unrecorded(self) -> int:
        """Calls past the records, and records no call reached: each fails the rerun."""
        return self._count.overrun + self._records - len(self._count.used)


def stand_ins(script: ExtensionScript | None) -> StandIns:
    records = list(script.hooks) if script is not None else []
    count = _Count()
    bound = [
        _stand_in(name, [r for r in records if r.extension == name], count)
        for name in stand_in_order(records)
    ]
    return StandIns(HookRunner(bound), len(records), count)
