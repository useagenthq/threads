"""A crash drill at a cancel's application (design §7, Phase 1 proofs): the process dies inside the
member's append that takes the cancel (its receipt and cancel_requested). Nothing of it is stored;
on the restart the still-pending cancel wakes the member, which applies it once and ends cancelled
once. Mirrors TypeScript's test/team/crash-cancel.test.ts."""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from team.crash_kit import CrashError, Point, crashing, reached, restart
from team.run_kit import Answering, call, receipts, say, start, types
from team.team_kit import assert_team_replays

from threads import Agent, Completed, agent, scripted_model
from threads.log import BranchId, MemberEndedEvent
from threads.result import Ok
from threads.team.rows import member_rows

if TYPE_CHECKING:
    from pydantic import JsonValue


def _lead(*, fresh: bool) -> Agent[None, str]:
    script: list[JsonValue] = [
        start("c1", "researcher", "Read."),
        call("c2", "cancel", {"member": "researcher-1"}),
        *(say("Final.") for _ in range(4)),
    ]
    model = scripted_model({"responses": script}) if fresh else Answering(lambda _r: say("Final."))
    researcher = agent(name="researcher", model=Answering(lambda _r: say("Done.")))
    return agent(name="lead", model=model, team=[researcher])


_CANCEL = Point(
    "the member's cancel",
    # The member's append consumes the lead's cancel mail (c2) in the same transaction.
    lambda _c, sql, p: (
        "UPDATE mail SET state = 'consumed'" in sql
        and any(isinstance(x, str) and x.endswith(":c2") for x in p)
    ),
)


def test_a_crash_inside_a_cancels_application_stores_none_of_it_the_restart_applies_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        store = await crashing(tmp_path / "t.db", monkeypatch, _CANCEL)
        with pytest.raises(CrashError):
            await _lead(fresh=True).run("Work.", store=store)
        assert reached(), "the drill reached its commit point"
        again = await restart(tmp_path / "t.db", _lead(fresh=False))
        assert isinstance(again.result, Completed), again.result
        rows = await again.sq.run(lambda c: member_rows(c, again.team))
        row = next(r for r in rows if r.name == "researcher-1")
        assert row.branch_id is not None
        read = await again.sq.read(BranchId(row.branch_id), 0)
        assert isinstance(read, Ok)
        member = read.value.fold.events
        assert len(receipts(member, "cancel")) == 1
        assert types(member).count("cancel_requested") == 1
        ended = [e for e in member if isinstance(e, MemberEndedEvent)]
        assert [e.data.result.status for e in ended] == ["cancelled"]
        await assert_team_replays(again.sq, again.team)

    asyncio.run(main())
