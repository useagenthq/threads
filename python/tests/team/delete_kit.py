"""Shared by the deletion tests: staged team logs in a store, renumbered copies, counts."""

from collections.abc import Callable
from pathlib import Path

from pydantic import JsonValue
from team.team_kit import (
    TEAM,
    TENANT,
    Line,
    holding,
    rechain,
    staged,
)

from threads.agents.store import Store, open_store
from threads.log import ThreadId
from threads.result import Err, Ok
from threads.store.deletion import TEAM_TABLES, DeleteError, delete_thread
from threads.team.rebuild import rebuild_team_index

CASES = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "cases"
NOW = 1_790_000_100_000
OTHER_TEAM = "0192c000-0000-7000-8000-000000000002"
WRITER = ThreadId("0192a000-0000-7000-8000-0000000000b4")
SETTLE = 3
"""team-settle-wakes-lead's threads: lead, researcher, team log."""
REBIND = 4
"""team-failed-rebind-bounces's threads: lead, researcher, writer, team log."""
PENDING = 2
"""team-tree-starting-member-pending's threads: lead and team log."""


def remapped(raw: bytes, *, lead_parent: JsonValue = None) -> bytes:
    """The log as another team's: its threads, branches and team renumbered (b -> c), and its
    lead's thread_started given `lead_parent`."""
    text = raw.decode()
    for prefix in ("0192a000-0000-7000-8000-0000000000b", "0192b000-0000-7000-8000-0000000000b"):
        text = text.replace(prefix, prefix[:-1] + "c")
    text = text.replace(TEAM, OTHER_TEAM)

    def parented(events: list[Line]) -> list[Line]:
        data = events[0]["data"]
        assert isinstance(data, dict)
        if lead_parent is not None and events[0]["type"] == "thread_started" and "team" in data:
            events[0]["data"] = {**data, "parent": lead_parent}
        return events

    return rechain(text.encode(), parented)


async def team(case: str) -> Store:
    store = await holding(staged(case))
    assert await rebuild_team_index(await open_store(store), TEAM) == Ok(None)
    return store


async def delete(store: Store, thread: ThreadId) -> Ok[int] | Err[DeleteError]:
    return await (await open_store(store)).run(lambda c: delete_thread(c, TENANT, thread, NOW))


async def count(store: Store, sql: str) -> int:
    (n,) = await (await open_store(store)).run(lambda c: c.execute(sql).fetchone())
    assert isinstance(n, int)
    return n


async def team_rows(store: Store) -> int:
    total = 0
    for table in TEAM_TABLES:
        total += await count(store, f"SELECT COUNT(*) FROM {table}")  # noqa: S608
    return total


def parent_is(parent: JsonValue) -> Callable[[list[Line]], list[Line]]:
    def edit(events: list[Line]) -> list[Line]:
        data = obj(events[0]["data"])
        events[0]["data"] = {**data, "parent": parent}
        return events

    return edit


def obj(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value
