# pyright: strict
"""Log builder (branch segments, chain, head) and the reference reducer."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, TypedDict, Unpack

from .common import BRANCH, MAX_SAFE, T0, THREAD, aref, arr, eid, num, obj, sha, text
from .jcs import JsonValue, Obj, canonical
from .render import COMPACT_INSTRUCTION, GUIDE_PREFIX, render, transcript

if TYPE_CHECKING:
    from collections.abc import Callable


class AddOpts(TypedDict, total=False):
    actor: str
    principal: Obj
    critical: bool
    branch_id: str
    time: int


# The implementation named in every header this run writes. Recover cases append, and only the
# header's writer may append, so __main__ builds them once per implementation.
WRITERS = ("threads-py", "threads-ts")
_writer = [WRITERS[0]]


def set_writer(impl: str) -> None:
    _writer[0] = impl


class Log:
    def __init__(self, branch: str = BRANCH, epoch: int = 1, thread: str = THREAD) -> None:
        self.branch, self.epoch, self.thread = branch, epoch, thread
        self.lines: list[bytes] = [self.header(branch, thread)]
        self.events: list[Obj] = []  # resolved chain, in seq order
        self.artifacts: dict[str, bytes] = {}

    @staticmethod
    def header(branch: str, thread: str = THREAD) -> bytes:
        return canonical(
            {
                "format": "threads.log",
                "format_version": 1,
                "thread_id": thread,
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
            "thread_id": self.thread,
            "branch_id": branch_id or self.branch,
            "epoch": self.epoch,
            "type": type_,
            "type_version": 1,
            "time": opts.get("time", T0 + seq * 1000),
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
        c = Log(self.branch, self.epoch, self.thread)
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
        c.lines = [*self.lines[: idx + 1], Log.header(branch, self.thread)]
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

    def model_request(
        self, attempt: int = 1, *, compaction: bool = False, cause: Obj | None = None
    ) -> Obj:
        """A turn request, or (compaction=True) the summarizer side request; `cause` is the
        compaction_requested a requested compaction answers."""
        if cause is not None:
            instruction = self._requested_instruction(cause)
            through: int | None = num(cause["seq"])
        else:
            instruction = self._compact_instruction() if compaction else None
            through = None
        body, line0 = render(self.events, self.artifacts, instruction, through)
        data: Obj = {
            "attempt": attempt,
            "request_ref": self.art(body, "application/x-ndjson"),
            "declared_prefix": {"bytes": len(line0), "sha256": sha(line0)},
        }
        if compaction or cause is not None:
            data["purpose"] = "compaction"
        if cause is not None:
            data["cause_event_id"] = cause["event_id"]
        return self.add("model_request", data)

    def _requested_instruction(self, request: Obj) -> str:
        """The fixed instruction, then the request's instructions and the guides after it."""
        extra = [text(v) for v in (obj(request["data"]).get("instructions"),) if v is not None]
        for e in self.events:
            d = obj(e["data"])
            guide = d.get("hook") == "before_compact" and d.get("decision") == "guide"
            if num(e["seq"]) > num(request["seq"]) and guide and "reason" in d:
                extra.append(text(d["reason"]))
        return COMPACT_INSTRUCTION + (GUIDE_PREFIX + "\n".join(extra) if extra else "")

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
        self.team_log = False
        self.settle_monitors: set[str] = set()

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

    def woken(self, _e: Obj, _d: Obj) -> None:
        self.in_turn = True

    def team_opened(self, _e: Obj, _d: Obj) -> None:
        self.team_log = True

    def wait_started(self, e: Obj, d: Obj) -> None:
        for m in arr(d["members"]):
            self.settle_monitors.add(f"{e['branch_id']}:{e['event_id']}:{text(obj(m)['name'])}")

    def message_received(self, _e: Obj, d: Obj) -> None:
        """Mail opens a turn only in a member's log, with no turn open, when it is ordinary mail
        or a task or end notification, and no park but the one it resolves is left."""
        env = obj(d["envelope"])
        if self.in_turn or self.team_log or not self._opens(env):
            return
        resolved = {"kind": "member", "id": env.get("monitor_id")}
        self.in_turn = all(p == resolved for p in self.parked)

    def _opens(self, env: Obj) -> bool:
        kind = text(env["kind"])
        if kind in ("message", "ask"):
            return True
        if kind == "bounce":
            return "ask_id" not in env
        notified = kind in ("member_settled", "member_ended")
        return notified and text(env["monitor_id"]) not in self.settle_monitors

    def turn_completed(self, _e: Obj, _d: Obj) -> None:
        self.in_turn, self.turns = False, self.turns + 1

    def model_response(self, _e: Obj, d: Obj) -> None:
        usage = obj(d["usage"])
        i = 0 if usage["input_tokens"] is None else num(usage["input_tokens"])
        o = 0 if usage["output_tokens"] is None else num(usage["output_tokens"])
        if self.input_tokens + i > MAX_SAFE or self.output_tokens + o > MAX_SAFE:
            # A total past the wire's integers can't be carried: the response counts as unknown.
            self.unknown += 1
            return
        self.input_tokens += i
        self.output_tokens += o
        self.unknown += usage["input_tokens"] is None or usage["output_tokens"] is None

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
    "woken": _Reducer.woken,
    "team_opened": _Reducer.team_opened,
    "wait_started": _Reducer.wait_started,
    "message_received": _Reducer.message_received,
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


def turn_openers(events: list[Obj]) -> set[str]:
    """The event ids that opened a turn, as the reducer decides it."""
    r, opened = _Reducer(0), set[str]()
    for e in events:
        before = r.in_turn
        r.event(e)
        if r.in_turn and not before:
            opened.add(text(e["event_id"]))
    return opened


def reduce(log: Log, now: int) -> Obj:
    r = _Reducer(now)
    for e in log.events:
        r.event(e)
    return {
        "thread_id": log.thread,
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
