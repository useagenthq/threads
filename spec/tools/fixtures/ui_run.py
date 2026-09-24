# pyright: strict
"""Reference `ui` case runner (spec/conformance/README.md, "ui"): the frames one connection
sends for a case's input, and the stock client's messages after them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import arr, num, obj, text
from .jcs import canonical
from .ui_close import outcome
from .ui_fold import ag_fold, ag_projection, ai_fold, fold_turns
from .ui_live import Session
from .ui_map import AG, AI, Facts, chunks

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

DONE = "[DONE]"


def input_text(d: Obj) -> str:
    if "text" in d:
        return text(d["text"])
    return "".join(text(obj(p)["text"]) for p in arr(d["content"]) if obj(p)["type"] == "text")


def snapshot(history: list[Obj], receipts: Obj) -> Obj:
    """MESSAGES_SNAPSHOT of the chain through the snapshot point."""
    turns: list[tuple[str, str, list[Obj]]] = []
    facts = Facts()
    for e in history:
        d = obj(e["data"])
        if e["type"] == "user_input":
            eid = text(e["event_id"])
            mid = text(d.get("client_message_id", receipts.get(eid, eid)))
            turns.append((mid, input_text(d), []))
            facts = Facts()
        elif turns:
            turns[-1][2].extend(chunks(AG, e, facts))
            facts.add(e)
    messages: list[JsonValue] = list(fold_turns(turns))
    return {"type": "MESSAGES_SNAPSHOT", "messages": messages}


def _differs(entry: Obj, recorded: str, answer: str | None) -> bool:
    status, payload = entry["status"], entry.get("payload")
    decision = (
        "deny"
        if status == "cancelled"
        else obj(payload).get("decision")
        if isinstance(payload, dict)
        else None
    )
    if recorded == "granted":
        return decision != "grant"
    if recorded == "denied":
        return decision != "deny"
    if recorded in ("expired", "cancelled"):
        return status != "cancelled"
    given = obj(payload).get("answer") if isinstance(payload, dict) else None
    joined = "\n".join(text(x) for x in arr(given)) if isinstance(given, list) else given
    return status == "cancelled" or joined != answer


def _recorded(events: list[Obj], iid: str, now: int) -> tuple[str, str | None]:
    """How the log settled interrupt `iid` (approval or question), with a question's answer."""
    decided = [
        e
        for e in events
        if e["type"] in ("approval_granted", "approval_denied")
        and obj(e["data"])["challenge_id"] == iid
    ]
    if decided:
        return ("granted" if decided[-1]["type"] == "approval_granted" else "denied", None)
    asked = [
        e
        for e in events
        if e["type"] == "approval_requested" and obj(e["data"])["challenge_id"] == iid
    ]
    if asked:
        expired = num(obj(asked[-1]["data"])["expires_at"]) <= now
        return ("expired" if expired else "open", None)
    results = [
        obj(e["data"])
        for e in events
        if e["type"] == "tool_result" and obj(e["data"])["call_id"] == iid
    ]
    if results and results[-1]["origin"] == "answered":
        return ("answered", text(results[-1]["preview"]))
    return ("cancelled", None)


def conflicts(events: list[Obj], entries: list[Obj], now: int) -> list[Obj]:
    out: list[Obj] = []
    for entry in entries:
        iid = text(entry["interruptId"])
        recorded, answer = _recorded(events, iid, now)
        if recorded == "open":
            raise AssertionError(f"a ui case's resume entry {iid} is settled")
        if _differs(entry, recorded, answer):
            value: Obj = {"interruptId": iid, "recorded": recorded}
            out.append({"type": "CUSTOM", "name": "threads.resume_conflict", "value": value})
    return out


def _plan(events: list[Obj], inp: Obj, now: int) -> Obj:
    run_id = text(inp["run_id"])
    request = next(e for e in events if e["event_id"] == run_id)
    ids = inp.get("ids", {"threadId": request["thread_id"], "runId": run_id})
    plan: Obj = {"protocol": inp["protocol"], "run_id": run_id, "ids": ids}
    if "after" in inp:
        after = text(inp["after"])
        if inp["protocol"] == AG:
            plan["replay"] = int(after.split(":")[0])
        else:
            plan["after"] = after
    if inp.get("replay") is True:
        plan["replay"] = "head"
    extra: list[JsonValue] = list(
        conflicts(events, [obj(x) for x in arr(inp.get("resume", []))], now)
    )
    plan["extra"] = extra
    return plan


class _Timeline:
    """The hub, the log's head and the connection, as the case's steps move them."""

    def __init__(self, events: list[Obj], inp: Obj, now: int) -> None:
        self.events, self.inp = events, inp
        self.plan = _plan(events, inp, now)
        self.receipts = obj(inp.get("receipts", {}))
        self.head = 0
        self.started: set[str] = set()
        self.inbox: list[tuple[str, int, str]] = []
        self.session: Session | None = None
        self.missed: set[str] | None = None
        self.out: list[Obj] = []
        self.registered = False
        self.ended = False

    def visible(self) -> list[Obj]:
        return [e for e in self.events if num(e["seq"]) <= self.head]

    def step(self, s: Obj) -> None:
        if "register" in s:
            self.missed = set(self.started) if "live" in self.inp else None
            self.registered = True
            return
        elif "delta" in s:
            d = obj(s["delta"])
            self.started.add(f"{text(d['request_event_id'])}:{num(d['part'])}")
            if self.registered:
                self.inbox.append((text(d["request_event_id"]), num(d["part"]), text(d["text"])))
        else:
            self.head = num(s["commit"])
            for e in self.visible():
                if e["type"] in (
                    "model_response",
                    "model_response_recovered",
                    "model_attempt_abandoned",
                ):
                    rid = text(obj(e["data"])["request_event_id"])
                    self.started = {p for p in self.started if not p.startswith(f"{rid}:")}
        if self.registered:
            self.read()

    def read(self) -> None:
        visible = self.visible()
        if self.session is None:
            self.session = Session(self.plan, visible, self.missed)
            replay = self.plan.get("replay")
            snap = None
            if replay is not None:
                h = self.session.after[0]
                snap = snapshot([e for e in visible if num(e["seq"]) <= h], self.receipts)
            self.out += self.session.opening(visible, snap)
        for request, part, body in self.inbox:
            self.out += self.session.delta(request, part, body)
        self.inbox = []
        frames, broken = self.session.read(visible)
        self.out += frames
        if broken is not None:
            self.ended = True
            return
        if outcome(visible, self.session.run_id) is not None:
            self.out += self.session.close(visible)
            if self.plan["protocol"] == AI:
                self.out.append({"data": DONE})
            self.ended = True


def _frames(events: list[Obj], inp: Obj, now: int) -> list[Obj]:
    t = _Timeline(events, inp, now)
    last: Obj = {"commit": num(events[-1]["seq"])}
    register: Obj = {"register": True}
    steps = [obj(s) for s in arr(inp["live"])] if "live" in inp else [register, last]
    for s in steps:
        if t.ended:
            break
        t.step(s)
    if not t.ended:
        raise AssertionError("a ui case's timeline ends with the run's outcome")
    return t.out


def _received(events: list[Obj], inp: Obj, now: int) -> list[Obj]:
    """What an AI SDK client resuming at a cursor already holds: the stream through it."""
    if inp["protocol"] != AI or "after" not in inp:
        return []
    full = _frames(events, {k: v for k, v in inp.items() if k != "after"}, now)
    ids = [f.get("id") for f in full]
    return full[: ids.index(inp["after"]) + 1]


def run_case(events: list[Obj], inp: Obj, now: int) -> tuple[list[bytes], list[Obj]]:
    """The frames.jsonl lines and the client's messages."""
    out = _frames(events, inp, now)
    lines = [canonical(f) for f in out]
    data = [obj(f["data"]) for f in _received(events, inp, now) + out if f["data"] != DONE]
    if inp["protocol"] == AI:
        return lines, ai_fold(data)
    return lines, ag_projection(ag_fold(data))
