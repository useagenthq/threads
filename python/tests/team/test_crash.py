"""Crash drills at each commit point of a team run (design §7, Phase 1 proofs): the process dies
inside the transaction that commits the lead's start, a member's materialize, the member's
settlement, or the lead's receipt of it. Nothing of that transaction is stored; the restart (the
host's recovery of the open run, no new input) finishes the run with exactly one start, one member
branch, one task input and one settlement received. Mirrors TypeScript's
test/team/crash.test.ts."""

import asyncio
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest
from team.crash_kit import CrashError, Point, crashing, mentions, reached, restart
from team.run_kit import say, start
from team.team_kit import assert_team_replays

from threads import Agent, Completed, agent, scripted_model
from threads.log import BranchId, MemberStartedEvent, MessageReceivedEvent, UserInputEvent
from threads.result import Ok
from threads.team.rows import member_rows, team_row


def _a_researchers(conn: sqlite3.Connection, params: Sequence[object]) -> bool:
    """Whether the statement's thread (its second parameter) is researcher-1's."""
    thread = params[1] if len(params) > 1 else None
    row = sqlite3.Connection.execute(
        conn, "SELECT 1 FROM team_members WHERE thread_id = ? AND name = 'researcher-1'", (thread,)
    ).fetchone()
    return row is not None


POINTS = (
    Point(
        "the lead's start",
        lambda _c, sql, p: "INSERT INTO team_members" in sql and mentions(p, "researcher-1"),
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
        store = await crashing(tmp_path, monkeypatch, point)
        with pytest.raises(CrashError):
            await _team(fresh=True, researcher=["Done."]).run("Work.", store=store)
        assert reached(), "the drill reached its commit point"

        again = await restart(tmp_path, _team(fresh=False, researcher=["Done.", "Done."]))
        assert isinstance(again.result, Completed), again.result
        sq, team = again.sq, again.team
        read = await sq.read(again.lead, 0)
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
