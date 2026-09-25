"""Crash drills at the commit points of an ask and a wait (design §7, Phase 1 proofs): the process
dies inside the transaction that opens the ask, sends the reply, closes the ask, registers the
wait, fires the member's settle notice, or finishes the wait. Nothing of that transaction is
stored; the host's recovery of the open run finishes it with exactly one of each. Mirrors
TypeScript's test/team/crash-ask.test.ts."""

import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import JsonValue
from team.crash_kit import CrashError, Point, crashing, reached, restart
from team.run_kit import Answering, ask_ids, call, reply_to, result_of, say, start
from team.team_kit import assert_team_replays

from threads import Agent, Completed, agent, scripted_model
from threads.log import BranchId, Event, MessageSentEvent
from threads.result import Ok
from threads.team.rows import member_rows


def _researcher() -> Agent[None, str]:
    """A researcher that replies to an ask it hasn't answered yet, and otherwise reports."""

    def answer(request: str) -> JsonValue:
        # Its own reply (call r1) is in the history once it has replied.
        if ask_ids(request) and '"call_id":"r1"' not in request:
            return reply_to("r1", request, "Batteries.")
        return say("Done.")

    return agent(name="researcher", model=Answering(answer))


def _lead(first: Sequence[JsonValue]) -> Agent[None, str]:
    model = (
        scripted_model({"responses": [*first, *(say("Final.") for _ in range(4))]})
        if first
        else Answering(lambda _r: say("Final."))
    )
    return agent(name="lead", model=model, team=[_researcher()])


ASK: list[JsonValue] = [
    start("c1", "researcher", "Read."),
    call("c2", "ask", {"to": "researcher-1", "question": "Which topic?"}),
]
WAIT: list[JsonValue] = [
    start("c1", "researcher", "Read."),
    call("c2", "wait", {"members": ["researcher-1"]}),
]
ASK_POINTS = (
    Point("the lead's ask", lambda _c, sql, _p: "INSERT INTO asks" in sql),
    Point(
        "the member's reply",
        lambda _c, sql, p: "INSERT INTO mail" in sql and len(p) > 2 and p[2] == "reply",  # noqa: PLR2004 - the kind column
    ),
    Point("the lead's close of the ask", lambda _c, sql, _p: "UPDATE asks SET state" in sql),
)
WAIT_POINTS = (
    Point(
        "the lead's wait",
        lambda _c, sql, p: "INSERT INTO monitors" in sql and len(p) > 5 and p[5] == "settle",  # noqa: PLR2004 - the kind column
    ),
    Point(
        "the member's settle notice",
        lambda _c, sql, p: (
            "DELETE FROM monitors WHERE monitor_id" in sql
            and any(isinstance(x, str) and x.endswith(":researcher-1") for x in p)
        ),
    ),
    Point("the wait's finish", lambda _c, sql, _p: "DELETE FROM monitors WHERE wait_id" in sql),
)


async def _drill(
    point: Point, first: Sequence[JsonValue], where: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Sequence[Event], Sequence[Event]]:
    store = await crashing(where, monkeypatch, point)
    with pytest.raises(CrashError):
        await _lead(first).run("Work.", store=store)
    assert reached(), "the drill reached its commit point"
    again = await restart(where, _lead([]))
    assert isinstance(again.result, Completed), again.result
    lead = await again.sq.read(again.lead, 0)
    assert isinstance(lead, Ok)
    rows = await again.sq.run(lambda c: member_rows(c, again.team))
    row = next(r for r in rows if r.name == "researcher-1")
    assert row.branch_id is not None
    member = await again.sq.read(BranchId(row.branch_id), 0)
    assert isinstance(member, Ok)
    await assert_team_replays(again.sq, again.team)
    return lead.value.fold.events, member.value.fold.events


def _sent(log: Sequence[Event], kind: str) -> int:
    return sum(isinstance(e, MessageSentEvent) and e.data.envelope.kind == kind for e in log)


@pytest.mark.parametrize("point", ASK_POINTS, ids=[p.name for p in ASK_POINTS])
def test_a_crash_inside_an_ask_commit_point_the_restart_closes_the_ask_once(
    point: Point, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lead, member = asyncio.run(_drill(point, ASK, tmp_path, monkeypatch))
    assert _sent(lead, "ask") == 1
    assert sum(e.type == "ask_closed" for e in lead) == 1
    got = result_of(lead, "c2")
    assert (got["status"], got["text"]) == ("answered", "Batteries.")
    assert _sent(member, "reply") == 1


@pytest.mark.parametrize("point", WAIT_POINTS, ids=[p.name for p in WAIT_POINTS])
def test_a_crash_inside_a_wait_commit_point_the_restart_finishes_the_wait_once(
    point: Point, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lead, _member = asyncio.run(_drill(point, WAIT, tmp_path, monkeypatch))
    assert sum(e.type == "wait_started" for e in lead) == 1
    assert sum(e.type == "wait_finished" for e in lead) == 1
    got = result_of(lead, "c2")
    assert (got["status"], got["timed_out"]) == ("waited", False)
    # A crash between the member's answer and its turn's end ends that turn interrupted on
    # recovery (API run recovery), so the member settles failed there; either way it settles once
    # and the wait counts it.
    finished = got["finished"]
    assert isinstance(finished, list)
    assert len(finished) == 1
