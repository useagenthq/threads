"""The team index on the write path (spec/schema/README.md, "Teams"): the index hooks every append
runs in its transaction (`threads.store.indexing`), with the same insert_rows/change_rows a
rebuild folds, so the replay rule holds by construction."""

from typing import TYPE_CHECKING, Final

from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.events_v1 import Team
from threads.log import ParseError, TeamOpenedEvent, ThreadStartedEvent

# Modules, not their names: opening runs these hooks for the branch it opens (a cycle).
from threads.store import lease, opening
from threads.store.appended import Appended
from threads.store.conn import Conn
from threads.store.lines import Draft
from threads.store.sql import text_of
from threads.team import wake
from threads.team.index import TeamLog, change_rows, insert_rows

if TYPE_CHECKING:
    from pydantic import JsonValue


def team_rows(conn: Conn, a: Appended) -> ParseError | None:
    """The team rows the append's events insert, then the rows they change, on a team's own
    branch only: one that opens with this append, a member's or lead's (its
    `team_members.branch_id`), or a team log. A fork of a lead or member shares its thread but
    writes no team rows, so the index stays the fold of the team's own logs (team fork is
    deferred)."""
    opens = any(isinstance(e, ThreadStartedEvent | TeamOpenedEvent) for e in a.events)
    if not opens and not _teams_of(conn, a):
        return None
    taken = _taken_team(conn, a)
    if taken is not None:
        message = f"team {taken} already exists; a lead's first append opens it"
        return ParseError("invalid_transition", message)
    log = TeamLog(a.thread_id, a.branch_id)
    insert_rows(conn, log, a.events)
    change_rows(conn, log, a.events, a.opened)
    return None


def _taken_team(conn: Conn, a: Appended) -> str | None:
    """A team a lead's thread_started names that the store already holds: a second root for it."""
    for e in a.events:
        if not isinstance(e, ThreadStartedEvent) or e.data.team is MISSING:
            continue
        found = conn.execute("SELECT 1 FROM teams WHERE team_id = ?", (e.data.team.id,)).fetchone()
        if found is not None:
            return e.data.team.id
    return None


def open_team_log(conn: Conn, a: Appended) -> ParseError | None:
    """A lead's first append (its thread_started names a team) opens the team log with
    team_opened, in the same transaction: the teams row comes from it, the lead's row from the
    thread_started. Nothing but this append opens that branch, so finding it open is refused."""
    for e in a.events:
        if not isinstance(e, ThreadStartedEvent) or e.data.team is MISSING:
            continue
        log = e.data.team.log_branch_id
        opened = opening.open_branch(conn, _team_log(a, e.data.agent_name, e.data.team), a.now)
        if isinstance(opened, ParseError):
            return opened
        if opened == opening.ALREADY_OPEN:
            message = f"team log {log} already exists; a lead's first append opens it"
            return ParseError("invalid_transition", message)
    return None


def _team_log(a: Appended, lead: str, team: Team) -> "opening.BranchOpening":
    """The team log `a`'s lead opens: the thread and branch its `team` names, with team_opened."""
    ref: JsonValue = {"tenant": a.tenant_id, "team": team.id, "name": lead, "generation": 1}
    opened = Draft("team_opened", {"team": team.id, "lead": ref, "lead_thread_id": a.thread_id})
    # Free at once: the next writer (an operator, the team worker) takes epoch 2.
    held = lease.Lease(a.holder_id, 1, a.now)
    return opening.BranchOpening(
        a.tenant_id, team.log_thread_id, team.log_branch_id, held, (opened,)
    )


_FEED: Final = (
    "INSERT INTO team_feed (team_id, epoch, feed_offset, branch_id, seq)"
    " SELECT ?, e, 1 + COALESCE("
    "(SELECT MAX(feed_offset) FROM team_feed WHERE team_id = ? AND epoch = e), 0), ?, ?"
    " FROM (SELECT COALESCE(MAX(epoch), 1) AS e FROM team_feed WHERE team_id = ?)"
)


def feed_rows(conn: Conn, a: Appended) -> ParseError | None:
    """One team_feed row per appended event, under every team the branch belongs to: a
    member's or lead's (a nested lead has two), or the team whose log it is. Offsets continue
    the team's current epoch."""
    for team in _teams_of(conn, a):
        for e in a.events:
            conn.execute(_FEED, (team, team, a.branch_id, e.seq, team))
        wake.appended(team)
    return None


def _teams_of(conn: Conn, a: Appended) -> list[str]:
    """The teams whose own branch this is: a member's or lead's, or the team log."""
    rows = conn.execute(
        "SELECT team_id FROM team_members WHERE thread_id = ? AND branch_id = ?"
        " UNION SELECT team_id FROM teams WHERE team_log_branch_id = ?",
        (a.thread_id, a.branch_id, a.branch_id),
    ).fetchall()
    return [text_of(team) for (team,) in rows]
