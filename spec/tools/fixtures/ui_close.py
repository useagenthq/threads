# pyright: strict
"""Reference run slice, outcome and closing frames of a UI stream (spec/schema/ui/README.md,
"Opening and closing"). The outcome is the host-api RunOutcome as the log shows it."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from .common import arr, num, obj, text
from .run_end import run_end_seq, run_of
from .ui_map import AI, ag_started, subagent

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj
    from .ui_map import Facts

# turn_completed reasons whose failed code is model_error; the rest fail with their own name.
_MODEL_ERROR = ("error", "interrupted")

APPROVAL_SCHEMA: Obj = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision"],
    "properties": {
        "decision": {"enum": ["grant", "deny"]},
        "remember_rule": {
            "description": "grant only: appended as permission_rule_added. Must be one of the "
            "challenge's suggested_rules.",
            "$ref": "urn:threads:schema:events:v1#/$defs/PermissionRule",
        },
        "reason": {"type": "string"},
    },
}
ANSWER_SCHEMA: Obj = {
    "description": "The answer to an ask_user question: text, or the chosen options.",
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {
        "answer": {
            "oneOf": [
                {"type": "string", "minLength": 1},
                {"type": "array", "minItems": 1, "items": {"type": "string"}},
            ]
        }
    },
}


def run_slice(events: list[Obj], run_id: str) -> list[Obj]:
    """The run's events: its user_input through its end, or up to the next user_input."""
    start = next(i for i, e in enumerate(events) if e["event_id"] == run_id)
    end = run_end_seq(events, events[start])
    own = events[start:]
    if end is not None:
        return [e for e in own if num(e["seq"]) <= end]
    later = [i for i, e in enumerate(own) if i > 0 and e["type"] == "user_input"]
    return own[: later[0]] if later else own


def _parks(events: list[Obj]) -> list[Obj]:
    """The park addresses open at the log's end, in the order they were parked."""
    open_: list[Obj] = []
    for e in events:
        if e["type"] == "parked":
            open_.append(obj(obj(e["data"])["address"]))
        elif e["type"] == "resumed":
            open_.remove(obj(obj(e["data"])["address"]))
    return open_


def outcome(events: list[Obj], run_id: str) -> Obj | None:
    """{status, ...} of the run once it has ended or parked; None while it goes on."""
    request = next(e for e in events if e["event_id"] == run_id)
    run = run_of(events, request)
    status = text(run["status"])
    own = run_slice(events, run_id)
    if status == "running":
        return None
    if status == "parked":
        pending: list[JsonValue] = [*_parks(events)]
        return {"status": "parked", "pending": pending}
    if status == "completed":
        return {"status": "completed", "output": _output(own, run["output_event_id"])}
    if status == "cancelled":
        return {"status": "cancelled"}
    return {"status": status, "code": _code(own, status)}


def _code(own: list[Obj], status: str) -> JsonValue:
    """A failed run's error code, the budget limit hit, or the handoff's thread."""
    if status == "failed":
        ended = obj([e for e in own if e["type"] == "turn_completed"][-1]["data"])
        reason = text(ended["reason"])
        return ended.get("code", "model_error" if reason in _MODEL_ERROR else reason)
    if status == "budget_exhausted":
        return obj([e for e in own if e["type"] == "budget_exceeded"][-1]["data"])["limit"]
    return obj([e for e in own if e["type"] == "handoff"][-1]["data"])["to_thread_id"]


def _output(own: list[Obj], response_id: JsonValue) -> JsonValue:
    accepted = [
        obj(e["data"])
        for e in own
        if e["type"] == "output_validated" and obj(e["data"])["outcome"] == "accepted"
    ]
    if accepted:
        return accepted[-1]["value"]
    response = next(e for e in own if e["event_id"] == response_id)
    return "".join(
        text(obj(p)["text"])
        for p in arr(obj(response["data"])["content"])
        if obj(p)["type"] == "text"
    )


def _iso(ms: int) -> str:
    at = datetime.datetime.fromtimestamp(ms // 1000, datetime.UTC)
    return at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def interrupt(address: Obj, f: Facts) -> Obj:
    kind, pid = text(address["kind"]), text(address["id"])
    if kind == "approval" and pid in f.challenges:
        ch = f.challenges[pid]
        call = text(ch["call_id"])
        name = text(f.calls[call]["name"]) if call in f.calls else "the call"
        return {
            "id": pid,
            "reason": "tool_approval",
            "toolCallId": call,
            "message": f"approve {name}",
            "responseSchema": APPROVAL_SCHEMA,
            "expiresAt": _iso(num(ch["expires_at"])),
        }
    if kind == "input":
        inp = obj(f.calls[pid]["input"]) if pid in f.calls else {}
        question = inp.get("question")
        return {
            "id": pid,
            "reason": "user_input",
            "toolCallId": pid,
            "message": question if isinstance(question, str) else "",
            "responseSchema": ANSWER_SCHEMA,
        }
    return {"id": pid, "reason": f.parks.get(f"{kind}:{pid}", kind)}


def text_end(p: str, pid: str) -> Obj:
    return (
        {"type": "text-end", "id": pid}
        if p == AI
        else {"type": "TEXT_MESSAGE_END", "messageId": pid}
    )


def _open_state(p: str, out: Obj, f: Facts) -> list[Obj]:
    frames: list[Obj] = []
    if f.open_step():
        frames.append(
            {"type": "finish-step"} if p == AI else {"type": "STEP_FINISHED", "stepName": "model"}
        )
    parked = out["status"] == "parked"
    for child, agent, _call in f.running():
        if p == AI:
            frames.append(subagent(child, agent, "running" if parked else "stopped"))
        elif parked:
            frames.append(
                {
                    "type": "SUBAGENT_FINISHED",
                    "subagentRunId": child,
                    "outcome": {"type": "suspended"},
                }
            )
        else:
            frames.append(
                {
                    "type": "SUBAGENT_ERROR",
                    "subagentRunId": child,
                    "message": f"subagent {agent} stopped: {out['status']}",
                }
            )
    return frames


def _answerable(a: JsonValue) -> bool:
    return obj(a)["kind"] in ("approval", "input")


def _ai_end(out: Obj, f: Facts) -> list[Obj]:
    status = out["status"]
    if status == "completed":
        return [
            {
                "type": "finish",
                "finishReason": "content-filter" if f.last_stop == "refusal" else "stop",
            }
        ]
    if status == "parked":
        reason = "tool-calls" if all(_answerable(a) for a in arr(out["pending"])) else "other"
        return [{"type": "finish", "finishReason": reason}]
    if status == "cancelled":
        return [{"type": "abort", "reason": "cancelled"}]
    length = status == "failed" and out["code"] == "max_output"
    return [
        {"type": "error", "errorText": f"{status}: {out['code']}"},
        {"type": "finish", "finishReason": "length" if length else "error"},
    ]


def _ag_end(out: Obj, f: Facts, ids: Obj) -> list[Obj]:
    status = out["status"]
    finished: Obj = {"type": "RUN_FINISHED", **ids}
    if status == "completed":
        done: Obj = {**finished, "outcome": {"type": "success"}}
        if out["output"] is not None:
            done["result"] = out["output"]
        return [done]
    if status == "parked":
        interrupts: list[JsonValue] = [interrupt(obj(a), f) for a in arr(out["pending"])]
        return [{**finished, "outcome": {"type": "interrupt", "interrupts": interrupts}}]
    if status == "cancelled":
        return [{**finished, "outcome": {"type": "cancelled"}}]
    return [{"type": "RUN_ERROR", "message": f"{status}: {out['code']}", "code": status}]


def closing(p: str, out: Obj, f: Facts, open_live: list[str], ids: Obj) -> list[Obj]:
    """The closing chunks: what is still open, the live parts, then the outcome."""
    end = _ai_end(out, f) if p == AI else _ag_end(out, f, ids)
    return _open_state(p, out, f) + [text_end(p, pid) for pid in open_live] + end


def preamble(f: Facts) -> list[Obj]:
    """What the log shows open at a replay's snapshot point."""
    step: list[Obj] = [{"type": "STEP_STARTED", "stepName": "model"}] if f.open_step() else []
    return step + [ag_started(c, a, k) for c, a, k in f.running()]
