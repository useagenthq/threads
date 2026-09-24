"""Crash drills at each commit point of a team run (design §7, Phase 1 proofs): the process dies
inside the transaction that commits the lead's start, a member's materialize, the member's
settlement, or the lead's receipt of it. Nothing of that transaction is stored; the restart (the
host's recovery of the open run, no new input) finishes the run with exactly one start, one member
branch, one task input and one settlement received. Mirrors TypeScript's
test/team/crash.test.ts."""

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import pytest
from team.run_kit import say, start
from team.team_kit import assert_team_replays

from threads import Agent, Completed, Principal, agent, scripted_model, sqlite
from threads.agents.run import RunOptions, execute
from threads.agents.store import open_store
from threads.log import (
    BranchId,
    MemberStartedEvent,
    MessageReceivedEvent,
    ThreadId,
    UserInputEvent,
)
from threads.result import Ok
from threads.team.rows import member_rows, team_row
from threads.thread.handle import Thread

OPERATOR = Principal(issuer="api", tenant="local", subject="operator")


class CrashError(Exception):
    """The process dying at a commit point."""


type At = Callable[[sqlite3.Connection, str, Sequence[object]], bool]


def _mentions(params: Sequence[object], text: str) -> bool:
    return any(
        (isinstance(p, str) and text in p) or (isinstance(p, bytes) and text.encode() in p)
        for p in params
    )


def _a_researchers(conn: sqlite3.Connection, params: Sequence[object]) -> bool:
    """Whether the statement's thread (its second parameter) is researcher-1's."""
    thread = params[1] if len(params) > 1 else None
    row = sqlite3.Connection.execute(
        conn, "SELECT 1 FROM team_members WHERE thread_id = ? AND name = 'researcher-1'", (thread,)
    ).fetchone()
    return row is not None


@dataclass(frozen=True, slots=True)
class Point:
    name: str
    at: At


POINTS = (
    Point(
        "the lead's start",
        lambda _c, sql, p: "INSERT INTO team_members" in sql and _mentions(p, "researcher-1"),
    ),
    Point(
        "the member's materialize", lambda _c, sql, _p: "UPDATE team_members SET branch_id" in sql
    ),
    Point(
        "the member's settlement",
        lambda c, sql, p: "UPDATE team_members SET result" in sql and _a_researchers(c, p),
    ),
    Point(
        "the lead's receipt of the settlement",
        lambda _c, sql, p: (
            "UPDATE mail SET state = 'consumed'" in sql
            and not any(isinstance(x, str) and x.endswith(":c1") for x in p)
        ),
    ),
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


def _team(*, fresh: bool, researcher: Sequence[str]) -> Agent[None, str]:
    opening = [start("c1", "researcher", "Go.")] if fresh else []
    lead_script = [*opening, say("Started."), say("Final."), say("Final.")]
    member = agent(
        name="researcher", model=scripted_model({"responses": [say(t) for t in researcher]})
    )
    return agent(name="lead", model=scripted_model({"responses": lead_script}), team=[member])


@pytest.mark.parametrize("point", POINTS, ids=[p.name for p in POINTS])
def test_a_crash_inside_a_commit_point_stores_none_of_it_the_restart_finishes_once(
    point: Point, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        crashing = sqlite(str(tmp_path))
        with monkeypatch.context() as m:
            m.setattr(sqlite3, "connect", partial(sqlite3.connect, factory=_Crashing))
            await open_store(crashing)
        _Crashing.point = point.at
        with pytest.raises(CrashError):
            await _team(fresh=True, researcher=["Done."]).run("Work.", store=crashing)
        assert _Crashing.point is None, "the drill reached its commit point"

        store = sqlite(str(tmp_path))
        sq = await open_store(store)
        teams: list[tuple[str, str]] = await sq.run(
            lambda c: c.execute("SELECT lead_thread_id, team_id FROM teams").fetchall()
        )
        ((lead_thread, team),) = teams
        lead_branch = await sq.root(ThreadId(lead_thread))
        assert isinstance(lead_branch, Ok)
        thread = Thread(ThreadId(lead_thread), lead_branch.value, store)
        restarted = _team(fresh=False, researcher=["Done.", "Done."])
        options: RunOptions[None] = {"store": store, "principal": OPERATOR, "thread": thread}
        result = await execute(restarted.definition, None, options, None, lambda _e: None)
        assert isinstance(result, Completed), result

        read = await sq.read(lead_branch.value, 0)
        assert isinstance(read, Ok)
        lead = read.value.fold.events
        assert len([e for e in lead if isinstance(e, MemberStartedEvent)]) == 1
        members = [r for r in await sq.run(lambda c: member_rows(c, team)) if r.role == "member"]
        assert [r.name for r in members] == ["researcher-1"]
        assert members[0].branch_id is not None
        member = await sq.read(BranchId(members[0].branch_id), 0)
        assert isinstance(member, Ok)
        inputs = [e for e in member.value.fold.events if isinstance(e, UserInputEvent)]
        assert len(inputs) == 1
        notices = [
            e
            for e in lead
            if isinstance(e, MessageReceivedEvent)
            and e.data.envelope.kind in ("member_settled", "member_ended")
        ]
        assert len(notices) == 1
        row = await sq.run(lambda c: team_row(c, team))
        assert row is not None
        assert row.closed_at is None
        await assert_team_replays(sq, team)

    asyncio.run(main())
