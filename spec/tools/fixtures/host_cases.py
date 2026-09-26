# pyright: strict
"""The Teams Phase 2 cases of lane 29D (spec/schema/README.md, "Teams Phase 2"): a caller's ask of
a host member answered, the two replay cases whose rows only one log holds, a host member's failed
and hop-capped turns, an end that bounces the ask it had taken, a deleted caller, and the feed.
Supervision's cases are staged in host_super.py until lane 29E's build."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import NOW, arr, obj, text
from .host_feed import feed
from .host_pieces import (
    SUPPORT_THREAD,
    ask_billing,
    billing,
    billing_log,
    caller_log,
    ended_bounce,
    host_team_log,
    reply,
    take,
    turn_failed,
    turn_failed_bounces,
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
# A host member's own lifetime budget, the one budget that ends it rather than its turn.
OWN_BUDGET: Obj = {
    "scope": "thread",
    "limit": "max_cost_nanos",
    "limit_value": 5_000_000_000,
    "observed": 5_200_000_000,
    "observed_is_upper_bound": False,
}
REBIND_FAILED: Obj = {
    "member": billing(),
    "status": "failed",
    "error": {"code": "pin_mismatch", "message": "rebind failed: pin_mismatch"},
}


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
    _ends(root)
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


def _ends(root: pathlib.Path) -> None:
    """The two ends a host member's own append can take beside a turn failure."""
    team, bill, support, ask = asked()
    take(bill, ask)
    bill.add("budget_exceeded", OWN_BUDGET)
    bill.add("turn_completed", {"reason": "budget_exhausted"})
    result: Obj = {"member": billing(), "status": "budget_exhausted", "budget": OWN_BUDGET}
    ended = bill.add("member_ended", {"result": result})
    back = ended_bounce(bill, ask, result, ended)
    bill.add("message_sent", {"envelope": back})
    close_ask(
        support,
        back,
        {"status": "member_ended", "result": result},
        {
            "status": "member_ended",
            "result": result,
        },
    )
    write_host(
        root,
        "host-member-end-bounces-taken-ask",
        "Teams Phase 2 (coordinator decision 6, 2026-09-26): billing's own thread budget, a "
        "lifetime cap for a host member, runs out in the turn that took support's ask. The "
        "append ends the member, so rule 53 does not apply: no turn_failed bounce and no "
        "member_idle. The ask the turn had taken is no pending row, so the end's refusal of "
        "pending mail cannot reach it; the same append bounces it member_ended with the end's "
        "result, whose causal is that member_ended. support closes the ask member_ended at once "
        "instead of waiting out its deadline for an answer from a member that has ended.",
        {"team": team, "billing": bill, "support": support},
    )
    team, bill, support, ask = asked()
    take(bill, ask)
    end = bill.add("turn_completed", {"reason": "error", "code": "content_unsupported"})
    error: Obj = {"code": "content_unsupported", "message": "billing's turn failed"}
    turn_failed_bounces(bill, [ask], error, end)
    bill.add("member_ended", {"result": REBIND_FAILED})
    write_host(
        root,
        "host-member-turn-failed-then-ended",
        "Rule 53's order (coordinator decision 7, 2026-09-26): a reader admits a failed turn's "
        "turn_failed bounces followed by the member's member_ended with no member_idle between. "
        "A member that fails a turn and then ends never became idle, and demanding member_idle "
        "in between would force both runtimes to emit a transition that did not happen. This "
        "pins the readers, not the writer: an append that ends the member skips rule 53's "
        "bounces and bounces member_ended instead (host-member-end-bounces-taken-ask), so no "
        "writer produces this order. An ask a turn_failed bounce answered is not bounced again.",
        {"team": team, "billing": bill, "support": support},
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
