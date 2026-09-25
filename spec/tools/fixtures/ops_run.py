# pyright: strict
"""The reference executor for spec/conformance/vectors/team-ops.json: loads a vector's world,
runs its op on the reference fold and index, and reports the outcome, the appended event types and
the row changes. Every log after the op must pass the reference validator (ref_team.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import ops_host
from .common import arr, num, obj, sha, text
from .jcs import JsonValue, canonical
from .log import Log
from .ops_consume import consume, deadline
from .ops_life import end, idle
from .ops_member import start
from .ops_observe import monitor, wait
from .ops_send import ask, cancel, reply, send
from .ops_start import materialize
from .ops_supervise import delete, supervise
from .ops_world import World
from .ref_team import check_log, cross

if TYPE_CHECKING:
    from collections.abc import Callable

    from .jcs import Obj

OPS: dict[str, Callable[[World, str, Obj], Obj]] = {
    "send": send,
    "ask": ask,
    "reply": reply,
    "cancel": cancel,
    "wait": wait,
    "monitor": monitor,
    "consume": consume,
    "deadline": deadline,
    "materialize": materialize,
    "start": start,
    "idle": idle,
    "end": end,
}
# Teams Phase 2: the same store ops on a host team's world (callers and host members), and its own.
HOST_OPS: dict[str, Callable[[World, str, Obj], Obj]] = {
    "send": ops_host.send,
    "ask": ops_host.ask,
    "reply": ops_host.reply,
    "consume": ops_host.consume,
    "turn_failure": ops_host.turn_failure,
    "supervise": supervise,
    "delete": delete,
}
# Every table's primary key (store.sql). team_feed is left out: an op adds one feed row per
# appended event, so the appended types already say which.
KEYS: dict[str, tuple[str, ...]] = {
    "teams": ("team_id",),
    "team_members": ("team_id", "name", "generation"),
    "mail": ("mail_id",),
    "asks": ("ask_id",),
    "monitors": ("monitor_id",),
    "operator_receipts": ("tenant_id", "team_id", "op", "idempotency_key"),
    "pending_wakes": ("branch_id", "child_thread_id"),
}


def dump_log(log: Log) -> Obj:
    """A log as a vector gives it: every event but its prev_hash, which each runner chains
    under its own header."""
    events: list[JsonValue] = [{k: v for k, v in e.items() if k != "prev_hash"} for e in log.events]
    return {"thread_id": log.thread, "branch_id": log.branch, "events": events}


def load_log(given: Obj) -> Log:
    log = Log(text(given["branch_id"]), thread=text(given["thread_id"]))
    for raw in arr(given["events"]):
        e: Obj = {**obj(raw), "prev_hash": sha(log.lines[-1])}
        log.events.append(e)
        log.lines.append(canonical(e))
        log.epoch = num(e["epoch"])
    return log


def rows(w: World) -> Obj:
    ix = w.index()
    return {table: ix[table] for table in KEYS}


def _key(table: str, row: JsonValue) -> str:
    return canonical({k: obj(row)[k] for k in KEYS[table]}).decode()


def row_changes(before: Obj, after: Obj) -> Obj:
    """Per table, the rows the op inserted, the rows it changed (as they are now) and the keys
    of the rows it deleted; a table the op left alone is absent."""
    out: Obj = {}
    for table, cols in KEYS.items():
        old = {_key(table, r): r for r in arr(before[table])}
        new = {_key(table, r): r for r in arr(after[table])}
        change: Obj = {}
        inserted = [new[k] for k in new if k not in old]
        updated = [new[k] for k in new if k in old and old[k] != new[k]]
        deleted: list[JsonValue] = [{c: obj(old[k])[c] for c in cols} for k in old if k not in new]
        for name, got in (("insert", inserted), ("update", updated), ("delete", deleted)):
            if got:
                change[name] = got
        if change:
            out[table] = change
    return out


def run(world: Obj, vector: Obj) -> tuple[Obj, list[str]]:
    """The vector's op on its world: (what it expects, as the executor computes it; problems)."""
    logs = {label: load_log(obj(g)) for label, g in obj(world["logs"]).items()}
    given = obj(vector.get("given", {}))
    w = World(
        logs,
        num(vector["now"]),
        mailbox=num(given.get("mailbox", 100)),
        concurrent=num(given.get("concurrent", 4)),
        headroom=given.get("headroom", True) is True,
        templates={k: obj(v) for k, v in obj(given.get("templates", {})).items()},
        rules=[obj(r) for r in arr(given.get("rules", []))],
    )
    problems = _validate(w, "world")
    before = rows(w)
    if before != world["rows"]:
        problems.append("the world's rows are not the rows its logs rebuild")
    ops = HOST_OPS if _host(w) else OPS
    outcome = ops[text(vector["op"])](w, text(vector["by"]), obj(vector["input"]))
    problems += _validate(w, "after the op")
    appended: Obj = {k: list[JsonValue](v) for k, v in w.appended.items()}
    return {
        "outcome": outcome,
        "appended": appended,
        "rows": row_changes(before, rows(w)),
    }, problems


def _host(w: World) -> bool:
    """A host team's world: its team log opens with team_opened{kind: host}."""
    return any(
        log.events and obj(log.events[0]["data"]).get("kind") == "host" for log in w.logs.values()
    )


def _validate(w: World, when: str) -> list[str]:
    events = {label: log.events for label, log in w.logs.items()}
    found = [f for f in (check_log(k, v) for k, v in sorted(events.items())) if f]
    found += [f for f in [cross(events)] if f]
    return [f"{when}: {label}@{seq} breaks {why}" for label, seq, why in found]
