"""The in-process wake behind team.events(follow=True) (spec/api.json Team.events.follow): an
append to any of a team's branches wakes this process's followers, so a follower polls only for
the commits of other processes. It carries no data and is never a source of truth: a missed wake
costs one poll interval, never an item.

`appended` is called on the store's thread, from the feed index hook, while the append's
transaction is still open. That is safe because the store has one thread and one connection
(`threads.store.worker`): the woken follower's read is queued behind this append, so it runs
after the commit (or after the rollback, and finds nothing).
"""

import asyncio
import contextlib
import threading
from typing import Final

_lock: Final = threading.Lock()
"""`appended` runs on the store's thread and `changed` on the event loop's."""

_waiting: Final[dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Event]]]] = {}
"""Waiting followers by team id, each with the loop its event belongs to."""


def appended(team: str) -> None:
    """Called for each team a committing append belongs to. A wake never fails an append: a
    follower whose loop has since closed is dropped, not raised at the writer."""
    with _lock:
        woken = _waiting.pop(team, [])
    for loop, event in woken:
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(event.set)


def changed(team: str) -> asyncio.Event:
    """Registered before the reader looks for new rows, so an append between the look and the
    wait still wakes it. `forget` drops the registration when the reader stops waiting."""
    event = asyncio.Event()
    entry = (asyncio.get_running_loop(), event)
    with _lock:
        _waiting.setdefault(team, []).append(entry)
    return event


def forget(team: str, event: asyncio.Event) -> None:
    with _lock:
        live = _waiting.get(team)
        if live is None:
            return
        _waiting[team] = [e for e in live if e[1] is not event]
        if not _waiting[team]:
            del _waiting[team]
