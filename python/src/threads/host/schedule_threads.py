"""Which thread a schedule's occurrences go to (store.sql `schedule_threads`). A schedule keeps
its thread while its agent pins the same config; a config change moves it to a new thread once
the old one is quiet."""

import sqlite3
from collections.abc import Sequence
from dataclasses import replace

from threads.agents.start import same_pin
from threads.agents.store import open_store
from threads.agents.teams import lead_started
from threads.host.schedule_pass import Pass
from threads.log import BranchId, ParseError, ThreadId
from threads.redaction import published
from threads.result import Err, Ok
from threads.store import Draft
from threads.store.lines import uuid7
from threads.store.opening import insert_root, new_root
from threads.store.schedules import (
    Due,
    current_thread,
    insert_pending,
    make_current,
    pending_rows,
    reserved,
)
from threads.store.sql import export, root, transaction
from threads.store.verify import verify_export


class _RefusedError(Exception):
    """Rolls the reservation back when the index hooks refuse the schedule's new thread."""

    def __init__(self, error: ParseError) -> None:
        super().__init__(error.message)
        self.error = error


async def reserve_due(
    p: Pass, started: Draft, due: Sequence[Due], now: int
) -> Err[ParseError] | None:
    """Reserves due occurrences on the schedule's thread, in one transaction with finding that
    thread: a deletion commits wholly before (a new thread is made) or after (these rows are
    retired). A schedule without a thread gets one; so does one whose agent now pins another
    config than its thread's, once that thread is quiet. Keys another scheduler reserved are
    skipped first, so a losing scheduler never moves the schedule. A new thread the index hooks
    refuse reserves nothing, and is the refusal."""
    if not due:
        return None
    thread_id, branch_id = ThreadId(uuid7(now)), BranchId(uuid7(now))
    # A lead's new thread names a new team, which its first append opens.
    definition = p.runner.bound_to(due[0].agent).definition
    opens = replace(started, data=lead_started(definition, started.data, now))
    made = new_root(p.tenant, thread_id, branch_id, opens, now)
    if isinstance(made, Err):
        raise ValueError(f"a pinned thread_started fails its own checks: {made.error.message}")
    fresh, tenant_id = made.value, p.tenant

    def reserve(conn: sqlite3.Connection) -> None:
        with transaction(conn):
            todo = [d for d in due if not reserved(conn, tenant_id, d)]
            if not todo:
                return
            schedule_id = todo[0].schedule_id
            thread = current_thread(conn, tenant_id, schedule_id)
            if thread is None or not _keeps(conn, tenant_id, thread, started, now):
                make_current(conn, tenant_id, schedule_id, fresh.row.thread_id, now)
                error = insert_root(conn, fresh, f"schedule-{uuid7(now)}")
                if error is not None:
                    # Refused inside the transaction: nothing of the reservation is written.
                    raise _RefusedError(error)
                thread = fresh.row.thread_id
            for d in todo:
                insert_pending(conn, tenant_id, thread, d, now)

    sq = await open_store(p.store)
    try:
        await sq.run(lambda c: published(fresh.content, lambda: reserve(c)))
    except _RefusedError as refused:
        return Err(refused.error)
    return None


def _keeps(
    conn: sqlite3.Connection, tenant_id: str, thread_id: ThreadId, started: Draft, now: int
) -> bool:
    """Whether the schedule stays on its thread: it pins the same config, or it doesn't but the
    thread is still busy. A config change moves to a new thread only once the old one is quiet (no
    open turn, no undecided reservation), so no new run starts while the old one goes on."""
    branch = root(conn, thread_id, tenant_id)
    read = None if branch is None else verify_export(export(conn, branch), now)
    if not isinstance(read, Ok):
        # An unreadable thread can't be shown quiet: the reservation fails, and the identity stays.
        raise TypeError(f"schedule thread {thread_id} can't be read")
    fold = read.value.fold
    if same_pin(fold.events, started):
        return True
    return fold.in_turn or bool(pending_rows(conn, tenant_id, thread_id))
