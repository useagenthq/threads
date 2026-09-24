"""Regressions from the lane 21D review (plans/reviews/claude-lane21d-review.md): a member doing
real I/O (H1), a member's subagent under the run budget (H2), each member turn under its own
principal (H3), a parked member whose definition changed (H4), a closed team (M1), and a member
whose agent this process lacks (M2). Mirrors TypeScript's test/team/review.test.ts."""

import asyncio
from collections.abc import Sequence

from pydantic import BaseModel, JsonValue
from team.run_kit import Watched, call, events, member_events, receipts, say, sq_of, start
from team.team_kit import assert_team_replays

from threads import (
    BudgetExhausted,
    Completed,
    Failed,
    Parked,
    Principal,
    RunContext,
    Store,
    TeamAgent,
    agent,
    scripted_model,
    sqlite,
    tool,
)
from threads.agents.definition import Definition
from threads.agents.store import now_ms
from threads.agents.team_worker import MemberRun, TeamWorker, WorkerEnv
from threads.agents.teams import member_pin
from threads.log import (
    BranchId,
    Budget,
    MemberEndedEvent,
    ThreadId,
    ToolResultEvent,
    UserInputEvent,
)
from threads.result import Ok
from threads.store import Draft
from threads.team.rows import member_rows
from threads.thread.handle import open_thread

ALICE = Principal(issuer="api", tenant="local", subject="alice")
BOB = Principal(issuer="api", tenant="local", subject="bob")


class _Mail(BaseModel):
    to: str


class _Nothing(BaseModel):
    pass


async def _sent(_args: _Mail, _ctx: RunContext[None]) -> str:
    return "sent"


def _mailer(instructions: str, lead: Sequence[JsonValue]) -> TeamAgent[None, str]:
    """A lead whose researcher parks on an approval to send an email; `instructions` is its
    deploy."""
    send_email = tool(
        name="send_email", description="Send an email.", input=_Mail, runs="host", execute=_sent
    )
    researcher = agent(
        name="researcher",
        instructions=instructions,
        model=scripted_model(
            {"responses": [call("m1", "send_email", {"to": "bob"}), say("Mailed bob.")]}
        ),
        tools=[send_email],
    )
    return agent(name="lead", model=scripted_model({"responses": list(lead)}), team=[researcher])


async def _ended_with(store: Store, team: str) -> JsonValue:
    member = await member_events(store, team, "researcher-1")
    ended = next((e for e in member if isinstance(e, MemberEndedEvent)), None)
    assert ended is not None, "the member ended"
    return ended.data.model_dump(mode="json", by_alias=True)["result"]


def test_h1_a_lead_waits_for_a_member_whose_model_answers_after_real_io() -> None:
    async def slow(_n: int) -> None:
        await asyncio.sleep(0.02)

    async def main() -> None:
        researcher = agent(
            name="researcher", model=Watched(scripted_model({"responses": [say("Done.")]}), slow)
        )
        lead = agent(
            name="lead",
            model=scripted_model(
                {"responses": [start("c1", "researcher", "Go."), say("Started."), say("Reported.")]}
            ),
            team=[researcher],
        )
        r = await asyncio.wait_for(lead.run("Work.", store=sqlite(":memory:")), 5)
        assert isinstance(r, Completed), r
        assert r.output == "Reported."

    asyncio.run(main())


def test_h2_a_members_subagent_is_charged_to_the_run_budget() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        helper = agent(name="helper", model=scripted_model({"responses": [say("Helped.")]}))
        spawn = call("s1", "spawn_agent", {"agent": "helper", "prompt": "Help."})
        researcher = agent(
            name="researcher",
            model=scripted_model({"responses": [spawn, say("Done.")]}),
            subagents=[helper],
        )
        lead = agent(
            name="lead",
            model=scripted_model(
                {"responses": [start("c1", "researcher", "Go."), say("Started."), say("Reported.")]}
            ),
            team=[researcher],
        )
        r = await lead.run("Work.", store=store, budget=Budget(max_model_requests=5))
        assert isinstance(r, BudgetExhausted), r
        opener = next(e for e in await events(store, r.thread) if isinstance(e, UserInputEvent))
        sq = await sq_of(store)
        charged: list[tuple[int]] = await sq.run(
            lambda c: c.execute(
                "SELECT COUNT(*) FROM budget_ledger"
                " WHERE budget_id = ? AND limit_name = 'max_model_requests'",
                (f"run:{r.thread.id}:{opener.event_id}",),
            ).fetchall()
        )
        assert charged == [(5,)]
        await assert_team_replays(sq, r.team.ref.id)

    asyncio.run(main())


def test_h3_a_members_turn_opened_by_bobs_message_acts_as_bob() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        seen: list[str] = []
        answered = asyncio.Event()

        async def whoami(_args: _Nothing, ctx: RunContext[None]) -> str:
            seen.append(ctx.principal.subject)
            if len(seen) == 2:  # noqa: PLR2004 - Bob's turn
                answered.set()
            return "ok"

        researcher = agent(
            name="researcher",
            model=scripted_model(
                {
                    "responses": [
                        call("w1", "whoami", {}),
                        say("Task done."),
                        call("w2", "whoami", {}),
                        say("Message done."),
                    ]
                }
            ),
            tools=[
                tool(
                    name="whoami",
                    description="Who is asking.",
                    input=_Nothing,
                    runs="host",
                    effect="read_only",
                    execute=whoami,
                )
            ],
        )

        async def waits(n: int) -> None:
            # Its last answer waits until the researcher has run Bob's turn.
            if n == 5:  # noqa: PLR2004 - the lead's answer after its send
                await answered.wait()

        script = [
            start("c1", "researcher", "Go."),
            say("Started."),
            say("Reported."),
            call("c2", "send", {"to": "researcher-1", "text": "One more."}),
            say("Sent."),
            say("Noted."),
        ]
        lead = agent(
            name="lead",
            model=Watched(scripted_model({"responses": script}), waits),
            team=[researcher],
        )
        first = await lead.run("Work.", store=store, principal=ALICE)
        await asyncio.wait_for(
            lead.run("And more.", store=store, thread=first.thread, principal=BOB), 5
        )
        assert seen == ["alice", "bob"]
        await assert_team_replays(await sq_of(store), first.team.ref.id)

    asyncio.run(main())


def test_h4_a_parked_member_whose_definition_changed_ends_failed_pin_mismatch() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        errors: list[object] = []
        asyncio.get_running_loop().set_exception_handler(lambda _l, c: errors.append(c))
        r = await _mailer("v1", [start("c1", "researcher", "Mail bob."), say("Started.")]).run(
            "Go.", store=store
        )
        assert isinstance(r, Parked), r
        # A deploy changed the researcher's instructions while it waited.
        lead = _mailer("v2", [say("The researcher could not go on."), say("Next.")])
        again = await asyncio.wait_for(lead.run("Again.", store=store, thread=r.thread), 5)
        assert isinstance(again, Completed), again
        assert again.output == "Next."
        await asyncio.sleep(0.01)
        assert errors == []
        ended = await _ended_with(store, r.team.ref.id)
        assert isinstance(ended, dict)
        assert ended["status"] == "failed"
        assert ended["error"] == {"code": "pin_mismatch", "message": "rebind failed: pin_mismatch"}
        assert len(receipts(await events(store, r.thread), "member_ended")) == 1
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())


def test_n5_a_changed_member_with_an_effect_in_doubt_parks_on_it_and_ends_once_settled() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        operator = Principal(issuer="api", tenant="local", subject="operator")
        r = await _mailer("v1", [start("c1", "researcher", "Mail bob."), say("Started.")]).run(
            "Go.", store=store
        )
        sq = await sq_of(store)
        rows = await sq.run(lambda c: member_rows(c, r.team.ref.id))
        row = next(m for m in rows if m.name == "researcher-1")
        assert row.branch_id is not None
        branch = BranchId(row.branch_id)
        member = await open_thread(store, ThreadId(row.thread_id))
        assert isinstance(member, Ok)
        pending = await member.value.pending_approvals()
        assert isinstance(pending, Ok)
        approved = await member.value.approve(pending.value[0].challenge_id, operator)
        assert isinstance(approved, Ok), approved
        # A process crashed right after the email's effect_begin was durable.
        crashed = await sq.acquire(branch, "crashed", now_ms)
        assert isinstance(crashed, Ok), crashed
        begun = await crashed.value.append([Draft("effect_begin", {"call_id": "m1", "attempt": 1})])
        assert isinstance(begun, Ok), begun
        await crashed.value.release()
        # A deploy changed the researcher while the email may have been sent.
        again = await _mailer("v2", [say("Unused.")]).run("Again.", store=store, thread=r.thread)
        assert isinstance(again, Parked), again
        after = await member_events(store, r.team.ref.id, "researcher-1")
        assert [e.type for e in after][-2:] == ["effect_unknown", "parked"]
        assert not any(e.type in ("member_ended", "tool_result") for e in after)
        # A human settles it: the next run ends the member from the record, never re-sending.
        resolved = await member.value.resolve_parked(f"{branch}:m1", "assume_done", operator)
        assert isinstance(resolved, Ok), resolved
        lead = _mailer("v2", [say("Noted."), say("Done.")])
        third = await lead.run("Once more.", store=store, thread=r.thread)
        assert isinstance(third, Completed), third
        settled = await member_events(store, r.team.ref.id, "researcher-1")
        result = next(e for e in settled if isinstance(e, ToolResultEvent))
        assert (result.data.call_id, result.data.origin) == ("m1", "executed")
        ended = await _ended_with(store, r.team.ref.id)
        assert isinstance(ended, dict)
        assert ended["status"] == "failed"
        assert isinstance(ended["error"], dict)
        assert ended["error"]["code"] == "pin_mismatch"
        await assert_team_replays(sq, r.team.ref.id)

    asyncio.run(main())


def test_m1_a_lead_whose_run_fails_returns_without_waiting_on_its_members_turns() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        release = asyncio.Event()

        async def held(n: int) -> None:
            if n == 1:
                await release.wait()

        # The lead's next request fails, which closes the team.
        refused: JsonValue = {"error": {"reason": "provider_error", "http_status": 400}}
        lead = agent(
            name="lead",
            model=scripted_model({"responses": [start("c1", "researcher", "Go."), refused]}),
            team=[
                agent(
                    name="researcher",
                    model=Watched(scripted_model({"responses": [say("Late.")]}), held),
                )
            ],
        )
        try:
            r = await asyncio.wait_for(lead.run("Work.", store=store), 2)
        finally:
            release.set()
        assert isinstance(r, Failed), r

    asyncio.run(main())


def test_m2_a_member_whose_agent_this_process_lacks_ends_failed_pin_unavailable() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        r = await _mailer("v1", [start("c1", "researcher", "Mail bob."), say("Started.")]).run(
            "Go.", store=store
        )
        assert isinstance(r, Parked), r
        sq = await sq_of(store)

        async def never(_d: Definition[None], _m: MemberRun) -> None:
            raise AssertionError("no member of this process runs")

        team = r.team.ref.id
        worker = TeamWorker(WorkerEnv(store, sq, lambda: team, {}, member_pin, never))
        worker.start()
        await worker.stop()
        ended = await _ended_with(store, team)
        assert isinstance(ended, dict)
        assert ended["status"] == "failed"
        assert isinstance(ended["error"], dict)
        assert ended["error"]["code"] == "pin_unavailable"
        await assert_team_replays(sq, team)

    asyncio.run(main())
