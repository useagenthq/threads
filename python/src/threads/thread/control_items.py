"""`Thread.cancel` across processes (lane 29F): a control item in the `inbox` that the branch's
lease holder applies at its next step boundary, so a cancel is durable whoever runs the thread.

It is the intake that already exists, under the reserved channel `api`, where `address` is the
thread id, nothing is delivered and only `cancel` and `stop_when_idle` apply. The requester's
authority is checked before the row is written, because applying an item never checks again.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import Field, TypeAdapter

from threads._generated.host_api_v1 import CancelAccepted
from threads._strict_model import StrictModel
from threads.log import Principal, ThreadId
from threads.log.jcs import canonicalize
from threads.reduce.handlers import to_json
from threads.result import Ok
from threads.store.conn import Conn, transaction
from threads.store.lines import uuid7
from threads.store.sql import blob_of, int_of, text_of

API_CHANNEL: Final = "api"
"""The reserved channel of a control item no channel adapter owns."""

_INSTALLATION: Final = "local"

type Command = Literal["cancel", "stop_when_idle"]


class Control(StrictModel):
    """A control item of an inbound batch (`threads.host.channel.Inbound`), and the `api`
    channel's own item: `Thread.cancel`'s durable barrier. One shape, declared here rather than
    in the host package, because the loop that applies it is core and core imports no host."""

    kind: Literal["control"]
    principal: Principal
    address: str = Field(min_length=1)
    item_key: str = Field(min_length=1)
    command: Command


_CONTROL: TypeAdapter[Control] = TypeAdapter(Control)


@dataclass(frozen=True, slots=True)
class Item:
    """One unconsumed control item of a thread."""

    inbox_id: int
    item_key: str
    command: Command
    principal: Principal


def pending(conn: Conn, thread_id: ThreadId) -> tuple[Item, ...]:
    """The thread's unconsumed `api` control items, oldest first. Read inside the transaction that
    applies them, so what is read is what is consumed. A thread id is unique across the store, so
    the tenant is not part of the lookup."""
    rows = conn.execute(
        "SELECT inbox_id, item_key, item FROM inbox WHERE thread_id = ? AND channel = ?"
        " AND consumed_seq IS NULL ORDER BY inbox_id",
        (thread_id, API_CHANNEL),
    ).fetchall()
    found: list[Item] = []
    for inbox_id, key, item in rows:
        # A stored row that is not an api control item is a broken store invariant, never a value.
        control = _CONTROL.validate_json(blob_of(item))
        found.append(Item(int_of(inbox_id), text_of(key), control.command, control.principal))
    return tuple(found)


def waiting(conn: Conn, thread_id: ThreadId) -> bool:
    """Whether a control item waits for this thread: the holder's cheap check at each boundary."""
    found = conn.execute(
        "SELECT 1 FROM inbox WHERE thread_id = ? AND channel = ? AND consumed_seq IS NULL LIMIT 1",
        (thread_id, API_CHANNEL),
    ).fetchone()
    return found is not None


def item_of(thread_id: ThreadId, principal: Principal, command: Command, now: int) -> Control:
    """The item `write` stores: its key is a fresh UUIDv7, its address the thread itself."""
    return Control(
        kind="control",
        principal=principal,
        address=thread_id,
        item_key=uuid7(now),
        command=command,
    )


def write(conn: Conn, tenant_id: str, item: Control, now: int) -> CancelAccepted:
    """The durable control item, by its key. One transaction: an unconsumed item of the same
    command is the answer, so a caller that asks again (a parent barring a busy child at every
    step) never queues a second barrier for the same thread."""
    thread_id = ThreadId(item.address)
    with transaction(conn):
        for waiting_item in pending(conn, thread_id):
            if waiting_item.command == item.command:
                return CancelAccepted(item_key=waiting_item.item_key)
        text = canonicalize(to_json(item))
        if not isinstance(text, Ok):
            raise AssertionError("a control item always canonicalizes")
        conn.execute(
            "INSERT INTO inbox (tenant_id, channel, installation_id, item_key, delivery_id,"
            " thread_id, item, received_at, consumed_seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)"
            " ON CONFLICT DO NOTHING",
            (
                tenant_id,
                API_CHANNEL,
                _INSTALLATION,
                item.item_key,
                item.item_key,
                thread_id,
                text.value.encode("utf-8"),
                now,
            ),
        )
        return CancelAccepted(item_key=item.item_key)


def consume(conn: Conn, items: Sequence[Item], seq: int) -> bool:
    """Marks the items consumed at `seq`, the first event of the append that applies them, under
    the `consumed_seq IS NULL` CAS. False when another owner applied one first: the caller rolls
    its append back, so an item is never applied twice."""
    for item in items:
        done = conn.execute(
            "UPDATE inbox SET consumed_seq = ? WHERE inbox_id = ? AND consumed_seq IS NULL",
            (seq, item.inbox_id),
        )
        if done.rowcount != 1:
            return False
    return True
