"""mail.claim and its expiry (design §4.6): a worker that claimed a member's pending mail and died
holds it for the claim TTL; no other worker wakes the member for it until then, and afterwards
exactly one consumes it. Mirrors TypeScript's test/team/claims.test.ts."""

import asyncio

import pytest
from pydantic import JsonValue
from team.run_kit import Watched, call, member_events, receipts, say, sq_of, start
from team.team_kit import assert_team_replays

from threads import Agent, TeamAgent, agent, scripted_model, sqlite
from threads.agents import team_worker
from threads.agents.store import now_ms
from threads.log import BranchId
from threads.result import Ok
from threads.team.constants import TEAM_CONSTANTS
from threads.team.rows import member_rows


def _researcher() -> Agent[None, str]:
    return agent(
        name="researcher", model=scripted_model({"responses": [say("Done."), say("Read it.")]})
    )


def _lead(responses: list[JsonValue]) -> TeamAgent[None, str]:
    return agent(name="lead", model=scripted_model({"responses": responses}), team=[_researcher()])


def test_a_dead_workers_claim_holds_the_mail_for_its_ttl_then_one_worker_consumes_it_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skew = [0]
    monkeypatch.setattr(team_worker, "now_ms", lambda: now_ms() + skew[0])

    async def main() -> None:
        store = sqlite(":memory:")
        # Run 1: the researcher does its task and goes idle.
        first = await _lead(
            [start("c1", "researcher", "Go."), say("Started."), say("Reported.")]
        ).run("Work.", store=store)
        thread, team = first.thread, first.team.ref.id
        # Run 2: another executor holds the researcher's lease, so this run's worker claims the
        # message it sends, can't run the member, and stops with the claim still live, as a
        # worker that dies holding it would.
        sq = await sq_of(store)
        row = next(m for m in await sq.run(lambda c: member_rows(c, team)) if m.role == "member")
        assert row.branch_id is not None
        holder = await sq.acquire(BranchId(row.branch_id), "elsewhere", now_ms)
        assert isinstance(holder, Ok), holder

        async def claimed(n: int) -> None:
            while n == 2:  # noqa: PLR2004 - the lead's answer after its send
                rows = await sq.run(
                    lambda c: c.execute(
                        "SELECT claim_token FROM mail WHERE kind = 'message'"
                    ).fetchall()
                )
                if rows and rows[0][0] is not None:
                    return
                await asyncio.sleep(0.005)

        script = [call("c2", "send", {"to": "researcher-1", "text": "More."}), say("Sent.")]
        second = agent(
            name="lead",
            model=Watched(scripted_model({"responses": script}), claimed),
            team=[_researcher()],
        )
        await second.run("One more thing.", store=store, thread=thread)
        await holder.value.release()
        # Within the TTL no other worker wakes the member: the message stays pending.
        await _lead([say("Waiting.")]).run("Anything?", store=store, thread=thread)
        assert receipts(await member_events(store, team, "researcher-1"), "message") == []
        # Past the TTL the next worker takes the claim over, and the member consumes it once.
        skew[0] = TEAM_CONSTANTS.claim_ttl_ms + 1
        await _lead([say("Checking.")]).run("Now?", store=store, thread=thread)
        await _lead([say("Again.")]).run("And now?", store=store, thread=thread)
        assert len(receipts(await member_events(store, team, "researcher-1"), "message")) == 1
        await assert_team_replays(sq, team)

    asyncio.run(main())
