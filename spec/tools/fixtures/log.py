# pyright: strict
"""Log builder (branch segments, chain, head) and the reference reducer."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, TypedDict, Unpack

from .common import BRANCH, T0, THREAD, aref, eid, num, obj, render, sha, text
from .jcs import JsonValue, Obj, canonical

if TYPE_CHECKING:
    from collections.abc import Callable


class AddOpts(TypedDict, total=False):
    actor: str
    principal: Obj
    critical: bool
    branch_id: str


class Log:
    def __init__(self, branch: str = BRANCH, epoch: int = 1) -> None:
        self.branch, self.epoch = branch, epoch
        self.lines: list[bytes] = [self.header(branch)]
        self.events: list[Obj] = []  # resolved chain, in seq order
        self.artifacts: dict[str, bytes] = {}

    @staticmethod
    def header(branch: str) -> bytes:
        return canonical(
            {
                "format": "threads.log",
                "format_version": 1,
                "thread_id": THREAD,
                "branch_id": branch,
                "created_at": T0,
                "writer": {"impl": "threads-py", "version": "0.1.0"},
            }
        )

    @property
    def seq(self) -> int:
        return num(self.events[-1]["seq"]) if self.events else 0

    def add(
        self,
        type_: str,
        data: Obj,
        **opts: Unpack[AddOpts],
    ) -> Obj:
        seq = self.seq + 1
        a: Obj = {"kind": opts.get("actor", "host")}
        principal = opts.get("principal")
        if principal:
            a["principal"] = principal
        critical = opts.get("critical", True)
        branch_id = opts.get("branch_id")
        e: Obj = {
            "seq": seq,
            "event_id": eid(seq, self.branch),
            "thread_id": THREAD,
            "branch_id": branch_id or self.branch,
            "epoch": self.epoch,
            "type": type_,
            "type_version": 1,
            "time": T0 + seq * 1000,
            "actor": a,
            "prev_hash": sha(self.lines[-1]),
            "critical": critical,
            "data": data,
        }
        self.events.append(e)
        self.lines.append(canonical(e))
        return e

    def art(self, b: bytes, mt: str) -> Obj:
        self.artifacts[sha(b)] = b
        return aref(b, mt)

    def copy(self) -> Log:
        c = Log(self.branch, self.epoch)
        c.lines, c.events, c.artifacts = (
            list(self.lines),
            list(self.events),
            dict(self.artifacts),
        )
        return c

    def fork(self, at_seq: int, branch: str, sandbox_id: str, epoch: int) -> Log:
        """Child export: ancestor lines through at_seq, then the child's header and fork event."""
        idx = next(i for i, ln in enumerate(self.lines) if json.loads(ln).get("seq") == at_seq)
        c = self.copy()
        c.branch, c.epoch = branch, epoch
        c.lines = [*self.lines[: idx + 1], Log.header(branch)]
        c.events = [e for e in self.events if num(e["seq"]) <= at_seq]
        c.add(
            "fork",
            {
                "parent_branch_id": self.branch,
                "at_hash": sha(self.lines[idx]),
                "reason": "snapshot",
                "sandbox_id": sandbox_id,
            },
        )
        return c

    def model_request(self, attempt: int = 1) -> Obj:
        body, line0 = render(self.events)
        return self.add(
            "model_request",
            {
                "attempt": attempt,
                "request_ref": self.art(body, "application/x-ndjson"),
                "declared_prefix": {"bytes": len(line0), "sha256": sha(line0)},
            },
        )

    def model_response(self, req: Obj, content: list[JsonValue], stop: str, usage: Obj) -> Obj:
        return self.add(
            "model_response",
            {
                "request_event_id": req["event_id"],
                "content": content,
                "stop_reason": stop,
                "usage": usage,
                "completeness": "complete",
            },
            actor="model",
        )

    def tool_call(self, req: Obj, call_id: str, name: str, inp: Obj) -> Obj:
        return self.add(
            "tool_call",
            {
                "call_id": call_id,
                "name": name,
                "input": inp,
                "request_event_id": req["event_id"],
            },
        )

    def head(self) -> bytes:
        return canonical(
            {
                "format": "threads.head",
                "format_version": 1,
                "branch_id": self.branch,
                "seq": self.seq,
                "hash": sha(self.lines[-1]),
            }
        )

    def body(self) -> bytes:
        return b"".join(ln + b"\n" for ln in self.lines)

    def export(self) -> bytes:
        return self.body() + self.head() + b"\n"


# ---------- reference reduce (ReducedState, spec/conformance/README.md) ----------
ROLES = {
    "user_input": "user",
    "steer": "user",
    "injected": "context",
    "heartbeat": "context",
    "model_response": "assistant",
    "model_response_recovered": "assistant",
    "tool_result": "tool",
    "tool_result_late": "tool",
}
EFFECT_STATUS = {
    "effect_begin": "begun",
    "effect_commit": "committed",
    "effect_unknown": "unknown",
    "effect_resolved": "resolved",
}


class _Reducer:
    """Mutable fold state for reduce(); one small handler per event type."""

    def __init__(self, now: int) -> None:
        self.now = now
        self.epoch = self.turns = self.input_tokens = self.output_tokens = 0
        self.in_turn = self.cancelled = False
        self.pending: list[str] = []
        self.parked: list[JsonValue] = []
        self.fork_points: list[JsonValue] = []
        self.transcript: list[JsonValue] = []
        self.effects: dict[str, Obj] = {}
        self.call_branch: dict[str, str] = {}
        self.scopes: dict[str, str] = {}

    def event(self, e: Obj) -> None:
        t = text(e["type"])
        self.epoch = num(e["epoch"])
        if t in ROLES:
            self.transcript.append({"role": ROLES[t], "event_id": e["event_id"]})
        if t in EFFECT_STATUS:
            self.effect(e, EFFECT_STATUS[t])
            return
        handler = HANDLERS.get(t)
        if handler is not None:
            handler(self, e, obj(e["data"]))

    def effect(self, e: Obj, status: str) -> None:
        cid = text(obj(e["data"])["call_id"])
        key = f"{self.call_branch[cid]}:{cid}"
        self.effects.setdefault(key, {"effect_key": key, "call_id": cid})["status"] = status

    def user_input(self, _e: Obj, _d: Obj) -> None:
        self.in_turn = True

    def turn_completed(self, _e: Obj, _d: Obj) -> None:
        self.in_turn, self.turns = False, self.turns + 1

    def model_response(self, _e: Obj, d: Obj) -> None:
        usage = obj(d["usage"])
        self.input_tokens += num(usage["input_tokens"])
        self.output_tokens += num(usage["output_tokens"])

    def tool_call(self, e: Obj, d: Obj) -> None:
        self.pending.append(text(d["call_id"]))
        self.call_branch[text(d["call_id"])] = text(e["branch_id"])

    def tool_result(self, _e: Obj, d: Obj) -> None:
        self.pending.remove(text(d["call_id"]))

    def park(self, _e: Obj, d: Obj) -> None:
        self.parked.append(d["address"])

    def resume(self, _e: Obj, d: Obj) -> None:
        self.parked.remove(d["address"])

    def cancel_requested(self, e: Obj, d: Obj) -> None:
        self.scopes[text(e["event_id"])] = text(d["scope"])

    def cancelled_event(self, _e: Obj, d: Obj) -> None:
        scope = self.scopes.get(text(d["request_event_id"]))
        self.cancelled = self.cancelled or scope in {"thread", "tree"}

    def snapshot(self, e: Obj, d: Obj) -> None:
        exp = d["expires_at"]
        settled = all(v["status"] in {"committed", "resolved"} for v in self.effects.values())
        live = exp is None or num(exp) > self.now
        if not self.in_turn and not self.pending and not self.parked and settled and live:
            self.fork_points.append({"seq": e["seq"], "snapshot_event_id": e["event_id"]})

    def status(self) -> str:
        if self.cancelled:
            return "cancelled"
        if self.parked:
            return "parked"
        return "in_turn" if self.in_turn else "idle"


HANDLERS: dict[str, Callable[[_Reducer, Obj, Obj], None]] = {
    "user_input": _Reducer.user_input,
    "turn_completed": _Reducer.turn_completed,
    "model_response": _Reducer.model_response,
    "model_response_recovered": _Reducer.model_response,
    "tool_call": _Reducer.tool_call,
    "tool_result": _Reducer.tool_result,
    "parked": _Reducer.park,
    "resumed": _Reducer.resume,
    "cancel_requested": _Reducer.cancel_requested,
    "cancelled": _Reducer.cancelled_event,
    "snapshot": _Reducer.snapshot,
}


def reduce(log: Log, now: int) -> Obj:
    r = _Reducer(now)
    for e in log.events:
        r.event(e)
    return {
        "thread_id": THREAD,
        "branch_id": log.branch,
        "epoch": r.epoch,
        "turns_completed": r.turns,
        "pending_calls": list[JsonValue](r.pending),
        "effects": list[JsonValue](r.effects.values()),
        "parked": r.parked,
        "fork_points": r.fork_points,
        "usage": {"input_tokens": r.input_tokens, "output_tokens": r.output_tokens},
        "transcript": r.transcript,
        "status": r.status(),
        "head": {"seq": log.seq, "hash": sha(log.lines[-1])},
    }
