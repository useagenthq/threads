"""The team index on the write path (spec/schema/README.md, "Teams"): the index hooks every append
runs in its transaction (`threads.store.indexing`), with the same insert_rows/change_rows a
rebuild folds, so the replay rule holds by construction."""

import sqlite3
from typing import TYPE_CHECKING, Final

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import BranchId, ParseError, ThreadId, ThreadStartedEvent

# Modules, not their names: opening runs these hooks for the branch it opens (a cycle).
from threads.store import lease, opening
from threads.store.appended import Appended
from threads.store.lines import Draft, uuid7
from threads.team.index import TeamLog, change_rows, insert_rows

if TYPE_CHECKING:
    from pydantic import JsonValue


def index_rows(conn: sqlite3.Connection, a: Appended) -> ParseError | None:
    """The rows the append's events insert, then the rows they change (pending_wakes
    included)."""
    log = TeamLog(a.thread_id, a.branch_id)
    insert_rows(conn, log, a.events)
    change_rows(conn, log, a.events, a.opened)
    return None


def open_team_log(conn: sqlite3.Connection, a: Appended) -> ParseError | None:
    """A lead's first append (its thread_started names a team) opens the team log with
    team_opened, in the same transaction: the teams row comes from it, the lead's row from the
    thread_started. Nothing but this append opens that branch, so finding it open is refused."""
    for e in a.events:
        if not isinstance(e, ThreadStartedEvent) or e.data.team is MISSING:
            continue
        log = e.data.team.log_branch_id
        opened = opening.open_branch(
            conn, _team_log(a, e.data.agent_name, e.data.team.id, log), a.now
        )
        if isinstance(opened, ParseError):
            return opened
        if opened == opening.ALREADY_OPEN:
            message = f"team log {log} already exists; a lead's first append opens it"
            return ParseError("invalid_transition", message)
    return None


def _team_log(a: Appended, lead: str, team: str, log: BranchId) -> "opening.BranchOpening":
    """The team log of the team `a`'s lead opens: its own thread, and its team_opened."""
    ref: JsonValue = {"tenant": a.tenant_id, "team": team, "name": lead, "generation": 1}
    opened = Draft("team_opened", {"team": team, "lead": ref, "lead_thread_id": a.thread_id})
    # Free at once: the next writer (an operator, the team worker) takes epoch 2.
    held = lease.Lease(a.holder_id, 1, a.now)
    return opening.BranchOpening(a.tenant_id, ThreadId(uuid7(a.now)), log, held, (opened,))


_FEED: Final = (
    "INSERT INTO team_feed (team_id, epoch, feed_offset, branch_id, seq)"
    " SELECT ?, e, 1 + COALESCE("
    "(SELECT MAX(feed_offset) FROM team_feed WHERE team_id = ? AND epoch = e), 0), ?, ?"
    " FROM (SELECT COALESCE(MAX(epoch), 1) AS e FROM team_feed WHERE team_id = ?)"
)


def feed_rows(conn: sqlite3.Connection, a: Appended) -> ParseError | None:
    """One team_feed row per appended event, under every team the branch belongs to: a
    member's or lead's (a nested lead has two), or the team whose log it is. Offsets continue
    the team's current epoch."""
    teams: list[tuple[str]] = conn.execute(
        "SELECT team_id FROM team_members WHERE thread_id = ? AND branch_id = ?"
        " UNION SELECT team_id FROM teams WHERE team_log_branch_id = ?",
        (a.thread_id, a.branch_id, a.branch_id),
    ).fetchall()
    for (team,) in teams:
        for e in a.events:
            conn.execute(_FEED, (team, team, a.branch_id, e.seq, team))
    return None
