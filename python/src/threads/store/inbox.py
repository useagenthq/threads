"""The durable channel inbox.

A verified batch is inserted whole in one transaction, each item under its own key, before the
webhook is answered. A run consumes an item by appending its event with `consume` bound to the
append, which sets `consumed_seq` in the same transaction.
"""

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from threads.log import ParseError, ThreadId
from threads.store.companion import Companion
from threads.store.sql import blob_of, int_of, text_of, transaction
from threads.store.verify import StoredEvent


@dataclass(frozen=True, slots=True)
class Item:
    """One parsed item of a verified batch, keyed for dedup and addressed to a conversation."""

    channel: str
    installation_id: str
    item_key: str
    delivery_id: str
    address: str
    item: bytes
    """The Inbound item's canonical JSON."""


@dataclass(frozen=True, slots=True)
class Row:
    inbox_id: int
    channel: str
    installation_id: str
    item_key: str
    delivery_id: str
    thread_id: ThreadId
    item: bytes


def insert_batch(
    conn: sqlite3.Connection,
    tenant_id: str,
    items: Sequence[Item],
    now: int,
    new_thread: Callable[[], ThreadId],
) -> frozenset[ThreadId]:
    """Every item in one transaction; a duplicate is a no-op. The address maps to its thread by
    an atomic create-or-get. Returns the threads that have unconsumed items now."""
    threads: set[ThreadId] = set()
    with transaction(conn):
        for item in items:
            thread = _thread(conn, tenant_id, item, new_thread)
            conn.execute(
                "INSERT INTO inbox (tenant_id, channel, installation_id, item_key, delivery_id,"
                " thread_id, item, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT DO NOTHING",
                (
                    tenant_id,
                    item.channel,
                    item.installation_id,
                    item.item_key,
                    item.delivery_id,
                    thread,
                    item.item,
                    now,
                ),
            )
            threads.add(thread)
    return frozenset(threads)


def _thread(
    conn: sqlite3.Connection, tenant_id: str, item: Item, new_thread: Callable[[], ThreadId]
) -> ThreadId:
    where = (tenant_id, item.channel, item.installation_id, item.address)
    conn.execute(
        "INSERT INTO channel_threads (tenant_id, channel, installation_id, address, thread_id)"
        " VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (*where, new_thread()),
    )
    (thread,) = conn.execute(
        "SELECT thread_id FROM channel_threads WHERE tenant_id = ? AND channel = ?"
        " AND installation_id = ? AND address = ?",
        where,
    ).fetchone()
    return ThreadId(text_of(thread))


def pending(conn: sqlite3.Connection, tenant_id: str, thread_id: ThreadId) -> tuple[Row, ...]:
    """The thread's unconsumed items, in arrival order."""
    return _rows(
        conn,
        "WHERE tenant_id = ? AND thread_id = ? AND consumed_seq IS NULL ORDER BY inbox_id",
        (tenant_id, thread_id),
    )


def all_rows(conn: sqlite3.Connection, tenant_id: str) -> tuple[Row, ...]:
    """Every row of the tenant, in insertion order."""
    return _rows(conn, "WHERE tenant_id = ? ORDER BY inbox_id", (tenant_id,))


def unconsumed_threads(conn: sqlite3.Connection) -> tuple[tuple[str, ThreadId], ...]:
    """(tenant, thread) of every thread with an unconsumed item: what a restarted host drains."""
    rows: list[tuple[object, object]] = conn.execute(
        "SELECT DISTINCT tenant_id, thread_id FROM inbox WHERE consumed_seq IS NULL"
    ).fetchall()
    return tuple((text_of(t), ThreadId(text_of(th))) for t, th in rows)


def _rows(conn: sqlite3.Connection, where: str, args: tuple[str, ...]) -> tuple[Row, ...]:
    found: list[tuple[object, ...]] = conn.execute(
        # Fixed WHERE fragments from this module; every value is bound.
        "SELECT inbox_id, channel, installation_id, item_key, delivery_id, thread_id, item"  # noqa: S608
        " FROM inbox " + where,
        args,
    ).fetchall()
    return tuple(
        Row(
            int_of(i),
            text_of(c),
            text_of(inst),
            text_of(k),
            text_of(d),
            ThreadId(text_of(t)),
            blob_of(item),
        )
        for i, c, inst, k, d, t, item in found
    )


@dataclass(frozen=True, slots=True)
class Conversation:
    channel: str
    installation_id: str
    address: str


def conversation(
    conn: sqlite3.Connection, tenant_id: str, thread_id: ThreadId
) -> Conversation | None:
    """The conversation a channel thread answers to, or None for a thread no channel owns."""
    row: tuple[object, object, object] | None = conn.execute(
        "SELECT channel, installation_id, address FROM channel_threads"
        " WHERE tenant_id = ? AND thread_id = ?",
        (tenant_id, thread_id),
    ).fetchone()
    return None if row is None else Conversation(*(text_of(v) for v in row))


def consume(inbox_id: int) -> Companion:
    """Marks the item consumed by the append's first event, once."""

    def mark(conn: sqlite3.Connection, events: Sequence[StoredEvent]) -> ParseError | None:
        done = conn.execute(
            "UPDATE inbox SET consumed_seq = ? WHERE inbox_id = ? AND consumed_seq IS NULL",
            (events[0].seq, inbox_id),
        )
        if done.rowcount == 1:
            return None
        return ParseError("seq_conflict", f"inbox item {inbox_id} is already consumed")

    return mark
