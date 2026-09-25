# pyright: strict
"""The Teams Phase 2 clauses of the cross-log rules (spec/schema/README.md, rules 43 and 51):
what only a host team's logs and its callers' logs together show. ref_team runs them with the
Phase 1 clauses."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import num, obj, text
from .ref_host import TURN_KEPT

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj


def caller_key(c: JsonValue) -> str:
    return f"caller/{text(obj(c)['branch_id'])}"


def turn_failed_bounce(events: list[Obj], env: Obj, sent: dict[str, Obj]) -> str | None:
    """Rule 43: a turn_failed bounce answers its ask: its causal is the failed turn's
    turn_completed in this log, and it carries the ask's provenance back to the asker."""
    cause = obj(env["causal"])["event_id"]
    end = next((e for e in events if e["event_id"] == cause), None)
    if end is None or end["type"] != "turn_completed" or obj(end["data"])["reason"] in TURN_KEPT:
        return "43: a turn_failed bounce's causal is its failed turn's turn_completed"
    ask = sent.get(text(env["ask_id"]))
    if ask is None:
        return None
    back = _reply_address(ask)
    ok = env["provenance"] == ask["provenance"] and env["to"] == back
    return None if ok else "43: a turn_failed bounce goes back to its asker, with its provenance"


def _reply_address(ask: Obj) -> JsonValue:
    frm = obj(ask["from"])
    if "caller" in frm:
        return frm
    if "operator" in frm:
        return "team_log"
    return {"name": frm["name"], "generation": frm["generation"]}


def reply_to_caller(env: Obj, sent: dict[str, Obj]) -> str | None:
    """Rule 43: a reply to a caller answers an ask that caller sent."""
    to = env["to"]
    if env["kind"] != "reply" or not isinstance(to, dict) or "caller" not in to:
        return None
    ask = sent.get(text(env["ask_id"]))
    ok = ask is None or ask["from"] == to
    return None if ok else "43: a reply to a caller answers that caller's ask"


def host_member_thread(e: Obj, logs: dict[str, list[Obj]]) -> str | None:
    """Rule 43: a host member's thread_started names the member_started that started it."""
    hm = obj(obj(e["data"])["host_member"])
    for es in logs.values():
        for s in es:
            d = obj(s["data"])
            if s["type"] == "member_started" and d.get("thread_id") == e["thread_id"]:
                m = obj(d["member"])
                same = (m["team"], m["name"], m["generation"]) == (
                    hm["team"],
                    hm["name"],
                    hm["generation"],
                )
                ok = same and d.get("host_member") is True
                return None if ok else "43: a host member's thread is not its member_started's"
    return None


def supervised(e: Obj, logs: dict[str, list[Obj]]) -> str | None:
    """Rule 51 across logs: a decision names its generation's member_ended, and restarts exactly
    when the policy allows and the member failed."""
    d = obj(e["data"])
    ended, policy = obj(d["ended"]), obj(d["policy"])
    end = next(
        (
            s
            for es in logs.values()
            for s in es
            if (s["branch_id"], s["seq"]) == (ended["branch_id"], ended["seq"])
        ),
        None,
    )
    if end is None:
        return None  # the member's log is not among these logs
    if end["type"] != "member_ended":
        return "51: a decision's ended names no member_ended"
    started = next(
        (
            obj(s["data"])["thread_id"]
            for es in logs.values()
            for s in es
            if s["type"] == "member_started" and obj(s["data"])["member"] == d["member"]
        ),
        None,
    )
    if end["thread_id"] != started:
        return "51: a decision's ended is not its member's own member_ended"
    failed = obj(obj(end["data"])["result"])["status"] == "failed"
    allowed = policy["restart"] == "on_failure" and num(d["restarts_in_window"]) < num(
        policy["max_restarts"]
    )
    restart = d["action"] == "restart"
    return (
        None if restart == (allowed and failed) else "51: restart exactly when allowed and failed"
    )


def host_clause(e: Obj, logs: dict[str, list[Obj]]) -> str | None:
    """The Phase 2 clauses on a host member's thread_started and on a supervisor decision."""
    if e["type"] == "thread_started" and "host_member" in obj(e["data"]):
        return host_member_thread(e, logs)
    return supervised(e, logs) if e["type"] == "supervisor_decided" else None
