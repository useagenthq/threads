"""team.events (spec/schema/README.md, "The feed"; lane 29B adds follow): a pure read of the
team_feed rows in offset order, each event as stored with its cursor and source. It never writes
and never drives the team. A follower is woken by an in-process append at once, and by its poll
for the commits of other processes."""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Literal

from threads.agents.team_feed_sources import Sources, sources
from threads.agents.team_handle_types import (
    EpochRestarted,
    TeamCursor,
    TeamEvent,
    TeamItem,
)
from threads.store import SqliteStore
from threads.store.conn import Conn
from threads.store.sql import int_of, text_of
from threads.team.constants import TEAM_CONSTANTS
from threads.team.wake import changed, forget

_PAGE = 256
"""Feed rows read per query: the epoch is paged, never loaded whole."""


class InvalidCursorError(Exception):
    """team.events was given an `after` that names no readable position: a cursor of a later
    epoch than the feed's, or an offset past the head of the current one (spec/api.json
    Team.events). The HTTP team stream answers it 400 invalid_cursor."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "invalid_cursor"
        self.message = message


@dataclass(frozen=True, slots=True)
class FeedHead:
    """The current epoch, its last offset, and whether the team has closed."""

    epoch: int
    head: int
    closed: bool


def _head(conn: Conn, team: str) -> FeedHead | None:
    """`closed_at` and the head from one snapshot, so a closed team's head already holds the
    items of the append that closed it."""
    row = conn.execute(
        "SELECT epoch, MAX(feed_offset),"
        " (SELECT closed_at FROM teams WHERE team_id = ?) FROM team_feed"
        " WHERE team_id = ? AND epoch = (SELECT MAX(epoch) FROM team_feed WHERE team_id = ?)",
        (team, team, team),
    ).fetchone()
    if row is None or row[0] is None or row[1] is None:
        return None
    return FeedHead(int_of(row[0]), int_of(row[1]), row[2] is not None)


async def feed_head(sq: SqliteStore, team: str) -> FeedHead | None:
    return await sq.run(lambda c: _head(c, team))


def cursor_against(
    head: FeedHead, after: TeamCursor
) -> Literal["resume", "restart", "invalid_cursor"]:
    """How a cursor reads against the feed's head (lane 29B): `restart` for an older epoch, and
    `invalid_cursor` for a later one or an offset outside the current epoch. Phase 1 restarted
    on any other epoch; a later epoch is refused now."""
    if after.epoch > head.epoch:
        return "invalid_cursor"
    if after.epoch < head.epoch:
        return "restart"
    return "resume" if 0 <= after.offset <= head.head else "invalid_cursor"


def _page(conn: Conn, team: str, epoch: int, span: tuple[int, int]) -> list[tuple[int, str, int]]:
    rows = conn.execute(
        "SELECT feed_offset, branch_id, seq FROM team_feed WHERE team_id = ? AND epoch = ?"
        " AND feed_offset > ? AND feed_offset <= ? ORDER BY feed_offset LIMIT ?",
        (team, epoch, *span, _PAGE),
    ).fetchall()
    return [(int_of(o), text_of(b), int_of(s)) for o, b, s in rows]


def _start_at(head: FeedHead, after: TeamCursor | None, team: str) -> tuple[int, int]:
    """Where the stream opens: the epoch it believes it is in, and the offset to resume from. An
    older `after` leaves that epoch behind the head, so the loop opens with one
    epoch_restarted."""
    if after is None:
        return head.epoch, 0
    against = cursor_against(head, after)
    if against == "invalid_cursor":
        raise InvalidCursorError(
            f"cursor {after.epoch}:{after.offset} is not in team {team}'s feed"
        )
    if against == "resume":
        return head.epoch, after.offset
    return after.epoch, 0


async def _next_head(
    sq: SqliteStore, team: str, head: FeedHead, cursor: int, poll_s: float
) -> FeedHead:
    """The head a follower goes on from: at once when the feed has already moved, else after a
    wait on the in-process wake or the poll."""
    # Registered before the look, so an append between the look and the wait still wakes it.
    event = changed(team)
    try:
        found = await feed_head(sq, team)
        if found is not None and (found.epoch != head.epoch or found.head > cursor):
            return found
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), poll_s)
    finally:
        forget(team, event)
    found = await feed_head(sq, team)
    return head if found is None else found


async def team_events(
    sq: SqliteStore,
    team: str,
    after: TeamCursor | None = None,
    *,
    follow: bool = False,
    poll_ms: int = TEAM_CONSTANTS.wake_poll_in_process_ms,
) -> AsyncGenerator[TeamItem]:
    """Without `follow`, the feed committed when the call began, after `after`, then the end.
    With `follow`, the same and then new items as they commit, until the team closes (after the
    items of the append that closed it) or the caller stops. A rebuilt index is a new epoch: one
    epoch_restarted names it, and the stream goes on from its start."""
    head = await feed_head(sq, team)
    if head is None:
        return
    epoch, cursor = _start_at(head, after, team)
    src = await sources(sq, team)
    while True:
        if head.epoch != epoch:
            yield EpochRestarted(TeamCursor(head.epoch, 0))
            epoch, cursor = head.epoch, 0
        async for item in _drain(sq, team, src, head, cursor):
            cursor = item.cursor.offset
            yield item
        if not follow or head.closed:
            return
        # The restart above keeps them equal, so the wait compares against this epoch's head.
        head = await _next_head(sq, team, head, cursor, poll_ms / 1000)


async def _drain(
    sq: SqliteStore, team: str, src: Sources, head: FeedHead, cursor: int
) -> AsyncIterator[TeamEvent]:
    """The rows after `cursor` up to the snapshot's head, paged, as items."""
    start = cursor
    while start < head.head:
        rows = await sq.run(lambda c, s=start: _page(c, team, head.epoch, (s, head.head)))
        for offset, branch, seq in rows:
            event, source = await src.at(branch, seq)
            yield TeamEvent(TeamCursor(head.epoch, offset), source, event)
            start = offset
        if len(rows) < _PAGE:
            return
