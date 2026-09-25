# pyright: strict
"""Reference `validate_next` for the Teams Phase 2 rules on one log (spec/schema/README.md,
"Semantic rules" 50-55): host team logs, supervision, callers, a host member's turn failures, the
failed ask and one sender class per host member turn. ref_rules.Check runs it before its own
checks and folds it after them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import num, obj, text
from .host_ids import opened_at_derived_ids, tenant_team, turn_failure_code

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

# A host member's turn that ends any other way failed: only that turn ends (rule 53).
TURN_KEPT = frozenset({"end_turn", "cancelled"})


def sender_class(env: Obj) -> str:
    """Who decided a mail to a host member (rule 55): a rule is keyed by the sender's agent, so
    one caller agent, one member name, or the operator."""
    frm = obj(env["from"])
    if "caller" in frm:
        return f"caller:{text(obj(frm['caller'])['agent'])}"
    return "operator" if "operator" in frm else f"member:{text(frm['name'])}"


class HostCheck:
    """The Phase 2 part of one log's fold."""

    def __init__(self) -> None:
        self.host_team = self.host_member = self.lead_team = False
        self.thread = self.branch = self.agent = ""
        self.generations: dict[str, int] = {}  # host member name -> latest generation
        self.decided: dict[tuple[str, int], str] = {}  # (name, generation) -> action
        self.restarts: dict[str, list[int]] = {}  # name -> times of restart decisions
        self.last: Obj | None = None  # the previous event
        self.requests: set[str] = set()  # operator_request event ids
        self.turn_asks: list[str] = []  # asks received in the open turn, unanswered
        self.answered: set[str] = set()  # asks replied to or bounced
        self.failing = False  # a failed turn ended, and its member_idle{turn_failed} is due
        self.error: JsonValue = None  # that turn's error, once its first bounce names it
        self.code = ""  # the code that turn's end maps to (rule 53)
        self.turn_class: str | None = None
        self.bounces_in: dict[str, JsonValue] = {}  # ask id -> a received turn_failed error

    # ---------- checks ----------
    def check(self, e: Obj, d: Obj, t: str) -> str | None:
        why = self._placement(e, d, t)
        if why is None and self.failing:
            why = self._while_failing(d, t)
        handler = _CHECKS.get(t)
        return why or (handler(self, e, d) if handler else None)

    def _placement(self, e: Obj, d: Obj, t: str) -> str | None:
        """Rule 50: host-only events and forms stay in a host team's or host member's log, and
        a host team's log is at the ids its tenant derives."""
        if t == "team_opened" and d.get("kind") == "host" and not opened_at_derived_ids(e):
            return "50: a host team's ids are not the ones its tenant derives"
        if t == "supervisor_decided" and not self.host_team:
            return "50: supervisor_decided outside a host team log"
        if t == "member_started" and (d.get("host_member") is True) != self.host_team:
            return "50: a host team log starts only host members, and only it does"
        if t == "member_idle" and "turn_failed" in d and not self.host_member:
            return "53: member_idle{turn_failed} outside a host member's log"
        if t == "budget_exceeded" and d.get("scope") == "hop" and not self.host_member:
            return "53: a hop cap outside a host member's log"
        return None

    def _while_failing(self, d: Obj, t: str) -> str | None:
        """Rule 53: after a failed turn, its asks' bounces, then member_idle{turn_failed}; or the
        same append ends the member (its own budget, a failed rebind), and rule 53 is off."""
        if t == "message_sent" and obj(d["envelope"]).get("code") == "turn_failed":
            return None
        if t == "member_ended":
            return None
        if t == "member_idle" and "turn_failed" in d:
            return None
        return f"53: {t} before a failed turn's bounces and member_idle{{turn_failed}}"

    def rule_started(self, e: Obj, d: Obj) -> str | None:
        """Rule 51: a host member's generations."""
        if not self.host_team:
            return None
        m = obj(d["member"])
        name, gen = text(m["name"]), num(m["generation"])
        if "restart_of" not in d:
            ok = gen == 1 and name not in self.generations and "provenance" not in d
            return None if ok else "51: a first host member start is generation 1, once"
        was = num(d["restart_of"])
        if gen != was + 1 or self.generations.get(name) != was:
            return "51: a restart starts the next generation of the latest one"
        action = self.decided.get((name, was))
        if action == "restart":
            follows = self.last is not None and self.last["type"] == "supervisor_decided"
            ok = follows and "provenance" not in d
            return None if ok else "51: a supervised restart directly follows its decision"
        if action == "stop" and "provenance" in d:
            root = obj(obj(d["provenance"])["root_request"])
            mine = root["thread_id"] == e["thread_id"] and root["event_id"] in self.requests
            return None if mine else "51: an operator restart follows its operator_request"
        return "51: a restart names a generation the supervisor decided on"

    def rule_decided(self, e: Obj, d: Obj) -> str | None:
        """Rule 51: one decision per ended generation, with the logged count and policy."""
        m, policy = obj(d["member"]), obj(d["policy"])
        name, gen = text(m["name"]), num(m["generation"])
        if self.generations.get(name, 0) < gen:
            return "51: a decision on a generation this log never started"
        if (name, gen) in self.decided:
            return "51: a second decision on one ended generation"
        now, window = num(e["time"]), num(policy["within_ms"])
        count = sum(1 for at in self.restarts.get(name, []) if now - at < window)
        if num(d["restarts_in_window"]) != count:
            return "51: restarts_in_window is not the logged count"
        allowed = policy["restart"] == "on_failure" and count < num(policy["max_restarts"])
        if d["action"] == "restart" and not allowed:
            return "51: a restart the policy does not allow"
        return None

    def rule_sent(self, e: Obj, d: Obj) -> str | None:
        """Rules 52 and 53: a caller's mail names this log and its tenant's host team; a
        turn_failed bounce answers a failed turn's ask."""
        env = obj(d["envelope"])
        why = self._caller_sent(e, env) if "caller" in obj(env["from"]) else None
        if why is None and env.get("code") == "turn_failed":
            why = self._turn_failed(env)
        if why is None and env["kind"] == "reply" and env["ask_id"] in self.answered:
            why = "53: a reply to an ask already bounced"
        return why

    def _caller_sent(self, e: Obj, env: Obj) -> str | None:
        c = obj(obj(env["from"])["caller"])
        if not tenant_team(env):
            return "52: a caller's mail is for another tenant's host team"
        here = (c["thread_id"], c["branch_id"]) == (e["thread_id"], self.branch)
        if self.host_member or not here:
            return "52: a caller's mail names its own log, which is no host member's"
        return None if c["agent"] == self.agent else "52: a caller's agent is its thread's agent"

    def _turn_failed(self, env: Obj) -> str | None:
        if not self.failing or text(env["ask_id"]) not in self.turn_asks:
            return "53: a turn_failed bounce names no unanswered ask of a failed turn"
        if self.error is not None and env["error"] != self.error:
            return "53: one failed turn's bounces carry one error"
        code_ok = obj(env["error"])["code"] == self.code
        return None if code_ok else "53: a turn_failed error's code is not its turn end's"

    def rule_received(self, e: Obj, d: Obj) -> str | None:
        """Rules 52 and 55: a caller's receipt names this log; a host member turn takes one
        sender class."""
        env = obj(d["envelope"])
        to = env["to"]
        caller = isinstance(to, dict) and "caller" in to
        if (caller or self.host_member) and not tenant_team(env):
            return "52: mail of another tenant's principal in a host team"
        if isinstance(to, dict) and "caller" in to:
            c = obj(to["caller"])
            if (c["thread_id"], c["branch_id"]) != (e["thread_id"], self.branch):
                return "52: a receipt to a caller is in that caller's log"
        klass = sender_class(env)
        ordinary = env["kind"] in ("message", "ask")
        if self.host_member and ordinary and self.turn_class not in (None, klass):
            return "55: a host member's turn takes mail of one sender class"
        return None

    def rule_idle(self, _e: Obj, d: Obj) -> str | None:
        """Rule 53: member_idle{turn_failed} closes a failed turn once every ask is bounced."""
        if "turn_failed" not in d:
            return None
        if not self.failing or self.turn_asks:
            return "53: member_idle{turn_failed} before every ask of its failed turn bounced"
        same = self.error is None or d["turn_failed"] == self.error
        if not same:
            return "53: its error is not the turn's bounces'"
        code = obj(d["turn_failed"])["code"]
        return None if code == self.code else "53: its code is not its turn end's"

    def rule_ask_closed(self, _e: Obj, d: Obj) -> str | None:
        """Rule 54: failed closes an ask whose turn_failed bounce this log received."""
        outcome, ask = obj(d["outcome"]), text(d["ask_id"])
        if outcome["status"] == "failed":
            got = self.bounces_in.get(ask)
            return None if got == outcome["error"] else "54: failed names no received bounce"
        return "54: a turn_failed bounce closes its ask failed" if ask in self.bounces_in else None

    # ---------- the fold ----------
    def advance(self, e: Obj, d: Obj, t: str, first: bool) -> None:
        if first:
            self._first(e, d, t)
        step = _FOLDS.get(t)
        if step is not None:
            step(self, e, d)
        self.last = e

    def _first(self, e: Obj, d: Obj, t: str) -> None:
        self.thread, self.branch = text(e["thread_id"]), text(e["branch_id"])
        if t == "team_opened":
            self.host_team = d.get("kind") == "host"
            self.lead_team = not self.host_team
        elif t == "thread_started":
            self.host_member = "host_member" in d
            self.agent = text(d["agent_name"])

    def started(self, _e: Obj, d: Obj) -> None:
        m = obj(d["member"])
        self.generations[text(m["name"])] = num(m["generation"])

    def decision(self, e: Obj, d: Obj) -> None:
        m = obj(d["member"])
        key = (text(m["name"]), num(m["generation"]))
        self.decided[key] = text(d["action"])
        if d["action"] == "restart":
            self.restarts.setdefault(key[0], []).append(num(e["time"]))

    def received(self, _e: Obj, d: Obj) -> None:
        env = obj(d["envelope"])
        if self.host_member and env["kind"] in ("message", "ask") and self.turn_class is None:
            self.turn_class = sender_class(env)
        if self.host_member and env["kind"] == "ask":
            self.turn_asks.append(text(env["ask_id"]))
        if env["kind"] == "bounce" and env.get("code") == "turn_failed":
            self.bounces_in[text(env["ask_id"])] = env["error"]

    def sent(self, _e: Obj, d: Obj) -> None:
        env = obj(d["envelope"])
        if env.get("code") == "turn_failed":
            self.error = env["error"]
        if env["kind"] in ("reply", "bounce") and "ask_id" in env:
            ask = text(env["ask_id"])
            self.answered.add(ask)
            if ask in self.turn_asks:
                self.turn_asks.remove(ask)

    def turn_end(self, _e: Obj, d: Obj) -> None:
        if self.host_member and d["reason"] not in TURN_KEPT:
            self.failing, self.error = True, None
            self.code = "" if d["reason"] == "handoff" else turn_failure_code(d)
            return
        self.turn_asks, self.turn_class = [], None

    def idle(self, _e: Obj, d: Obj) -> None:
        if "turn_failed" in d:
            self.failing, self.error, self.turn_asks, self.turn_class = False, None, [], None

    def ended(self, _e: Obj, _d: Obj) -> None:
        """A member_ended in a failed turn's append: the member ends instead, so its taken,
        unanswered asks close at their deadlines."""
        self.failing, self.error, self.turn_asks, self.turn_class = False, None, [], None

    def request(self, e: Obj, _d: Obj) -> None:
        self.requests.add(text(e["event_id"]))


_CHECKS = {
    "member_started": HostCheck.rule_started,
    "supervisor_decided": HostCheck.rule_decided,
    "message_sent": HostCheck.rule_sent,
    "message_received": HostCheck.rule_received,
    "member_idle": HostCheck.rule_idle,
    "ask_closed": HostCheck.rule_ask_closed,
}
_FOLDS = {
    "member_started": HostCheck.started,
    "supervisor_decided": HostCheck.decision,
    "message_received": HostCheck.received,
    "message_sent": HostCheck.sent,
    "turn_completed": HostCheck.turn_end,
    "member_idle": HostCheck.idle,
    "operator_request": HostCheck.request,
    "member_ended": HostCheck.ended,
}
