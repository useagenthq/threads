"""What the team worker reads between passes: a member run's one principal, whether a branch's
lease is free, whether a team is closed, and every member of a team tree. Mirrors TypeScript's
agent/team/scan.ts."""

from threads.log import Principal, Provenance
from threads.reduce import Fold
from threads.store.conn import Conn
from threads.store.sql import int_of, text_of
from threads.team.provenance import turn_provenance
from threads.team.rows import MemberRow, member_rows, own_rows, pending_for, team_row


def principal_of(conn: Conn, fold: Fold, row: MemberRow) -> Principal | None:
    """The one principal a member run acts under (design §2.6: one turn, one authority): its open
    turn's, else that of the first mail it would take. Mail of another principal waits for the
    next run."""
    if fold.in_turn:
        opened = turn_provenance(conn, fold.events)
        return None if opened is None else Provenance.model_validate(opened).principal
    first = next(iter(pending_for(conn, own_rows(conn, row.thread_id))), None)
    return None if first is None else first.provenance.principal


def lease_free(conn: Conn, branch: str, now: int) -> bool:
    """The branch's lease is free: expired or released."""
    row = conn.execute("SELECT expires_at FROM leases WHERE branch_id = ?", (branch,)).fetchone()
    return row is None or int_of(row[0]) <= now


def closed(conn: Conn, team: str) -> bool:
    """The team was closed: its lead ended."""
    found = team_row(conn, team)
    return found is not None and found.closed_at is not None


def members_under(conn: Conn, root: str) -> list[MemberRow]:
    """The member rows of the team and of every team led by one of its members, recursively."""
    teams, out = [root], list[MemberRow]()
    while teams:
        rows = [r for r in member_rows(conn, teams.pop()) if r.role == "member"]
        out += rows
        for r in rows:
            led = conn.execute(
                "SELECT team_id FROM teams WHERE lead_thread_id = ?", (r.thread_id,)
            ).fetchall()
            teams += [text_of(t) for (t,) in led]
    return out
