"""A member run that fails with a bug while the lead is parked on its members (21E.1 re-review F2):
the lead waits on the team's progress, which raises the failure, so lead.run raises it instead of
returning parked. Mirrors TypeScript's test/team/member-bug.test.ts."""

import asyncio
from typing import TYPE_CHECKING

import pytest
from team.run_kit import Watched, call, say, start

from threads import agent, scripted_model, sqlite
from threads.agents.team_worker import TeamWorker
from threads.log import BranchId
from threads.team.rows import MemberRow

if TYPE_CHECKING:
    from pydantic import JsonValue


def test_a_member_runs_bug_while_the_lead_is_parked_on_members_reaches_lead_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = TeamWorker._member  # pyright: ignore[reportPrivateUsage] - fails the member's run
    runs = [0]

    async def failing(
        self: TeamWorker, row: MemberRow, branch: BranchId, holder: str | None = None
    ) -> None:
        if row.name == "researcher-1":
            runs[0] += 1
            if runs[0] >= 2:  # noqa: PLR2004 - its run after the writer's ask
                raise RuntimeError("member bug")
        await member(self, row, branch, holder)

    monkeypatch.setattr(TeamWorker, "_member", failing)

    async def third_waits(n: int) -> None:
        # The lead's third request waits, so the member's failure lands while it is parked.
        if n == 3:  # noqa: PLR2004 - the request after both starts
            await asyncio.sleep(0.8)

    async def main() -> None:
        researcher = agent(
            name="researcher", model=scripted_model({"responses": [say("Done."), say("x")]})
        )
        writer_script: list[JsonValue] = [
            call("w1", "ask", {"to": "researcher-1", "question": "?"}),
            say("Report."),
        ]
        writer = agent(name="writer", model=scripted_model({"responses": writer_script}))
        script = [start("c1", "researcher", "Go."), start("c2", "writer", "Ask."), say("Started.")]
        script += [say("Final.")] * 6
        lead = agent(
            name="lead",
            model=Watched(scripted_model({"responses": script}), third_waits),
            team=[researcher, writer],
        )
        with pytest.raises(RuntimeError, match="member bug"):
            await asyncio.wait_for(lead.run("Go.", store=sqlite(":memory:")), 10)

    asyncio.run(main())
