"""Rebuilding a team's index from its logs alone (the replay rule): the lead's log, every member
log and the team log, read verified, checked across (rule 43), then folded with the same
`insert_rows` / `change_rows` the writer uses, in one transaction."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic.experimental.missing_sentinel import MISSING

from threadsai.log import ParseError, TeamOpenedEvent, ThreadId, ThreadStartedEvent
from threadsai.result import Err, Ok
from threadsai.store import wakes
from threadsai.store.conn import Conn, one
from threadsai.store.deletion import TEAM_TABLES
from threadsai.store.sql import export, int_of, text_of, transaction
from threadsai.store.started import Opened, opened_threads
from threadsai.store.verify import VerifiedLog, verify_export
from threadsai.team.cross import TeamLogEvents, check_team_logs
from threadsai.team.host_logs import drop_deleted_callers, host_team_branches
from threadsai.team.index import TeamLog, change_rows, insert_rows, turn_openers

if TYPE_CHECKING:
    from threadsai.store import SqliteStore


@dataclass(frozen=True, slots=True)
class _Read:
    log: TeamLog
    verified: VerifiedLog


def team_branches(conn: Conn, tenant_id: str, team_id: str) -> list[TeamLog]:
    """The team's logs, by branch: its lead's, every member's (parent team_member through the
    lead) and its team log; empty when nothing names the team. A host team has no lead, so it is
    found at the ids its tenant derives (Teams Phase 2)."""
    opened = opened_threads(conn, tenant_id)
    lead = next((o for o in opened if _leads(o, team_id)), None)
    if lead is None:
        return host_team_branches(conn, tenant_id, team_id, opened)
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


async def rebuild_team_index(store: "SqliteStore", team_id: str) -> Ok[None] | Err[ParseError]:
    """Wipes and refolds every index row of `team_id` (and its branches' pending_wakes); the
    feed starts a new epoch. not_found: no lead names the team. invalid_transition: rule 43. The
    reads, the check and the refold are one transaction, so no append lands between the read
    and the wipe."""
    tenant = store.tables.tenant_id
    return await store.run(lambda c: _rebuild(c, tenant, team_id))


def _rebuild(conn: Conn, tenant: str, team_id: str) -> Ok[None] | Err[ParseError]:
    with transaction(conn):
        # Nothing is written before a refusal, so the transaction commits nothing then.
        return refold_team(conn, tenant, team_id)


def refold_team(conn: Conn, tenant: str, team_id: str) -> Ok[None] | Err[ParseError]:
    """`rebuild_team_index` in the caller's transaction (an import's)."""
    logs = team_branches(conn, tenant, team_id)
    if not logs:
        return Err(ParseError("not_found", f"no team {team_id}"))
    reads = _read_all(conn, logs)
    if isinstance(reads, Err):
        return reads
    broken = check_team_logs(
        [TeamLogEvents(r.log.thread_id, r.log.branch_id, r.verified.fold.events) for r in reads],
        team_id,
    )
    if broken is not None:
        message = f"{broken.branch_id}: {broken.message}"
        return Err(ParseError("invalid_transition", message, broken.seq))
    _refold(conn, tenant, team_id, reads)
    return Ok(None)


def _read_all(conn: Conn, logs: Sequence[TeamLog]) -> list[_Read] | Err[ParseError]:
    """Each log read back through the store's boundary, on the rebuild's own transaction."""
    reads: list[_Read] = []
    for log in logs:
        read = verify_export(export(conn, log.branch_id), 0)
        if isinstance(read, Err):
            error = read.error
            return Err(ParseError(error.code, f"{log.branch_id}: {error.message}", error.seq))
        reads.append(_Read(log, read.value))
    return reads


def _feed_branches(conn: Conn, team_id: str) -> set[str]:
    """The branches in the team's feed: its members' (the lead's included) and its team log. A
    caller's events are in no feed (Teams Phase 2)."""
    rows = conn.execute(
        "SELECT branch_id FROM team_members WHERE team_id = ? AND branch_id IS NOT NULL"
        " UNION SELECT team_log_branch_id FROM teams WHERE team_id = ?",
        (team_id, team_id),
    ).fetchall()
    return {text_of(b) for (b,) in rows}


def _refold(conn: Conn, tenant: str, team_id: str, reads: Sequence[_Read]) -> None:
    (epoch,) = one(
        conn.execute(
            "SELECT COALESCE(MAX(epoch), 0) FROM team_feed WHERE team_id = ?", (team_id,)
        ).fetchone()
    )
    for table in TEAM_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE team_id = ?", (team_id,))  # noqa: S608
    for r in reads:
        wakes.refold(conn, r.log.branch_id, r.verified.fold.events)
    for r in reads:
        insert_rows(conn, r.log, r.verified.fold.events, team_id)
    for r in reads:
        change_rows(conn, r.log, r.verified.fold.events, turn_openers(r.verified), team_id)
    drop_deleted_callers(conn, tenant, team_id)
    in_feed = _feed_branches(conn, team_id)
    feed = sorted(
        (r.log.branch_id, e.seq)
        for r in reads
        if r.log.branch_id in in_feed
        for e, _ in r.verified.segments[-1].events
    )
    conn.executemany(
        "INSERT INTO team_feed (team_id, epoch, feed_offset, branch_id, seq)"
        " VALUES (?, ?, ?, ?, ?)",
        [(team_id, int_of(epoch) + 1, i, b, s) for i, (b, s) in enumerate(feed, 1)],
    )
