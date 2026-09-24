"""How a UI stream ends (spec/schema/ui/README.md, "Opening and closing"): first whatever the log
shows still open (a model step, a running legacy subagent) and the parts this connection opened
live, then the run's outcome. A pure function of the log; none of it carries an id."""

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import JsonValue

from threads.host.ui.ai_sdk import subagent
from threads.host.ui.facts import RunFacts, Spawned
from threads.host.ui.frame import Chunk, Protocol
from threads.host.ui.interrupts import answerable, interrupts
from threads.log import ParkAddress


@dataclass(frozen=True, slots=True)
class RunIds:
    """The ids the AG-UI run events echo: the client's on a POST, the threads ids on a GET."""

    thread_id: str
    run_id: str

    def json(self) -> dict[str, JsonValue]:
        return {"threadId": self.thread_id, "runId": self.run_id}


@dataclass(frozen=True, slots=True)
class Ending:
    """A run's outcome (host-api RunOutcome) as closing reads it."""

    status: str
    output: JsonValue = None
    pending: tuple[ParkAddress, ...] = ()
    code: str = ""
    """The failure's code, the budget limit hit, or the handoff's thread."""


def ending(outcome: JsonValue) -> Ending:
    """The host's JSON outcome (threads.host.outcome), read back."""
    if not isinstance(outcome, dict):
        raise AssertionError("a run outcome is an object")
    status = outcome.get("status")
    if not isinstance(status, str):
        raise AssertionError("a run outcome has a status")
    return Ending(status, outcome.get("output"), _pending(outcome.get("pending")), _code(outcome))


def _pending(raw: JsonValue) -> tuple[ParkAddress, ...]:
    items = raw if isinstance(raw, list) else []
    return tuple(ParkAddress.model_validate(a) for a in items)


def _code(outcome: dict[str, JsonValue]) -> str:
    error, budget, to = outcome.get("error"), outcome.get("budget"), outcome.get("to_thread_id")
    found = (
        error.get("code")
        if isinstance(error, dict)
        else budget.get("limit")
        if isinstance(budget, dict)
        else to
    )
    return found if isinstance(found, str) else ""


def closing(
    protocol: Protocol, end: Ending, facts: RunFacts, open_live: Sequence[str], ids: RunIds
) -> list[Chunk]:
    live = [text_end(protocol, i) for i in open_live]
    tail = _ai_sdk_end(end, facts) if protocol == "ai-sdk" else _ag_ui_end(end, facts, ids)
    return [*_open_state(protocol, end, facts), *live, *tail]


def text_end(protocol: Protocol, part: str) -> Chunk:
    if protocol == "ai-sdk":
        return {"type": "text-end", "id": part}
    return {"type": "TEXT_MESSAGE_END", "messageId": part}


def _open_state(protocol: Protocol, end: Ending, facts: RunFacts) -> list[Chunk]:
    step: list[Chunk] = []
    if facts.open_step() is not None:
        step = [
            {"type": "finish-step"}
            if protocol == "ai-sdk"
            else {"type": "STEP_FINISHED", "stepName": "model"}
        ]
    return [*step, *(_child(protocol, end, s) for s in facts.running())]


def _child(protocol: Protocol, end: Ending, s: Spawned) -> Chunk:
    parked = end.status == "parked"
    if protocol == "ai-sdk":
        return subagent(s.child, s.agent, "running" if parked else "stopped")
    if parked:
        outcome: JsonValue = {"type": "suspended"}
        return {"type": "SUBAGENT_FINISHED", "subagentRunId": s.child, "outcome": outcome}
    return {
        "type": "SUBAGENT_ERROR",
        "subagentRunId": s.child,
        "message": f"subagent {s.agent} stopped: {end.status}",
    }


def _ai_sdk_end(end: Ending, facts: RunFacts) -> list[Chunk]:
    match end.status:
        case "completed":
            reason = "content-filter" if facts.last_stop() == "refusal" else "stop"
            return [{"type": "finish", "finishReason": reason}]
        case "parked":
            reason = "tool-calls" if all(answerable(a) for a in end.pending) else "other"
            return [{"type": "finish", "finishReason": reason}]
        case "cancelled":
            return [{"type": "abort", "reason": "cancelled"}]
        case _:
            length = end.status == "failed" and end.code == "max_output"
            return [
                {"type": "error", "errorText": f"{end.status}: {end.code}"},
                {"type": "finish", "finishReason": "length" if length else "error"},
            ]


def _ag_ui_end(end: Ending, facts: RunFacts, ids: RunIds) -> list[Chunk]:
    finished: dict[str, JsonValue] = {"type": "RUN_FINISHED", **ids.json()}
    match end.status:
        case "completed":
            done: dict[str, JsonValue] = {**finished, "outcome": {"type": "success"}}
            if end.output is not None:
                done["result"] = end.output
            return [done]
        case "parked":
            outcome: JsonValue = {
                "type": "interrupt",
                "interrupts": interrupts(end.pending, facts),
            }
            return [{**finished, "outcome": outcome}]
        case "cancelled":
            return [{**finished, "outcome": {"type": "cancelled"}}]
        case _:
            message = f"{end.status}: {end.code}"
            return [{"type": "RUN_ERROR", "message": message, "code": end.status}]
