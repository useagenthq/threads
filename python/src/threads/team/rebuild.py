"""Rebuilding a team's index from its logs alone (the replay rule): the lead's log, every member
log and the team log, read verified, checked across (rule 43), then folded with the same
`insert_rows` / `change_rows` the writer uses, in one transaction."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import ParseError, TeamOpenedEvent, ThreadId, ThreadStartedEvent
from threads.result import Err, Ok
from threads.store import SqliteStore, VerifiedLog
from threads.store.sql import transaction
from threads.store.started import Opened, opened_threads
from threads.team.cross import TeamLogEvents, check_team_logs
from threads.team.index import TeamLog, change_rows, insert_rows, turn_openers

TEAM_TABLES = ("teams", "team_members", "mail", "asks", "monitors", "operator_receipts")
"""Every index table keyed by team_id, the feed aside (spec/schema/store.sql, version 4)."""


@dataclass(frozen=True, slots=True)
class _Read:
    log: TeamLog
    verified: VerifiedLog


def team_branches(conn: sqlite3.Connection, tenant_id: str, team_id: str) -> list[TeamLog]:
    """The team's logs, by branch: its lead's, every member's (parent team_member through the
    lead) and its team log; empty when no lead names the team."""
    opened = opened_threads(conn, tenant_id)
    lead = next((o for o in opened if _leads(o, team_id)), None)
    if lead is None:
        return []
    logs = [
        TeamLog(o.thread_id, o.branch_id)
        for o in opened
        if o is lead or _in_team(o, lead.thread_id, team_id)
    ]
    return sorted(logs, key=lambda log: log.branch_id)


def _leads(o: Opened, team_id: str) -> bool:
    e = o.event
    return (
        isinstance(e, ThreadStartedEvent)
        and e.data.team is not MISSING
        and e.data.team.id == team_id
    )


def _in_team(o: Opened, lead: ThreadId, team_id: str) -> bool:
    """A member (parent team_member through the lead) or the team log."""
    e = o.event
    if isinstance(e, TeamOpenedEvent):
        return e.data.team == team_id
    parent = e.data.parent
    return parent is not MISSING and parent.relation == "team_member" and parent.thread_id == lead


async def rebuild_team_index(store: SqliteStore, team_id: str) -> Ok[None] | Err[ParseError]:
    """Wipes and refolds every index row of `team_id` (and its branches' pending_wakes); the
    feed starts a new epoch. not_found: no lead names the team. invalid_transition: rule 43."""
    tenant = store.tables.tenant_id
    logs = await store.run(lambda c: team_branches(c, tenant, team_id))
    if not logs:
        return Err(ParseError("not_found", f"no team {team_id}"))
    reads: list[_Read] = []
    for log in logs:
        read = await store.read(log.branch_id, 0)
        if isinstance(read, Err):
            error = read.error
            return Err(ParseError(error.code, f"{log.branch_id}: {error.message}", error.seq))
        reads.append(_Read(log, read.value))
    broken = check_team_logs(
        [TeamLogEvents(r.log.thread_id, r.log.branch_id, r.verified.fold.events) for r in reads]
    )
    if broken is not None:
        message = f"{broken.branch_id}: {broken.message}"
        return Err(ParseError("invalid_transition", message, broken.seq))
    await store.run(lambda c: _refold(c, team_id, reads))
    return Ok(None)


def _refold(conn: sqlite3.Connection, team_id: str, reads: Sequence[_Read]) -> None:
    with transaction(conn):
        (epoch,) = conn.execute(
            "SELECT COALESCE(MAX(epoch), 0) FROM team_feed WHERE team_id = ?", (team_id,)
        ).fetchone()
        for table in (*TEAM_TABLES, "team_feed"):
            conn.execute(f"DELETE FROM {table} WHERE team_id = ?", (team_id,))  # noqa: S608
        for r in reads:
            conn.execute("DELETE FROM pending_wakes WHERE branch_id = ?", (r.log.branch_id,))
        for r in reads:
            insert_rows(conn, r.log, r.verified.fold.events, team_id)
        for r in reads:
            change_rows(conn, r.log, r.verified.fold.events, turn_openers(r.verified), team_id)
        feed = sorted(
            (r.log.branch_id, e.seq) for r in reads for e, _ in r.verified.segments[-1].events
        )
        conn.executemany(
            "INSERT INTO team_feed (team_id, epoch, feed_offset, branch_id, seq)"
            " VALUES (?, ?, ?, ?, ?)",
            [(team_id, int(epoch) + 1, i, b, s) for i, (b, s) in enumerate(feed, 1)],
        )
