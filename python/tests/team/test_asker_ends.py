"""An asker that ends (spec/schema/README.md, "Teams", "An asker that ends"): the writer parks on
its ask, the process dies as the researcher replies, and on the restart the writer's rebind fails
(its definition changed). Its end closes its open ask cancelled, so no asks row outlives it, and
the researcher's reply is refused one way or the other. Mirrors TypeScript's
test/team/asker-ends.test.ts."""

import asyncio
from pathlib import Path

import pytest
from pydantic import JsonValue
from team.crash_kit import CrashError, Point, crashing, reached, restart
from team.run_kit import Answering, ask_ids, call, reply_to, say, start, types
from team.team_kit import assert_team_replays

from threads import Agent, agent, scripted_model
from threads.log import AskClosedEvent, BranchId
from threads.result import Ok
from threads.team.rows import member_rows

_OPEN = "SELECT COUNT(*) FROM asks WHERE state = 'open'"


def _researcher() -> Agent[None, str]:
    def answer(request: str) -> JsonValue:
        if ask_ids(request) and '\\"name\\":\\"reply\\"' not in request:
            return reply_to("r1", request, "Batteries.")
        return say("Done.")

    return agent(name="researcher", model=Answering(answer))


def _lead(instructions: str, *, restarted: bool = False) -> Agent[None, str]:
    writer_script = [
        call("w1", "ask", {"to": "researcher-1", "question": "Which topic?"}),
        say("Report."),
    ]
    writer = agent(
        name="writer",
        instructions=instructions,
        model=scripted_model({"responses": writer_script}),
    )
    if restarted:  # the lead only finishes
        return agent(
            name="lead", model=Answering(lambda _r: say("Final.")), team=[_researcher(), writer]
        )
    script = [start("c1", "researcher", "Read."), start("c2", "writer", "Ask researcher-1.")]
    script += [say("Started.")] + [say("Final.")] * 6
    return agent(
        name="lead", model=scripted_model({"responses": script}), team=[_researcher(), writer]
    )


_REPLY = Point(
    "the researcher's reply",
    lambda _c, sql, p: "INSERT INTO mail" in sql and len(p) > 2 and p[2] == "reply",  # noqa: PLR2004 - the kind column
)


def test_an_asker_whose_rebind_fails_closes_its_open_ask_cancelled_before_its_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        store = await crashing(tmp_path / "t.db", monkeypatch, _REPLY)
        with pytest.raises(CrashError):
            await _lead("Write.").run("Work.", store=store)
        assert reached(), "the drill reached its commit point"

        again = await restart(tmp_path / "t.db", _lead("Write differently.", restarted=True))
        opened: list[tuple[int]] = await again.sq.run(lambda c: c.execute(_OPEN).fetchall())
        assert opened == [(0,)]
        rows = await again.sq.run(lambda c: member_rows(c, again.team))
        row = next(r for r in rows if r.name == "writer-1")
        assert row.state == "ended"
        assert row.branch_id is not None
        read = await again.sq.read(BranchId(row.branch_id), 0)
        assert isinstance(read, Ok)
        log = read.value.fold.events
        closed = next(e for e in log if isinstance(e, AskClosedEvent))
        assert closed.data.outcome.model_dump() == {"status": "cancelled"}
        at = types(log).index("ask_closed")
        assert types(log)[at + 1] == "member_ended"
        await assert_team_replays(again.sq, again.team)

    asyncio.run(main())
