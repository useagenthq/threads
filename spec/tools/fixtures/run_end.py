# pyright: strict
"""Reference `run` projection (spec/schema/README.md, "Run completion"): where the log's last run
ends and what it returns, from the lead's own log alone."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import arr, obj, text
from .turn_open import mail_opens_turn

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

# turn_completed reasons other than end_turn: the RunResult status each ends the run with.
ENDS = {"cancelled": "cancelled", "budget_exhausted": "budget_exhausted", "handoff": "handed_off"}


class _Run:
    """One pass over the log, tracking the run of `request` (a user_input's event id)."""

    def __init__(self, request: str, branch: str) -> None:
        self.request, self.branch = request, branch
        self.current: str | None = None  # the run of the open turn
        self.turn_open = False
        self.parks: list[JsonValue] = []
        self.spawned: dict[str, str] = {}  # background call_id -> the run that spawned it
        self.late_runs: dict[str, str] = {}  # tool_result_late event_id -> that run
        self.children: set[str] = set()  # run-owned background children not yet finished
        self.monitors: set[str] = set()  # run-owned members' task monitors not yet reported
        self.last_response: JsonValue = None
        self.output: JsonValue = None
        self.status: str | None = None
        self.done = False  # the run ended completed: at the first point its end held
        self.settle: set[str] = set()  # settle monitors of this log's waits
        self.tool_uses: dict[str, set[str]] = {}  # request -> its response's tool_use ids
        self.host_sends: set[str] = set()  # effect keys of the host's own sends

    def _opens(self, e: Obj, d: Obj) -> str | None:
        """The run of the turn this event opens, or None when it opens no turn."""
        t = e["type"]
        if t == "user_input":
            return text(e["event_id"])
        if t == "woken":
            return self.late_runs.get(text(arr(d["causes"])[0]))
        if t != "message_received":
            return None
        env = obj(d["envelope"])
        if not mail_opens_turn(env, self.settle, self.parks):
            return None
        return text(obj(obj(env["provenance"])["root_request"])["event_id"])

    def event(self, e: Obj) -> None:
        d, t = obj(e["data"]), text(e["type"])
        if not self.turn_open:
            run = self._opens(e, d)
            if run is not None:
                self.turn_open, self.current = True, run
        self._helpers(e, d, t)
        self._waits(e, d, t)
        self._sends(e, d, t)
        mine = self.turn_open and self.current == self.request
        if t in ("model_response", "model_response_recovered") and mine:
            self.last_response = e["event_id"]
        elif t == "turn_completed":
            if mine and self.status is None:
                reason = text(d["reason"])
                if reason == "end_turn":
                    self.output = self.last_response
                else:
                    self.status = ENDS.get(reason, "failed")
            self.turn_open = False
        elif t == "cancel_requested" and self._idle_cancel(d):
            self.status = "cancelled"
        elif t == "parked" and not self._host_park(obj(d["address"])):
            self.parks.append(d["address"])
        elif t == "resumed" and d["address"] in self.parks:
            self.parks.remove(d["address"])
        self.done = self.done or (self.status is None and self._ended())

    def _sends(self, e: Obj, d: Obj, t: str) -> None:
        """The host's own sends: a channel_send tool_call no response's tool_use asked for. A
        park on one's effect is the host's, never the run's."""
        if t in ("model_response", "model_response_recovered"):
            uses = [obj(p) for p in arr(d["content"])]
            ids = {text(p["call_id"]) for p in uses if p["type"] == "tool_use"}
            self.tool_uses[text(d["request_event_id"])] = ids
        elif t == "tool_call" and d["name"] == "channel_send":
            asked = self.tool_uses.get(text(d["request_event_id"]), set())
            if text(d["call_id"]) not in asked:
                self.host_sends.add(f"{text(e['branch_id'])}:{text(d['call_id'])}")

    def _host_park(self, address: Obj) -> bool:
        return address["kind"] == "effect" and text(address["id"]) in self.host_sends

    def _idle_cancel(self, d: Obj) -> bool:
        """A thread or tree cancel while no turn is open, after the run answered and before it
        ended: the run ends cancelled."""
        waiting = self.output is not None and not self.turn_open and not self.done
        return waiting and self.status is None and d["scope"] in ("thread", "tree")

    def _ended(self) -> bool:
        """The run's end holds: an answer, no turn open, nothing parked, every helper reported."""
        idle = not (self.turn_open or self.parks or self.children or self.monitors)
        return self.output is not None and idle

    def _waits(self, e: Obj, d: Obj, t: str) -> None:
        """A wait's settle monitors: their notifications resume the wait and open no turn."""
        if t == "wait_started":
            for m in arr(d["members"]):
                self.settle.add(f"{self.branch}:{e['event_id']}:{text(obj(m)['name'])}")

    def _helpers(self, e: Obj, d: Obj, t: str) -> None:
        if t == "agent_spawned" and d["mode"] == "background" and self.current is not None:
            self.spawned[text(d["call_id"])] = self.current
            if self.current == self.request:
                self.children.add(text(d["child_thread_id"]))
        elif t == "agent_finished":
            self.children.discard(text(d["child_thread_id"]))
        elif t == "tool_result_late" and text(d["call_id"]) in self.spawned:
            self.late_runs[text(e["event_id"])] = self.spawned[text(d["call_id"])]
        elif t == "member_started":
            if obj(obj(d["provenance"])["root_request"])["event_id"] == self.request:
                self.monitors.add(f"{self.branch}:{e['event_id']}:task")
        elif t == "message_received":
            env = obj(d["envelope"])
            if env["kind"] in ("member_settled", "member_ended"):
                self.monitors.discard(text(env.get("monitor_id", "")))

    def result(self) -> Obj:
        # The run ends at the first point its end held; a later turn of the same request (a
        # hosted wake after run() returned) continues the request, not this result.
        if self.done:
            return {"status": "completed", "output_event_id": self.output}
        if self.status is not None:
            return {"status": self.status, "output_event_id": None}
        # A park wins over an open turn: a lead parked on an approval or a child mid-turn has
        # still returned parked.
        if self.parks:
            return {"status": "parked", "output_event_id": None}
        if self.turn_open:
            return {"status": "running", "output_event_id": None}
        return {"status": "running", "output_event_id": None}


def run_projection(events: list[Obj]) -> Obj:
    """The `run` projection of the log's last run (its latest user_input)."""
    last = [e for e in events if e["type"] == "user_input"][-1]
    r = _Run(text(last["event_id"]), text(last["branch_id"]))
    for e in events:
        r.event(e)
    return r.result()
