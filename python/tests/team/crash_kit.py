"""Shared by the team crash drills: a connection that dies once inside one commit point's
transaction, and the restart, the host's recovery of the lead's open run with no new input (the
same as TypeScript's test/team/crash-kit.ts)."""

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import pytest

from threads import Agent, Principal, RunResult, Store, sqlite
from threads.agents.run import RunOptions, execute
from threads.agents.store import open_store
from threads.log import BranchId, ThreadId
from threads.result import Ok
from threads.store import SqliteStore
from threads.store.sql import text_of
from threads.thread.handle import Thread

OPERATOR = Principal(issuer="api", tenant="local", subject="operator")


class CrashError(Exception):
    """The process dying at a commit point."""


type At = Callable[[sqlite3.Connection, str, Sequence[object]], bool]


@dataclass(frozen=True, slots=True)
class Point:
    name: str
    at: At


def mentions(params: Sequence[object], text: str) -> bool:
    """A statement parameter holds `text` (a string, or JSON bytes)."""
    return any(
        (isinstance(p, str) and text in p) or (isinstance(p, bytes) and text.encode() in p)
        for p in params
    )


class _Crashing(sqlite3.Connection):
    """A connection that dies once, at `point`."""

    point: At | None = None

    def execute(self, sql: str, parameters: Sequence[object] = (), /) -> sqlite3.Cursor:  # type: ignore[override] - narrowed for the drill
        point = type(self).point
        if point is not None and point(self, sql, parameters):
            type(self).point = None
            raise CrashError(sql)
        return super().execute(sql, parameters)


async def crashing(where: Path, monkeypatch: pytest.MonkeyPatch, point: Point) -> Store:
    """A store on `where` whose connection dies once at `point`."""
    store = sqlite(str(where))
    with monkeypatch.context() as m:
        m.setattr(sqlite3, "connect", partial(sqlite3.connect, factory=_Crashing))
        await open_store(store)
    _Crashing.point = point.at
    return store


def reached() -> bool:
    """Whether the drill reached its commit point."""
    return _Crashing.point is None


@dataclass(frozen=True, slots=True)
class Restarted:
    result: RunResult[str]
    sq: SqliteStore
    lead: BranchId
    team: str


async def restart(where: Path, lead: Agent[None, str]) -> Restarted:
    """The host's recovery of the lead's open run with no new input, on a fresh connection."""
    store = sqlite(str(where))
    sq = await open_store(store)
    teams = await sq.run(
        lambda c: c.execute("SELECT lead_thread_id, team_id FROM teams").fetchall()
    )
    ((lead_column, team_column),) = teams
    lead_thread, team = text_of(lead_column), text_of(team_column)
    branch = await sq.root(ThreadId(lead_thread))
    assert isinstance(branch, Ok)
    thread = Thread(ThreadId(lead_thread), branch.value, store)
    options: RunOptions[None] = {"store": store, "principal": OPERATOR, "thread": thread}
    result = await execute(lead.definition, None, options, None, lambda _e: None)
    return Restarted(result, sq, branch.value, team)
