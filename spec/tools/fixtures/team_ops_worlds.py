# pyright: strict
"""The worlds the team op vectors start from, built with the reference ops themselves (so every
world is one a correct runtime can reach) and the fixture builders' steps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .common import ALICE, T0, eid, text
from .ops_consume import consume
from .ops_observe import monitor, wait
from .ops_send import ask, cancel, reply, send
from .ops_start import materialize
from .ops_world import World
from .pieces import call, user
from .team_pieces import (
    MEMBER_BRANCH,
    RESEARCHER,
    WRITER,
    WRITER_BRANCH,
    WRITER_THREAD,
    Route,
    at,
    envelope,
    lead_log,
    team_log,
)
from .team_steps import idle, start

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

NOW = T0 + 100_000  # every world's builder times are before it
DUE = NOW + 120_000  # an ask or wait a world opens at NOW is due here
REQUESTS = tuple(f"0192d000-0000-7000-8000-00000000010{i}" for i in range(1, 6))
GLOBEX: Obj = {"issuer": "api", "tenant": "globex", "subject": "mallory"}


def team(*, writer: bool = False) -> World:
    """The lead's run started researcher-1 (and writer-1); both are starting, the lead's turn is
    open."""
    lead = lead_log()
    root = text(user(lead, "Research batteries.")["event_id"])
    start(lead, root, RESEARCHER, "c1")
    if writer:
        start(lead, root, WRITER, "c2", WRITER_THREAD)
    return World({"lead": lead, "team": team_log()}, NOW, stamp=False)


def run(w: World, name: str = "researcher-1", rebind: str = "ok") -> None:
    """Materialize a starting member (the researcher or the writer)."""
    label = name.split("-", maxsplit=1)[0]
    branch = MEMBER_BRANCH if label == "researcher" else WRITER_BRANCH
    inp: Obj = {"member": name, "label": label, "branch_id": branch, "rebind": rebind}
    materialize(w, label, inp)


def go_idle(w: World, label: str, text_: str) -> None:
    """The member's task turn ends completed: member_idle, and each settle monitor and the
    unfired task monitor on it fires member_settled, in monitor_id order."""
    log = w.logs[label]
    row = w.own_row(label)
    if row is None:
        raise AssertionError(label)
    done = idle(log, w.caller(label), text_)
    ended = log.events[-1]
    prov = w.turn_provenance(label)
    for m in w.rows("monitors"):
        if m["target_name"] != row["name"] or m["kind"] == "end":
            continue
        notice = envelope(
            f"{log.branch}:{eid(log.seq + 1, log.branch)}",
            "member_settled",
            Route(w.ref(row), w.address(text(m["watcher_branch_id"])), prov),
            at(text(ended["event_id"]), log.thread),
            monitor_id=m["monitor_id"],
            result=done,
        )
        log.add("message_sent", {"envelope": notice})


def dispatch(w: World, label: str, op: str, args: Obj, cid: str) -> Obj:
    """A model call in label's open turn, dispatched by the reference op."""
    call(w.logs[label], op, args, cid)
    ops = {
        "send": send,
        "ask": ask,
        "reply": reply,
        "cancel": cancel,
        "wait": wait,
        "monitor": monitor,
    }
    return ops[op](w, label, {"call_id": cid, "args": args})


def pending_call(w: World, label: str, op: str, args: Obj, cid: str) -> Obj:
    """The call a vector's op dispatches: the model asked for it, nothing decided yet."""
    call(w.logs[label], op, args, cid)
    return {"call_id": cid, "args": args}


def operator(rid: str, body_: Obj, key: str | None = None, who: Obj = ALICE) -> Obj:
    inp: Obj = {"request_id": rid, "principal": who, "body": body_}
    if key is not None:
        inp["idempotency_key"] = key
    return inp


def take(w: World, label: str) -> None:
    consume(w, label, {})


def writer_asks(w: World) -> str:
    """writer-1 asks the researcher and parks on the ask (its first park notifies the lead)."""
    dispatch(w, "writer", "ask", {"to": "researcher-1", "question": "Which topic?"}, "c1")
    return f"{WRITER_BRANCH}:c1"


def refused(code: str) -> Obj:
    return {"code": code, "status": "refused"}


def running(*, writer: bool = False) -> World:
    w = team(writer=writer)
    run(w)
    if writer:
        run(w, "writer-1")
    return w


def ended() -> World:
    """researcher-1's rebind failed: it ended before its first model request."""
    w = team()
    run(w, rebind="pin_unavailable")
    return w


@dataclass(frozen=True, slots=True)
class Vec:
    """One authored vector: its world, its op, and what the op must do. The row changes are not
    authored: the executor computes them with the reference index."""

    name: str
    section: str
    description: str
    world: World
    op: str
    by: str
    input: Obj
    outcome: Obj
    appended: dict[str, list[str]]
    now: int = NOW
    given: Obj = field(default_factory=dict[str, "JsonValue"])
