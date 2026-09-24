"""What opened each main branch of a tenant: how deletion and the team index find a thread's
parent, the team it leads, and the team logs, the log being the only truth."""

import sqlite3
from dataclasses import dataclass

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import BranchId, TeamOpenedEvent, ThreadId, ThreadStartedEvent, parse_log_line
from threads.result import Ok
from threads.store.sql import blob_of, text_of


@dataclass(frozen=True, slots=True)
class Opened:
    thread_id: ThreadId
    branch_id: BranchId
    event: ThreadStartedEvent | TeamOpenedEvent


def opened_threads(conn: sqlite3.Connection, tenant_id: str) -> list[Opened]:
    """The first event of every main branch of the tenant. A first line that doesn't parse links
    nothing, so its thread stays deletable on its own."""
    rows: list[tuple[object, object, object]] = conn.execute(
        "SELECT b.thread_id, b.branch_id, e.line FROM events e"
        " JOIN branches b ON b.branch_id = e.branch_id"
        " WHERE b.tenant_id = ? AND b.parent_branch_id IS NULL AND e.seq = 1"
        " AND e.type IN ('thread_started', 'team_opened')",
        (tenant_id,),
    ).fetchall()
    out: list[Opened] = []
    for thread, branch, line in rows:
        parsed = parse_log_line(blob_of(line).decode("utf-8", "surrogatepass"))
        if isinstance(parsed, Ok) and isinstance(
            parsed.value, ThreadStartedEvent | TeamOpenedEvent
        ):
            out.append(Opened(ThreadId(text_of(thread)), BranchId(text_of(branch)), parsed.value))
    return out


def owner_of(opened: Opened) -> ThreadId | None:
    """The thread `opened` can't outlive: a child's parent, a team log's lead."""
    e = opened.event
    if isinstance(e, TeamOpenedEvent):
        return e.data.lead_thread_id
    parent = e.data.parent
    if parent is MISSING or parent.relation not in ("subagent", "team_member"):
        return None
    return parent.thread_id
