# pyright: strict
"""Reference tree walk over a team (spec/schema/README.md, "Tree walks with teams"): which members
cost and usage count, which count zero as pending, and when a missing branch is log_corrupt."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import arr, obj, text
from .team_index import team_index

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj
    from .log import Log

NOTICES = frozenset({"member_settled", "member_ended"})


def _notified(starter: Log, monitor: str) -> bool:
    """The starter's log received a task notification for this monitor."""
    return any(
        e["type"] == "message_received"
        and obj(obj(e["data"])["envelope"])["kind"] in NOTICES
        and obj(obj(e["data"])["envelope"]).get("monitor_id") == monitor
        for e in starter.events
    )


def _started(logs: list[Log], member: JsonValue) -> tuple[Log, Obj]:
    """The starter's log and its member_started for this member."""
    for log in logs:
        for e in log.events:
            if e["type"] == "member_started" and obj(e["data"])["member"] == member:
                return log, e
    raise AssertionError(f"no member_started for {member}")


def tree(logs: list[Log]) -> Obj:
    """{counted, pending} member names in the lead's tree, or {error: log_corrupt, name}."""
    index = team_index(logs)
    rows = [obj(r) for r in arr(index["team_members"])]
    tenant = obj(arr(index["teams"])[0])["tenant_id"]
    by_thread = {log.thread: log for log in logs}
    counted: list[JsonValue] = []
    pending: list[JsonValue] = []
    seen: set[str] = set()
    for row in rows:
        if text(row["thread_id"]) in seen:
            continue  # a nested lead has two rows and one thread: counted once
        seen.add(text(row["thread_id"]))
        if row["role"] == "lead" or text(row["thread_id"]) in by_thread:
            counted.append(row["name"])
            continue
        ref: Obj = {
            "tenant": tenant,
            "team": row["team_id"],
            "name": row["name"],
            "generation": row["generation"],
        }
        starter, started = _started(logs, ref)
        monitor = f"{starter.branch}:{started['event_id']}:task"
        if row["state"] != "starting" or _notified(starter, monitor):
            return {"error": "log_corrupt", "name": row["name"]}
        pending.append(row["name"])
    return {"counted": counted, "pending": pending}
