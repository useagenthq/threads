"""The index hooks every append runs in its transaction, after its event rows (the replay rule):
its wake rows, the team rows its events insert and change, then a lead's first append opens its
team log, then the team feed, so a new team's feed starts with team_opened."""

import sqlite3
from collections.abc import Sequence
from typing import Final

from threads.log import Event, ParseError, UnknownEvent
from threads.store.appended import Appended, IndexHook
from threads.store.verify import StoredEvent
from threads.store.wakes import wake_rows
from threads.team.write import feed_rows, open_team_log, team_rows

HOOKS: Final[tuple[IndexHook, ...]] = (wake_rows, team_rows, open_team_log, feed_rows)


def known(events: Sequence[StoredEvent]) -> tuple[Event, ...]:
    """The known events among appended ones."""
    return tuple(e for e in events if not isinstance(e, UnknownEvent))


def index_append(conn: sqlite3.Connection, appended: Appended) -> ParseError | None:
    """Runs every index hook over one append; the first error wins."""
    for hook in HOOKS:
        error = hook(conn, appended)
        if error is not None:
            return error
    return None
