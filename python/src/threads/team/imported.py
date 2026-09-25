"""An import stores a log's bytes without its appends' hooks, so in the same transaction the
index is folded again from the logs: each imported branch's wake rows, and every team the log
belongs to, rebuilt whole. A team whose lead is not stored yet has no rows until its lead is
imported (that import rebuilds it). A log that breaks rule 43 with the stored team is refused."""

import time

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import ParseError, TeamOpenedEvent, ThreadStartedEvent
from threads.result import Err
from threads.store import receipts, sql, wakes
from threads.store.conn import Conn
from threads.store.indexing import known
from threads.store.started import opened_threads
from threads.store.verify import VerifiedLog
from threads.team.rebuild import refold_team


class _UndoError(Exception):
    def __init__(self, error: ParseError) -> None:
        self.error = error


def import_indexed(
    conn: Conn, log: VerifiedLog, tenant: str, dropped_ref: str | None
) -> ParseError | None:
    """Stores the log's segments and folds the index rows they change, in one transaction."""
    try:
        with sql.transaction(conn):
            error = sql.insert_segments(conn, log, tenant, dropped_ref) or _index(conn, log, tenant)
            if error is not None:
                raise _UndoError(error)
    except _UndoError as undo:
        return undo.error
    return None


def _index(conn: Conn, log: VerifiedLog, tenant: str) -> ParseError | None:
    now = int(time.time() * 1000)
    for s in log.segments:
        events = known([e for e, _ in s.events])
        wakes.refold(conn, s.header.branch_id, events)
        receipts.rebuild_ui(conn, tenant, events, now)
    for team in _teams_of(conn, log, tenant):
        rebuilt = refold_team(conn, tenant, team)
        if isinstance(rebuilt, Err) and rebuilt.error.code != "not_found":
            return rebuilt.error
    return None


def _teams_of(conn: Conn, log: VerifiedLog, tenant: str) -> list[str]:
    """The teams the log's thread belongs to, from its opening event: the team it leads, the
    team whose log it is, and, for a member, its lead's team."""
    events = log.segments[0].events
    first = events[0][0] if events else None
    if isinstance(first, TeamOpenedEvent):
        return [first.data.team]
    if not isinstance(first, ThreadStartedEvent):
        return []
    own = [] if first.data.team is MISSING else [first.data.team.id]
    parent = first.data.parent
    if parent is MISSING or parent.relation != "team_member":
        return own
    lead = next((o for o in opened_threads(conn, tenant) if o.thread_id == parent.thread_id), None)
    if lead is None or not isinstance(lead.event, ThreadStartedEvent):
        return own
    team = lead.event.data.team
    return own if team is MISSING else [*own, team.id]
