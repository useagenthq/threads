"""@ag-ui/client@1.0.0's event sequence rules (`verifyEvents`), ported for the events threads
sends: runs open and close in order, and every message, reasoning span, tool call, step and
subagent opens before it is used and closes before its run finishes. The Python suite runs every
AG-UI stream through it, as the TypeScript suite runs them through the stock client."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from pydantic import JsonValue

_OPENERS = {
    "TEXT_MESSAGE_START": "messages",
    "TOOL_CALL_START": "tools",
    "REASONING_START": "spans",
    "REASONING_MESSAGE_START": "reasoning",
    "STEP_STARTED": "steps",
}
_USERS = {
    "TEXT_MESSAGE_CONTENT": ("messages", False),
    "TEXT_MESSAGE_END": ("messages", True),
    "TOOL_CALL_ARGS": ("tools", False),
    "TOOL_CALL_END": ("tools", True),
    "REASONING_MESSAGE_CONTENT": ("reasoning", False),
    "REASONING_MESSAGE_END": ("reasoning", True),
    "REASONING_END": ("spans", True),
    "STEP_FINISHED": ("steps", True),
}
_KEYS = {
    "messages": "messageId",
    "reasoning": "messageId",
    "spans": "messageId",
    "tools": "toolCallId",
    "steps": "stepName",
}


@dataclass
class _Run:
    open: dict[str, set[str]] = field(
        default_factory=lambda: {k: set[str]() for k in (*_KEYS, "subagents")}
    )
    closed_subagents: set[str] = field(default_factory=set[str])


class AgUiSequenceError(Exception):
    pass


def check(events: Iterable[Mapping[str, JsonValue]]) -> None:
    """Raises AgUiSequenceError at the first event the stock client's verifier refuses."""
    run: _Run | None = None
    state = "none"  # none, active, finished, errored
    for e in events:
        kind = str(e.get("type"))
        if state == "errored" and kind != "RUN_STARTED":
            raise AgUiSequenceError(f"{kind} after RUN_ERROR")
        if state == "finished" and kind not in ("RUN_ERROR", "RUN_STARTED"):
            raise AgUiSequenceError(f"{kind} after RUN_FINISHED")
        if state == "none" and kind not in ("RUN_STARTED", "RUN_ERROR"):
            raise AgUiSequenceError("the first event must be RUN_STARTED")
        if kind == "RUN_STARTED":
            if state == "active":
                raise AgUiSequenceError("RUN_STARTED while a run is active")
            run, state = _Run(), "active"
            continue
        if kind == "RUN_ERROR":
            state = "errored"
            continue
        if run is None:
            raise AgUiSequenceError(f"{kind} outside a run")
        if kind == "RUN_FINISHED":
            _finish(run)
            state = "finished"
            continue
        _event(run, kind, e)


def _finish(run: _Run) -> None:
    for kind, ids in run.open.items():
        if ids:
            raise AgUiSequenceError(f"RUN_FINISHED while {kind} are active: {sorted(ids)}")


def _event(run: _Run, kind: str, e: Mapping[str, JsonValue]) -> None:
    if kind in _OPENERS:
        bucket = _OPENERS[kind]
        key = str(e.get(_KEYS[bucket]))
        if key in run.open[bucket]:
            raise AgUiSequenceError(f"{kind}: {key} is already active")
        run.open[bucket].add(key)
    elif kind in _USERS:
        bucket, closes = _USERS[kind]
        key = str(e.get(_KEYS[bucket]))
        if key not in run.open[bucket]:
            raise AgUiSequenceError(f"{kind}: no active {bucket} {key}")
        if closes:
            run.open[bucket].discard(key)
    elif kind.startswith("SUBAGENT_"):
        _subagent(run, kind, e)


def _subagent(run: _Run, kind: str, e: Mapping[str, JsonValue]) -> None:
    sub = e.get("subagentRunId")
    if not isinstance(sub, str):
        raise AgUiSequenceError(f"{kind} without a subagentRunId")
    active = run.open["subagents"]
    if kind == "SUBAGENT_STARTED":
        if sub in active or sub in run.closed_subagents:
            raise AgUiSequenceError(f"SUBAGENT_STARTED: {sub} was already started")
        active.add(sub)
        return
    if sub not in active:
        raise AgUiSequenceError(f"{kind}: no active subagent {sub}")
    if kind == "SUBAGENT_ERROR" and not isinstance(e.get("message"), str):
        raise AgUiSequenceError("SUBAGENT_ERROR without a message")
    active.discard(sub)
    run.closed_subagents.add(sub)
