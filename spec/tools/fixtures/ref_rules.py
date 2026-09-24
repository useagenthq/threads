# pyright: strict
"""Reference `validate_next` for semantic rules 31-42, 44 and 45 on one log (spec/schema/README.md,
"Semantic rules"). Stdlib only, like the reference reducer: `ref_check` runs it over every case,
so a new rule that contradicts an accepted case fails `gen_fixtures.py --check`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import arr, obj, text
from .ref_fold import advance, joins, mail_run
from .turn_open import mail_opens_turn, mail_renders

if TYPE_CHECKING:
    from collections.abc import Callable

    from .jcs import JsonValue, Obj
    from .ref_fold import Run

TEAM_LOG = frozenset(
    {
        "operator_request", "operator_refused", "message_policy_decided", "member_started",
        "message_sent", "message_received", "mail_refused", "ask_closed", "wait_started",
        "member_observed", "wait_finished",
    }
)  # fmt: skip


def principal(p: JsonValue) -> str:
    q = obj(p)
    return "/".join(text(q[k]) for k in ("issuer", "tenant", "subject"))


class Check:
    """The fold one log's rules read, advanced one event at a time."""

    def __init__(self) -> None:
        self.first = True
        self.team_log = self.member = self.stopped = False
        self.branch = ""
        self.lead_thread = ""
        self.turn: Run | None = None
        self.last_run: Run | None = None
        self.parks: list[JsonValue] = []
        self.mail_done: set[str] = set()
        self.asks_in: set[str] = set()
        self.replied: set[str] = set()
        self.asks_out: set[str] = set()
        self.replies_in: dict[str, str] = {}
        self.settle: set[str] = set()
        self.waits: set[str] = set()
        self.monitors: set[str] = set()
        self.task_monitors: set[str] = set()
        self.requests: set[str] = set()
        self.request_events: set[str] = set()
        self.pending: set[str] = set()
        self.spawn_runs: dict[str, Run] = {}
        self.trailing: dict[str, str] = {}
        self.handed_off = False
        self.barred: set[str] = set()  # background calls a thread or tree cancel followed
        self.ended_runs: set[Run] = set()  # runs with a turn that ended but end_turn
        self.ended = self.idle_ok = self.inputs = False

    # ---------- one event: the first broken rule, then the fold step ----------
    def step(self, e: Obj) -> str | None:
        d, t = obj(e["data"]), text(e["type"])
        why = self._check(e, d, t)
        if why is None:
            before = self.turn
            self._fold(e, d, t)
            opened = before is None and self.turn is not None
            self.idle_ok = (t == "turn_completed" and d.get("reason") == "end_turn") or (
                self.idle_ok and not opened
            )
        self.first = False
        return why

    def _check(self, e: Obj, d: Obj, t: str) -> str | None:
        if t == "team_opened" and not self.first:
            return "33: team_opened after a branch's first event"
        repair = t == "fork" and d.get("reason") == "repair"
        if self.team_log and t not in TEAM_LOG and not repair:
            return f"33: a team log takes no {t}"
        handler = CHECKS.get(t)
        return handler(self, e, d) if handler else None

    # rule 31, 34, 45: receipts
    def rule_received(self, e: Obj, d: Obj) -> str | None:
        env = obj(d["envelope"])
        if d["mail_id"] != env["mail_id"]:
            return "31: a receipt's mail_id is not its envelope's"
        if env["mail_id"] in self.mail_done:
            return "31: one mail received twice"
        if env["kind"] == "task":
            return "34: a task arrives as user_input{team_task}"
        return self._receipt_run(e, env)

    def _receipt_run(self, e: Obj, env: Obj) -> str | None:
        run = mail_run(env)
        if principal(obj(e["actor"])["principal"]) != run[0]:
            return "45: a receipt's actor is not its provenance principal"
        if self.turn is not None and not joins(self.turn, run) and mail_renders(env, self.settle):
            return "34: mail of another request joins an open turn"
        if (self.ended or self.stopped) and self.opens(env):
            return "37: an ended or cancelled member's log opens a turn"
        return None

    def rule_mail_refused(self, _e: Obj, d: Obj) -> str | None:
        return "31: a mail refused after it was taken" if d["mail_id"] in self.mail_done else None

    def opens(self, env: Obj) -> bool:
        if self.turn is not None or self.team_log:
            return False
        return mail_opens_turn(env, self.settle, self.parks)

    # rules 35, 44, 45: mail this log sends
    def rule_sent(self, _e: Obj, d: Obj) -> str | None:
        env = obj(d["envelope"])
        if env["kind"] == "ask" and env["ask_id"] != env["mail_id"]:
            return "44: an ask's ask_id is not its mail_id"
        if env["kind"] == "reply":
            ask = text(env["ask_id"])
            if ask not in self.asks_in or ask in self.replied:
                return "35: a reply names no received, unreplied ask"
        sender = obj(env["from"])
        if self.team_log and "operator" in sender and sender["operator"] not in self.requests:
            return "42: operator mail before its operator_request"
        return None

    def rule_ask_closed(self, _e: Obj, d: Obj) -> str | None:
        ask, outcome = text(d["ask_id"]), obj(d["outcome"])
        if ask not in self.asks_out:
            return "36: ask_closed closes no open ask of this log"
        if outcome["status"] == "answered" and self.replies_in.get(text(outcome["reply"])) != ask:
            return "36: answered names no received reply to this ask"
        return None

    # rules 37-38: member_ended and member_idle
    def rule_ended(self, _e: Obj, _d: Obj) -> str | None:
        return "37: member_ended twice" if self.ended else None

    def rule_idle(self, _e: Obj, _d: Obj) -> str | None:
        if self.ended:
            return "37: member_idle after member_ended"
        return None if self.idle_ok else "38: member_idle without its task's turn end"

    # rules 39-40: waits, monitors and parks
    def rule_wait(self, e: Obj, d: Obj) -> str | None:
        names = [obj(m)["name"] for m in arr(d["members"])]
        if len(set(names)) < len(names):
            return "39: a wait lists a member twice"
        waits = {f"{e['branch_id']}:{r}" for r in self.requests}
        if self.team_log and d["wait_id"] not in waits:
            return "42: an operator wait before its operator_request"
        return None

    def rule_wait_finished(self, _e: Obj, d: Obj) -> str | None:
        return None if d["wait_id"] in self.waits else "39: wait_finished names no open wait"

    def rule_observed(self, _e: Obj, d: Obj) -> str | None:
        return None if d["monitor_id"] in self.monitors else "39: member_observed names no monitor"

    def rule_parked(self, _e: Obj, d: Obj) -> str | None:
        address = obj(d["address"])
        kind, ident = text(address["kind"]), text(address["id"])
        known = {"ask": self.asks_out, "wait": self.waits, "member": self.task_monitors}
        if kind in known and ident not in known[kind]:
            return "40: a park names nothing of this log"
        return None

    # rules 41, 45: inputs
    def rule_input(self, _e: Obj, d: Obj) -> str | None:
        if self.ended or self.stopped:
            return "37: an ended or cancelled member's log opens a turn"
        if d.get("mail_id") in self.mail_done:
            return "31: a task taken twice"
        task = d["source"] == "team_task"
        if self.member and task == self.inputs:
            return "41: a member's task is its first input, and only it"
        if not self.member and task:
            return "41: team_task input outside a member's log"
        return None

    # rules 42, 45: the operator side
    def rule_request(self, e: Obj, d: Obj) -> str | None:
        prov = obj(d["provenance"])
        who = {principal(obj(e["actor"])["principal"]), principal(d["principal"])}
        if who != {principal(prov["principal"])}:
            return "45: an operator_request's principals differ"
        if prov["root_request"] != {"thread_id": e["thread_id"], "event_id": e["event_id"]}:
            return "45: an operator_request's root request is not itself"
        return None

    def rule_refused(self, _e: Obj, d: Obj) -> str | None:
        return (
            None if d["request_id"] in self.requests else "42: operator_refused before its request"
        )

    def rule_policy(self, _e: Obj, d: Obj) -> str | None:
        if "request_id" in d:
            return None if d["request_id"] in self.requests else "42: decision before its request"
        return None if d["call_id"] in self.pending else "42: decision names no pending call"

    def rule_started(self, e: Obj, d: Obj) -> str | None:
        parent = obj(d["parent"])
        if self.team_log:
            if parent["thread_id"] != self.lead_thread:
                return "45: parent is not the lead"
            root = obj(obj(d["provenance"])["root_request"])
            ours = root["thread_id"] == e["thread_id"] and root["event_id"] in self.request_events
            return None if ours else "42: an operator start before its operator_request"
        own = (parent["thread_id"], parent["branch_id"], parent["event_id"])
        me = (e["thread_id"], e["branch_id"], e["event_id"])
        return None if own == me else "45: a lead's member_started parent is not itself"

    # rules 32, 45: background wakes
    def rule_woken(self, e: Obj, d: Obj) -> str | None:
        if self.ended or self.stopped:
            return "37: an ended or cancelled member's log opens a turn"
        causes = [text(c) for c in arr(d["causes"])]
        runs = [self.spawn_runs.get(self.trailing.get(c, "")) for c in causes]
        if self.turn is not None or len(set(causes)) < len(causes):
            return "32: woken with a turn open, or naming a cause twice"
        block = list(self.trailing)
        tail = block[min((block.index(c) for c in causes if c in block), default=0) :]
        if set(tail) != set(causes) or None in runs or len(set(runs)) != 1:
            return "32: woken causes are not one run's children of this append"
        run = runs[0]
        if self.handed_off or run in self.ended_runs or self._barred(causes):
            return "32: woken after a handoff, for a run that ended otherwise, or after a cancel"
        if run is None or principal(obj(e["actor"])["principal"]) != run[0]:
            return "45: woken principal"
        return None

    def _barred(self, causes: list[str]) -> bool:
        """A thread or tree cancel request followed a cause's spawn."""
        return any(self.trailing.get(c) in self.barred for c in causes)

    def _fold(self, e: Obj, d: Obj, t: str) -> None:
        advance(self, e, d, t)
        if t == "tool_result_late":
            self.trailing[text(e["event_id"])] = text(d["call_id"])
        elif t != "agent_finished":
            self.trailing.clear()


CHECKS: dict[str, Callable[[Check, Obj, Obj], str | None]] = {
    "message_received": Check.rule_received,
    "message_sent": Check.rule_sent,
    "mail_refused": Check.rule_mail_refused,
    "ask_closed": Check.rule_ask_closed,
    "member_ended": Check.rule_ended,
    "member_idle": Check.rule_idle,
    "wait_started": Check.rule_wait,
    "wait_finished": Check.rule_wait_finished,
    "member_observed": Check.rule_observed,
    "parked": Check.rule_parked,
    "user_input": Check.rule_input,
    "operator_request": Check.rule_request,
    "operator_refused": Check.rule_refused,
    "message_policy_decided": Check.rule_policy,
    "member_started": Check.rule_started,
    "woken": Check.rule_woken,
}
