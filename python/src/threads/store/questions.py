"""The `questions` projection (store.sql): a row per ask_user park, for the host's expiry scan.

It is inserted `open` in the transaction that appends `parked{awaiting_input}` and changes only
when the call's settling result is appended: `answered` for an answer, `expired` for anything
else. Time alone never changes a row, and the log decides every answer (a missing row blocks
nothing)."""

import sqlite3
from collections.abc import Sequence

from threads.log import ParkedEvent, ToolResultEvent
from threads.store.project_rows import QuestionRow, Rows, write_rows
from threads.store.verify import StoredEvent


def record(
    conn: sqlite3.Connection, tenant_id: str, events: Sequence[StoredEvent], now: int
) -> None:
    for event in events:
        if isinstance(event, ParkedEvent) and event.data.reason == "awaiting_input":
            open_row(conn, tenant_id, event)
        elif isinstance(event, ToolResultEvent):
            settle(conn, event, now)


def open_row(conn: sqlite3.Connection, tenant_id: str, event: ParkedEvent) -> None:
    write_rows(conn, tenant_id, Rows((), (QuestionRow(event, "open"),)))


def settle(conn: sqlite3.Connection, event: ToolResultEvent, now: int) -> None:
    state = "answered" if event.data.origin == "answered" else "expired"
    conn.execute(
        "UPDATE questions SET state = ?, decided_at = ?"
        " WHERE branch_id = ? AND call_id = ? AND state = 'open'",
        (state, now, event.branch_id, event.data.call_id),
    )
