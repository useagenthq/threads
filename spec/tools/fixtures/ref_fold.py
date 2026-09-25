# pyright: strict
"""The fold step of the reference validator (ref_rules.py): what each event leaves for the rules
of the events after it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import arr, obj, text
from .turn_open import NOTICES

if TYPE_CHECKING:
    from collections.abc import Callable

    from .jcs import JsonValue, Obj
    from .ref_rules import Check


def _key(p: JsonValue) -> str:
    q = obj(p)
    return "/".join(text(q[k]) for k in ("issuer", "tenant", "subject"))


# (principal key, root request thread_id, event_id). A member's task turn has no root request
# in its own log (it is in the lead's): None, and only its principal is compared (rule 34).
type Run = tuple[str, str | None, str | None]


def joins(turn: Run, run: Run) -> bool:
    """Whether mail of `run` shares the open turn's run (rule 34)."""
    return turn[0] == run[0] and (turn[1] is None or turn[1:] == run[1:])


def mail_run(env: Obj) -> Run:
    """The run a mail belongs to: its provenance's principal and full root request."""
    prov = obj(env["provenance"])
    root = obj(prov["root_request"])
    return (_key(prov["principal"]), text(root["thread_id"]), text(root["event_id"]))


def _first(c: Check, e: Obj, d: Obj, t: str) -> None:
    c.branch = text(e["branch_id"])
    if t == "team_opened":
        c.team_log, c.lead_thread = True, str(d.get("lead_thread_id", ""))
    elif t == "thread_started" and "parent" in d:
        c.member = obj(d["parent"])["relation"] == "team_member"


def _received(c: Check, e: Obj, env: Obj) -> None:
    if c.opens(env):
        c.turn = mail_run(env)
    c.mail_done.add(text(env["mail_id"]))
    if env["kind"] == "ask":
        c.asks_in.add(text(env["ask_id"]))
    elif env["kind"] == "reply":
        c.replies_in[text(env["mail_id"])] = text(env["ask_id"])
    elif env["kind"] in NOTICES:
        c.monitors.discard(text(env["monitor_id"]))


def _registered(c: Check, e: Obj, d: Obj, t: str) -> None:
    if t == "member_started":
        c.task_monitors.add(f"{c.branch}:{e['event_id']}:task")
    elif t == "monitor_set":
        c.monitors.add(f"{c.branch}:{e['event_id']}:{text(obj(d['member'])['name'])}")
    elif t == "wait_started":
        c.waits.add(text(d["wait_id"]))
        for m in arr(d["members"]):
            ident = f"{c.branch}:{e['event_id']}:{text(obj(m)['name'])}"
            c.monitors.add(ident)
            c.settle.add(ident)
    elif t == "wait_finished":
        c.waits.discard(text(d["wait_id"]))
    elif t == "member_observed":
        c.monitors.discard(text(d["monitor_id"]))


def _input(c: Check, e: Obj, d: Obj) -> None:
    who = _key(obj(e["actor"])["principal"])
    task = d["source"] == "team_task"
    c.turn = (who, None, None) if task else (who, text(e["thread_id"]), text(e["event_id"]))
    c.inputs = True
    if d["source"] == "team_task":
        c.mail_done.add(text(d["mail_id"]))


def _woken(c: Check, _e: Obj, d: Obj) -> None:
    c.turn = c.spawn_runs[c.trailing[text(arr(d["causes"])[0])]]


def _sent(c: Check, _e: Obj, d: Obj) -> None:
    env = obj(d["envelope"])
    if env["kind"] == "ask":
        c.asks_out.add(text(env["ask_id"]))
    elif env["kind"] == "reply":
        c.replied.add(text(env["ask_id"]))


def _spawned(c: Check, _e: Obj, d: Obj) -> None:
    if d["mode"] == "background" and c.turn is not None:
        c.spawn_runs[text(d["call_id"])] = c.turn


def _resumed(c: Check, _e: Obj, d: Obj) -> None:
    if d["address"] in c.parks:
        c.parks.remove(d["address"])


def _set(field: str, key: str) -> Callable[[Check, Obj, Obj], None]:
    def add(c: Check, _e: Obj, d: Obj) -> None:
        getattr(c, field).add(text(d[key]))

    return add


def _request(c: Check, e: Obj, d: Obj) -> None:
    c.requests.add(text(d["request_id"]))
    c.request_events.add(text(e["event_id"]))


def _end_turn(c: Check, _e: Obj, d: Obj) -> None:
    if d["reason"] != "end_turn" and c.turn is not None:
        c.ended_runs.add(c.turn)
    c.turn = None


def _member_ended(c: Check, _e: Obj, _d: Obj) -> None:
    c.ended = True


def _cancel(c: Check, _e: Obj, d: Obj) -> None:
    """A tree cancel stops a member for good: its end follows. A lead's cancel ends only that
    run's turn; a later run may still give it input."""
    c.stopped = c.stopped or (c.member and d["scope"] == "tree")
    # Rule 32: no background child spawned before a thread or tree cancel wakes its parent.
    if d["scope"] in ("thread", "tree"):
        c.barred.update(c.spawn_runs)


FOLDS: dict[str, Callable[[Check, Obj, Obj], None]] = {
    "user_input": _input,
    "woken": _woken,
    "turn_completed": _end_turn,
    "cancel_requested": _cancel,
    "handoff": lambda c, _e, _d: setattr(c, "handed_off", True),
    "message_received": lambda c, e, d: _received(c, e, obj(d["envelope"])),
    "mail_refused": _set("mail_done", "mail_id"),
    "message_sent": _sent,
    "ask_closed": lambda c, _e, d: c.asks_out.discard(text(d["ask_id"])),
    "member_ended": _member_ended,
    "operator_request": _request,
    "tool_call": _set("pending", "call_id"),
    "tool_result": lambda c, _e, d: c.pending.discard(text(d["call_id"])),
    "agent_spawned": _spawned,
    "parked": lambda c, _e, d: c.parks.append(d["address"]),
    "resumed": _resumed,
}


def advance(c: Check, e: Obj, d: Obj, t: str) -> None:
    if c.first:
        _first(c, e, d, t)
    step = FOLDS.get(t)
    if step is not None:
        step(c, e, d)
    _registered(c, e, d, t)
