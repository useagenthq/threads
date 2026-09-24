# pyright: strict
"""Reference UI mapping (spec/schema/ui/README.md, "Mapping"): one committed event of a run as
AI SDK UI message stream chunks or AG-UI events, given the facts of the run's events before it.
Written from the README, independently of either runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import arr, obj, text
from .jcs import canonical

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

AI = "ai-sdk"
AG = "ag-ui"
_MODEL = ("model_request", "model_response", "model_response_recovered", "model_attempt_abandoned")
MODEL_STEP: Obj = {"type": "STEP_STARTED", "stepName": "model"}
STEP_DONE: Obj = {"type": "STEP_FINISHED", "stepName": "model"}


class Facts:
    """What a run's frames read from the events before one."""

    def __init__(self) -> None:
        self.purposes: dict[str, str] = {}
        self.answered: set[str] = set()
        self.last_turn: str | None = None
        self.last_stop: str | None = None
        self.proposed: set[str] = set()
        self.spawned: dict[str, tuple[str, str]] = {}  # child -> (agent, call_id), start order
        self.finished: set[str] = set()
        self.calls: dict[str, Obj] = {}  # call_id -> tool_call data
        self.challenges: dict[str, Obj] = {}  # challenge_id -> approval_requested data
        self.parks: dict[str, str] = {}  # "<kind>:<id>" -> reason

    def is_turn(self, request: str) -> bool:
        return self.purposes.get(request) != "compaction"

    def open_step(self) -> bool:
        return self.last_turn is not None and self.last_turn not in self.answered

    def running(self) -> list[tuple[str, str, str]]:
        return [(c, a, k) for c, (a, k) in self.spawned.items() if c not in self.finished]

    def add(self, e: Obj) -> None:
        t, d = text(e["type"]), obj(e["data"])
        if t in _MODEL:
            self._model(e, t, d)
        elif t == "agent_spawned":
            self.spawned[text(d["child_thread_id"])] = (text(d["agent_name"]), text(d["call_id"]))
        elif t == "agent_finished":
            self.finished.add(text(d["child_thread_id"]))
        elif t == "tool_call":
            self.calls[text(d["call_id"])] = d
        elif t == "approval_requested":
            self.challenges[text(d["challenge_id"])] = d
        elif t == "parked":
            address = obj(d["address"])
            self.parks[f"{text(address['kind'])}:{text(address['id'])}"] = text(d["reason"])

    def _model(self, e: Obj, t: str, d: Obj) -> None:
        if t == "model_request":
            purpose = text(d.get("purpose", "turn"))
            self.purposes[text(e["event_id"])] = purpose
            if purpose != "compaction":
                self.last_turn = text(e["event_id"])
            return
        request = text(d["request_event_id"])
        self.answered.add(request)
        if t == "model_attempt_abandoned" or not self.is_turn(request):
            return
        self.last_stop = text(d["stop_reason"])
        for p in arr(d["content"]):
            if obj(p)["type"] == "tool_use":
                self.proposed.add(text(obj(p)["call_id"]))


def shown(content: list[JsonValue]) -> list[tuple[int, Obj]]:
    """The response parts that show, with their index in the content."""
    out: list[tuple[int, Obj]] = []
    for i, raw in enumerate(content):
        p = obj(raw)
        kind = p["type"]
        body = p.get("text") if kind == "text" else p.get("summary")
        if kind == "tool_use" or (kind in ("text", "reasoning") and body not in (None, "")):
            out.append((i, p))
    return out


def ai_part(request: str, i: int, p: Obj) -> list[Obj]:
    if p["type"] == "tool_use":
        return [
            {
                "type": "tool-input-available",
                "toolCallId": p["call_id"],
                "toolName": p["name"],
                "input": p["input"],
            }
        ]
    pid = f"{request}:{i}"
    kind = text(p["type"])
    body = p["text"] if kind == "text" else p["summary"]
    return [
        {"type": f"{kind}-start", "id": pid},
        {"type": f"{kind}-delta", "id": pid, "delta": body},
        {"type": f"{kind}-end", "id": pid},
    ]


def ag_part(request: str, i: int, p: Obj) -> list[Obj]:
    if p["type"] == "tool_use":
        call = p["call_id"]
        return [
            {
                "type": "TOOL_CALL_START",
                "toolCallId": call,
                "toolCallName": p["name"],
                "parentMessageId": request,
            },
            {"type": "TOOL_CALL_ARGS", "toolCallId": call, "delta": canonical(p["input"]).decode()},
            {"type": "TOOL_CALL_END", "toolCallId": call},
        ]
    pid = f"{request}:{i}"
    if p["type"] == "text":
        return [
            {"type": "TEXT_MESSAGE_START", "messageId": pid, "role": "assistant"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": pid, "delta": p["text"]},
            {"type": "TEXT_MESSAGE_END", "messageId": pid},
        ]
    return [
        {"type": "REASONING_START", "messageId": pid},
        {"type": "REASONING_MESSAGE_START", "messageId": pid, "role": "reasoning"},
        {"type": "REASONING_MESSAGE_CONTENT", "messageId": pid, "delta": p["summary"]},
        {"type": "REASONING_MESSAGE_END", "messageId": pid},
        {"type": "REASONING_END", "messageId": pid},
    ]


def subagent(child: str, agent: str, status: str) -> Obj:
    return {"type": "data-subagent", "id": child, "data": {"agent": agent, "status": status}}


def ag_started(child: str, agent: str, call: str) -> Obj:
    return {
        "type": "SUBAGENT_STARTED",
        "subagentRunId": child,
        "name": agent,
        "parentToolCallId": call,
    }


def _model(p: str, e: Obj, f: Facts) -> list[Obj]:
    t, d = e["type"], obj(e["data"])
    if t == "model_request":
        if d.get("purpose") == "compaction":
            return []
        return [{"type": "start-step"}] if p == AI else [MODEL_STEP]
    request = text(d["request_event_id"])
    if not f.is_turn(request):
        return []
    done: Obj = {"type": "finish-step"} if p == AI else STEP_DONE
    if t == "model_attempt_abandoned":
        if p == AI:
            data: Obj = {"status": "abandoned", "reason": d["reason"]}
            return [{"type": "data-attempt", "id": request, "data": data}, done]
        value: Obj = {"requestId": request, "reason": d["reason"]}
        return [{"type": "CUSTOM", "name": "threads.attempt_abandoned", "value": value}, done]
    make = ai_part if p == AI else ag_part
    return [c for i, part in shown(arr(d["content"])) for c in make(request, i, part)] + [done]


def _result_ai(e: Obj) -> Obj:
    d = obj(e["data"])
    call, preview = d["call_id"], d["preview"]
    origin = d.get("origin")
    if e["type"] == "tool_result" and origin == "deferred":
        return {
            "type": "tool-output-available",
            "toolCallId": call,
            "output": preview,
            "preliminary": True,
        }
    if e["type"] == "tool_result" and origin == "denied":
        return {"type": "tool-output-denied", "toolCallId": call}
    if d["is_error"]:
        return {"type": "tool-output-error", "toolCallId": call, "errorText": preview}
    return {"type": "tool-output-available", "toolCallId": call, "output": preview}


def _approval(e: Obj) -> Obj:
    d = obj(e["data"])
    if e["type"] == "approval_requested":
        return {
            "type": "tool-approval-request",
            "approvalId": d["challenge_id"],
            "toolCallId": d["call_id"],
        }
    c: Obj = {
        "type": "tool-approval-response",
        "approvalId": d["challenge_id"],
        "approved": e["type"] == "approval_granted",
    }
    if "reason" in d:
        c["reason"] = d["reason"]
    return c


def _tools(p: str, e: Obj, f: Facts) -> list[Obj]:
    t, d = e["type"], obj(e["data"])
    if text(d["call_id"]) not in f.proposed:
        return []
    if text(t).startswith("approval_"):
        return [_approval(e)] if p == AI else []
    if p == AI:
        return [_result_ai(e)]
    if t == "tool_result" and d.get("origin") == "deferred":
        return []
    return [
        {
            "type": "TOOL_CALL_RESULT",
            "messageId": e["event_id"],
            "toolCallId": d["call_id"],
            "content": d["preview"],
            "role": "tool",
        }
    ]


def _children(p: str, e: Obj, f: Facts) -> list[Obj]:
    d = obj(e["data"])
    child = text(d["child_thread_id"])
    if e["type"] == "agent_spawned":
        agent = text(d["agent_name"])
        if p == AI:
            return [subagent(child, agent, "running")]
        return [ag_started(child, agent, text(d["call_id"]))]
    if child not in f.spawned:
        return []
    agent, status = f.spawned[child][0], text(d["status"])
    if p == AI:
        return [subagent(child, agent, status)]
    if status == "completed":
        return [
            {"type": "SUBAGENT_FINISHED", "subagentRunId": child, "outcome": {"type": "success"}}
        ]
    return [
        {
            "type": "SUBAGENT_ERROR",
            "subagentRunId": child,
            "message": f"subagent {agent} ended: {status}",
            "code": status,
        }
    ]


_TOOLS = (
    "approval_requested",
    "approval_granted",
    "approval_denied",
    "tool_result",
    "tool_result_late",
)


def chunks(p: str, e: Obj, f: Facts) -> list[Obj]:
    """The event's chunks, given the facts of the run's events before it."""
    t = e["type"]
    if t in _MODEL:
        return _model(p, e, f)
    if t in _TOOLS:
        return _tools(p, e, f)
    if t in ("agent_spawned", "agent_finished"):
        return _children(p, e, f)
    if t == "retry_scheduled":
        until = obj(e["data"])["not_before"]
        if p == AI:
            return [
                {
                    "type": "data-status",
                    "id": "status",
                    "data": {"status": "retry_wait", "until": until},
                }
            ]
        return [{"type": "CUSTOM", "name": "threads.retry_wait", "value": {"until": until}}]
    return []


def event_frames(p: str, e: Obj, f: Facts) -> list[Obj]:
    """The event's frames with their ids <seq>:<k>; then the event joins the facts."""
    out: list[Obj] = [{"id": f"{e['seq']}:{k}", "data": c} for k, c in enumerate(chunks(p, e, f))]
    f.add(e)
    return out
