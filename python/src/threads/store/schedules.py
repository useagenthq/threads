"""Schedule occurrence claims: a scheduler
inserts the occurrence's row before it appends schedule_fired, so two schedulers that see the
same due occurrence start one run."""

import sqlite3
from typing import Literal

from threads.log import ThreadId


def claim(  # noqa: PLR0913, PLR0917 - one row's columns
    conn: sqlite3.Connection,
    tenant_id: str,
    schedule_id: str,
    occurrence_at: int,
    thread_id: ThreadId,
    now: int,
    state: Literal["fired", "skipped"] = "fired",
) -> bool:
    """True when this caller claimed the occurrence; False when another already had."""
    done = conn.execute(
        "INSERT INTO schedule_occurrences (tenant_id, schedule_id, occurrence_at, state,"
        " thread_id, claimed_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (tenant_id, schedule_id, occurrence_at, state, thread_id, now),
    )
    return done.rowcount == 1
