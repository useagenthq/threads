# pyright: strict
"""The staged Teams Phase 2 cases (spec/schema/README.md, "Teams Phase 2"): a caller's ask of a
host member answered, the two replay cases whose rows only one log holds, a host member's failed
and hop-capped turns, supervision (a restart, the cap, and asks across a restart), a deleted
caller, and the feed. Staged until lane 29's build reads them (spec/conformance/README.md)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import NOW, arr, eid, num, obj, text
from .host_feed import feed
from .host_pieces import (
    POLICY,
    SUPPORT_THREAD,
    ask_billing,
    billing,
    billing_log,
    caller_log,
    host_start,
    host_team_log,
    reply,
    take,
    turn_failed,
)
from .jcs import JsonValue, canonical
from .log import Log, reduce
from .pieces import answer, case, dump, result, user
from .team_index import team_index
from .team_steps import FAM, host

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj

QUESTION = "Is INV-1001 paid?"


def write_host(
    root: pathlib.Path,
    name: str,
    desc: str,
    logs: dict[str, Log],
    error: Obj | None = None,
    **more: JsonValue,
) -> None:
    """A staged `team` case of a host team: each log's export, then every log's reduced state
    and the index the logs rebuild (no tree: a host team has no lead), or the logs' error."""
    d = root / name
    (d / "logs").mkdir(parents=True)
    for label, log in logs.items():
        (d / "logs" / f"{label}.jsonl").write_bytes(log.export())
    inp: Obj = {"logs": list[JsonValue](logs), **more}
    meta = case(name, FAM, "team", desc, input=inp)
    expected: Obj
    if error is not None:
        expected = {"outcome": "error", "error": error}
    else:
        gone = more.get("deleted")
        deleted = frozenset(text(t) for t in arr(gone)) if gone is not None else frozenset[str]()
        index = team_index(list(logs.values()), deleted)
        expected = {
            "outcome": "ok",
            "states": {label: reduce(log, NOW) for label, log in logs.items()},
            "index": index,
        }
        reads = more.get("feed")
        if isinstance(reads, list):
            expected["feed"] = feed(list(logs.values()), index, reads)
    (d / "case.json").write_text(dump(meta))
    (d / "expected.json").write_text(dump(expected))


def asked() -> tuple[Log, Log, Log, Obj]:
    """support asks billing; billing hasn't taken it. (host team log, billing, support, ask)"""
    support = caller_log()
    root = user(support, QUESTION)
    ask = ask_billing(support, "support", str(root["event_id"]), "c1", QUESTION)
    return host_team_log(), billing_log(), support, ask


def replied() -> tuple[Log, Log, Log, Obj, Obj]:
    team, bill, support, ask = asked()
    take(bill, ask)
    back = reply(bill, ask, "r1", "Paid.")
    result(bill, "r1", canonical({"status": "sent", "id": back["mail_id"]}).decode())
    answer(bill, "Answered support.")
    done: Obj = {
        "member": billing(),
        "status": "completed",
        "output": {"text": "Answered support."},
    }
    bill.add("member_idle", {"result": done})
    return team, bill, support, ask, back


def close_ask(support: Log, env: Obj, outcome: Obj, value: Obj) -> None:
    """The caller consumes billing's answer: ask_closed, resumed and the ask call's one result."""
    got = take(support, env)
    host(support, "ask_closed", {"ask_id": env["ask_id"], "outcome": outcome})
    address: Obj = {"kind": "ask", "id": env["ask_id"]}
    host(support, "resumed", {"address": address, "cause_event_id": got["event_id"]})
    result(support, "c1", canonical({"ask_id": env["ask_id"], **value}).decode())
    answer(support, "Here is what billing said.")


def build(root: pathlib.Path) -> None:
    _answered(root)
    _replay(root)
    _failed(root)
    _supervised(root)
    _deleted(root)


def _answered(root: pathlib.Path) -> None:
    team, bill, support, _ask, back = replied()
    answered: Obj = {"status": "answered", "member": billing(), "text": "Paid."}
    close_ask(support, back, {"status": "answered", "reply": back["mail_id"]}, answered)
    reads: list[JsonValue] = [
        {},
        {"after": {"epoch": 1, "offset": 2}},
        {"after": {"epoch": 0, "offset": 5}},
        {"after": {"epoch": 2, "offset": 0}},
        {"after": {"epoch": 1, "offset": 99}},
    ]
    write_host(
        root,
        "host-caller-ask-answered",
        "Teams Phase 2: support, a caller in no team, asks billing, a host member of acme's host "
        "team (its ids derived from the tenant). The host rule decides the ask (source "
        "message_policy); billing's ask turn replies to the caller address and goes idle; "
        "support consumes the reply (ask_closed answered, resumed, one tool_result). The index "
        "holds a to_kind member mail and a to_kind caller mail, both consumed; the caller's "
        "events are in no feed. The feed reads: from the start, after offset 2, an older "
        "epoch's cursor (epoch_restarted, then everything), a later epoch's and one past the "
        "head (invalid_cursor).",
        {"team": team, "billing": bill, "support": support},
        feed=reads,
    )


def _replay(root: pathlib.Path) -> None:
    team, bill, support, _ask = asked()
    write_host(
        root,
        "host-caller-ask-pending-only-in-caller-log",
        "Teams Phase 2 replay (D.4): support's ask is committed and billing hasn't consumed it. "
        "Only the caller's log holds it, so a rebuild finds the pending to_kind member mail and "
        "the open asks row (asker_branch_id the caller's branch) through the caller's log.",
        {"team": team, "billing": bill, "support": support},
    )
    team, bill, support, _ask, _back = replied()
    write_host(
        root,
        "host-caller-reply-unconsumed",
        "Teams Phase 2 replay (D.4): billing's reply is committed and support hasn't consumed "
        "it: the to_kind caller mail is rebuilt pending from billing's log, and support's ask "
        "stays open and parked.",
        {"team": team, "billing": bill, "support": support},
    )


def _fail_turn(bill: Log, ask: Obj, error: Obj, hop: bool) -> None:
    take(bill, ask)
    if hop:
        cap: Obj = {
            "scope": "hop",
            "limit": "max_cost_nanos",
            "limit_value": 500_000_000,
            "observed": 600_000_000,
            "observed_is_upper_bound": False,
        }
        bill.add("budget_exceeded", cap)
        end = bill.add("turn_completed", {"reason": "budget_exhausted"})
    else:
        end = bill.add("turn_completed", {"reason": "error", "code": "content_unsupported"})
    turn_failed(bill, [ask], error, end)


def _failed(root: pathlib.Path) -> None:
    for name, hop, code in (
        ("host-member-turn-failed", False, "content_unsupported"),
        ("host-member-hop-capped", True, "budget_exhausted"),
    ):
        team, bill, support, ask = asked()
        error: Obj = {"code": code, "message": f"billing's turn failed: {code}"}
        _fail_turn(bill, ask, error, hop)
        failed: Obj = {"status": "failed", "error": error}
        close_ask(support, _last_sent(bill), failed, failed)
        why = (
            "the rule's per-hop cap (budget_exceeded{scope: hop}) ends the turn budget_exhausted"
            if hop
            else "a pre-dispatch error ends the turn (turn_completed{error})"
        )
        write_host(
            root,
            name,
            f"Teams Phase 2 (D.3): billing takes support's ask, and {why}. Only the turn ends: "
            "the same append bounces the ask turn_failed with the error and records "
            "member_idle{turn_failed}; billing's row is idle and keeps no new result, and no "
            "member_ended is written. support closes the ask failed with the same error.",
            {"team": team, "billing": bill, "support": support},
        )


def _last_sent(log: Log) -> Obj:
    e = next(e for e in reversed(log.events) if e["type"] == "message_sent")
    return obj(obj(e["data"])["envelope"])


def _end_gen1(bill: Log) -> Obj:
    failed: Obj = {
        "member": billing(1),
        "status": "failed",
        "error": {"code": "pin_unavailable", "message": "rebind failed: pin_unavailable"},
    }
    return bill.add("member_ended", {"result": failed})


def decide(team: Log, ended: Obj, action: str, count: int, *, policy: Obj = POLICY) -> None:
    """The supervisor's decision on the generation `ended` (its member_ended) ends."""
    gen = num(obj(obj(obj(ended["data"])["result"])["member"])["generation"])
    data: Obj = {
        "member": billing(gen),
        "ended": {"branch_id": ended["branch_id"], "seq": ended["seq"]},
        "action": action,
        "restarts_in_window": count,
        "policy": policy,
    }
    team.add("supervisor_decided", data)
    if action == "restart":
        team.add("member_started", host_start(gen + 1, restart_of=gen))


def _supervised(root: pathlib.Path) -> None:
    team, bill1 = host_team_log(), billing_log(1)
    ended = _end_gen1(bill1)
    decide(team, ended, "restart", 0)
    write_host(
        root,
        "host-supervisor-restart",
        "Teams Phase 2 (E): billing's generation 1 fails its rebind (member_ended{failed "
        "pin_unavailable}). The host team log's writer decides once: supervisor_decided{restart, "
        "restarts_in_window 0} and, in the same append, member_started of generation 2 with "
        "restart_of 1. Generation 2 opens a new, empty thread and is idle.",
        {"team": team, "billing": bill1, "billing2": billing_log(2)},
    )
    team, bill1, bill2 = host_team_log(), billing_log(1), billing_log(2)
    capped: Obj = {**POLICY, "max_restarts": 1}
    decide(team, _end_gen1(bill1), "restart", 0, policy=capped)
    ended2 = bill2.add("member_ended", {"result": {**failure(2)}})
    decide(team, ended2, "stop", 1, policy=capped)
    write_host(
        root,
        "host-supervisor-stop-at-cap",
        "Teams Phase 2 (E): with maxRestarts 1, generation 1 fails and is restarted; generation "
        "2 fails within the window, so restarts_in_window is 1 and the decision is stop. The "
        "name stays ended: no generation 3.",
        {"team": team, "billing": bill1, "billing2": bill2},
    )
    _across_restart(root)


def failure(gen: int) -> Obj:
    return {
        "member": billing(gen),
        "status": "failed",
        "error": {"code": "pin_mismatch", "message": "rebind failed: pin_mismatch"},
    }


def _across_restart(root: pathlib.Path) -> None:
    team, bill1, support, ask = asked()
    bill1.add("mail_refused", {"mail_id": ask["mail_id"], "code": "member_ended"})
    result_: Obj = {**failure(1)}
    ended = bill1.add("member_ended", {"result": result_})
    bounce: Obj = {
        "mail_id": f"{bill1.branch}:{eid(bill1.seq + 1, bill1.branch)}",
        "kind": "bounce",
        "team": ask["team"],
        "from": billing(1),
        "to": ask["from"],
        "provenance": ask["provenance"],
        "causal": {"thread_id": bill1.thread, "event_id": bill1.events[-2]["event_id"]},
        "ask_id": ask["ask_id"],
        "code": "member_ended",
        "result": result_,
    }
    bill1.add("message_sent", {"envelope": bounce})
    decide(team, ended, "restart", 0)
    ended_as: Obj = {"status": "member_ended", "result": result_}
    close_ask(support, bounce, ended_as, {"status": "member_ended", "result": result_})
    write_host(
        root,
        "host-ask-across-restart-never-stale",
        "Teams Phase 2 (E, the stale negative): support's ask is pending when billing's "
        "generation 1 ends. Its end append refuses the ask (mail_refused{member_ended}, the row "
        "returned) and bounces it to the caller with the result; the supervisor restarts "
        "generation 2. The ask closes member_ended; no mail is ever stale, since a sender binds "
        "the current generation and an end refuses every pending row.",
        {"team": team, "billing": bill1, "billing2": billing_log(2), "support": support},
    )


def _deleted(root: pathlib.Path) -> None:
    team, bill, _support, _ask, _back = replied()
    write_host(
        root,
        "host-deleted-caller-rows-not-rebuilt",
        "Teams Phase 2 (D.4, R29-1): support asked billing, consumed the reply and was deleted "
        "(its log is gone). billing's log still holds the receipt and the reply, but a rebuild "
        "skips every mail and ask naming a tombstoned caller, so the rebuilt index equals the "
        "one the delete left: no row names the deleted branch.",
        {"team": team, "billing": bill},
        deleted=[SUPPORT_THREAD],
    )
