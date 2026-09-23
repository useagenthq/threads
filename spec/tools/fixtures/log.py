# pyright: strict
"""Log builder (branch segments, chain, head) and the reference reducer."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, TypedDict, Unpack

from .common import BRANCH, T0, THREAD, aref, eid, num, obj, sha, text
from .jcs import JsonValue, Obj, canonical
from .render import COMPACT_INSTRUCTION, GUIDE_PREFIX, render, transcript

if TYPE_CHECKING:
    from collections.abc import Callable


class AddOpts(TypedDict, total=False):
    actor: str
    principal: Obj
    critical: bool
    branch_id: str


# The implementation named in every header this run writes. Recover cases append, and only the
# header's writer may append, so __main__ builds them once per implementation.
WRITERS = ("threads-py", "threads-ts")
_writer = [WRITERS[0]]


def set_writer(impl: str) -> None:
    _writer[0] = impl


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
                "writer": {"impl": _writer[0], "version": "0.1.0"},
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

    def fork(
        self,
        at_seq: int,
        branch: str,
        sandbox_id: str | None,
        epoch: int,
        knowledge: str = "pinned",
    ) -> Log:
        """Child export: ancestor lines through at_seq, then the child's header and fork event.

        sandbox_id None is an operator repair fork: no sandbox, no knowledge policy.
        """
        idx = next(i for i, ln in enumerate(self.lines) if json.loads(ln).get("seq") == at_seq)
        c = self.copy()
        c.branch, c.epoch = branch, epoch
        c.lines = [*self.lines[: idx + 1], Log.header(branch)]
        c.events = [e for e in self.events if num(e["seq"]) <= at_seq]
        data: Obj = {"parent_branch_id": self.branch, "at_hash": sha(self.lines[idx])}
        if sandbox_id is None:
            data["reason"] = "repair"
        else:
            data |= {
                "reason": "snapshot",
                "sandbox_id": sandbox_id,
                "knowledge_policy": knowledge,
            }
        c.add("fork", data)
        return c

    def model_request(self, attempt: int = 1, *, compaction: bool = False) -> Obj:
        """A turn request, or (compaction=True) the summarizer side request of."""
        instruction = self._compact_instruction() if compaction else None
        body, line0 = render(self.events, self.artifacts, instruction)
        data: Obj = {
            "attempt": attempt,
            "request_ref": self.art(body, "application/x-ndjson"),
            "declared_prefix": {"bytes": len(line0), "sha256": sha(line0)},
        }
        if compaction:
            data["purpose"] = "compaction"
        return self.add("model_request", data)

    def _compact_instruction(self) -> str:
        """The fixed instruction plus before_compact guide text since the last model_request."""
        guides: list[str] = []
        for e in reversed(self.events):
            if e["type"] == "model_request":
                break
            d = obj(e["data"])
            guide = d.get("hook") == "before_compact" and d.get("decision") == "guide"
            # A guide without a reason adds nothing.
            if e["type"] == "hook_decision" and guide and "reason" in d:
                guides.insert(0, text(d["reason"]))
        return COMPACT_INSTRUCTION + (GUIDE_PREFIX + "\n".join(guides) if guides else "")

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
        self.epoch = self.turns = self.input_tokens = self.output_tokens = self.unknown = 0
        self.in_turn = self.cancelled = self.repair = False
        self.pending: list[str] = []
        self.parked: list[JsonValue] = []
        self.fork_points: list[JsonValue] = []
        self.effects: dict[str, Obj] = {}
        self.call_branch: dict[str, str] = {}
        self.scopes: dict[str, str] = {}

    def event(self, e: Obj) -> None:
        t = text(e["type"])
        self.epoch = num(e["epoch"])
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
        i, o = usage["input_tokens"], usage["output_tokens"]
        self.input_tokens += 0 if i is None else num(i)
        self.output_tokens += 0 if o is None else num(o)
        self.unknown += i is None or o is None

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

    def fork(self, _e: Obj, d: Obj) -> None:
        # The last fork on the resolved chain is this branch's own.
        self.repair = d["reason"] == "repair"

    def status(self) -> str:
        if self.repair:
            return "inspection_only"
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
    "fork": _Reducer.fork,
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
        "usage": {
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "unknown_responses": r.unknown,
        },
        "transcript": transcript(log.events, log.artifacts),
        "status": r.status(),
        "head": {"seq": log.seq, "hash": sha(log.lines[-1])},
    }
