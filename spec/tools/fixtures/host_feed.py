# pyright: strict
"""The `feed` projection of a Teams Phase 2 case (spec/conformance/README.md): team.events reads
of a rebuilt feed. A rebuild starts epoch 1 with offsets in (branch_id, seq) order, as both
runtimes' rebuilds assign them; a cursor of an older epoch restarts, and one from a later epoch
or past the head is invalid_cursor (lane 29B)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import arr, num, obj, text
from .jcs import JsonValue

if TYPE_CHECKING:
    from .jcs import Obj
    from .log import Log

EPOCH = 1


def _source(log: Log, e: Obj, member: JsonValue) -> JsonValue:
    """A member's branch is the member; the team log's own events are the team's. An event of an
    operator request would be the operator's; these cases have none."""
    if member is not None:
        return {"kind": "member", "member": member}
    d = obj(e["data"])
    if e["type"] in ("team_opened", "supervisor_decided") or "provenance" not in d:
        return {"kind": "team"}
    raise AssertionError(f"{log.branch}: an operator event in a host feed case")


def items(logs: list[Log], members: dict[str, JsonValue], feed_branches: set[str]) -> list[Obj]:
    """Every feed row as an item, in offset order. members maps a member branch to its ref."""
    rows = sorted(
        (log.branch, num(e["seq"]), log, e)
        for log in logs
        if log.branch in feed_branches
        for e in log.events
    )
    return [
        {
            "kind": "event",
            "cursor": {"epoch": EPOCH, "offset": i},
            "source": _source(log, e, members.get(branch)),
            "branch_id": branch,
            "seq": seq,
        }
        for i, (branch, seq, log, e) in enumerate(rows, 1)
    ]


def read(all_items: list[Obj], after: JsonValue) -> Obj:
    if after is None:
        return {"items": list[JsonValue](all_items)}
    cursor = obj(after)
    epoch, offset = num(cursor["epoch"]), num(cursor["offset"])
    if epoch > EPOCH or (epoch == EPOCH and not 0 <= offset <= len(all_items)):
        return {"error": "invalid_cursor"}
    if epoch < EPOCH:
        restart: Obj = {"kind": "epoch_restarted", "cursor": {"epoch": EPOCH, "offset": 0}}
        return {"items": [restart, *all_items]}
    return {"items": list[JsonValue](all_items[offset:])}


def feed(logs: list[Log], index: Obj, reads: list[JsonValue]) -> list[JsonValue]:
    """One answer per read, from the rows the index rebuilt."""
    members: dict[str, JsonValue] = {
        text(r["branch_id"]): {
            "tenant": _tenant(index),
            "team": r["team_id"],
            "name": r["name"],
            "generation": r["generation"],
        }
        for r in map(obj, arr(index["team_members"]))
        if r["branch_id"] is not None
    }
    branches = {text(obj(r)["branch_id"]) for r in arr(index["team_feed"])}
    every = items(logs, members, branches)
    return [read(every, obj(r).get("after")) for r in reads]


def _tenant(index: Obj) -> JsonValue:
    return obj(arr(index["teams"])[0])["tenant_id"]
