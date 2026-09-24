# pyright: strict
"""Reference client folds (spec/conformance/README.md, "ui"): the messages the stock AI SDK
client (`processUIMessageStream` of ai@7.0.113) and the stock AG-UI client
(`defaultApplyEvents` of @ag-ui/client@1.0.0) build from a stream, for the chunks threads sends,
in the canonical projection the cases compare."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from .common import arr, obj, text

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

# ---------- AG-UI ----------


def ag_apply(messages: list[Obj], e: Obj) -> list[Obj]:
    """One event applied to the client's messages (the list is updated in place, or replaced)."""
    t = e["type"]
    if t == "MESSAGES_SNAPSHOT":
        return _merge(messages, [obj(m) for m in arr(e["messages"])])
    if t in ("TEXT_MESSAGE_START", "REASONING_MESSAGE_START"):
        role = "reasoning" if t == "REASONING_MESSAGE_START" else text(e.get("role", "assistant"))
        if not any(m["id"] == e["messageId"] for m in messages):
            made: Obj = {"id": e["messageId"], "role": role, "content": ""}
            messages.append(made)
    elif t in ("TEXT_MESSAGE_CONTENT", "REASONING_MESSAGE_CONTENT"):
        target = next((m for m in messages if m["id"] == e["messageId"]), None)
        if target is not None:
            target["content"] = text(target.get("content", "")) + text(e["delta"])
    elif t == "TOOL_CALL_START":
        _call(messages, e)
    elif t == "TOOL_CALL_ARGS":
        _args(messages, e)
    elif t == "TOOL_CALL_RESULT":
        _result(messages, e)
    return messages


def _args(messages: list[Obj], e: Obj) -> None:
    owner = _owner(messages, text(e["toolCallId"]))
    for c in arr(owner["toolCalls"]) if owner is not None else []:
        if obj(c)["id"] == e["toolCallId"]:
            fn = obj(obj(c)["function"])
            fn["arguments"] = text(fn["arguments"]) + text(e["delta"])


def _owner(messages: list[Obj], call: str) -> Obj | None:
    for m in messages:
        if any(obj(c)["id"] == call for c in arr(m.get("toolCalls", []))):
            return m
    return None


def _call(messages: list[Obj], e: Obj) -> None:
    call = text(e["toolCallId"])
    if _owner(messages, call) is not None:
        return
    parent = e.get("parentMessageId")
    existing = next((m for m in messages if m["id"] == parent), None) if parent else None
    owner = existing if existing is not None and existing["role"] == "assistant" else None
    if owner is None:
        mid = parent if parent is not None and existing is None else call
        made: Obj = {"id": mid, "role": "assistant", "toolCalls": []}
        messages.append(made)
        owner = made
    calls = arr(owner.setdefault("toolCalls", []))
    function: Obj = {"name": e["toolCallName"], "arguments": ""}
    entry: Obj = {"id": call, "type": "function", "function": function}
    calls.append(entry)


def _result(messages: list[Obj], e: Obj) -> None:
    call = text(e["toolCallId"])
    message: Obj = {
        "id": e["messageId"],
        "role": "tool",
        "toolCallId": call,
        "content": e["content"],
    }
    owner = next(
        (
            i
            for i, m in enumerate(messages)
            if m["role"] == "assistant"
            and any(obj(c)["id"] == call for c in arr(m.get("toolCalls", [])))
        ),
        None,
    )
    if owner is None:
        messages.append(message)
        return
    at = owner + 1
    while at < len(messages) and messages[at]["role"] == "tool":
        at += 1
    messages.insert(at, message)


def _merge(messages: list[Obj], incoming: list[Obj]) -> list[Obj]:
    by_id = {text(m["id"]): m for m in incoming}
    keeps_reasoning = not any(m["role"] == "reasoning" for m in incoming)
    kept = [
        copy.deepcopy(by_id.get(text(m["id"]), m))
        for m in messages
        if text(m["id"]) in by_id or (keeps_reasoning and m["role"] == "reasoning")
    ]
    ids = {text(m["id"]) for m in kept}
    return kept + [copy.deepcopy(m) for m in incoming if text(m["id"]) not in ids]


def ag_fold(events: list[Obj], initial: list[Obj] | None = None) -> list[Obj]:
    messages = copy.deepcopy(initial or [])
    for e in events:
        messages = ag_apply(messages, e)
    return messages


def fold_turns(turns: list[tuple[str, str, list[Obj]]]) -> list[Obj]:
    """foldAgUi: each run's user message, then its frames, in order."""
    messages: list[Obj] = []
    for mid, body, frames in turns:
        messages.append({"id": mid, "role": "user", "content": body})
        for e in frames:
            messages = ag_apply(messages, e)
    return messages


# ---------- AI SDK ----------

AI_KEYS = (
    "type",
    "id",
    "text",
    "state",
    "toolCallId",
    "input",
    "output",
    "errorText",
    "approval",
    "preliminary",
    "data",
)


# Chunks after which processUIMessageStream publishes no new snapshot of the message.
_SILENT = ("start-step", "finish-step", "finish", "abort", "error")


class AiMessage:
    """The assistant message processUIMessageStream assembles from one stream, as the client
    holds it: the snapshot published at the last chunk that wrote one."""

    def __init__(self) -> None:
        self.id = ""
        self.parts: list[Obj] = []
        self.open: dict[str, Obj] = {}
        self.published: Obj | None = None

    def _step(self) -> list[Obj]:
        starts = [i for i, p in enumerate(self.parts) if p["type"] == "step-start"]
        return self.parts[starts[-1] + 1 :] if starts else self.parts

    def _tool(self, call: str) -> Obj:
        for p in [*reversed(self._step()), *reversed(self.parts)]:
            if text(p["type"]).startswith("tool-") and p.get("toolCallId") == call:
                return p
        raise AssertionError(f"no tool invocation {call}")

    def apply(self, c: Obj) -> None:
        t = text(c["type"])
        if t == "start":
            self.id = text(c["messageId"])
        elif t in ("text-start", "reasoning-start"):
            part: Obj = {"type": t.removesuffix("-start"), "text": "", "state": "streaming"}
            if t == "reasoning-start":
                part["id"] = c["id"]
            self.open[text(c["id"])] = part
            self.parts.append(part)
        elif t in ("text-delta", "reasoning-delta"):
            p = self.open[text(c["id"])]
            p["text"] = text(p["text"]) + text(c["delta"])
        elif t in ("text-end", "reasoning-end"):
            self.open.pop(text(c["id"]))["state"] = "done"
        elif t == "start-step":
            self.parts.append({"type": "step-start"})
        else:
            self._tools(t, c)

    def _tools(self, t: str, c: Obj) -> None:
        if t.startswith("data-"):
            self._data(t, c)
        elif t in ("tool-approval-request", "tool-approval-response"):
            self._approval(t, c)
        else:
            self._invocation(t, c)

    def _data(self, t: str, c: Obj) -> None:
        same = [p for p in self.parts if p["type"] == t and p.get("id") == c.get("id")]
        if same and "id" in c:
            same[0]["data"] = c["data"]
        else:
            self.parts.append({k: v for k, v in c.items() if k in ("type", "id", "data")})

    def _approval(self, t: str, c: Obj) -> None:
        if t == "tool-approval-request":
            p = self._tool(text(c["toolCallId"]))
            p["state"], p["approval"] = "approval-requested", {"id": c["approvalId"]}
            return
        p = next(p for p in self.parts if obj(p.get("approval", {})).get("id") == c["approvalId"])
        approval: Obj = {
            **obj(p.get("approval", {})),
            "id": c["approvalId"],
            "approved": c["approved"],
        }
        if "reason" in c:
            approval["reason"] = c["reason"]
        p["state"], p["approval"] = "approval-responded", approval

    def _invocation(self, t: str, c: Obj) -> None:
        if t == "tool-input-available":
            call = text(c["toolCallId"])
            name = f"tool-{text(c['toolName'])}"
            found = [p for p in self._step() if p["type"] == name and p.get("toolCallId") == call]
            if found:
                found[0].update({"state": "input-available", "input": c["input"]})
            else:
                self.parts.append(
                    {
                        "type": name,
                        "toolCallId": call,
                        "state": "input-available",
                        "input": c["input"],
                    }
                )
        elif t in ("tool-output-available", "tool-output-error"):
            p = self._tool(text(c["toolCallId"]))
            available = t == "tool-output-available"
            p["state"] = "output-available" if available else "output-error"
            for key in ("output", "errorText", "preliminary"):
                p.pop(key, None)
            for key in ("output", "preliminary") if available else ("errorText",):
                if key in c:
                    p[key] = c[key]
        elif t == "tool-output-denied":
            self._tool(text(c["toolCallId"]))["state"] = "output-denied"

    def projection(self) -> Obj:
        parts: list[JsonValue] = [{k: p[k] for k in AI_KEYS if k in p} for p in self.parts]
        return {"id": self.id, "role": "assistant", "parts": parts}


def ai_fold(chunks: list[Obj]) -> list[Obj]:
    m = AiMessage()
    for c in chunks:
        m.apply(c)
        if c["type"] not in _SILENT:
            m.published = copy.deepcopy(m.projection())
    return [] if m.published is None else [m.published]


def ag_projection(messages: list[Obj]) -> list[Obj]:
    keys = ("id", "role", "content", "toolCallId", "toolCalls")
    return [{k: m[k] for k in keys if k in m} for m in messages]
