"""Shared by the cross-process cancel drills (lane 29F): the inbox rows to assert on, the dead
holder's expired leases, and a runtime the next lease taker builds over an existing branch."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from corpus import Clock
from kit import Tools, allow_all

from threads.log import BranchId
from threads.loop.model import Model
from threads.loop.runtime import Runtime, serving
from threads.result import Ok
from threads.store import SqliteStore
from threads.store.sql import int_of, text_of


@dataclass(frozen=True, slots=True)
class Row:
    inbox_id: int
    channel: str
    consumed_seq: int | None


def rows(path: Path) -> tuple[Row, ...]:
    """Every inbox row of the store file, oldest first, read on a connection of its own."""
    with closing(sqlite3.connect(path)) as conn:
        found = conn.execute(
            "SELECT inbox_id, channel, consumed_seq FROM inbox ORDER BY inbox_id"
        ).fetchall()
    return tuple(Row(int_of(i), text_of(c), None if s is None else int_of(s)) for i, c, s in found)


def expire_leases(path: Path) -> None:
    """The dead process's leases are gone: what a restart finds."""
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE leases SET expires_at = 0")


async def resumed(
    sq: SqliteStore, branch: BranchId, model: Model, clock: Clock, holder: str = "restart"
) -> Runtime:
    """A runtime over an existing branch, as the next lease taker builds one."""
    taken = await sq.acquire(branch, holder, clock)
    assert isinstance(taken, Ok), taken
    return Runtime(
        sq, taken.value, serving(model), Tools({}, clock), allow_all, clock, clock.wait_until
    )
