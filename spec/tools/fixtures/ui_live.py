# pyright: strict
"""Reference UI connection (spec/schema/ui/README.md, "Live text", "Cursors", "Replays"): one
connection's frames over a scripted timeline of hub deltas and log commits, as the `ui` case
kind runs it (spec/conformance/README.md)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import arr, num, obj, text
from .ui_close import closing, outcome, preamble, run_slice, text_end
from .ui_map import AI, Facts, event_frames, shown

if TYPE_CHECKING:
    from .jcs import Obj

INF = 1 << 62


class Live:
    """The parts one connection streams live, and their substitution at commit."""

    def __init__(self, p: str, missed: set[str]) -> None:
        self.p, self.missed = p, missed
        self.sent: dict[str, str] = {}
        self.refused: set[str] = set()
        self.stopped: set[str] = set()

    def _text(self, pid: str, delta: str) -> Obj:
        if self.p == AI:
            return {"type": "text-delta", "id": pid, "delta": delta}
        return {"type": "TEXT_MESSAGE_CONTENT", "messageId": pid, "delta": delta}

    def delta(self, request: str, part: int, body: str) -> list[Obj]:
        pid = f"{request}:{part}"
        if pid in self.sent:
            self.sent[pid] += body
            return [{"data": self._text(pid, body)}]
        if pid in self.missed or pid in self.refused:
            return []
        attaches = request not in self.stopped and (
            part == 0 or f"{request}:{part - 1}" in self.sent
        )
        if not attaches:
            self.refused.add(pid)
            self.stopped.add(request)
            return []
        self.sent[pid] = body
        start: Obj = (
            {"type": "text-start", "id": pid}
            if self.p == AI
            else {"type": "TEXT_MESSAGE_START", "messageId": pid, "role": "assistant"}
        )
        return [{"data": start}, {"data": self._text(pid, body)}]

    def settle(self, request: str) -> list[str]:
        mine = [pid for pid in self.sent if pid.startswith(f"{request}:")]
        for pid in mine:
            del self.sent[pid]
        self.stopped.discard(request)
        return mine

    def substitute(self, request: str, frames: list[Obj], texts: dict[str, str]) -> list[Obj] | str:
        for pid, sent in self.sent.items():
            if pid.startswith(f"{request}:") and not texts.get(pid, "\0").startswith(sent):
                return pid
        out: list[Obj] = []
        for f in frames:
            c = obj(f["data"])
            key = "id" if self.p == AI else "messageId"
            starts = c["type"] in ("text-start", "TEXT_MESSAGE_START")
            content = c["type"] in ("text-delta", "TEXT_MESSAGE_CONTENT")
            pid = c.get(key)
            if not (starts or content) or not isinstance(pid, str) or pid not in self.sent:
                out.append(f)
            elif content:
                rest = texts[pid][len(self.sent[pid]) :]
                if rest:
                    out.append({"id": f["id"], "data": self._text(pid, rest)})
        self.settle(request)
        return out


class Session:
    """One connection: opening, committed events (cursor-filtered), live deltas, closing."""

    def __init__(self, plan: Obj, events: list[Obj], missed: set[str] | None) -> None:
        self.p, self.run_id = text(plan["protocol"]), text(plan["run_id"])
        self.plan, self.facts = plan, Facts()
        self.live = Live(self.p, missed) if missed is not None else None
        self.requests: set[str] = set()
        self.settled: set[str] = set()
        self.queued: dict[str, list[tuple[str, int, str]]] = {}
        self.after = self._after(events)
        self.seen = 0
        if plan.get("replay") is not None:
            for e in run_slice(events, self.run_id):
                if num(e["seq"]) <= self.after[0]:
                    self._event(e)
            self.seen = self.after[0]

    def _after(self, events: list[Obj]) -> tuple[int, int]:
        replay = self.plan.get("replay")
        if replay == "head":
            return (num(run_slice(events, self.run_id)[-1]["seq"]), INF)
        if replay is not None:
            return (num(replay), INF)
        after = self.plan.get("after")
        if after is None:
            return (0, -1)
        seq, k = text(after).split(":")
        return (int(seq), int(k))

    def opening(self, events: list[Obj], snapshot: Obj | None) -> list[Obj]:
        ids = obj(self.plan["ids"])
        start: Obj = (
            {"type": "start", "messageId": self.run_id}
            if self.p == AI
            else {"type": "RUN_STARTED", **ids}
        )
        extra: list[Obj] = [obj(x) for x in arr(self.plan.get("extra", []))]
        if snapshot is None:
            return [{"data": c} for c in [start, *extra]]
        return [{"data": c} for c in [start, snapshot, *extra, *preamble(self.facts)]]

    def _sends(self, seq: int, k: int) -> bool:
        return seq > self.after[0] or (seq == self.after[0] and k > self.after[1])

    def _event(self, e: Obj) -> tuple[list[Obj], str | None]:
        frames = [
            f
            for k, f in enumerate(event_frames(self.p, e, self.facts))
            if self._sends(num(e["seq"]), k)
        ]
        live, d = self.live, obj(e["data"])
        if live is None:
            return frames, None
        if e["type"] == "model_request":
            return self._request(live, text(e["event_id"]), d, frames), None
        if e["type"] == "model_attempt_abandoned":
            rid = text(d["request_event_id"])
            self.settled.add(rid)
            ends: list[Obj] = [{"data": text_end(self.p, pid)} for pid in live.settle(rid)]
            return ends + frames, None
        if e["type"] in ("model_response", "model_response_recovered"):
            return self._response(live, d, frames)
        return frames, None

    def _request(self, live: Live, rid: str, d: Obj, frames: list[Obj]) -> list[Obj]:
        """A turn request's queued deltas go out after its frames; a compaction's never do."""
        if d.get("purpose") == "compaction":
            self.settled.add(rid)
            return frames
        self.requests.add(rid)
        for request, part, body in self.queued.pop(rid, []):
            frames += live.delta(request, part, body)
        return frames

    def _response(self, live: Live, d: Obj, frames: list[Obj]) -> tuple[list[Obj], str | None]:
        rid = text(d["request_event_id"])
        self.settled.add(rid)
        texts = {
            f"{rid}:{i}": text(p["text"])
            for i, p in shown(arr(d["content"]))
            if p["type"] == "text"
        }
        opened = [pid for pid in live.sent if pid.startswith(f"{rid}:")]
        out = live.substitute(rid, frames, texts)
        if isinstance(out, str):
            return [{"data": text_end(self.p, pid)} for pid in opened], out
        return out, None

    def delta(self, request: str, part: int, body: str) -> list[Obj]:
        if self.live is None or request in self.settled:
            return []
        if request in self.requests:
            return self.live.delta(request, part, body)
        self.queued.setdefault(request, []).append((request, part, body))
        return []

    def read(self, events: list[Obj]) -> tuple[list[Obj], str | None]:
        frames: list[Obj] = []
        for e in run_slice(events, self.run_id):
            if num(e["seq"]) <= self.seen:
                continue
            self.seen = num(e["seq"])
            got, broken = self._event(e)
            frames += got
            if broken is not None:
                return frames, broken
        return frames, None

    def close(self, events: list[Obj]) -> list[Obj]:
        out = outcome(events, self.run_id)
        if out is None:
            raise AssertionError("a ui case's run has an outcome at the log's end")
        open_live = list(self.live.sent) if self.live is not None else []
        ids = obj(self.plan["ids"])
        return [{"data": c} for c in closing(self.p, out, self.facts, open_live, ids)]
