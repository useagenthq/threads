# pyright: strict
"""Reference Render v1 (normative text: spec/schema/README.md) and the transcript projection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import arr, num, obj, text
from .jcs import JsonValue, Obj, canonical

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

CLEARED = "[tool result cleared: call_id={}; read it with read_tool_result]"
REDACTED = b"[redacted]"
COMPACT_INSTRUCTION = (
    "Summarize the conversation so far for your own continuation. Keep the user's goals and "
    "constraints, decisions made, files and identifiers touched, open tasks with their status, "
    "and the next step. Reply with the summary only."
)
GUIDE_PREFIX = "\n\nAdditional instructions:\n"
OPAQUE = frozenset({"reasoning", "hosted_tool"})
ROLES = {
    "user_input": "user",
    "steer": "user",
    "injected": "context",
    "heartbeat": "context",
    "model_response": "assistant",
    "model_response_recovered": "assistant",
    "tool_result": "tool",
    "tool_result_late": "tool",
    "compacted": "summary",
}


def wrap(d: Obj, body: str) -> str:
    origin = text(obj(d["origin"])["id"])
    source = text(d["source"])
    if d["trust"] == "untrusted_reference":
        return f'<reference source="{source}" id="{origin}" untrusted="true">\n{body}\n</reference>'
    return f'<context source="{source}" id="{origin}">\n{body}\n</context>'


def user_line(t: str) -> Obj:
    return {"role": "user", "content": [{"type": "text", "text": t}]}


def spec_line(t: JsonValue) -> JsonValue:
    s = obj(t)
    if s.get("defer_loading") is True:
        return {"name": s["name"], "description": s["description"], "deferred": True}
    return {k: s[k] for k in ("name", "description", "input_schema")}


def _redact(part: JsonValue, spans: list[JsonValue]) -> JsonValue:
    p = obj(part)
    b = text(p["text"]).encode()
    for sp in sorted((obj(x) for x in spans), key=lambda x: num(x["start"]), reverse=True):
        b = b[: num(sp["start"])] + REDACTED + b[num(sp["end"]) :]
    return {"type": "text", "text": b.decode()}


class _View:
    """One pre-pass over the events before a request: settings epoch, edits, drops."""

    def __init__(self, events: list[Obj], artifacts: dict[str, bytes]) -> None:
        self.events, self.artifacts = events, artifacts
        ts = obj(next(e for e in events if e["type"] == "thread_started")["data"])
        self.system, self.tools = ts["instructions"], [spec_line(t) for t in arr(ts["tools"])]
        self.settings: Obj = {k: ts[k] for k in ("model", "model_params", "adapter")}
        self.cut = 0
        self.side: set[JsonValue] = set()
        self.cleared: set[str] = set()
        self.redactions: dict[tuple[str, int], list[JsonValue]] = {}
        self.denied: set[JsonValue] = set()
        ranges: list[Obj] = []
        for e in events:
            d, t = obj(e["data"]), e["type"]
            if t == "settings_changed":
                self.settings = obj(d["settings"])
                if self.settings["reasoning_carryover"] == "omit_prior":
                    self.cut = num(e["seq"])
            elif t == "model_request" and d.get("purpose") == "compaction":
                self.side.add(e["event_id"])
            elif t == "context_edited":
                self._edit(d)
            elif t == "hook_decision" and d["hook"] == "before_input":
                if d["decision"] in ("deny", "failed"):
                    self.denied.add(d["input_event_id"])
            elif t == "compacted":
                ranges.append(e)
        self.ranges = [c for c in ranges if not any(self._covers(o, num(c["seq"])) for o in ranges)]

    def _edit(self, d: Obj) -> None:
        for x in arr(d["edits"]):
            ed = obj(x)
            cid = text(ed["call_id"])
            if ed["action"] == "clear":
                self.cleared.add(cid)
            else:
                self.redactions.setdefault((cid, num(ed["part"])), []).extend(arr(ed["spans"]))

    @staticmethod
    def _covers(c: Obj, seq: int) -> bool:
        d = obj(c["data"])
        return num(d["from_seq"]) <= seq <= num(d["to_seq"])

    def line0(self) -> bytes:
        s = self.settings
        head: Obj = {
            "adapter": s["adapter"],
            "model": s["model"],
            "params": s["model_params"],
            "system": self.system,
            "tools": self.tools,
        }
        return canonical(head) + b"\n"

    # ---------- one builder per model-visible event type ----------
    def user(self, e: Obj) -> Obj | None:
        if e["event_id"] in self.denied:
            return None
        d = obj(e["data"])
        if "content" in d:
            return {"role": "user", "content": d["content"]}
        return user_line(text(d["text"]))

    def injected(self, e: Obj) -> Obj:
        d = obj(e["data"])
        body = text(d["text"]) if "text" in d else self.artifacts[text(obj(d["ref"])["sha256"])]
        return user_line(wrap(d, body if isinstance(body, str) else body.decode()))

    def heartbeat(self, e: Obj) -> Obj:
        ids = arr(obj(e["data"])["running_call_ids"])
        return user_line(
            "<heartbeat>\nrunning: " + ", ".join(text(x) for x in ids) + "\n</heartbeat>"
        )

    def tools_changed(self, e: Obj) -> Obj:
        return {"role": "tools", "tools": [spec_line(x) for x in arr(obj(e["data"])["tools"])]}

    def assistant(self, e: Obj) -> Obj | None:
        d = obj(e["data"])
        if d["request_event_id"] in self.side:
            return None
        old = num(e["seq"]) < self.cut
        parts = [p for p in arr(d["content"]) if not (old and obj(p)["type"] in OPAQUE)]
        return {"role": "assistant", "content": parts} if parts else None

    def result(self, e: Obj) -> Obj:
        d = obj(e["data"])
        cid = text(d["call_id"])
        parts: list[JsonValue]
        if cid in self.cleared:
            parts = [{"type": "text", "text": CLEARED.format(cid)}]
        else:
            raw = arr(d["content"]) if "content" in d else [{"type": "text", "text": d["preview"]}]
            parts = [
                _redact(p, self.redactions[(cid, i)]) if (cid, i) in self.redactions else p
                for i, p in enumerate(raw)
            ]
        m: Obj = {"role": "tool", "call_id": cid, "is_error": d["is_error"], "content": parts}
        if e["type"] == "tool_result_late":
            m["late"] = True
        return m

    def summary(self, c: Obj) -> Obj:
        ref = obj(obj(c["data"])["summary_ref"])
        sha = text(ref["sha256"])
        body = self.artifacts[sha].decode()
        return user_line(
            f'<reference source="summary" id="{sha}" untrusted="true">\n{body}\n</reference>'
        )

    # ---------- the body walk ----------
    def walk(self) -> Iterator[tuple[Obj, Callable[[], Obj | None]]]:
        """Yields (event, builder) in render order; a builder returning None renders nothing."""
        for e in self.events:
            seq = num(e["seq"])
            c = next((r for r in self.ranges if self._covers(r, seq)), None)
            if c is None:
                build = BUILDERS.get(text(e["type"]))
                if build is not None:
                    yield e, _bind(build, self, e)
                continue
            if seq == num(obj(c["data"])["from_seq"]):
                yield c, _bind(_View.summary, self, c)
                kept = [
                    x
                    for x in self.events
                    if x["type"] == "tools_changed" and self._covers(c, num(x["seq"]))
                ]
                if kept:
                    yield kept[-1], _bind(_View.tools_changed, self, kept[-1])


def _bind(fn: Callable[[_View, Obj], Obj | None], v: _View, e: Obj) -> Callable[[], Obj | None]:
    return lambda: fn(v, e)


BUILDERS: dict[str, Callable[[_View, Obj], Obj | None]] = {
    "user_input": _View.user,
    "steer": _View.user,
    "injected": _View.injected,
    "heartbeat": _View.heartbeat,
    "tools_changed": _View.tools_changed,
    "model_response": _View.assistant,
    "model_response_recovered": _View.assistant,
    "tool_result": _View.result,
    "tool_result_late": _View.result,
}


def render(
    events: list[Obj], artifacts: dict[str, bytes], instruction: str | None = None
) -> tuple[bytes, bytes]:
    """Returns (request bytes, declared prefix bytes = line 0). instruction: compaction request."""
    v = _View(events, artifacts)
    line0 = v.line0()
    out = [line0]
    for _, build in v.walk():
        m = build()
        if m is not None:
            out.append(canonical(m) + b"\n")
    if instruction is not None:
        out.append(canonical(user_line(instruction)) + b"\n")
    return b"".join(out), line0


def transcript(events: list[Obj], artifacts: dict[str, bytes]) -> list[JsonValue]:
    """ReducedState.transcript: the conversational entries the next request renders."""
    out: list[JsonValue] = []
    for e, build in _View(events, artifacts).walk():
        role = ROLES.get(text(e["type"]))
        if role is not None and (role == "summary" or build() is not None):
            out.append({"role": role, "event_id": e["event_id"]})
    return out
